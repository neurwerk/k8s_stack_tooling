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
from dataclasses import replace
from pathlib import Path
from typing import Never, TextIO, override

import questionary
import requests
from k8s_stack_tooling.addons import ContractError, Setup, VerifiedSelection, valid_value
from k8s_stack_tooling.addons.schema import Target
from kubernetes.client.exceptions import ApiException

from openbao_stack_setup.access import apply_access, complete_access
from openbao_stack_setup.catalog import (
    AUTH_KEYCLOAK_ACTIVE_DIRECTORY_EXTERNAL_SECRET,
    AUTH_KEYCLOAK_ACTIVE_DIRECTORY_HELM_RELEASE,
    BOOTSTRAP_EXTERNAL_SECRETS,
    BOOTSTRAP_HELM_RELEASES,
    BOOTSTRAP_SECRET_STORES,
    CONTEXTFORGE_EXTERNAL_SECRETS,
    CONTEXTFORGE_PROVIDER_APPS_PATH,
    CONTEXTFORGE_SECRET_STORE,
    DOCLING_EXTERNAL_SECRETS,
    DOCLING_SECRET_STORES,
    GLITCHTIP_POSTGRES_EXTERNAL_SECRET,
    GLITCHTIP_SECRET_STORE,
    PROVIDER_REFRESH_TARGETS,
    REJECTION_DEBUG_EXTERNAL_SECRETS,
    TAILSCALE_EXTERNAL_SECRET,
    TAILSCALE_SECRET_STORE,
    HelmReleaseTarget,
)
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError, TokenAccessorLookupError
from openbao_stack_setup.cluster import Cluster, ClusterError, StackIdentity
from openbao_stack_setup.contextforge import ROTATE_BEFORE_SECONDS, reconcile_contextforge_token
from openbao_stack_setup.credentials import (
    BOOTSTRAP_PASSWORDS,
    BootstrapPassword,
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
from openbao_stack_setup.declarative import (
    apply_credentials,
    complete_handover,
    compose_readers,
    handover_definitions,
    plan_credentials,
)
from openbao_stack_setup.glitchtip import parse_dsn_export, provisioning_payload, store_project_dsn
from openbao_stack_setup.mcp import reconcile_mcp
from openbao_stack_setup.native import NativeError
from openbao_stack_setup.providers import MANAGED_CREDENTIALS, PROVIDERS, Provider, update_provider
from openbao_stack_setup.reconcile import (
    CURRENT_RECONCILIATION_VERSION,
    ReconciliationIdentity,
    reconcile_openbao,
    reserve_reconciliation,
)
from openbao_stack_setup.recovery import (
    RecoveryKit,
    RecoveryKitError,
    load,
    new_kit,
    new_static_seal,
    update,
    validate_pending_password_bindings,
    with_bootstrap_passwords_acknowledged,
    with_checkpoint,
    with_initialization_material,
    with_pending_bootstrap_passwords,
    write_new,
)
from openbao_stack_setup.seed import seed_bootstrap
from openbao_stack_setup.selection import (
    SelectedRun,
    approved_pull_inputs,
    selected_run,
    verify_client_sources,
)

_ADDRESS = "https://127.0.0.1:8200"
_BOOTSTRAP_PASSWORD_ACKNOWLEDGEMENT = "I HAVE SAVED THESE PASSWORDS"  # noqa: S105
_TOKEN_TIDY_ATTEMPTS = 30
_TOKEN_TIDY_INTERVAL_SECONDS = 1


_BOOTSTRAP_EXTERNAL_SECRETS = BOOTSTRAP_EXTERNAL_SECRETS
_BOOTSTRAP_SECRET_STORES = BOOTSTRAP_SECRET_STORES
_BOOTSTRAP_HELM_RELEASES = BOOTSTRAP_HELM_RELEASES
_PROVIDER_REFRESH_TARGETS = PROVIDER_REFRESH_TARGETS


class SetupError(RuntimeError):
    """Raised for a redacted setup failure."""


class _Parser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> Never:
        # Unknown argv can contain accidentally supplied credentials.
        self.exit(
            2, "Invalid arguments; use stack-setup --help. Credentials are never arguments.\n"
        )


def _arguments() -> argparse.ArgumentParser:
    parser = _Parser(prog="stack-setup")
    commands = parser.add_subparsers(dest="command", required=True)
    _guarded(commands.add_parser("preflight"))
    bootstrap = _guarded(commands.add_parser("bootstrap"))
    bootstrap.add_argument("--custody-root", type=Path)
    bootstrap.add_argument("--confirm", metavar="CLIENT")
    _operation_arguments(bootstrap)
    reconcile = _guarded(commands.add_parser("reconcile"))
    reconcile.add_argument("--custody-root", type=Path)
    reconcile.add_argument("--custodian-package", action="append", type=Path, required=True)
    reconcile.add_argument("--confirm", metavar="CLIENT")
    _operation_arguments(reconcile)
    reconcile.add_argument("--rotate-contextforge-token", action="store_true")
    reconcile.add_argument(
        "--prepare-rejected-request-debug",
        action="store_true",
        help="Prepare isolated debug credentials before enabling client runtime resources.",
    )
    status = _guarded(commands.add_parser("status"), sources=False)
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
    oauth = _guarded(secret_commands.add_parser("set-contextforge-oauth"))
    oauth.add_argument("integration_id")
    dsn = _guarded(secret_commands.add_parser("import-glitchtip-dsn"))
    for field in ("organization", "project", "project-name", "team"):
        dsn.add_argument(f"--{field}", required=True)
    addon = commands.add_parser("addon")
    addon_commands = addon.add_subparsers(dest="addon_command", required=True)
    for mode in ("refresh", "provision", "restart"):
        operation = _guarded(addon_commands.add_parser(mode))
        operation.add_argument("addon_id")
        operation.add_argument("--confirm", metavar="CLIENT")
    return parser


def _guarded(parser: argparse.ArgumentParser, *, sources: bool = True) -> argparse.ArgumentParser:
    parser.add_argument("--context", required=True)
    parser.add_argument("--client", required=True)
    if sources:
        parser.add_argument("--client-root", type=Path, required=True)
        parser.add_argument("--source-cache", type=Path, required=True)
        parser.add_argument("--cluster-dir", required=True)
    return parser


def _operation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--operation-id", required=True, help="Nonsecret ID reused only to resume this operation."
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--previous-process-stopped", action="store_true")
    parser.add_argument(
        "--input-group",
        action="append",
        metavar="ADDON/GROUP",
        help="Update an enabled group; missing enabled inputs are prompted automatically.",
    )
    parser.add_argument(
        "--native-namespace", help="Existing scratch namespace for declared native preparation."
    )


def main() -> None:
    """Parse a single guarded operator command."""
    args = _arguments().parse_args()
    try:
        verified = None
        if args.command != "status":
            cluster = Cluster(args.context)
            cluster.identity(args.client)
            verified = verify_client_sources(
                cluster, args.client_root, args.source_cache, args.cluster_dir
            )
        _dispatch(args, verified)
    except (
        ClusterError,
        ContractError,
        CustodyError,
        NativeError,
        OpenBaoError,
        RecoveryKitError,
        SetupError,
    ) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from None


def _dispatch(args: argparse.Namespace, verified: VerifiedSelection | None) -> None:
    if args.command == "status":
        _status(args.context, args.client, args.custody_root)
        return
    if verified is None:
        raise SetupError("Complete verified source selection is required")
    SelectedRun(verified, "").require_verified()
    if args.command in ("bootstrap", "reconcile"):
        _confirm(args.context, args.client, "Prepare selected credentials", args.confirm)
        run = selected_run(Cluster(args.context), verified, args)
        if args.command == "bootstrap":
            _bootstrap(
                args.context, args.client, args.custody_root, run=run, confirmation=args.client
            )
        else:
            _reconcile(
                args.context,
                args.client,
                args.custody_root,
                args.custodian_package,
                args.client,
                args.rotate_contextforge_token,
                args.prepare_rejected_request_debug,
                run=run,
            )
    elif args.command == "preflight":
        _preflight(args.context, args.client, verified)
    elif args.command == "recovery":
        _verify_recovery(args.context, args.client, args.custody_root, args.custodian_package)
    elif args.command == "addon":
        _addon_lifecycle(args, verified)
    elif getattr(args, "secret_command", None) == "set-contextforge-oauth":
        _set_contextforge_oauth(args.context, args.client, args.integration_id)
    elif getattr(args, "secret_command", None) == "import-glitchtip-dsn":
        _import_glitchtip_dsn(args)
    else:
        _set_provider(args.context, args.client, args.provider)


def _confirm(context: str, client: str, action: str, confirmation: str | None = None) -> None:
    answer = confirmation
    if answer is None:
        answer = _ask_text(
            f"{action} for client {client!r} on context {context!r}? "
            "Type the client name to continue:"
        )
    if answer != client:
        raise SetupError("Confirmation did not match the requested client")


def _preflight(context: str, client: str, verified: VerifiedSelection) -> None:
    SelectedRun(verified, "").require_verified()
    cluster = Cluster(context)
    identity = cluster.identity(client)
    smtp_required = cluster.require_bootstrap_prerequisites()
    active_directory_required = cluster.active_directory_required()
    tailscale_enabled = cluster.tailscale_enabled()
    docling_enabled = cluster.docling_enabled()
    glitchtip_enabled = cluster.glitchtip_enabled()
    addons = tuple(item.definition.metadata.name for item in verified.addons)
    contextforge_admin_email = cluster.contextforge_admin_email()
    mcp_ids = cluster.studio_mcp_ids()
    rejection_debug_enabled = cluster.rejection_debug_enabled()
    endpoint = cluster.validate_kubernetes_api_endpoint()
    print(
        "Preflight passed for "
        f"client={identity.client} cluster={identity.cluster_id} context={context}"
    )
    print(f"Bootstrap prerequisites verified; SMTP credentials required={smtp_required}")
    print(f"Active Directory credentials required={active_directory_required}")
    print(f"Tailscale OAuth catalog selected={tailscale_enabled}")
    print(f"GlitchTip staged credential catalog selected={glitchtip_enabled}")
    print(f"Docling credential catalog selected={docling_enabled}")
    print(f"Selected addon catalogs={','.join(addons) or 'none'}")
    print(f"Rejected-request debug credential catalog selected={rejection_debug_enabled}")
    print(f"ContextForge staged credential catalog selected={contextforge_admin_email is not None}")
    print(
        f"Studio MCP shared-key catalog selected={mcp_ids is not None} paths={len(mcp_ids or ())}"
    )
    print(f"Kubernetes API endpoint verified: {endpoint.address}:{endpoint.port}")
    print("Verify K3s --secrets-encryption on the control-plane node before bootstrap.")


def _bootstrap(
    context: str,
    client: str,
    custody_root: Path | None,
    *,
    run: SelectedRun,
    confirmation: str | None = None,
) -> None:
    run.require_verified()
    print("Validating cluster identity and bootstrap prerequisites...")
    cluster = Cluster(context)
    identity = cluster.identity(client)
    cluster.require_bootstrap_prerequisites()
    active_directory_required = cluster.active_directory_required()
    tailscale_enabled = cluster.tailscale_enabled()
    docling_enabled = cluster.docling_enabled()
    glitchtip_enabled = cluster.glitchtip_enabled()
    contextforge_admin_email = cluster.contextforge_admin_email()
    mcp_ids = cluster.studio_mcp_ids()
    rejection_debug_enabled = cluster.rejection_debug_enabled()
    _confirm(context, client, "Bootstrap OpenBao", confirmation)
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
                docling_enabled,
                contextforge_admin_email,
                mcp_ids,
                rejection_debug_enabled,
                tailscale_enabled=tailscale_enabled,
                glitchtip_enabled=glitchtip_enabled,
                run=run,
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
                docling_enabled,
                contextforge_admin_email,
                mcp_ids,
                rejection_debug_enabled,
                tailscale_enabled=tailscale_enabled,
                glitchtip_enabled=glitchtip_enabled,
                run=run,
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
            docling_enabled,
            contextforge_admin_email,
            mcp_ids,
            rejection_debug_enabled,
            tailscale_enabled=tailscale_enabled,
            glitchtip_enabled=glitchtip_enabled,
            run=run,
        )


def _seed_and_finish(
    cluster: Cluster,
    unauthenticated: OpenBaoClient,
    root_token: str,
    kit: RecoveryKit,
    recovery_file: Path,
    active_directory_required: bool,
    docling_enabled: bool = False,
    contextforge_admin_email: str | None = None,
    mcp_ids: tuple[str, ...] | None = None,
    rejection_debug_enabled: bool = False,
    *,
    tailscale_enabled: bool = False,
    glitchtip_enabled: bool = False,
    run: SelectedRun,
) -> None:
    root = OpenBaoClient(_ADDRESS, root_token, unauthenticated.ca_cert, unauthenticated.session)
    try:
        initial = None
        if kit.checkpoint == "initialized":
            print("Collecting provider credentials and seeding OpenBao...")
            initial = _prompt_bootstrap_credentials(
                cluster.smtp_required(), active_directory_required
            )
        kit = _prepare_selected(
            cluster,
            unauthenticated,
            root,
            kit,
            recovery_file,
            run,
            initial=initial,
            docling_enabled=docling_enabled,
            contextforge_admin_email=contextforge_admin_email,
            mcp_ids=mcp_ids,
            rejection_debug_enabled=rejection_debug_enabled,
            tailscale_enabled=tailscale_enabled,
            glitchtip_enabled=glitchtip_enabled,
        )
        if kit.checkpoint == "initialized":
            kit = with_checkpoint(kit, "seeded")
            update(recovery_file, kit)
    finally:
        root.revoke_self()
    kit = with_checkpoint(kit, "complete")
    update(recovery_file, kit)
    print("OpenBao bootstrap completed; no root token was retained.")


def _reconcile(
    context: str,
    client: str,
    custody_root: Path | None,
    package_paths: list[Path] | None,
    confirmation: str | None = None,
    rotate_contextforge_token: bool = False,
    prepare_rejected_request_debug: bool = False,
    *,
    run: SelectedRun,
) -> None:
    run.require_verified()
    cluster = Cluster(context)
    identity = cluster.identity(client)
    tailscale_enabled = cluster.tailscale_enabled()
    docling_enabled = cluster.docling_enabled()
    glitchtip_enabled = cluster.glitchtip_enabled()
    contextforge_admin_email = cluster.contextforge_admin_email()
    if rotate_contextforge_token:
        if contextforge_admin_email is None:
            raise SetupError("ContextForge must be selected before token rotation")
        cluster.require_contextforge_stopped()
    mcp_ids = cluster.studio_mcp_ids()
    rejection_debug_enabled = cluster.rejection_debug_enabled()
    prepare_debug = prepare_rejected_request_debug or rejection_debug_enabled
    print(
        f"Rejected-request debug credential preparation selected={prepare_debug}; "
        f"runtime consumer refresh selected={rejection_debug_enabled}"
    )
    _confirm(context, client, "Reconcile OpenBao", confirmation)
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
            if rotate_contextforge_token:
                cluster.require_contextforge_stopped()
            _prepare_selected(
                cluster,
                unauthenticated,
                root,
                kit,
                paths.seal_file,
                run,
                docling_enabled=docling_enabled,
                glitchtip_enabled=glitchtip_enabled,
                contextforge_admin_email=contextforge_admin_email,
                mcp_ids=mcp_ids,
                rejection_debug_enabled=prepare_debug,
                tailscale_enabled=tailscale_enabled,
                rotate_contextforge_token=rotate_contextforge_token,
            )
        finally:
            root.revoke_self()
    print(
        "Selected credentials and access prepared; temporary root revoked. "
        "Lifecycle actions are separate."
    )


def _prepare_selected(
    cluster: Cluster,
    unauthenticated: OpenBaoClient,
    root: OpenBaoClient,
    kit: RecoveryKit,
    recovery_file: Path,
    run: SelectedRun,
    *,
    initial: tuple[dict[str, dict[str, str]], dict[str, str], dict[str, str]] | None = None,
    docling_enabled: bool = False,
    contextforge_admin_email: str | None = None,
    mcp_ids: tuple[str, ...] | None = None,
    rejection_debug_enabled: bool = False,
    tailscale_enabled: bool = False,
    glitchtip_enabled: bool = False,
    rotate_contextforge_token: bool = False,
) -> RecoveryKit:
    run.require_verified()
    identity = ReconciliationIdentity(kit.client, kit.cluster_id, kit.namespace_uid)
    reservation = reserve_reconciliation(
        root,
        identity,
        run.operation_id,
        run.fingerprint,
        compose_readers(run.setups),
        resume=run.resume,
        previous_process_stopped=run.previous_process_stopped,
    )
    definitions = dict.fromkeys(run.setups, run.fingerprint)
    base_passwords = tuple(
        replace(item, definition=run.fingerprint) for item in BOOTSTRAP_PASSWORDS
    )
    descriptors = (*base_passwords, *handover_definitions(run.setups, definitions))
    kit, pending = _prepare_bootstrap_passwords(root, kit, recovery_file, definitions=descriptors)
    supplied = _selected_inputs(root, run)
    plan = plan_credentials(
        root,
        run.setups,
        supplied=supplied,
        prepared=run.prepared,
        handover=kit,
        definitions=definitions,
        base_handover=base_passwords,
    )
    core_passwords = {item.key: pending[item.key] for item in base_passwords if item.key in pending}
    options = {
        "docling_enabled": docling_enabled,
        "contextforge_admin_email": contextforge_admin_email,
        "rejection_debug_enabled": rejection_debug_enabled,
        "tailscale_enabled": tailscale_enabled,
        "glitchtip_enabled": glitchtip_enabled,
        "reservation": reservation,
    }
    if initial is not None:
        providers, smtp, active_directory = initial
        seed_bootstrap(
            root,
            providers,
            smtp,
            core_passwords,
            identity,
            active_directory=active_directory,
            **options,
        )
    else:
        reconcile_openbao(root, identity, core_passwords, **options)
    _prepare_mcp_credentials(root, mcp_ids)
    if contextforge_admin_email is not None:
        _prepare_contextforge_token(
            root, kit.client, kit.cluster_id, kit.namespace_uid, rotate=rotate_contextforge_token
        )
    apply_credentials(root, plan, reservation=reservation)
    apply_access(root, reservation)
    _verify_secret_operator(cluster, unauthenticated)
    _revoke_other_root_tokens(root)
    if kit.pending_bootstrap_passwords:
        kit = complete_handover(root, kit, recovery_file)
    complete_access(root, reservation, applied_version=CURRENT_RECONCILIATION_VERSION)
    return kit


def _selected_inputs(root: OpenBaoClient, run: SelectedRun) -> dict[str, dict[str, dict[str, str]]]:
    supplied: dict[str, dict[str, dict[str, str]]] = {}
    for addon, setup in run.setups.items():
        for group in setup.input_groups:
            consumers = [
                (record.path, name)
                for record in setup.records
                for name, field in record.fields.items()
                if field.supplied is not None and field.supplied.group == group.id
            ]
            if not consumers:
                continue
            requested = f"{addon}/{group.id}" in run.input_groups
            existing = [(root.read_secret(path), name) for path, name in consumers]
            if not requested and all(record and name in record.values for record, name in existing):
                continue
            values = {key: _ask_password(f"{addon}/{group.id} {key}:") for key in group.fields}
            if any(not valid_value(values[key], rule) for key, rule in group.fields.items()):
                raise SetupError("Supplied credential group is invalid; values hidden")
            supplied.setdefault(addon, {})[group.id] = values
    return supplied


def _prepare_contextforge_token(
    root: OpenBaoClient,
    client: str,
    cluster_id: str,
    namespace_uid: str,
    *,
    rotate: bool = False,
) -> None:
    ttl = reconcile_contextforge_token(
        root,
        {"client": client, "clusterId": cluster_id, "namespaceUid": namespace_uid},
        rotate=rotate,
    )
    print(f"ContextForge nonrenewable Vault token verified; remaining lifetime={ttl}s.")
    if ttl <= ROTATE_BEFORE_SECONDS:
        print(
            "Schedule stopped two-custodian token rotation now; expiry blocks native OAuth access."
        )


def _prepare_mcp_credentials(root: OpenBaoClient, identities: tuple[str, ...] | None) -> None:
    if identities is not None:
        created = reconcile_mcp(root, identities)
        print(
            f"Prepared Studio MCP shared credentials: selected_paths={len(identities)} "
            f"initialized_records={created}"
        )


def _prepare_bootstrap_passwords(
    client: OpenBaoClient,
    kit: RecoveryKit,
    recovery_file: Path,
    *,
    definitions: tuple[BootstrapPassword, ...] | None = None,
) -> tuple[RecoveryKit, dict[str, str]]:
    bindings = _password_bindings(definitions) if definitions is not None else None
    if kit.pending_password_bindings is not None:
        validate_pending_password_bindings(kit, bindings or {})
    elif kit.pending_bootstrap_passwords is not None and definitions is not None:
        raise SetupError("Pending password delivery has no selected-definition binding")
    if kit.pending_bootstrap_passwords is None:
        client.ensure_kv_v2_mount()
        planned = plan_bootstrap_passwords(client, definitions=definitions)
        if not planned:
            return kit, {}
        kit = with_pending_bootstrap_passwords(
            kit,
            planned,
            bindings=({key: bindings[key] for key in planned} if bindings is not None else None),
        )
        update(recovery_file, kit)
    passwords = kit.pending_bootstrap_passwords
    if passwords is None:
        raise SetupError("Bootstrap password delivery state is invalid")
    if not kit.bootstrap_passwords_acknowledged:
        if definitions is None:
            _deliver_bootstrap_passwords(passwords)
        else:
            _deliver_bootstrap_passwords(passwords, definitions=definitions)
        kit = with_bootstrap_passwords_acknowledged(kit)
        update(recovery_file, kit)
    return kit, dict(passwords)


def _password_bindings(definitions: tuple[BootstrapPassword, ...]) -> dict[str, dict[str, str]]:
    bindings = {
        item.key: {
            "label": item.label,
            "path": item.path,
            "field": item.field,
            "definition": item.definition,
        }
        for item in definitions
    }
    if len(bindings) != len(definitions) or any(
        not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)
        for key, binding in bindings.items()
        for value in (key, *binding.values())
    ):
        raise SetupError("Selected password definitions are invalid")
    if len({(item.path, item.field) for item in definitions}) != len(definitions):
        raise SetupError("Selected password destinations are duplicated")
    return bindings


def _deliver_bootstrap_passwords(
    passwords: dict[str, str], *, definitions: tuple[BootstrapPassword, ...] = BOOTSTRAP_PASSWORDS
) -> None:
    try:
        with (
            Path("/dev/tty").open("r", encoding="utf-8") as terminal_input,
            Path("/dev/tty").open("w", encoding="utf-8") as terminal_output,
        ):
            if not terminal_input.isatty() or not terminal_output.isatty():
                raise SetupError("Bootstrap password delivery requires a controlling TTY")
            _write_bootstrap_passwords(
                terminal_input, terminal_output, passwords, definitions=definitions
            )
    except OSError:
        raise SetupError("Bootstrap password delivery requires a controlling TTY") from None


def _write_bootstrap_passwords(
    terminal_input: TextIO,
    terminal_output: TextIO,
    passwords: dict[str, str],
    *,
    definitions: tuple[BootstrapPassword, ...] = BOOTSTRAP_PASSWORDS,
) -> None:
    terminal_output.write("Save these newly generated bootstrap passwords now.\n")
    terminal_output.write("They will not be displayed again after acknowledgement.\n\n")
    for password in definitions:
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
    root_accessors: tuple[str, ...] = ()
    try:
        root_accessors = _other_root_accessors(client, current_accessor)
    except TokenAccessorLookupError:
        client.tidy_tokens()
        for attempt in range(_TOKEN_TIDY_ATTEMPTS):
            time.sleep(_TOKEN_TIDY_INTERVAL_SECONDS)
            try:
                root_accessors = _other_root_accessors(client, current_accessor)
                break
            except TokenAccessorLookupError:
                if attempt == _TOKEN_TIDY_ATTEMPTS - 1:
                    raise SetupError("OpenBao token tidy did not remove stale accessors") from None
    for accessor in root_accessors:
        client.revoke_accessor(accessor)


def _other_root_accessors(client: OpenBaoClient, current_accessor: str) -> tuple[str, ...]:
    root_accessors = []
    for accessor in client.list_token_accessors():
        metadata = client.lookup_accessor(accessor)
        policies = metadata.get("policies")
        if accessor != current_accessor and isinstance(policies, list) and "root" in policies:
            root_accessors.append(accessor)
    return tuple(root_accessors)


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
    tailscale_enabled = cluster.tailscale_enabled()
    docling_enabled = cluster.docling_enabled()
    contextforge_admin_email = cluster.contextforge_admin_email()
    mcp_ids = cluster.studio_mcp_ids()
    paths = prepare_custody_paths(custody_root or default_custody_root(client))
    checkpoint = _bound_kit(paths.seal_file, identity).checkpoint
    with _openbao(cluster) as api:
        initialized = api.initialized()
    print(f"client={client} initialized={initialized} recovery_checkpoint={checkpoint}")
    print(f"Tailscale OAuth catalog selected={tailscale_enabled}")
    print(f"Docling credential catalog selected={docling_enabled}")
    print("Use preflight to verify the complete selected add-on sources.")
    print(f"ContextForge staged credential catalog selected={contextforge_admin_email is not None}")
    print(
        f"Studio MCP shared-key catalog selected={mcp_ids is not None} paths={len(mcp_ids or ())}"
    )


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
    if provider_name == "tailscale-oauth":
        if not cluster.tailscale_enabled():
            raise SetupError("Tailscale OAuth catalog is not selected for the client")
        target = TAILSCALE_EXTERNAL_SECRET
        if not cluster.external_secret_exists(target.name, target.namespace):
            raise SetupError("Approved Tailscale OAuth ExternalSecret is missing")
    if provider_name.startswith("glitchtip-") and not cluster.glitchtip_enabled():
        raise SetupError("GlitchTip is not selected for the client")
    if provider_name == "docling-inference" and cluster.docling_inference_mode() != "private-vlm":
        raise SetupError("Docling private-vlm inference is not selected for the client")
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


def _import_glitchtip_dsn(args: argparse.Namespace) -> None:
    """Read the selected project and store its DSN without printing it."""
    cluster = Cluster(args.context)
    cluster.identity(args.client)
    if not cluster.glitchtip_enabled():
        raise SetupError("GlitchTip is not selected for the client")
    if not cluster.external_secret_exists("glitchtip-adapter", "glitchtip"):
        raise SetupError("Approved GlitchTip adapter ExternalSecret is missing")
    _confirm(args.context, args.client, "Import verified GlitchTip project DSN")
    dsn = _read_glitchtip_dsn(cluster, args)
    with _openbao(cluster) as unauthenticated:
        jwt = cluster.token_request("secret-operator")
        token = unauthenticated.kubernetes_login("secret-operator", jwt)
        operator = OpenBaoClient(_ADDRESS, token, unauthenticated.ca_cert, unauthenticated.session)
        try:
            store_project_dsn(operator, dsn)
        finally:
            operator.revoke_self()
    cluster.force_external_secret_refresh("glitchtip-adapter", "glitchtip", "glitchtip-adapter")
    print("Verified GlitchTip DSN handed off; contents hidden; Secret refresh requested.")


def _read_glitchtip_dsn(cluster: Cluster, args: argparse.Namespace) -> str:
    """Run reviewed read-only source over stdin; never log or display captured stdout."""
    pod = cluster.glitchtip_application_pod()
    command = [
        _kubectl_executable(),
        "--context",
        cluster.context,
        "--namespace",
        "glitchtip",
        "exec",
        "-i",
        pod,
        "--container",
        "glitchtip",
        "--",
        "env",
        "PYTHONPATH=/code",
        "DJANGO_SETTINGS_MODULE=glitchtip.settings",
        "python",
        "-",
        "export-dsn",
        "--organization",
        args.organization,
        "--project",
        args.project,
        "--project-name",
        args.project_name,
        "--team",
        args.team,
    ]
    try:
        result = subprocess.run(
            command,
            input=provisioning_payload(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SetupError("Private GlitchTip DSN export failed; output hidden") from None
    if result.returncode != 0:
        raise SetupError("Private GlitchTip DSN export failed; output hidden")
    return parse_dsn_export(result.stdout, args.organization, args.project)


def _set_contextforge_oauth(context: str, client: str, integration_id: str) -> None:
    """CAS-update one approved app field using routine scoped operator authority."""
    cluster = Cluster(context)
    cluster.identity(client)
    field = cluster.contextforge_oauth_integration(integration_id)
    if not cluster.external_secret_exists("contextforge-oauth-apps", "contextforge"):
        raise SetupError("Approved ContextForge OAuth app ExternalSecret is missing")
    _confirm(context, client, f"Update approved OAuth app {field}")
    value = _ask_password("Operator app client secret:")
    if not value.strip() or len(value) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SetupError("Invalid operator app secret; value is hidden")
    provider = Provider("contextforge-oauth", (CONTEXTFORGE_PROVIDER_APPS_PATH,), (field,))
    values = {field: value}
    value = ""
    try:
        with _openbao(cluster) as unauthenticated:
            jwt = cluster.token_request("secret-operator")
            token = unauthenticated.kubernetes_login("secret-operator", jwt)
            operator = OpenBaoClient(
                _ADDRESS, token, unauthenticated.ca_cert, unauthenticated.session
            )
            try:
                update_provider(operator, provider, values)
            finally:
                operator.revoke_self()
    finally:
        values.clear()
    cluster.force_external_secret_refresh(
        "contextforge-oauth-apps", "contextforge", "contextforge-oauth-apps"
    )
    print("Updated approved ContextForge operator app field; Secret refresh requested.")


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
    if provider.name in ("glitchtip-owner", "glitchtip-smtp"):
        _converge_glitchtip(cluster)
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
        for helm_release in target.helm_releases:
            _force_helm_release(cluster, helm_release)


def _refresh_bootstrap_external_secrets(
    cluster: Cluster,
    active_directory_required: bool,
    docling_enabled: bool = False,
    contextforge_admin_email: str | None = None,
    rejection_debug_enabled: bool = False,
) -> None:
    targets = _BOOTSTRAP_EXTERNAL_SECRETS
    if active_directory_required:
        targets += (AUTH_KEYCLOAK_ACTIVE_DIRECTORY_EXTERNAL_SECRET,)
    if docling_enabled:
        targets += DOCLING_EXTERNAL_SECRETS
    if contextforge_admin_email is not None:
        targets += CONTEXTFORGE_EXTERNAL_SECRETS
    targets += REJECTION_DEBUG_EXTERNAL_SECRETS if rejection_debug_enabled else ()
    total = len(targets)
    for index, target in enumerate(targets, start=1):
        print(f"Refreshing ExternalSecret {target.namespace}/{target.name} ({index}/{total})...")
        cluster.force_external_secret_refresh(target.name, target.namespace, target.target_secret)


def _converge_glitchtip(cluster: Cluster) -> None:
    """Verify generated database delivery without waiting for unentered private inputs."""
    store = GLITCHTIP_SECRET_STORE
    cluster.ensure_secret_store_ready(store.name, store.namespace)
    target = GLITCHTIP_POSTGRES_EXTERNAL_SECRET
    cluster.force_external_secret_refresh(target.name, target.namespace, target.target_secret)


def _converge_runtime(
    cluster: Cluster,
    active_directory_required: bool,
    docling_enabled: bool = False,
    contextforge_admin_email: str | None = None,
    rejection_debug_enabled: bool = False,
    *,
    tailscale_enabled: bool = False,
    glitchtip_enabled: bool = False,
) -> None:
    """Converge catalog-owned Kubernetes consumers after privileged access is revoked."""
    print("Converging SecretStores and ExternalSecrets; this takes approximately 2 minutes...")
    _converge_bootstrap_secret_stores(cluster)
    if glitchtip_enabled:
        _converge_glitchtip(cluster)
        print(
            "GlitchTip generated database credential delivery verified; enter SMTP and initial "
            "owner credentials separately with secret set glitchtip-smtp and secret set "
            "glitchtip-owner. App provisioning and adapter DSN handoff remain separate."
        )
    if tailscale_enabled:
        cluster.ensure_secret_store_ready(
            TAILSCALE_SECRET_STORE.name, TAILSCALE_SECRET_STORE.namespace
        )
        print(
            "Tailscale OAuth access prepared; use secret set tailscale-oauth to verify delivery. "
            "Operator activation remains separate."
        )
    if docling_enabled:
        for store in DOCLING_SECRET_STORES:
            cluster.ensure_secret_store_ready(store.name, store.namespace)
    if contextforge_admin_email is not None:
        cluster.ensure_secret_store_ready(
            CONTEXTFORGE_SECRET_STORE.name, CONTEXTFORGE_SECRET_STORE.namespace
        )
    _refresh_bootstrap_external_secrets(
        cluster,
        active_directory_required,
        docling_enabled,
        contextforge_admin_email,
        rejection_debug_enabled,
    )
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


def _addon_lifecycle(args: argparse.Namespace, verified: VerifiedSelection) -> None:
    run = SelectedRun(verified, "")
    run.require_verified()
    setup = run.setups.get(args.addon_id)
    if setup is None:
        raise SetupError("Add-on is not selected for this client")
    cluster = Cluster(args.context)
    cluster.identity(args.client)
    targets = []
    if args.addon_command == "provision":
        targets = setup.lifecycle.provision
    elif args.addon_command == "restart":
        targets = setup.lifecycle.restart
    product = next(
        item.definition
        for item in verified.addons
        if item.definition.metadata.name == args.addon_id
    )
    for namespace in sorted({target.namespace for target in targets}):
        requested = [item for item in product.spec.inputs if item.namespace == namespace]
        cluster.require_external_inputs(list(approved_pull_inputs(requested, verified, namespace)))
    _confirm(args.context, args.client, f"Add-on {args.addon_command}", args.confirm)
    _deliver_selected(cluster, setup)
    if args.addon_command == "provision":
        for target in setup.lifecycle.provision:
            if target.kind == "HelmRelease":
                _force_helm_release(cluster, HelmReleaseTarget(target.name, target.namespace))
            else:
                cluster.wait_declared_job(target.name, target.namespace)
    elif args.addon_command == "restart":
        for target in setup.lifecycle.restart:
            _restart_target(cluster, target)
    print(f"Selected add-on {args.addon_command} completed.")


def _deliver_selected(cluster: Cluster, setup: Setup) -> None:
    deliveries = {(item.namespace, item.name): item for item in setup.deliveries}
    targets = list(setup.lifecycle.delivery)
    if not targets:
        seen = set()
        for delivery in setup.deliveries:
            store = (delivery.namespace, delivery.secret_store)
            if store not in seen:
                targets.append(Target(kind="SecretStore", namespace=store[0], name=store[1]))
                seen.add(store)
            targets.append(
                Target(kind="ExternalSecret", namespace=delivery.namespace, name=delivery.name)
            )
    requested = {
        (target.namespace, target.name) for target in targets if target.kind == "ExternalSecret"
    }
    if requested != deliveries.keys():
        raise SetupError("Delivery inventory must cover every selected ExternalSecret exactly")
    for target in targets:
        if target.kind == "SecretStore":
            cluster.ensure_secret_store_ready(target.name, target.namespace)
        else:
            delivery = deliveries[(target.namespace, target.name)]
            cluster.ensure_secret_store_ready(delivery.secret_store, delivery.namespace)
            cluster.force_external_secret_refresh(
                delivery.name, delivery.namespace, delivery.target_secret
            )


def _restart_target(cluster: Cluster, target: Target) -> None:
    kubectl = _kubectl_executable()
    try:
        for action in ("restart", "status"):
            command = [
                kubectl,
                "--context",
                cluster.context,
                "--namespace",
                target.namespace,
                "rollout",
                action,
                f"{target.kind.lower()}/{target.name}",
            ]
            if action == "status":
                command.append("--timeout=10m")
            subprocess.run(
                command,
                check=True,
                timeout=660,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.SubprocessError):
        raise SetupError("Selected workload restart or readiness check failed") from None


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
