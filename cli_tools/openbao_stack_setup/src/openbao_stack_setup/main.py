"""Provide the SOPS-free, context-guarded OpenBao setup command."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

import questionary
import requests
from kubernetes.client.exceptions import ApiException

from openbao_stack_setup.catalog import (
    AUTH_KEYCLOAK_ACTIVE_DIRECTORY_EXTERNAL_SECRET,
    AUTH_KEYCLOAK_ACTIVE_DIRECTORY_HELM_RELEASE,
    BOOTSTRAP_EXTERNAL_SECRETS,
    BOOTSTRAP_HELM_RELEASES,
    BOOTSTRAP_SECRET_STORES,
    PROVIDER_REFRESH_TARGETS,
    HelmReleaseTarget,
)
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.cluster import Cluster, ClusterError, StackIdentity
from openbao_stack_setup.credentials import (
    BOOTSTRAP_PASSWORDS,
    plan_bootstrap_passwords,
)
from openbao_stack_setup.custody import (
    CustodianKey,
    CustodyError,
    CustodyPaths,
    PackageMetadata,
    binary_public_key,
    decrypt_package_share,
    default_custody_root,
    generate_custodian_keys,
    load_custodian_package,
    normalize_custodian_names,
    package_path,
    prepare_custody_paths,
    validate_package_set,
    write_custodian_package,
)
from openbao_stack_setup.providers import MANAGED_CREDENTIALS, PROVIDERS, Provider, update_provider
from openbao_stack_setup.reconcile import ReconciliationIdentity, reconcile_openbao
from openbao_stack_setup.recovery import (
    RecoveryKit,
    RecoveryKitError,
    load,
    new_kit,
    new_static_seal,
    update,
    with_bootstrap_passwords_acknowledged,
    with_checkpoint,
    with_initialization_material,
    with_pending_bootstrap_passwords,
    write_new,
)
from openbao_stack_setup.seed import seed_bootstrap

_ADDRESS = "https://127.0.0.1:8200"
_BOOTSTRAP_PASSWORD_ACKNOWLEDGEMENT = "I HAVE SAVED THESE PASSWORDS"  # noqa: S105


_BOOTSTRAP_EXTERNAL_SECRETS = BOOTSTRAP_EXTERNAL_SECRETS
_BOOTSTRAP_SECRET_STORES = BOOTSTRAP_SECRET_STORES
_BOOTSTRAP_HELM_RELEASES = BOOTSTRAP_HELM_RELEASES
_PROVIDER_REFRESH_TARGETS = PROVIDER_REFRESH_TARGETS


class SetupError(RuntimeError):
    """Raised for a redacted setup failure."""


def _arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stack-setup")
    commands = parser.add_subparsers(dest="command", required=True)
    _guarded(commands.add_parser("preflight"))
    bootstrap = _guarded(commands.add_parser("bootstrap"))
    bootstrap.add_argument("--custody-root", type=Path)
    reconcile = _guarded(commands.add_parser("reconcile"))
    reconcile.add_argument("--custody-root", type=Path)
    reconcile.add_argument("--custodian-package", action="append", type=Path, required=True)
    status = _guarded(commands.add_parser("status"))
    status.add_argument("--custody-root", type=Path)
    recovery = commands.add_parser("recovery")
    recovery_commands = recovery.add_subparsers(dest="recovery_command", required=True)
    verify = _guarded(recovery_commands.add_parser("verify"))
    verify.add_argument("--custody-root", type=Path)
    verify.add_argument("--custodian-package", action="append", type=Path, required=True)
    secret = commands.add_parser("secret")
    secret_commands = secret.add_subparsers(dest="secret_command", required=True)
    set_command = _guarded(secret_commands.add_parser("set"))
    set_command.add_argument("provider", choices=sorted(MANAGED_CREDENTIALS))
    return parser


def _guarded(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--context", required=True)
    parser.add_argument("--client", required=True)
    return parser


def main() -> None:
    """Parse a single guarded operator command."""
    args = _arguments().parse_args()
    try:
        if args.command == "preflight":
            _preflight(args.context, args.client)
        elif args.command == "bootstrap":
            _bootstrap(args.context, args.client, args.custody_root)
        elif args.command == "reconcile":
            _reconcile(
                args.context,
                args.client,
                args.custody_root,
                args.custodian_package,
            )
        elif args.command == "status":
            _status(args.context, args.client, args.custody_root)
        elif args.command == "recovery":
            _verify_recovery(args.context, args.client, args.custody_root, args.custodian_package)
        else:
            _set_provider(args.context, args.client, args.provider)
    except (ClusterError, CustodyError, OpenBaoError, RecoveryKitError, SetupError) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from None


def _confirm(context: str, client: str, action: str) -> None:
    answer = _ask_text(
        f"{action} for client {client!r} on context {context!r}? Type the client name to continue:"
    )
    if answer != client:
        raise SetupError("Confirmation did not match the requested client")


def _preflight(context: str, client: str) -> None:
    cluster = Cluster(context)
    identity = cluster.identity(client)
    smtp_required = cluster.require_bootstrap_prerequisites()
    active_directory_required = cluster.active_directory_required()
    endpoint = cluster.validate_kubernetes_api_endpoint()
    print(
        "Preflight passed for "
        f"client={identity.client} cluster={identity.cluster_id} context={context}"
    )
    print(f"Bootstrap prerequisites verified; SMTP credentials required={smtp_required}")
    print(f"Active Directory credentials required={active_directory_required}")
    print(f"Kubernetes API endpoint verified: {endpoint.address}:{endpoint.port}")
    print("Verify K3s --secrets-encryption on the control-plane node before bootstrap.")


def _bootstrap(
    context: str,
    client: str,
    custody_root: Path | None,
) -> None:
    _confirm(context, client, "Bootstrap OpenBao")
    print("Validating cluster identity and bootstrap prerequisites...")
    cluster = Cluster(context)
    identity = cluster.identity(client)
    cluster.require_bootstrap_prerequisites()
    active_directory_required = cluster.active_directory_required()
    endpoint = cluster.validate_kubernetes_api_endpoint()
    print(f"Kubernetes API endpoint verified: {endpoint.address}:{endpoint.port}")
    paths = prepare_custody_paths(custody_root or default_custody_root(client))
    print(f"Using private custody root {paths.root}")
    if paths.seal_file.exists():
        kit = _bound_kit(paths.seal_file, identity)
        print(f"Loaded cluster-bound custody checkpoint: {kit.checkpoint}")
    else:
        if cluster.seal_exists():
            raise SetupError("Static seal already exists; provide its matching seal file to resume")
        print("Collecting custodian names and generating three RSA-4096 OpenPGP key pairs...")
        names = _prompt_custodian_names()
        keys = generate_custodian_keys(names)
        key, key_id = new_static_seal()
        kit = new_kit(
            identity.client,
            identity.cluster_id,
            identity.namespace_uid,
            key,
            key_id,
            names,
            (keys[0].fingerprint, keys[1].fingerprint, keys[2].fingerprint),
            (keys[0].public_key, keys[1].public_key, keys[2].public_key),
            (keys[0].private_key, keys[1].private_key, keys[2].private_key),
        )
        write_new(paths.seal_file, kit)
        print("Creating the immutable OpenBao static-seal Secret...")
        cluster.create_seal(key, key_id)
        print("Reconciling the OpenBao release...")
        force_token = cluster.force_reconcile("openbao", "infra-openbao")
        cluster.wait_helm_release("openbao", "infra-openbao", force_token=force_token)
    print("Waiting for the OpenBao Service HTTPS endpoint...")
    cluster.wait_openbao_endpoint()
    print("Opening the TLS-verified local OpenBao port-forward...")
    with _openbao(cluster) as client_api:
        print("Checking OpenBao initialization state...")
        if not client_api.initialized():
            if kit.checkpoint != "seal-created":
                raise SetupError("Seal file state does not match uninitialized OpenBao")
            if kit.initialization_root_token is not None:
                raise SetupError(
                    "Seal file contains initialization material but OpenBao is uninitialized"
                )
            print("Initializing OpenBao; this can take several minutes...")
            root_token, encrypted_shares = client_api.initialize(_recovery_pgp_keys(kit))
            print("Initialization response received; persisting recovery material...")
            kit = with_initialization_material(kit, root_token, encrypted_shares)
            update(paths.seal_file, kit)
            print("Creating and decrypt-verifying all custodian packages...")
            _write_custodian_packages(paths, kit)
            kit = with_checkpoint(kit, "initialized")
            update(paths.seal_file, kit)
            _seed_and_finish(
                cluster,
                client_api,
                root_token,
                kit,
                paths.seal_file,
                active_directory_required,
            )
            return
        if kit.checkpoint == "seal-created":
            if kit.initialization_root_token is None or kit.encrypted_recovery_shares is None:
                raise SetupError(
                    "OpenBao initialized before its one-time response was durably recorded; "
                    "recovery requires escalation"
                )
            print("Resuming custodian package creation and verification...")
            _write_custodian_packages(paths, kit)
            root_token = kit.initialization_root_token
            kit = with_checkpoint(kit, "initialized")
            update(paths.seal_file, kit)
            _seed_and_finish(
                cluster,
                client_api,
                root_token,
                kit,
                paths.seal_file,
                active_directory_required,
            )
            return
        if kit.checkpoint == "complete":
            raise SetupError("OpenBao bootstrap is already complete")
        temporary_root = client_api.create_recovery_root_token(
            _decrypt_custodian_packages(_default_resume_packages(paths), kit)
        )
        _seed_and_finish(
            cluster,
            client_api,
            temporary_root,
            kit,
            paths.seal_file,
            active_directory_required,
        )


def _seed_and_finish(
    cluster: Cluster,
    unauthenticated: OpenBaoClient,
    root_token: str,
    kit: RecoveryKit,
    recovery_file: Path,
    active_directory_required: bool,
) -> None:
    root = OpenBaoClient(_ADDRESS, root_token, unauthenticated.ca_cert, unauthenticated.session)
    try:
        if kit.checkpoint == "initialized":
            print("Collecting provider credentials and seeding OpenBao...")
            providers, smtp, active_directory = _prompt_bootstrap_credentials(
                cluster.smtp_required(), active_directory_required
            )
            kit, bootstrap_passwords = _prepare_bootstrap_passwords(root, kit, recovery_file)
            report = seed_bootstrap(
                root,
                providers,
                smtp,
                bootstrap_passwords,
                ReconciliationIdentity(kit.client, kit.cluster_id, kit.namespace_uid),
                active_directory=active_directory,
            )
            kit = with_checkpoint(kit, "seeded")
            update(recovery_file, kit)
            print(
                "Seeded OpenBao "
                f"external_records={report.external_records_changed} "
                f"internal_records={report.internal_records_changed}"
            )
        else:
            print("Reconciling the versioned OpenBao catalog...")
            reconciled = reconcile_openbao(
                root,
                ReconciliationIdentity(kit.client, kit.cluster_id, kit.namespace_uid),
            )
            print(
                "Reconciled OpenBao catalog "
                f"version={reconciled.applied_version} "
                f"internal_records={reconciled.internal_records_changed} "
                f"internal_fields_added={reconciled.internal_fields_added}"
            )
        print("Verifying restricted secret-operator access and revoking other root tokens...")
        _verify_secret_operator(cluster, unauthenticated)
        _revoke_other_root_tokens(root)
    finally:
        root.revoke_self()
    _converge_runtime(cluster, active_directory_required)
    kit = with_checkpoint(kit, "complete")
    update(recovery_file, kit)
    print("OpenBao bootstrap completed; no root token was retained.")


def _reconcile(
    context: str,
    client: str,
    custody_root: Path | None,
    package_paths: list[Path] | None,
) -> None:
    _confirm(context, client, "Reconcile OpenBao")
    cluster = Cluster(context)
    identity = cluster.identity(client)
    active_directory_required = cluster.active_directory_required()
    cluster.require_openbao_release()
    paths = prepare_custody_paths(custody_root or default_custody_root(client))
    kit = _bound_kit(paths.seal_file, identity)
    if kit.checkpoint != "complete":
        raise SetupError("OpenBao bootstrap must be complete before reconciliation")
    recovery_shares = _decrypt_custodian_packages(
        _required_package_paths(package_paths),
        kit,
    )
    cluster.wait_openbao_endpoint()
    with _openbao(cluster) as unauthenticated:
        token = unauthenticated.create_recovery_root_token(recovery_shares)
        root = OpenBaoClient(_ADDRESS, token, unauthenticated.ca_cert, unauthenticated.session)
        try:
            report = reconcile_openbao(
                root,
                ReconciliationIdentity(
                    identity.client,
                    identity.cluster_id,
                    identity.namespace_uid,
                ),
            )
            _verify_secret_operator(cluster, unauthenticated)
            _revoke_other_root_tokens(root)
        finally:
            root.revoke_self()
    print(
        "Reconciled OpenBao catalog "
        f"from_version={report.previous_version} "
        f"to_version={report.applied_version} "
        f"replicated_records={report.replicated_records}; temporary root token revoked."
    )
    _converge_runtime(cluster, active_directory_required)
    print("OpenBao reconciliation completed.")


def _prepare_bootstrap_passwords(
    client: OpenBaoClient, kit: RecoveryKit, recovery_file: Path
) -> tuple[RecoveryKit, dict[str, str]]:
    if kit.pending_bootstrap_passwords is None:
        client.ensure_kv_v2_mount()
        planned = plan_bootstrap_passwords(client)
        if not planned:
            return kit, {}
        kit = with_pending_bootstrap_passwords(kit, planned)
        update(recovery_file, kit)
    passwords = kit.pending_bootstrap_passwords
    if passwords is None:
        raise SetupError("Bootstrap password delivery state is invalid")
    if not kit.bootstrap_passwords_acknowledged:
        _deliver_bootstrap_passwords(passwords)
        kit = with_bootstrap_passwords_acknowledged(kit)
        update(recovery_file, kit)
    return kit, dict(passwords)


def _deliver_bootstrap_passwords(passwords: dict[str, str]) -> None:
    try:
        with (
            Path("/dev/tty").open("r", encoding="utf-8") as terminal_input,
            Path("/dev/tty").open("w", encoding="utf-8") as terminal_output,
        ):
            if not terminal_input.isatty() or not terminal_output.isatty():
                raise SetupError("Bootstrap password delivery requires a controlling TTY")
            _write_bootstrap_passwords(terminal_input, terminal_output, passwords)
    except OSError:
        raise SetupError("Bootstrap password delivery requires a controlling TTY") from None


def _write_bootstrap_passwords(
    terminal_input: TextIO, terminal_output: TextIO, passwords: dict[str, str]
) -> None:
    terminal_output.write("Save these newly generated bootstrap passwords now.\n")
    terminal_output.write("They will not be displayed again after acknowledgement.\n\n")
    for password in BOOTSTRAP_PASSWORDS:
        value = passwords.get(password.key)
        if value is not None:
            terminal_output.write(f"{password.label}: {value}\n")
    terminal_output.write(
        "\nType 'I HAVE SAVED THESE PASSWORDS' to acknowledge secure storage, then press Enter: "
    )
    terminal_output.flush()
    try:
        acknowledgement = terminal_input.readline().strip()
    except (EOFError, KeyboardInterrupt):
        raise SetupError("Bootstrap password acknowledgement was cancelled") from None
    if acknowledgement != _BOOTSTRAP_PASSWORD_ACKNOWLEDGEMENT:
        raise SetupError("Bootstrap password acknowledgement did not match")


def _revoke_other_root_tokens(client: OpenBaoClient) -> None:
    current_accessor = client.self_accessor()
    for accessor in client.list_token_accessors():
        metadata = client.lookup_accessor(accessor)
        policies = metadata.get("policies")
        if accessor != current_accessor and isinstance(policies, list) and "root" in policies:
            client.revoke_accessor(accessor)


def _verify_secret_operator(cluster: Cluster, unauthenticated: OpenBaoClient) -> None:
    jwt = cluster.token_request("secret-operator")
    token = unauthenticated.kubernetes_login("secret-operator", jwt)
    operator = OpenBaoClient(_ADDRESS, token, unauthenticated.ca_cert, unauthenticated.session)
    try:
        operator.read_secret("infra-agentgateway/external")
    finally:
        operator.revoke_self()


def _status(context: str, client: str, custody_root: Path | None) -> None:
    cluster = Cluster(context)
    identity = cluster.identity(client)
    paths = prepare_custody_paths(custody_root or default_custody_root(client))
    checkpoint = _bound_kit(paths.seal_file, identity).checkpoint
    with _openbao(cluster) as api:
        initialized = api.initialized()
    print(f"client={client} initialized={initialized} recovery_checkpoint={checkpoint}")


def _verify_recovery(
    context: str,
    client: str,
    custody_root: Path | None,
    package_paths: list[Path] | None,
) -> None:
    _confirm(context, client, "Verify OpenBao recovery")
    cluster = Cluster(context)
    paths = prepare_custody_paths(custody_root or default_custody_root(client))
    kit = _bound_kit(paths.seal_file, cluster.identity(client))
    if kit.checkpoint == "seal-created":
        raise SetupError("Seal file has no recovery-share checkpoint")
    recovery_shares = _decrypt_custodian_packages(_required_package_paths(package_paths), kit)
    with _openbao(cluster) as api:
        token = api.create_recovery_root_token(recovery_shares)
        temporary = OpenBaoClient(_ADDRESS, token, api.ca_cert, api.session)
        try:
            temporary.list_token_accessors()
        finally:
            temporary.revoke_self()
    print("Recovery verification succeeded; temporary root token revoked.")


def _set_provider(context: str, client: str, provider_name: str) -> None:
    cluster = Cluster(context)
    cluster.identity(client)
    provider = MANAGED_CREDENTIALS[provider_name]
    if provider_name == "active-directory" and not cluster.active_directory_required():
        raise SetupError("Active Directory federation is disabled for the selected client")
    _confirm(context, client, f"Update {provider_name} credentials")
    values = _prompt_provider(provider)
    with _openbao(cluster) as unauthenticated:
        jwt = cluster.token_request("secret-operator")
        token = unauthenticated.kubernetes_login("secret-operator", jwt)
        operator = OpenBaoClient(_ADDRESS, token, unauthenticated.ca_cert, unauthenticated.session)
        try:
            update_provider(operator, provider, values)
        finally:
            operator.revoke_self()
    _refresh_provider(cluster, provider)
    print(f"Updated OpenBao paths {', '.join(provider.paths)}; refresh requested.")


def _prompt_all_providers() -> dict[str, dict[str, str]]:
    return {name: _prompt_provider(provider) for name, provider in PROVIDERS.items()}


def _prompt_bootstrap_credentials(
    smtp_required: bool,
    active_directory_required: bool,
) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, str]]:
    providers = _prompt_all_providers()
    smtp_values = {"username": "", "password": ""}
    if smtp_required:
        values = _prompt_provider(MANAGED_CREDENTIALS["smtp"])
        smtp_values = {
            "username": values["smtpUsername"],
            "password": values["smtpPassword"],
        }
    active_directory_values = (
        _prompt_provider(MANAGED_CREDENTIALS["active-directory"])
        if active_directory_required
        else {}
    )
    return providers, smtp_values, active_directory_values


def _prompt_provider(provider: Provider) -> dict[str, str]:
    values = {field: _ask_password(f"{provider.name} {field}:") for field in provider.fields}
    if any(not value.strip() for value in values.values()):
        raise SetupError("Credential fields must be nonblank")
    return values


def _ask_text(message: str) -> str:
    try:
        answer = questionary.text(message).ask()
    except (EOFError, KeyboardInterrupt):
        raise SetupError("Interactive prompt was cancelled") from None
    if not isinstance(answer, str):
        raise SetupError("Interactive prompt was cancelled")
    return answer


def _ask_password(message: str) -> str:
    try:
        answer = questionary.password(message).ask()
    except (EOFError, KeyboardInterrupt):
        raise SetupError("Interactive prompt was cancelled") from None
    if not isinstance(answer, str):
        raise SetupError("Interactive prompt was cancelled")
    return answer


def _refresh_provider(cluster: Cluster, provider: Provider) -> None:
    provider_fields = set(provider.fields)
    targets = tuple(
        target
        for target in _PROVIDER_REFRESH_TARGETS
        if target.path in provider.paths and provider_fields.issubset(target.fields)
    )
    if not targets:
        raise SetupError("Provider refresh target is unavailable")
    refreshed = []
    for target in targets:
        external_secret = target.external_secret
        cluster.force_external_secret_refresh(
            external_secret.name,
            external_secret.namespace,
            external_secret.target_secret,
        )
        refreshed.append(target)
    for target in refreshed:
        _force_helm_release(cluster, target.helm_release)


def _refresh_bootstrap_external_secrets(cluster: Cluster, active_directory_required: bool) -> None:
    targets = _BOOTSTRAP_EXTERNAL_SECRETS
    if active_directory_required:
        targets += (AUTH_KEYCLOAK_ACTIVE_DIRECTORY_EXTERNAL_SECRET,)
    total = len(targets)
    for index, target in enumerate(targets, start=1):
        print(f"Refreshing ExternalSecret {target.namespace}/{target.name} ({index}/{total})...")
        cluster.force_external_secret_refresh(target.name, target.namespace, target.target_secret)


def _converge_runtime(cluster: Cluster, active_directory_required: bool) -> None:
    """Converge catalog-owned Kubernetes consumers after privileged access is revoked."""
    print("Converging SecretStores and ExternalSecrets; this takes approximately 2 minutes...")
    _converge_bootstrap_secret_stores(cluster)
    _refresh_bootstrap_external_secrets(cluster, active_directory_required)
    print(
        "Reconciling infrastructure releases blocked on generated Secrets; "
        "this usually takes a few minutes. OpenSearch hooks may take 1-2 minutes; "
        "investigate if there is no progress after 10 minutes..."
    )
    _reconcile_bootstrap_helm_releases(cluster, active_directory_required)
    print("Reconciling the Flux infrastructure stage...")
    cluster.reconcile_kustomization("infrastructure")


def _converge_bootstrap_secret_stores(cluster: Cluster) -> None:
    total = len(_BOOTSTRAP_SECRET_STORES)
    for index, target in enumerate(_BOOTSTRAP_SECRET_STORES, start=1):
        print(f"Waiting for SecretStore {target.namespace}/{target.name} ({index}/{total})...")
        cluster.ensure_secret_store_ready(target.name, target.namespace)


def _reconcile_bootstrap_helm_releases(cluster: Cluster, active_directory_required: bool) -> None:
    targets = _BOOTSTRAP_HELM_RELEASES
    if active_directory_required:
        targets += (AUTH_KEYCLOAK_ACTIVE_DIRECTORY_HELM_RELEASE,)
    for target in targets:
        _force_helm_release(cluster, target)


def _force_helm_release(cluster: Cluster, target: HelmReleaseTarget) -> None:
    force_token = cluster.force_reconcile(target.name, target.namespace)
    cluster.wait_helm_release(
        target.name,
        target.namespace,
        timeout_seconds=target.timeout_seconds,
        force_token=force_token,
    )


def _bound_kit(path: Path, identity: StackIdentity) -> RecoveryKit:
    kit = load(path)
    if (kit.client, kit.cluster_id, kit.namespace_uid) != (
        identity.client,
        identity.cluster_id,
        identity.namespace_uid,
    ):
        raise SetupError("Recovery kit does not belong to the selected cluster and client")
    return kit


def _required_package_paths(paths: list[Path] | None) -> tuple[Path, Path]:
    """Require two different custodian packages for a threshold-two ceremony."""
    if paths is None or len(paths) != 2:
        raise SetupError("Exactly two custodian packages are required")
    try:
        distinct = paths[0].resolve(strict=True) != paths[1].resolve(strict=True)
    except OSError:
        distinct = False
    if not distinct:
        raise SetupError("Custodian packages must be distinct")
    return paths[0], paths[1]


def _prompt_custodian_names() -> tuple[str, str, str]:
    """Collect the three identities recorded in the recovery packages."""
    return normalize_custodian_names(
        [_ask_text(f"Custodian {index} name:") for index in range(1, 4)]
    )


def _recovery_pgp_keys(kit: RecoveryKit) -> tuple[str, str, str]:
    """Encode the staged public keys for the OpenBao initialization API."""
    keys = kit.custodian_public_keys
    if keys is None:
        raise SetupError("Seal file does not contain staged custodian public keys")
    return (
        base64.b64encode(binary_public_key(keys[0])).decode("ascii"),
        base64.b64encode(binary_public_key(keys[1])).decode("ascii"),
        base64.b64encode(binary_public_key(keys[2])).decode("ascii"),
    )


def _write_custodian_packages(paths: CustodyPaths, kit: RecoveryKit) -> None:
    """Create or validate all packages before advancing the recovery checkpoint."""
    public_keys = kit.custodian_public_keys
    private_keys = kit.custodian_private_keys
    encrypted_shares = kit.encrypted_recovery_shares
    if public_keys is None or private_keys is None or encrypted_shares is None:
        raise SetupError("Seal file does not contain complete custody package material")
    try:
        decoded_shares = tuple(base64.b64decode(value, validate=True) for value in encrypted_shares)
    except (binascii.Error, ValueError):
        raise SetupError("Seal file contains invalid encrypted recovery shares") from None
    if any(not value for value in decoded_shares):
        raise SetupError("Seal file contains invalid encrypted recovery shares")
    for index in range(3):
        key = CustodianKey(
            kit.custodian_fingerprints[index], public_keys[index], private_keys[index]
        )
        write_custodian_package(
            package_path(paths.package_dir, index + 1),
            _package_metadata(kit, index + 1),
            key,
            decoded_shares[index],
        )
    decrypted = tuple(
        decrypt_package_share(load_custodian_package(package_path(paths.package_dir, index)))
        for index in range(1, 4)
    )
    if len(set(decrypted)) != 3:
        raise SetupError("Custodian packages did not decrypt to three distinct recovery shares")
    print(f"Created custodian packages in {paths.package_dir}")


def _package_metadata(kit: RecoveryKit, share_index: int) -> PackageMetadata:
    """Build the expected immutable metadata for one recovery share."""
    return PackageMetadata(
        schema_version=1,
        ceremony_id=kit.ceremony_id,
        client=kit.client,
        cluster_id=kit.cluster_id,
        namespace_uid=kit.namespace_uid,
        static_seal_key_id=kit.static_seal_key_id,
        share_index=share_index,
        recovery_shares=3,
        recovery_threshold=2,
        custodian_name=kit.custodian_names[share_index - 1],
        fingerprint=kit.custodian_fingerprints[share_index - 1],
    )


def _default_resume_packages(paths: CustodyPaths) -> tuple[Path, Path]:
    """Select two locally retained packages while bootstrap is incomplete."""
    return package_path(paths.package_dir, 1), package_path(paths.package_dir, 2)


def _decrypt_custodian_packages(paths: tuple[Path, Path], kit: RecoveryKit) -> tuple[str, str]:
    """Validate package bindings and decrypt two shares in isolated GnuPG homes."""
    packages = (load_custodian_package(paths[0]), load_custodian_package(paths[1]))
    validate_package_set(packages, _package_metadata(kit, 1))
    for package in packages:
        metadata = package.metadata
        expected = _package_metadata(kit, metadata.share_index)
        if metadata != expected:
            raise SetupError("Custodian package does not match the recorded recovery ceremony")
    return decrypt_package_share(packages[0]), decrypt_package_share(packages[1])


@contextmanager
def _openbao(cluster: Cluster) -> Iterator[OpenBaoClient]:
    ca_data = _read_ca(cluster)
    kubectl = _kubectl_executable()
    with tempfile.TemporaryDirectory(prefix="stack-setup-") as directory:
        ca_cert = Path(directory) / "openbao-ca.crt"
        ca_cert.write_bytes(ca_data)
        ca_cert.chmod(0o600)
        try:
            process = subprocess.Popen(
                [
                    kubectl,
                    "--context",
                    cluster.context,
                    "-n",
                    "infra-openbao",
                    "port-forward",
                    "pod/infra-openbao-0",
                    "8200:8203",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise SetupError("Could not start kubectl port-forward") from None
        try:
            _wait_port_forward(process)
            with requests.Session() as session:
                yield OpenBaoClient(_ADDRESS, None, ca_cert, session)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


def _read_ca(cluster: Cluster) -> bytes:
    try:
        secret = cluster.core.read_namespaced_secret("infra-openbao-tls-secret", "infra-openbao")
    except ApiException:
        raise SetupError("Could not read the OpenBao TLS certificate") from None
    data = secret.data
    if not isinstance(data, dict):
        raise SetupError("OpenBao TLS Secret does not contain ca.crt")
    encoded = data.get("ca.crt")
    if not isinstance(encoded, str) or not encoded:
        raise SetupError("OpenBao TLS Secret does not contain a valid ca.crt value")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise SetupError("OpenBao TLS Secret has an invalid CA certificate") from None
    if not decoded:
        raise SetupError("OpenBao TLS Secret has an empty CA certificate")
    return decoded


def _kubectl_executable() -> str:
    executable = shutil.which("kubectl")
    if executable is None:
        raise SetupError("kubectl executable is unavailable")
    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError:
        raise SetupError("kubectl executable is unavailable") from None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise SetupError("kubectl executable is unavailable")
    return str(resolved)


def _wait_port_forward(process: subprocess.Popen[bytes]) -> None:
    for _ in range(300):
        if process.poll() is not None:
            raise SetupError("kubectl port-forward to OpenBao failed")
        try:
            with socket.create_connection(("127.0.0.1", 8200), timeout=0.2):
                return
        except OSError:
            time.sleep(0.2)
            continue
    raise SetupError("Timed out waiting for kubectl port-forward to OpenBao")
