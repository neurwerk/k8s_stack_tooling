from __future__ import annotations

from pathlib import Path

import pytest
from fake import FakeSession

from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.providers import MANAGED_CREDENTIALS, PROVIDERS, update_provider


def client(tmp_path: Path, session: FakeSession) -> OpenBaoClient:
    ca = tmp_path / "ca.crt"
    ca.write_text("CA", encoding="utf-8")
    return OpenBaoClient("https://bao.test", "root", ca, session)


@pytest.mark.parametrize(
    "values",
    [{}, {"braveApiKey": ""}, {"braveApiKey": "key", "extra": "value"}],
)
def test_provider_updates_require_exact_nonblank_fields(
    tmp_path: Path, values: dict[str, str]
) -> None:
    session = FakeSession()
    with pytest.raises(OpenBaoError, match="incomplete"):
        update_provider(client(tmp_path, session), PROVIDERS["brave"], values)
    assert session.secrets == {}


def test_smtp_is_a_managed_credential() -> None:
    assert MANAGED_CREDENTIALS["smtp"].paths == (
        "stack-setup/providers/smtp",
        "auth-keycloak/external",
        "monitor-kube-prometheus-stack/external",
    )
    assert MANAGED_CREDENTIALS["smtp"].fields == ("smtpUsername", "smtpPassword")


def test_active_directory_is_a_managed_credential() -> None:
    assert MANAGED_CREDENTIALS["active-directory"].paths == ("auth-keycloak/external",)
    assert MANAGED_CREDENTIALS["active-directory"].fields == (
        "activeDirectoryBindDn",
        "activeDirectoryBindCredential",
    )


def test_speech_updates_create_then_preserve_siblings_with_cas(tmp_path: Path) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    for direction, value in (("stt", "first"), ("tts", "second"), ("stt", "rotated")):
        name = f"librechat-{direction}"
        assert name not in PROVIDERS
        provider = MANAGED_CREDENTIALS[name]
        assert provider.paths == ("frontend-librechat/external",)
        assert provider.fields == (f"{direction}ApiKey",)
        update_provider(api, provider, {f"{direction}ApiKey": value})
    assert session.secrets["frontend-librechat/external"].values == {
        "sttApiKey": "rotated",
        "ttsApiKey": "second",
    }
    assert session.secrets["frontend-librechat/external"].version == 3
    assert len(session.secrets) == 1
