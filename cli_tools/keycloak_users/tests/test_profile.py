import json
import stat
from dataclasses import asdict
from pathlib import Path

import pytest

from keycloak_users.profile import (
    Profile,
    SafeError,
    config_path,
    identifier,
    load_profiles,
    save_profile,
    text,
)


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_atomic_profile_allowlist_and_preservation():
    assert load_profiles() == {}
    first = Profile("one", "https://id.example/auth", "realm", ca_path="/tmp/ca.pem")
    second = Profile("two", "https://id.example", "other", port=12345)
    save_profile(first)
    save_profile(second)
    assert load_profiles() == {"one": first, "two": second}
    assert json.loads(config_path().read_text()) == {"one": asdict(first), "two": asdict(second)}
    assert stat.S_IMODE(config_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path().parent.stat().st_mode) == 0o700
    assert list(config_path().parent.iterdir()) == [config_path()]
    assert first.issuer == "https://id.example/auth/realms/realm"
    assert second.callback == "http://127.0.0.1:12345/callback"


@pytest.mark.parametrize(
    "url",
    [
        "http://id.example",
        "https://user:secret@id.example",
        "https://id.example/evil",
        "https://id.example?query=yes",
        "https://id.example/#fragment",
        "https://id.example:bad",
        "https://id.example\\evil",
        "https://id.example\n",
        "https://",
        "https://id.example/../auth",
    ],
)
def test_reject_unsafe_server(url):
    with pytest.raises((SafeError, ValueError)):
        Profile("test", url, "realm")


@pytest.mark.parametrize(
    "changes",
    [
        {"port": 80},
        {"port": True},
        {"port": 65536},
        {"ca_path": "relative.pem"},
        {"realm": "../master"},
        {"client_id": "client?secret"},
        {"name": "\x1bmalicious"},
    ],
)
def test_reject_invalid_profile_fields(changes):
    with pytest.raises(SafeError):
        Profile(
            **({"name": "test", "server_url": "https://id.example", "realm": "realm"} | changes)
        )


@pytest.mark.parametrize(
    "content",
    [
        "{bad",
        "[]",
        '{"one": {}, "one": {}}',
        '{"one": null}',
        '{"one": {"name": "one", "server_url": "https://id.example", '
        '"realm": "r", "token": "secret"}}',
        '{"one": {"name": "other", "server_url": "https://id.example", "realm": "r"}}',
    ],
)
def test_malformed_file_not_overwritten(content):
    path = config_path()
    path.parent.mkdir()
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(SafeError):
        save_profile(Profile("new", "https://id.example", "realm"))
    assert path.read_text() == content


def test_symlinks_and_unsafe_permissions(tmp_path):
    path = config_path()
    path.parent.mkdir()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(SafeError, match="regular"):
        load_profiles()
    path.unlink()
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(SafeError, match="0600"):
        load_profiles()


def test_config_environment_and_printable_validation(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    with pytest.raises(SafeError):
        config_path()
    for value in (None, "", "x" * 513, "\x1b[31m"):
        with pytest.raises(SafeError):
            text(value)
    assert identifier("a.b-c_1") == "a.b-c_1"


def test_failed_atomic_replace_preserves_profiles_and_removes_temporary(monkeypatch):
    save_profile(Profile("old", "https://id.example", "realm"))
    before = config_path().read_bytes()

    def fail_replace(_self, _target):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(SafeError, match="atomically"):
        save_profile(Profile("new", "https://id.example", "realm"))
    assert config_path().read_bytes() == before
    assert list(config_path().parent.iterdir()) == [config_path()]
