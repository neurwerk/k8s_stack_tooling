"""Stateful fake requests session for OpenBao seed tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

import requests

from openbao_stack_setup.client import JsonValue


@dataclass(frozen=True)
class Call:
    """Capture one fake HTTP request."""

    method: str
    path: str
    headers: dict[str, str]
    body: JsonValue
    timeout: tuple[float, float]
    verify: str


@dataclass
class StoredSecret:
    """Store one fake KV record and version."""

    values: dict[str, JsonValue]
    version: int = 1


class FakeSession:
    """Implement the requests session subset used by OpenBaoClient."""

    def __init__(self) -> None:
        self.mounts: dict[str, JsonValue] = {}
        self.auth_methods: dict[str, JsonValue] = {}
        self.secrets: dict[str, StoredSecret] = {}
        self.calls: list[Call] = []
        self.failure: tuple[str, str, int, dict[str, JsonValue]] | None = None

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

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
        """Handle one fake OpenBao HTTP request."""
        path = urlsplit(url).path.removeprefix("/v1/")
        self.calls.append(Call(method, path, dict(headers), json, timeout, verify))
        if self.failure and (method, path) == self.failure[:2]:
            return _response(self.failure[2], self.failure[3])
        if method == "GET" and path == "sys/mounts":
            return _response(200, {"data": self.mounts})
        if method == "POST" and path == "sys/mounts/secret":
            self.mounts["secret/"] = {"type": "kv", "options": {"version": "2"}}
            return _response(204)
        if method == "GET" and path == "sys/auth":
            return _response(200, {"data": self.auth_methods})
        if method == "POST" and path == "sys/auth/kubernetes":
            self.auth_methods["kubernetes/"] = {"type": "kubernetes"}
            return _response(204)
        if path.startswith("secret/data/"):
            return self._secret_request(method, path.removeprefix("secret/data/"), json)
        return _response(204)

    def _secret_request(
        self,
        method: str,
        path: str,
        body: JsonValue,
    ) -> requests.Response:
        if method == "GET":
            stored = self.secrets.get(path)
            if stored is None:
                return _response(404, {"errors": ["not found"]})
            return _response(
                200,
                {"data": {"data": stored.values, "metadata": {"version": stored.version}}},
            )
        request_body = cast(dict[str, JsonValue], body)
        options = cast(dict[str, JsonValue], request_body["options"])
        values = cast(dict[str, JsonValue], request_body["data"])
        current = self.secrets.get(path)
        expected_cas = current.version if current else 0
        if options["cas"] != expected_cas:
            return _response(400, {"errors": ["CAS mismatch"]})
        self.secrets[path] = StoredSecret(dict(values), expected_cas + 1)
        return _response(204)


def request_body(call: Call) -> dict[str, JsonValue]:
    """Return a captured request body as a mapping."""
    return cast(dict[str, JsonValue], call.body)


def _response(status: int, body: JsonValue = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    if body is not None:
        response._content = json.dumps(body).encode()
        response.headers["Content-Type"] = "application/json"
    else:
        response._content = b""
    return response
