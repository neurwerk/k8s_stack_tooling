from __future__ import annotations

import base64
import subprocess
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from kubernetes.client.exceptions import ApiException

from openbao_stack_setup.cluster import ClusterError, KubernetesApiEndpoint, StackIdentity
from openbao_stack_setup.custody import CustodianKey, CustodyPaths
from openbao_stack_setup.main import (
    _BOOTSTRAP_EXTERNAL_SECRETS,
    _BOOTSTRAP_HELM_RELEASES,
    _BOOTSTRAP_SECRET_STORES,
    SetupError,
    _arguments,
    _ask_password,
    _ask_text,
    _bootstrap,
    _bound_kit,
    _confirm,
    _converge_bootstrap_secret_stores,
    _deliver_bootstrap_passwords,
    _kubectl_executable,
    _openbao,
    _preflight,
    _prepare_bootstrap_passwords,
    _prompt_bootstrap_credentials,
    _prompt_provider,
    _read_ca,
    _reconcile,
    _reconcile_bootstrap_helm_releases,
    _recovery_pgp_keys,
    _refresh_bootstrap_external_secrets,
    _refresh_provider,
    _required_package_paths,
    _seed_and_finish,
    _set_provider,
    _status,
    _verify_recovery,
    _verify_secret_operator,
    _wait_port_forward,
    _write_custodian_packages,
    main,
)
from openbao_stack_setup.providers import MANAGED_CREDENTIALS, PROVIDERS
from openbao_stack_setup.reconcile import ReconciliationIdentity
from openbao_stack_setup.recovery import RecoveryKit


def kit(checkpoint: str = "initialized") -> RecoveryKit:
    return RecoveryKit(
        4,
        "client",
        "cluster",
        "namespace",
        "seal",
        "key-id",
        checkpoint,
        None,
        False,
        "ceremony",
        ("One", "Two", "Three"),
        ("fingerprint-1", "fingerprint-2", "fingerprint-3"),
        None,
        None,
        None,
        None,
    )


@contextmanager
def opened(api: MagicMock) -> Iterator[MagicMock]:
    yield api


class ControllingTerminal:
    def __init__(self, input_value: str) -> None:
        self.input = StringIO(input_value)
        self.output = StringIO()

    def __enter__(self) -> ControllingTerminal:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def isatty(self) -> bool:
        return True

    def write(self, value: str) -> int:
        return self.output.write(value)

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        return self.input.readline()

    def close(self) -> None:
        return None

    def getvalue(self) -> str:
        return self.output.getvalue()


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["preflight", "--context", "ctx", "--client", "client"], "preflight"),
        (
            [
                "bootstrap",
                "--context",
                "ctx",
                "--client",
                "client",
                "--custody-root",
                "/tmp/custody",
            ],
            "bootstrap",
        ),
        (["status", "--context", "ctx", "--client", "client"], "status"),
        (
            [
                "reconcile",
                "--context",
                "ctx",
                "--client",
                "client",
                "--custodian-package",
                "/tmp/one.zip",
                "--custodian-package",
                "/tmp/two.zip",
            ],
            "reconcile",
        ),
        (
            [
                "recovery",
                "verify",
                "--context",
                "ctx",
                "--client",
                "client",
                "--custody-root",
                "/tmp/custody",
                "--custodian-package",
                "/tmp/one.zip",
                "--custodian-package",
                "/tmp/two.zip",
            ],
            "recovery",
        ),
        (
            ["secret", "set", "brave", "--context", "ctx", "--client", "client"],
            "secret",
        ),
    ],
)
def test_explicit_subcommands(argv: list[str], command: str) -> None:
    assert _arguments().parse_args(argv).command == command


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("preflight", "_preflight"),
        ("bootstrap", "_bootstrap"),
        ("reconcile", "_reconcile"),
        ("status", "_status"),
        ("recovery", "_verify_recovery"),
        ("secret", "_set_provider"),
    ],
)
def test_main_dispatches(command: str, target: str) -> None:
    args = SimpleNamespace(
        command=command,
        context="ctx",
        client="client",
        custody_root=Path("custody"),
        custodian_package=[Path("one"), Path("two")],
        provider="brave",
    )
    with (
        patch("openbao_stack_setup.main._arguments") as arguments,
        patch(f"openbao_stack_setup.main.{target}") as operation,
    ):
        arguments.return_value.parse_args.return_value = args
        main()
    operation.assert_called_once()


def test_main_redacts_expected_failures(capsys: pytest.CaptureFixture[str]) -> None:
    args = SimpleNamespace(command="preflight", context="ctx", client="client")
    with (
        patch("openbao_stack_setup.main._arguments") as arguments,
        patch("openbao_stack_setup.main._preflight", side_effect=ClusterError("safe message")),
        pytest.raises(SystemExit) as captured,
    ):
        arguments.return_value.parse_args.return_value = args
        main()
    assert captured.value.code == 1
    assert capsys.readouterr().out == "ERROR: safe message\n"


def test_confirmation_uses_exact_text() -> None:
    with patch("openbao_stack_setup.main._ask_text", return_value="client") as prompt:
        _confirm("ctx", "client", "Action")
    assert "Type the client name" in prompt.call_args.args[0]

    with (
        patch("openbao_stack_setup.main._ask_text", return_value="CLIENT"),
        pytest.raises(SetupError, match="did not match"),
    ):
        _confirm("ctx", "client", "Action")


@pytest.mark.parametrize("helper", [_ask_text, _ask_password])
@pytest.mark.parametrize("result", [None, KeyboardInterrupt(), EOFError()])
def test_prompt_cancellation_is_redacted(helper: Callable[[str], str], result: object) -> None:
    prompt = MagicMock()
    if isinstance(result, BaseException):
        prompt.return_value.ask.side_effect = result
    else:
        prompt.return_value.ask.return_value = result
    target = "questionary.text" if helper is _ask_text else "questionary.password"
    with (
        patch(f"openbao_stack_setup.main.{target}", prompt),
        pytest.raises(SetupError, match="cancelled"),
    ):
        helper("message")


def test_questionary_prompt_values() -> None:
    with (
        patch("openbao_stack_setup.main.questionary.text") as text,
        patch("openbao_stack_setup.main.questionary.password") as password,
    ):
        text.return_value.ask.return_value = "visible"
        password.return_value.ask.return_value = "secret"
        assert _ask_text("text") == "visible"
        assert _ask_password("password") == "secret"


def test_provider_and_smtp_prompts_validate_values() -> None:
    with patch("openbao_stack_setup.main._ask_password", side_effect=["access", "secret"]):
        assert _prompt_provider(PROVIDERS["route53"]) == {
            "accessKeyId": "access",
            "secretAccessKey": "secret",
        }
    with (
        patch("openbao_stack_setup.main._ask_password", return_value=" "),
        pytest.raises(SetupError, match="nonblank"),
    ):
        _prompt_provider(PROVIDERS["brave"])

    with (
        patch("openbao_stack_setup.main._prompt_all_providers", return_value={"providers": {}}),
        patch("openbao_stack_setup.main._ask_password", side_effect=["user", "password"]),
    ):
        providers, smtp, active_directory = _prompt_bootstrap_credentials(True, False)
    assert providers == {"providers": {}}
    assert smtp == {"username": "user", "password": "password"}
    assert active_directory == {}

    with (
        patch("openbao_stack_setup.main._prompt_all_providers", return_value={"providers": {}}),
    ):
        providers, smtp, active_directory = _prompt_bootstrap_credentials(False, False)
    assert providers == {"providers": {}}
    assert smtp == {"username": "", "password": ""}
    assert active_directory == {}

    with (
        patch("openbao_stack_setup.main._prompt_all_providers", return_value={}),
        patch(
            "openbao_stack_setup.main._prompt_provider",
            return_value={
                "activeDirectoryBindDn": "bind-dn",
                "activeDirectoryBindCredential": "bind-password",
            },
        ) as prompt,
    ):
        _, _, active_directory = _prompt_bootstrap_credentials(False, True)
    assert active_directory == {
        "activeDirectoryBindDn": "bind-dn",
        "activeDirectoryBindCredential": "bind-password",
    }
    prompt.assert_called_once_with(MANAGED_CREDENTIALS["active-directory"])


def test_bootstrap_password_delivery_uses_only_the_controlling_tty(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = ControllingTerminal("I HAVE SAVED THESE PASSWORDS\n")
    modes: list[str] = []
    passwords = {
        "keycloak": "keycloak-password",
        "dify": "dify-password",
        "langfuse": "langfuse-password",
        "grafana": "grafana-password",
        "machine": "machine-password",
    }

    def open_terminal(_: Path, mode: str, **__: object) -> ControllingTerminal:
        modes.append(mode)
        return terminal

    monkeypatch.setattr(Path, "open", open_terminal)

    _deliver_bootstrap_passwords(passwords)

    assert modes == ["r", "w"]
    output = terminal.getvalue()
    assert "Keycloak bootstrap administrator: keycloak-password" in output
    assert "Dify break-glass administrator: dify-password" in output
    assert "Langfuse initial administrator: langfuse-password" in output
    assert "Grafana administrator: grafana-password" in output
    assert "machine-password" not in output
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_bootstrap_password_delivery_requires_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = ControllingTerminal("no\n")
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: terminal)

    with pytest.raises(SetupError, match="did not match"):
        _deliver_bootstrap_passwords({"keycloak": "keycloak-password"})


def test_bootstrap_password_delivery_requires_a_controlling_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))

    with pytest.raises(SetupError, match="controlling TTY"):
        _deliver_bootstrap_passwords({"keycloak": "keycloak-password"})


def test_prepare_bootstrap_passwords_stages_acknowledgement_before_seeding(
    tmp_path: Path,
) -> None:
    root = MagicMock()
    planned = {"keycloak": "keycloak-password", "grafana": "grafana-password"}
    with (
        patch("openbao_stack_setup.main.plan_bootstrap_passwords", return_value=planned),
        patch("openbao_stack_setup.main._deliver_bootstrap_passwords") as deliver,
        patch("openbao_stack_setup.main.update") as update,
    ):
        prepared, values = _prepare_bootstrap_passwords(root, kit(), tmp_path / "recovery")

    root.ensure_kv_v2_mount.assert_called_once_with()
    deliver.assert_called_once_with(planned)
    assert values == planned
    assert prepared.bootstrap_passwords_acknowledged is True
    assert update.call_count == 2


def test_prepare_bootstrap_passwords_resumes_without_redisplaying_acknowledged_values(
    tmp_path: Path,
) -> None:
    acknowledged = replace(
        kit(),
        pending_bootstrap_passwords={"keycloak": "keycloak-password"},
        bootstrap_passwords_acknowledged=True,
    )
    with (
        patch("openbao_stack_setup.main._deliver_bootstrap_passwords") as deliver,
        patch("openbao_stack_setup.main.update") as update,
    ):
        prepared, values = _prepare_bootstrap_passwords(
            MagicMock(), acknowledged, tmp_path / "recovery"
        )

    assert prepared == acknowledged
    assert values == {"keycloak": "keycloak-password"}
    deliver.assert_not_called()
    update.assert_not_called()


def test_prepare_bootstrap_passwords_redisplays_pending_values_on_resume(tmp_path: Path) -> None:
    pending = replace(kit(), pending_bootstrap_passwords={"keycloak": "keycloak-password"})
    with (
        patch("openbao_stack_setup.main._deliver_bootstrap_passwords") as deliver,
        patch("openbao_stack_setup.main.update") as update,
    ):
        prepared, values = _prepare_bootstrap_passwords(MagicMock(), pending, tmp_path / "recovery")

    assert prepared.bootstrap_passwords_acknowledged is True
    assert values == {"keycloak": "keycloak-password"}
    deliver.assert_called_once_with({"keycloak": "keycloak-password"})
    update.assert_called_once_with(tmp_path / "recovery", prepared)


def test_preflight_reports_identity(capsys: pytest.CaptureFixture[str]) -> None:
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    cluster.require_bootstrap_prerequisites.return_value = False
    cluster.validate_kubernetes_api_endpoint.return_value = KubernetesApiEndpoint(
        "172.20.1.202", 6443
    )
    with patch("openbao_stack_setup.main.Cluster", return_value=cluster):
        _preflight("ctx", "client")
    output = capsys.readouterr().out
    assert "client=client cluster=cluster context=ctx" in output
    assert "SMTP credentials required=False" in output
    cluster.require_bootstrap_prerequisites.assert_called_once_with()
    cluster.validate_kubernetes_api_endpoint.assert_called_once_with()


def test_new_bootstrap_initializes_and_seeds(tmp_path: Path) -> None:
    seal_file = tmp_path / "seal.json"
    paths = CustodyPaths(tmp_path, seal_file, tmp_path / "packages")
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    cluster.seal_exists.return_value = False
    cluster.active_directory_required.return_value = True
    api = MagicMock()
    api.initialized.return_value = False
    api.initialize.return_value = ("root", ("one", "two", "three"))
    cluster.force_reconcile.return_value = "openbao-force"
    events: list[str] = []
    cluster.wait_helm_release.side_effect = lambda *args, **kwargs: events.append("wait")

    def open_api(_: MagicMock) -> AbstractContextManager[MagicMock]:
        events.append("open")
        return opened(api)

    initial = replace(
        kit("seal-created"),
        custodian_public_keys=("public-1", "public-2", "public-3"),
        custodian_private_keys=("private-1", "private-2", "private-3"),
    )
    staged = replace(
        initial,
        initialization_root_token="root",
        encrypted_recovery_shares=("one", "two", "three"),
    )
    initialized = kit()
    with (
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main.prepare_custody_paths", return_value=paths),
        patch(
            "openbao_stack_setup.main._prompt_custodian_names",
            return_value=("One", "Two", "Three"),
        ),
        patch(
            "openbao_stack_setup.main.generate_custodian_keys",
            return_value=(
                CustodianKey("fingerprint-1", "public-1", "private-1"),
                CustodianKey("fingerprint-2", "public-2", "private-2"),
                CustodianKey("fingerprint-3", "public-3", "private-3"),
            ),
        ),
        patch("openbao_stack_setup.main.new_static_seal", return_value=(b"x" * 32, "key-id")),
        patch("openbao_stack_setup.main.new_kit", return_value=initial),
        patch("openbao_stack_setup.main.write_new") as write_new,
        patch(
            "openbao_stack_setup.main._recovery_pgp_keys",
            return_value=("pgp-1", "pgp-2", "pgp-3"),
        ),
        patch("openbao_stack_setup.main.with_initialization_material", return_value=staged),
        patch("openbao_stack_setup.main._write_custodian_packages") as write_packages,
        patch("openbao_stack_setup.main.with_checkpoint", return_value=initialized),
        patch("openbao_stack_setup.main.update") as update,
        patch("openbao_stack_setup.main._openbao", side_effect=open_api),
        patch("openbao_stack_setup.main._seed_and_finish") as finish,
    ):
        _bootstrap("ctx", "client", tmp_path)

    write_new.assert_called_once_with(seal_file, initial)
    cluster.require_bootstrap_prerequisites.assert_called_once_with()
    cluster.create_seal.assert_called_once_with(b"x" * 32, "key-id")
    cluster.force_reconcile.assert_called_once_with("openbao", "infra-openbao")
    cluster.wait_helm_release.assert_called_once_with(
        "openbao", "infra-openbao", force_token="openbao-force"
    )
    cluster.wait_openbao_endpoint.assert_called_once_with()
    assert events == ["wait", "open"]
    api.initialize.assert_called_once_with(("pgp-1", "pgp-2", "pgp-3"))
    write_packages.assert_called_once_with(paths, staged)
    assert update.call_args_list == [call(seal_file, staged), call(seal_file, initialized)]
    finish.assert_called_once_with(cluster, api, "root", initialized, seal_file, True)
    cluster.active_directory_required.assert_called_once_with()


def test_bootstrap_resume_uses_recovery_root(tmp_path: Path) -> None:
    recovery_file = tmp_path / "recovery.json"
    recovery_file.write_text("existing", encoding="utf-8")
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    cluster.active_directory_required.return_value = False
    api = MagicMock()
    api.initialized.return_value = True
    api.create_recovery_root_token.return_value = "temporary"
    existing = kit("seeded")
    paths = CustodyPaths(tmp_path, recovery_file, tmp_path / "packages")
    with (
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main.prepare_custody_paths", return_value=paths),
        patch("openbao_stack_setup.main._bound_kit", return_value=existing),
        patch("openbao_stack_setup.main._openbao", side_effect=lambda _: opened(api)),
        patch("openbao_stack_setup.main._seed_and_finish") as finish,
        patch("openbao_stack_setup.main._decrypt_custodian_packages", return_value=("one", "two")),
    ):
        _bootstrap("ctx", "client", tmp_path)
    finish.assert_called_once_with(cluster, api, "temporary", existing, recovery_file, False)
    cluster.force_reconcile.assert_not_called()
    cluster.wait_helm_release.assert_not_called()
    cluster.wait_openbao_endpoint.assert_called_once_with()


def test_bootstrap_resumes_after_packages_precede_checkpoint(tmp_path: Path) -> None:
    seal_file = tmp_path / "seal.json"
    seal_file.write_text("existing", encoding="utf-8")
    paths = CustodyPaths(tmp_path, seal_file, tmp_path / "packages")
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    cluster.active_directory_required.return_value = False
    api = MagicMock()
    api.initialized.return_value = True
    staged = replace(
        kit("seal-created"),
        custodian_public_keys=("public-1", "public-2", "public-3"),
        custodian_private_keys=("private-1", "private-2", "private-3"),
        initialization_root_token="initial-root",
        encrypted_recovery_shares=("share-1", "share-2", "share-3"),
    )
    initialized = kit("initialized")
    with (
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main.prepare_custody_paths", return_value=paths),
        patch("openbao_stack_setup.main._bound_kit", return_value=staged),
        patch("openbao_stack_setup.main._openbao", return_value=opened(api)),
        patch("openbao_stack_setup.main._write_custodian_packages") as write_packages,
        patch("openbao_stack_setup.main.with_checkpoint", return_value=initialized),
        patch("openbao_stack_setup.main.update") as update,
        patch("openbao_stack_setup.main._seed_and_finish") as finish,
    ):
        _bootstrap("ctx", "client", tmp_path)

    write_packages.assert_called_once_with(paths, staged)
    update.assert_called_once_with(seal_file, initialized)
    finish.assert_called_once_with(cluster, api, "initial-root", initialized, seal_file, False)


def test_bootstrap_refreshes_every_declared_external_secret() -> None:
    expected = (
        ("auth-keycloak-openbao-secret", "auth-keycloak", "auth-keycloak-openbao-secret"),
        ("auth-keycloak-secrets", "auth-keycloak", "auth-keycloak-secrets"),
        ("auth-keycloak-smtp-secret", "auth-keycloak", "auth-keycloak-smtp-secret"),
        (
            "auth-keycloak-api-key-bridge-openbao-secret",
            "auth-keycloak-api-key-bridge",
            "auth-keycloak-api-key-bridge-openbao-secret",
        ),
        ("frontend-dify-openbao-secret", "frontend-dify", "frontend-dify-openbao-secret"),
        ("frontend-dify-runtime-secret", "frontend-dify", "frontend-dify-runtime-secret"),
        (
            "frontend-librechat-runtime-secret",
            "frontend-librechat",
            "frontend-librechat-runtime-secret",
        ),
        (
            "frontend-librechat-code-interpreter-runtime-secret",
            "librechat-code-interpreter",
            "frontend-librechat-code-interpreter-runtime-secret",
        ),
        (
            "frontend-studio-openbao-secret",
            "frontend-studio",
            "frontend-studio-openbao-secret",
        ),
        ("infra-agentgateway-secrets", "infra-agentgateway", "infra-agentgateway-secrets"),
        ("cert-manager-issuers-values", "infra-cert-manager", "cert-manager-issuers-values"),
        ("postgres-auth-values", "infra-postgres-auth", "postgres-auth-values"),
        (
            "postgres-operations-values",
            "infra-postgres-operations",
            "postgres-operations-values",
        ),
        (
            "monitor-fluent-bit-shared-ingest-secret",
            "monitor-fluent-bit",
            "monitor-fluent-bit-shared-ingest-secret",
        ),
        (
            "monitor-kube-prometheus-stack-secret",
            "monitor-kube-prometheus-stack",
            "monitor-kube-prometheus-stack-secret",
        ),
        (
            "monitor-kube-prometheus-stack-smtp-secret",
            "monitor-kube-prometheus-stack",
            "monitor-kube-prometheus-stack-smtp-secret",
        ),
        ("monitor-langfuse-secrets", "monitor-langfuse", "monitor-langfuse-secrets"),
        ("monitor-opensearch-secret", "monitor-opensearch", "monitor-opensearch-secret"),
        ("monitor-pii-engine-secrets", "monitor-pii-engine", "monitor-pii-engine-secrets"),
    )
    assert (
        tuple(
            (target.name, target.namespace, target.target_secret)
            for target in _BOOTSTRAP_EXTERNAL_SECRETS
        )
        == expected
    )

    cluster = MagicMock()
    _refresh_bootstrap_external_secrets(cluster, False)

    assert cluster.force_external_secret_refresh.call_args_list == [
        call(name, namespace, target_secret) for name, namespace, target_secret in expected
    ]

    cluster.reset_mock()
    _refresh_bootstrap_external_secrets(cluster, True)
    assert cluster.force_external_secret_refresh.call_args_list == [
        *[call(name, namespace, target_secret) for name, namespace, target_secret in expected],
        call(
            "auth-keycloak-active-directory-secret",
            "auth-keycloak",
            "auth-keycloak-active-directory-secret",
        ),
    ]


def test_bootstrap_converges_every_declared_secret_store(
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = (
        ("auth-keycloak-openbao-secret-store", "auth-keycloak"),
        ("auth-keycloak-api-key-bridge-openbao-secret-store", "auth-keycloak-api-key-bridge"),
        ("frontend-dify-openbao-secret-store", "frontend-dify"),
        ("frontend-librechat-openbao-secret-store", "frontend-librechat"),
        ("librechat-code-interpreter-openbao-secret-store", "librechat-code-interpreter"),
        ("frontend-studio-openbao-secret-store", "frontend-studio"),
        ("infra-agentgateway-openbao-secret-store", "infra-agentgateway"),
        ("infra-cert-manager-openbao-secret-store", "infra-cert-manager"),
        ("infra-postgres-auth-openbao-secret-store", "infra-postgres-auth"),
        ("infra-postgres-operations-openbao-secret-store", "infra-postgres-operations"),
        ("monitor-fluent-bit-openbao-secret-store", "monitor-fluent-bit"),
        ("monitor-kube-prometheus-stack-openbao-secret-store", "monitor-kube-prometheus-stack"),
        ("monitor-langfuse-openbao-secret-store", "monitor-langfuse"),
        ("monitor-opensearch-openbao-secret-store", "monitor-opensearch"),
        ("monitor-pii-engine-openbao-secret-store", "monitor-pii-engine"),
    )
    assert tuple((target.name, target.namespace) for target in _BOOTSTRAP_SECRET_STORES) == expected

    cluster = MagicMock()
    _converge_bootstrap_secret_stores(cluster)

    assert cluster.ensure_secret_store_ready.call_args_list == [
        call(name, namespace) for name, namespace in expected
    ]
    output = capsys.readouterr().out
    assert "(1/15)" in output
    assert "(15/15)" in output


def test_bootstrap_force_reconciles_blocked_helm_releases() -> None:
    expected = (
        ("cert-manager-issuers", "infra-cert-manager"),
        ("postgres-auth", "infra-postgres-auth"),
        ("postgres-operations", "infra-postgres-operations"),
        ("kube-prometheus-stack", "monitor-kube-prometheus-stack"),
        ("opensearch", "monitor-opensearch"),
        ("pii-engine", "monitor-pii-engine"),
    )
    assert tuple((target.name, target.namespace) for target in _BOOTSTRAP_HELM_RELEASES) == expected

    cluster = MagicMock()
    tokens = [f"force-{index}" for index in range(len(expected))]
    cluster.force_reconcile.side_effect = tokens
    _reconcile_bootstrap_helm_releases(cluster, False)

    assert cluster.force_reconcile.call_args_list == [
        call(name, namespace) for name, namespace in expected
    ]
    assert cluster.wait_helm_release.call_args_list == [
        call(name, namespace, timeout_seconds=1800, force_token=token)
        for (name, namespace), token in zip(expected, tokens, strict=True)
    ]

    cluster.reset_mock()
    cluster.force_reconcile.side_effect = [
        *[f"force-{index}" for index in range(len(expected))],
        "force-ad",
    ]
    _reconcile_bootstrap_helm_releases(cluster, True)
    assert cluster.force_reconcile.call_args_list[-1] == call(
        "keycloak-active-directory", "auth-keycloak"
    )
    assert cluster.wait_helm_release.call_args_list[-1] == call(
        "keycloak-active-directory",
        "auth-keycloak",
        timeout_seconds=900,
        force_token="force-ad",
    )


def test_bootstrap_rejects_unsafe_states(tmp_path: Path) -> None:
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    cluster.seal_exists.return_value = True
    missing = tmp_path / "missing.json"
    paths = CustodyPaths(tmp_path, missing, tmp_path / "packages")
    with (
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main.prepare_custody_paths", return_value=paths),
        pytest.raises(SetupError, match="Static seal already exists"),
    ):
        _bootstrap("ctx", "client", tmp_path)

    recovery_file = tmp_path / "existing.json"
    recovery_file.write_text("existing", encoding="utf-8")
    api = MagicMock()
    api.initialized.return_value = True
    paths = CustodyPaths(tmp_path, recovery_file, tmp_path / "packages")
    with (
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main.prepare_custody_paths", return_value=paths),
        patch("openbao_stack_setup.main._bound_kit", return_value=kit("seal-created")),
        patch("openbao_stack_setup.main._openbao", return_value=opened(api)),
        pytest.raises(SetupError, match="recovery requires escalation"),
    ):
        _bootstrap("ctx", "client", tmp_path)


def test_custodian_package_paths_must_be_distinct(tmp_path: Path) -> None:
    packages = [tmp_path / f"custodian-{index}.zip" for index in range(1, 3)]
    for path in packages:
        path.write_bytes(b"package")
    assert _required_package_paths(packages) == (packages[0], packages[1])
    with pytest.raises(SetupError, match="must be distinct"):
        _required_package_paths([packages[0], packages[0]])


def test_recovery_public_keys_use_binary_openpgp_encoding() -> None:
    staged = replace(
        kit("seal-created"),
        custodian_public_keys=("armored-1", "armored-2", "armored-3"),
        custodian_private_keys=("private-1", "private-2", "private-3"),
    )
    with patch(
        "openbao_stack_setup.main.binary_public_key",
        side_effect=[b"binary-1", b"binary-2", b"binary-3"],
    ):
        assert _recovery_pgp_keys(staged) == (
            base64.b64encode(b"binary-1").decode(),
            base64.b64encode(b"binary-2").decode(),
            base64.b64encode(b"binary-3").decode(),
        )


def test_custodian_packages_are_decrypted_before_checkpoint(tmp_path: Path) -> None:
    paths = CustodyPaths(tmp_path, tmp_path / "seal.json", tmp_path / "packages")
    staged = replace(
        kit("seal-created"),
        custodian_public_keys=("public-1", "public-2", "public-3"),
        custodian_private_keys=("private-1", "private-2", "private-3"),
        initialization_root_token="root",
        encrypted_recovery_shares=(
            base64.b64encode(b"encrypted-1").decode(),
            base64.b64encode(b"encrypted-2").decode(),
            base64.b64encode(b"encrypted-3").decode(),
        ),
    )
    with (
        patch("openbao_stack_setup.main.write_custodian_package") as write_package,
        patch("openbao_stack_setup.main.load_custodian_package", side_effect=[MagicMock()] * 3),
        patch(
            "openbao_stack_setup.main.decrypt_package_share",
            side_effect=["share-1", "share-2", "share-3"],
        ) as decrypt,
    ):
        _write_custodian_packages(paths, staged)

    assert write_package.call_count == 3
    assert decrypt.call_count == 3

    with (
        patch("openbao_stack_setup.main.write_custodian_package"),
        patch("openbao_stack_setup.main.load_custodian_package", side_effect=[MagicMock()] * 3),
        patch("openbao_stack_setup.main.decrypt_package_share", return_value="duplicate"),
        pytest.raises(SetupError, match="three distinct"),
    ):
        _write_custodian_packages(paths, staged)


def test_seed_finish_and_root_revocation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unauthenticated = MagicMock()
    unauthenticated.ca_cert = tmp_path / "ca"
    root = MagicMock()
    root.self_accessor.return_value = "current"
    root.list_token_accessors.return_value = ("current", "root", "user")
    root.lookup_accessor.side_effect = [
        {"policies": ["root"]},
        {"policies": ["root"]},
        {"policies": ["default"]},
    ]
    report = SimpleNamespace(external_records_changed=2, internal_records_changed=10)
    seeded = kit("seeded")
    complete = kit("complete")
    events: list[str] = []

    def seed(*_: object, **__: object) -> SimpleNamespace:
        events.append("seed")
        return report

    def record(name: str) -> Callable[..., None]:
        def operation(*_: object) -> None:
            events.append(name)

        return operation

    root.revoke_self.side_effect = record("revoke-self")
    cluster = MagicMock()
    cluster.reconcile_kustomization.side_effect = record("infrastructure")

    with (
        patch("openbao_stack_setup.main.OpenBaoClient", return_value=root),
        patch(
            "openbao_stack_setup.main._prompt_bootstrap_credentials",
            return_value=({}, {}, {}),
        ),
        patch(
            "openbao_stack_setup.main._prepare_bootstrap_passwords",
            return_value=(kit(), {"keycloak": "keycloak-password"}),
        ),
        patch("openbao_stack_setup.main.seed_bootstrap", side_effect=seed),
        patch("openbao_stack_setup.main.with_checkpoint", side_effect=[seeded, complete]),
        patch("openbao_stack_setup.main.update") as update,
        patch("openbao_stack_setup.main._verify_secret_operator", side_effect=record("verify")),
        patch(
            "openbao_stack_setup.main._converge_bootstrap_secret_stores",
            side_effect=record("stores"),
        ),
        patch(
            "openbao_stack_setup.main._refresh_bootstrap_external_secrets",
            side_effect=record("refresh"),
        ),
        patch(
            "openbao_stack_setup.main._reconcile_bootstrap_helm_releases",
            side_effect=record("reconcile"),
        ),
    ):
        _seed_and_finish(
            cluster,
            unauthenticated,
            "root",
            kit(),
            tmp_path / "recovery",
            True,
        )
    assert update.call_count == 2
    assert events == [
        "seed",
        "verify",
        "revoke-self",
        "stores",
        "refresh",
        "reconcile",
        "infrastructure",
    ]
    cluster.reconcile_kustomization.assert_called_once_with("infrastructure")
    cluster.active_directory_required.assert_not_called()
    root.revoke_accessor.assert_called_once_with("root")
    root.revoke_self.assert_called_once_with()
    output = capsys.readouterr().out
    assert "this takes approximately 2 minutes" in output
    assert "this usually takes a few minutes" in output
    assert "OpenSearch hooks may take 1-2 minutes" in output
    assert "no progress after 10 minutes" in output


def test_seeded_resume_reconciles_additive_internal_credentials(tmp_path: Path) -> None:
    unauthenticated = MagicMock()
    unauthenticated.ca_cert = tmp_path / "ca"
    root = MagicMock()
    complete = kit("complete")
    reconciled = SimpleNamespace(
        applied_version=1,
        internal_records_changed=1,
        internal_fields_added=7,
    )

    with (
        patch("openbao_stack_setup.main.OpenBaoClient", return_value=root),
        patch("openbao_stack_setup.main._prompt_bootstrap_credentials") as prompt,
        patch("openbao_stack_setup.main.seed_bootstrap") as seed,
        patch("openbao_stack_setup.main.reconcile_openbao", return_value=reconciled) as reconcile,
        patch("openbao_stack_setup.main._verify_secret_operator"),
        patch("openbao_stack_setup.main._revoke_other_root_tokens"),
        patch("openbao_stack_setup.main._refresh_bootstrap_external_secrets"),
        patch("openbao_stack_setup.main._reconcile_bootstrap_helm_releases"),
        patch("openbao_stack_setup.main.with_checkpoint", return_value=complete),
        patch("openbao_stack_setup.main.update") as update,
    ):
        _seed_and_finish(
            MagicMock(),
            unauthenticated,
            "root",
            kit("seeded"),
            tmp_path / "recovery",
            False,
        )

    reconcile.assert_called_once_with(
        root,
        ReconciliationIdentity("client", "cluster", "namespace"),
    )
    prompt.assert_not_called()
    seed.assert_not_called()
    root.revoke_self.assert_called_once_with()
    update.assert_called_once_with(tmp_path / "recovery", complete)


def test_seed_waits_for_bootstrap_password_acknowledgement(tmp_path: Path) -> None:
    unauthenticated = MagicMock()
    unauthenticated.ca_cert = tmp_path / "ca"
    root = MagicMock()
    with (
        patch("openbao_stack_setup.main.OpenBaoClient", return_value=root),
        patch(
            "openbao_stack_setup.main._prompt_bootstrap_credentials",
            return_value=({}, {}, {}),
        ),
        patch(
            "openbao_stack_setup.main._prepare_bootstrap_passwords",
            side_effect=SetupError("Bootstrap password acknowledgement did not match"),
        ),
        patch("openbao_stack_setup.main.seed_bootstrap") as seed,
        pytest.raises(SetupError, match="did not match"),
    ):
        _seed_and_finish(
            MagicMock(),
            unauthenticated,
            "root",
            kit(),
            tmp_path / "recovery",
            False,
        )
    seed.assert_not_called()
    root.revoke_self.assert_called_once_with()


def test_verify_secret_operator() -> None:
    cluster = MagicMock()
    cluster.token_request.return_value = "jwt"
    unauthenticated = MagicMock()
    unauthenticated.kubernetes_login.return_value = "token"
    operator = MagicMock()
    with patch("openbao_stack_setup.main.OpenBaoClient", return_value=operator):
        _verify_secret_operator(cluster, unauthenticated)
    operator.read_secret.assert_called_once_with("infra-agentgateway/external")
    operator.revoke_self.assert_called_once_with()


def test_status_and_recovery_verification(tmp_path: Path) -> None:
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    cluster.active_directory_required.return_value = False
    api = MagicMock()
    api.initialized.return_value = True
    api.create_recovery_root_token.return_value = "temporary"
    temporary = MagicMock()
    recovery_file = tmp_path / "operator-custody" / "openbao-seal.json"
    paths = CustodyPaths(tmp_path, recovery_file, tmp_path / "custodian-packages")
    with (
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main.prepare_custody_paths", return_value=paths),
        patch("openbao_stack_setup.main._bound_kit", return_value=kit("complete")),
        patch("openbao_stack_setup.main._openbao", side_effect=lambda _: opened(api)),
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main.OpenBaoClient", return_value=temporary),
        patch("openbao_stack_setup.main._decrypt_custodian_packages", return_value=("one", "two")),
        patch(
            "openbao_stack_setup.main._required_package_paths",
            return_value=(tmp_path / "one", tmp_path / "two"),
        ),
    ):
        _status("ctx", "client", tmp_path)
        _verify_recovery("ctx", "client", tmp_path, [tmp_path / "one", tmp_path / "two"])
    temporary.list_token_accessors.assert_called_once_with()
    temporary.revoke_self.assert_called_once_with()

    with (
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main.prepare_custody_paths", return_value=paths),
        patch("openbao_stack_setup.main._bound_kit", return_value=kit("seal-created")),
        patch("openbao_stack_setup.main._confirm"),
        pytest.raises(SetupError, match="no recovery-share checkpoint"),
    ):
        _verify_recovery("ctx", "client", tmp_path, [tmp_path / "one", tmp_path / "two"])


def test_reconcile_revokes_root_before_runtime_convergence(tmp_path: Path) -> None:
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    cluster.active_directory_required.return_value = False
    api = MagicMock()
    api.create_recovery_root_token.return_value = "temporary"
    root = MagicMock()
    events: list[str] = []
    root.revoke_self.side_effect = lambda: events.append("revoke")
    report = SimpleNamespace(previous_version=0, applied_version=1, replicated_records=1)
    paths = CustodyPaths(tmp_path, tmp_path / "seal.json", tmp_path / "packages")

    with (
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main.prepare_custody_paths", return_value=paths),
        patch("openbao_stack_setup.main._bound_kit", return_value=kit("complete")),
        patch(
            "openbao_stack_setup.main._required_package_paths",
            return_value=(Path("one"), Path("two")),
        ),
        patch("openbao_stack_setup.main._decrypt_custodian_packages", return_value=("one", "two")),
        patch("openbao_stack_setup.main._openbao", return_value=opened(api)),
        patch("openbao_stack_setup.main.OpenBaoClient", return_value=root),
        patch("openbao_stack_setup.main.reconcile_openbao", return_value=report) as reconcile,
        patch("openbao_stack_setup.main._verify_secret_operator"),
        patch("openbao_stack_setup.main._revoke_other_root_tokens"),
        patch(
            "openbao_stack_setup.main._converge_runtime",
            side_effect=lambda _cluster, _active_directory_required: events.append("converge"),
        ) as converge,
    ):
        _reconcile("ctx", "client", tmp_path, [Path("one"), Path("two")])

    reconcile.assert_called_once_with(
        root,
        ReconciliationIdentity("client", "cluster", "namespace"),
    )
    assert events == ["revoke", "converge"]
    converge.assert_called_once_with(cluster, False)


def test_reconcile_requires_complete_bootstrap(tmp_path: Path) -> None:
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    paths = CustodyPaths(tmp_path, tmp_path / "seal.json", tmp_path / "packages")
    with (
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main.prepare_custody_paths", return_value=paths),
        patch("openbao_stack_setup.main._bound_kit", return_value=kit("seeded")),
        pytest.raises(SetupError, match="must be complete"),
    ):
        _reconcile("ctx", "client", tmp_path, [Path("one"), Path("two")])


def test_provider_update_and_refresh() -> None:
    cluster = MagicMock()
    unauthenticated = MagicMock()
    unauthenticated.kubernetes_login.return_value = "token"
    operator = MagicMock()
    with (
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main._prompt_provider", return_value={"braveApiKey": "secret"}),
        patch("openbao_stack_setup.main._openbao", return_value=opened(unauthenticated)),
        patch("openbao_stack_setup.main.OpenBaoClient", return_value=operator),
        patch("openbao_stack_setup.main.update_provider") as update_provider,
        patch("openbao_stack_setup.main._refresh_provider") as refresh,
    ):
        _set_provider("ctx", "client", "brave")
    update_provider.assert_called_once()
    refresh.assert_called_once_with(cluster, PROVIDERS["brave"])

    cluster.force_reconcile.return_value = "force-123"
    _refresh_provider(cluster, PROVIDERS["route53"])
    cluster.force_external_secret_refresh.assert_called_once_with(
        "cert-manager-issuers-values",
        "infra-cert-manager",
        "cert-manager-issuers-values",
    )
    cluster.force_reconcile.assert_called_once_with("cert-manager-issuers", "infra-cert-manager")
    cluster.wait_helm_release.assert_called_once_with(
        "cert-manager-issuers",
        "infra-cert-manager",
        timeout_seconds=900,
        force_token="force-123",
    )

    cluster.reset_mock()
    cluster.force_reconcile.side_effect = ["force-124", "force-125"]
    _refresh_provider(cluster, MANAGED_CREDENTIALS["smtp"])
    assert cluster.force_external_secret_refresh.call_args_list == [
        call("auth-keycloak-smtp-secret", "auth-keycloak", "auth-keycloak-smtp-secret"),
        call(
            "monitor-kube-prometheus-stack-smtp-secret",
            "monitor-kube-prometheus-stack",
            "monitor-kube-prometheus-stack-smtp-secret",
        ),
    ]
    assert cluster.force_reconcile.call_args_list == [
        call("keycloak", "auth-keycloak"),
        call("kube-prometheus-stack", "monitor-kube-prometheus-stack"),
    ]
    assert cluster.wait_helm_release.call_args_list == [
        call("keycloak", "auth-keycloak", timeout_seconds=900, force_token="force-124"),
        call(
            "kube-prometheus-stack",
            "monitor-kube-prometheus-stack",
            timeout_seconds=900,
            force_token="force-125",
        ),
    ]

    cluster.reset_mock()
    cluster.force_reconcile.side_effect = None
    cluster.force_reconcile.return_value = "force-126"
    _refresh_provider(cluster, MANAGED_CREDENTIALS["active-directory"])
    cluster.force_external_secret_refresh.assert_called_once_with(
        "auth-keycloak-active-directory-secret",
        "auth-keycloak",
        "auth-keycloak-active-directory-secret",
    )
    cluster.force_reconcile.assert_called_once_with("keycloak-active-directory", "auth-keycloak")
    cluster.wait_helm_release.assert_called_once_with(
        "keycloak-active-directory",
        "auth-keycloak",
        timeout_seconds=900,
        force_token="force-126",
    )

    cluster.reset_mock()
    cluster.force_external_secret_refresh.side_effect = ClusterError(
        "Could not inspect ExternalSecret auth-keycloak/"
        "auth-keycloak-active-directory-secret: HTTP 404"
    )
    with pytest.raises(ClusterError, match="HTTP 404"):
        _refresh_provider(cluster, MANAGED_CREDENTIALS["active-directory"])
    cluster.force_reconcile.assert_not_called()

    cluster.force_external_secret_refresh.side_effect = ClusterError(
        "Could not refresh ExternalSecret infra-agentgateway/infra-agentgateway-secrets: HTTP 500"
    )
    with pytest.raises(ClusterError, match="HTTP 500"):
        _refresh_provider(cluster, PROVIDERS["brave"])


def test_active_directory_update_fails_before_prompt_or_openbao_when_disabled() -> None:
    cluster = MagicMock()
    cluster.active_directory_required.return_value = False
    with (
        patch("openbao_stack_setup.main._confirm") as confirm,
        patch("openbao_stack_setup.main.Cluster", return_value=cluster),
        patch("openbao_stack_setup.main._prompt_provider") as prompt,
        patch("openbao_stack_setup.main._openbao") as openbao,
        pytest.raises(SetupError, match="federation is disabled"),
    ):
        _set_provider("ctx", "client", "active-directory")

    cluster.identity.assert_called_once_with("client")
    cluster.active_directory_required.assert_called_once_with()
    confirm.assert_not_called()
    prompt.assert_not_called()
    openbao.assert_not_called()


def test_recovery_binding() -> None:
    identity = StackIdentity("client", "cluster", "namespace")
    with patch("openbao_stack_setup.main.load", return_value=kit()) as load:
        assert _bound_kit(Path("recovery"), identity) == kit()
    load.assert_called_once_with(Path("recovery"))

    with (
        patch("openbao_stack_setup.main.load", return_value=replace(kit(), client="other")),
        pytest.raises(SetupError, match="does not belong"),
    ):
        _bound_kit(Path("recovery"), identity)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (None, "does not contain"),
        ({}, "valid ca.crt"),
        ({"ca.crt": 123}, "valid ca.crt"),
        ({"ca.crt": "%%%"}, "invalid CA"),
        ({"ca.crt": ""}, "valid ca.crt"),
    ],
)
def test_tls_secret_validation(data: object, message: str) -> None:
    cluster = MagicMock()
    cluster.core.read_namespaced_secret.return_value = SimpleNamespace(data=data)
    with pytest.raises(SetupError, match=message):
        _read_ca(cluster)

    cluster.core.read_namespaced_secret.side_effect = ApiException(status=403)
    with pytest.raises(SetupError, match="Could not read"):
        _read_ca(cluster)


def test_tls_secret_decodes_ca() -> None:
    cluster = MagicMock()
    cluster.core.read_namespaced_secret.return_value = SimpleNamespace(
        data={"ca.crt": base64.b64encode(b"certificate").decode()}
    )
    assert _read_ca(cluster) == b"certificate"

    with (
        patch("openbao_stack_setup.main.base64.b64decode", return_value=b""),
        pytest.raises(SetupError, match="empty CA"),
    ):
        _read_ca(cluster)


def test_kubectl_resolution(tmp_path: Path) -> None:
    executable = tmp_path / "kubectl"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    with patch("openbao_stack_setup.main.shutil.which", return_value=str(executable)):
        assert _kubectl_executable() == str(executable.resolve())

    with (
        patch("openbao_stack_setup.main.shutil.which", return_value=None),
        pytest.raises(SetupError, match="unavailable"),
    ):
        _kubectl_executable()
    executable.chmod(0o600)
    with (
        patch("openbao_stack_setup.main.shutil.which", return_value=str(executable)),
        pytest.raises(SetupError, match="unavailable"),
    ):
        _kubectl_executable()


def test_port_forward_wait_states() -> None:
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = 1
    with pytest.raises(SetupError, match=r"port-forward.*failed"):
        _wait_port_forward(process)

    process.poll.return_value = None
    connection = MagicMock()
    with patch("openbao_stack_setup.main.socket.create_connection", return_value=connection):
        _wait_port_forward(process)
    connection.__enter__.assert_called_once_with()


def test_openbao_starts_and_stops_validated_kubectl() -> None:
    cluster = MagicMock()
    cluster.context = "ctx"
    process = MagicMock()
    process.poll.return_value = None
    session = MagicMock()
    session.__enter__.return_value = session
    with (
        patch("openbao_stack_setup.main._read_ca", return_value=b"CA"),
        patch("openbao_stack_setup.main._kubectl_executable", return_value="/usr/bin/kubectl"),
        patch("openbao_stack_setup.main.subprocess.Popen", return_value=process) as popen,
        patch("openbao_stack_setup.main._wait_port_forward"),
        patch("openbao_stack_setup.main.requests.Session", return_value=session),
        _openbao(cluster) as api,
    ):
        assert api.ca_cert.stat().st_mode & 0o777 == 0o600
    assert popen.call_args.args[0] == [
        "/usr/bin/kubectl",
        "--context",
        "ctx",
        "-n",
        "infra-openbao",
        "port-forward",
        "pod/infra-openbao-0",
        "8200:8203",
    ]
    process.send_signal.assert_called_once()

    process.wait.side_effect = subprocess.TimeoutExpired("kubectl", 10)
    with (
        patch("openbao_stack_setup.main._read_ca", return_value=b"CA"),
        patch("openbao_stack_setup.main._kubectl_executable", return_value="/usr/bin/kubectl"),
        patch("openbao_stack_setup.main.subprocess.Popen", return_value=process),
        patch("openbao_stack_setup.main._wait_port_forward"),
        patch("openbao_stack_setup.main.requests.Session", return_value=session),
        _openbao(cluster),
    ):
        pass
    process.kill.assert_called_once_with()


def test_openbao_redacts_spawn_failure() -> None:
    cluster = MagicMock()
    with (
        patch("openbao_stack_setup.main._read_ca", return_value=b"CA"),
        patch("openbao_stack_setup.main._kubectl_executable", return_value="/usr/bin/kubectl"),
        patch("openbao_stack_setup.main.subprocess.Popen", side_effect=OSError("private")),
        pytest.raises(SetupError, match="Could not start"),
        _openbao(cluster),
    ):
        pass
