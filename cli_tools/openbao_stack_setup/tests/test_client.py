from __future__ import annotations

import base64
import json as json_module
from pathlib import Path

import pytest
import requests

from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError


class QueueSession:
    def __init__(self, *items: requests.Response | requests.RequestException) -> None:
        self.items = list(items)
        self.calls: list[tuple[str, str, JsonValue, tuple[float, float]]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: JsonValue | None,
        timeout: tuple[float, float],
        verify: str,
    ) -> requests.Response:
        self.calls.append((method, url, json, timeout))
        item = self.items.pop(0)
        if isinstance(item, requests.RequestException):
            raise item
        return item


def response(
    status: int = 200, body: object | None = None, raw: bytes | None = None
) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    if raw is not None:
        result._content = raw
    elif body is not None:
        result._content = json_module.dumps(body).encode()
    else:
        result._content = b""
    return result


def client(tmp_path: Path, session: QueueSession) -> OpenBaoClient:
    ca = tmp_path / "ca.crt"
    ca.write_text("CA", encoding="utf-8")
    return OpenBaoClient("https://bao.test", "token", ca, session)


def encoded_token(token: str, otp: str) -> str:
    encoded = bytes(left ^ right for left, right in zip(token.encode(), otp.encode(), strict=True))
    return base64.b64encode(encoded).decode().rstrip("=")


def test_properties_and_initialization(tmp_path: Path) -> None:
    session = QueueSession(
        response(body={"initialized": True}),
        response(body={"root_token": "root", "recovery_keys_base64": ["one", "two", "three"]}),
    )
    api = client(tmp_path, session)

    assert api.ca_cert == tmp_path / "ca.crt"
    assert api.session is session
    assert api.initialized() is True
    assert api.initialize(("first", "second", "third")) == ("root", ("one", "two", "three"))
    assert session.calls[1][2] == {
        "recovery_shares": 3,
        "recovery_threshold": 2,
        "recovery_pgp_keys": ["first", "second", "third"],
    }
    assert session.calls[1][3] == (3.05, 180.0)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"initialized": "yes"}, "initialization status"),
        ({"root_token": "", "recovery_keys_base64": ["key"]}, "root token"),
        (
            {"root_token": "root", "recovery_keys_base64": []},
            "three base64-encoded recovery shares",
        ),
        ({"root_token": "root", "recovery_keys_base64": ["one", "two", ""]}, "three"),
    ],
)
def test_rejects_invalid_initialization_payloads(
    tmp_path: Path, payload: dict[str, JsonValue], message: str
) -> None:
    api = client(tmp_path, QueueSession(response(body=payload)))

    with pytest.raises(OpenBaoError, match=message):
        if "initialized" in payload:
            api.initialized()
        else:
            api.initialize(("one", "two", "three"))


def test_token_and_recovery_operations(tmp_path: Path) -> None:
    temporary_root = "temporary-root"
    otp = "otp-for-root!!"
    session = QueueSession(
        response(body={"auth": {"client_token": "operator"}}),
        response(body={"started": True, "nonce": "nonce", "otp": otp, "required": 2}),
        response(body={"complete": False}),
        response(body={"complete": True, "encoded_token": encoded_token(temporary_root, otp)}),
        response(body={"data": {"keys": ["one", "two"]}}),
        response(body={"data": {"policies": ["root"]}}),
        response(body={"data": {"accessor": "current"}}),
        response(204),
        response(204),
    )
    api = client(tmp_path, session)

    assert api.kubernetes_login("role", "jwt") == "operator"
    assert api.create_recovery_root_token(("recovery-one", "recovery-two")) == temporary_root
    assert [call[1] for call in session.calls[1:4]] == [
        "https://bao.test/v1/sys/generate-root/attempt",
        "https://bao.test/v1/sys/generate-root/update",
        "https://bao.test/v1/sys/generate-root/update",
    ]
    assert api.list_token_accessors() == ("one", "two")
    assert api.lookup_accessor("one") == {"policies": ["root"]}
    assert api.self_accessor() == "current"
    api.revoke_accessor("one")
    api.revoke_self()


@pytest.mark.parametrize(
    ("responses", "operation", "message"),
    [
        ([response(body={"auth": {"client_token": ""}})], "login", "did not return a token"),
        (
            [response(body={"started": True, "nonce": "", "otp": "otp", "required": 2})],
            "recovery",
            "active or invalid",
        ),
        (
            [
                response(body={"started": True, "nonce": "nonce", "otp": "otp", "required": 2}),
                response(body={"complete": False}),
                response(body={"complete": False}),
                response(204),
            ],
            "recovery",
            "temporary root token",
        ),
        ([response(body={"data": {"keys": [1]}})], "accessors", "invalid token accessors"),
        ([response(body={"data": {"accessor": ""}})], "self", "current token accessor"),
    ],
)
def test_rejects_invalid_auth_payloads(
    tmp_path: Path,
    responses: list[requests.Response],
    operation: str,
    message: str,
) -> None:
    api = client(tmp_path, QueueSession(*responses))

    with pytest.raises(OpenBaoError, match=message):
        if operation == "login":
            api.kubernetes_login("role", "jwt")
        elif operation == "recovery":
            api.create_recovery_root_token(("key-one", "key-two"))
        elif operation == "accessors":
            api.list_token_accessors()
        else:
            api.self_accessor()


@pytest.mark.parametrize("encoded", ["%%%", base64.b64encode(b"short").decode()])
def test_rejects_invalid_encoded_recovery_root_token(tmp_path: Path, encoded: str) -> None:
    session = QueueSession(
        response(body={"started": True, "nonce": "nonce", "otp": "longer", "required": 2}),
        response(body={"complete": False}),
        response(body={"complete": True, "encoded_token": encoded}),
        response(204),
    )

    with pytest.raises(OpenBaoError, match="invalid encoded temporary root token"):
        client(tmp_path, session).create_recovery_root_token(("key-one", "key-two"))

    assert session.calls[-1][0:2] == (
        "DELETE",
        "https://bao.test/v1/sys/generate-root/attempt",
    )


def test_mount_and_auth_reconciliation(tmp_path: Path) -> None:
    session = QueueSession(
        response(body={"data": {}}),
        response(204),
        response(body={"data": {"secret/": {"type": "kv", "options": {"version": "2"}}}}),
        response(body={"data": {}}),
        response(204),
        response(body={"data": {"kubernetes/": {"type": "kubernetes"}}}),
        response(204),
        response(204),
        response(204),
    )
    api = client(tmp_path, session)

    assert api.ensure_kv_v2_mount() is True
    assert api.ensure_kv_v2_mount() is False
    assert api.ensure_kubernetes_auth() is True
    assert api.ensure_kubernetes_auth() is False
    api.configure_kubernetes_auth()
    api.write_policy("name", "policy")
    api.write_kubernetes_role("namespace")


@pytest.mark.parametrize(
    ("payload", "method", "message"),
    [
        ({"data": {"secret/": {"type": "kv", "options": {"version": "1"}}}}, "mount", "not KV"),
        ({"data": {"kubernetes/": {"type": "other"}}}, "auth", "unexpected type"),
    ],
)
def test_rejects_incompatible_mounts(
    tmp_path: Path, payload: dict[str, JsonValue], method: str, message: str
) -> None:
    api = client(tmp_path, QueueSession(response(body=payload)))

    with pytest.raises(OpenBaoError, match=message):
        if method == "mount":
            api.ensure_kv_v2_mount()
        else:
            api.ensure_kubernetes_auth()


def test_secret_read_write_and_not_found(tmp_path: Path) -> None:
    session = QueueSession(
        response(body={"data": {"data": {"key": "value"}, "metadata": {"version": 3}}}),
        response(404, body={"errors": ["missing"]}),
        response(204),
    )
    api = client(tmp_path, session)

    record = api.read_secret("path")
    assert record is not None
    assert record.values == {"key": "value"}
    assert record.version == 3
    assert api.read_secret("missing") is None
    api.write_secret("path", {"key": "new"}, 3)


def test_request_failures_are_redacted(tmp_path: Path) -> None:
    sessions = [
        QueueSession(requests.ConnectionError("secret detail")),
        QueueSession(response(500, body={"secret": "detail"})),
        QueueSession(response(raw=b"not json")),
        QueueSession(response(body=[])),
    ]

    for session in sessions:
        with pytest.raises(OpenBaoError) as captured:
            client(tmp_path, session).initialized()
        assert "secret detail" not in str(captured.value)


def test_initialization_transport_failure_reports_unknown_outcome(tmp_path: Path) -> None:
    api = client(tmp_path, QueueSession(requests.ReadTimeout("private detail")))
    with pytest.raises(OpenBaoError, match="outcome is unknown") as captured:
        api.initialize(("one", "two", "three"))
    assert "private detail" not in str(captured.value)


def test_rejects_invalid_secret_version(tmp_path: Path) -> None:
    api = client(
        tmp_path,
        QueueSession(response(body={"data": {"data": {}, "metadata": {"version": "1"}}})),
    )

    with pytest.raises(OpenBaoError, match="secret version"):
        api.read_secret("path")
