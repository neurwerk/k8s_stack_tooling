"""Provide a fail-closed, redacting OpenBao HTTP client."""

from __future__ import annotations

import base64
import binascii
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import requests

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

_TIMEOUT: tuple[float, float] = (3.05, 15.0)
_INITIALIZE_TIMEOUT: tuple[float, float] = (3.05, 180.0)


class HttpSession(Protocol):
    """Describe the requests session operation used by the client."""

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
        """Send one HTTPS request."""
        ...


class OpenBaoError(RuntimeError):
    """Raised for redacted OpenBao transport or response failures."""


@dataclass(frozen=True)
class SecretRecord:
    """Represent one KV v2 value and its current version."""

    values: dict[str, JsonValue]
    version: int


class OpenBaoClient:
    """Call the subset of the OpenBao API needed by the setup operator."""

    def __init__(
        self,
        address: str,
        token: str | None,
        ca_cert: Path,
        session: HttpSession,
    ) -> None:
        """Initialize an authenticated TLS-verifying client."""
        self._address = address
        self._headers = {"X-Vault-Token": token} if token else {}
        self._ca_cert = str(ca_cert)
        self._session = session

    @property
    def ca_cert(self) -> Path:
        """Return the configured TLS trust anchor."""
        return Path(self._ca_cert)

    @property
    def session(self) -> HttpSession:
        """Return the HTTP session used for this request scope."""
        return self._session

    def ensure_kv_v2_mount(self) -> bool:
        """Enable the secret/ KV v2 mount when absent."""
        mounts = self._system_entries("sys/mounts")
        existing = mounts.get("secret/")
        if existing is None:
            self._write("sys/mounts/secret", {"type": "kv", "options": {"version": "2"}})
            return True
        config = _mapping(existing, "secret mount")
        options = _mapping(config.get("options"), "secret mount options")
        if config.get("type") != "kv" or options.get("version") != "2":
            raise OpenBaoError("OpenBao secret/ mount is not KV version 2")
        return False

    def initialized(self) -> bool:
        """Return the initialization state reported by OpenBao."""
        payload = _mapping(self._request("GET", "sys/init"), "initialization status")
        initialized = payload.get("initialized")
        if type(initialized) is not bool:
            raise OpenBaoError("OpenBao returned an invalid initialization status")
        return initialized

    def initialize(
        self, recovery_pgp_keys: tuple[str, str, str]
    ) -> tuple[str, tuple[str, str, str]]:
        """Initialize static-seal OpenBao with three encrypted, threshold-two recovery shares."""
        if len(set(recovery_pgp_keys)) != 3 or any(not key for key in recovery_pgp_keys):
            raise OpenBaoError("Three distinct recovery PGP keys are required")
        payload = _mapping(
            self._request(
                "POST",
                "sys/init",
                body={
                    "recovery_shares": 3,
                    "recovery_threshold": 2,
                    "recovery_pgp_keys": list(recovery_pgp_keys),
                },
                timeout=_INITIALIZE_TIMEOUT,
            ),
            "initialization response",
        )
        root_token = payload.get("root_token")
        recovery_keys = payload.get("recovery_keys_base64")
        if not isinstance(root_token, str) or not root_token:
            raise OpenBaoError("OpenBao initialization did not return a root token")
        if (
            not isinstance(recovery_keys, list)
            or len(recovery_keys) != 3
            or any(not isinstance(key, str) or not key for key in recovery_keys)
        ):
            raise OpenBaoError(
                "OpenBao initialization did not return three base64-encoded recovery shares"
            )
        return root_token, (
            cast(str, recovery_keys[0]),
            cast(str, recovery_keys[1]),
            cast(str, recovery_keys[2]),
        )

    def kubernetes_login(self, role: str, jwt: str) -> str:
        """Exchange a short-lived Kubernetes token for an OpenBao token."""
        payload = _mapping(
            self._request("POST", "auth/kubernetes/login", {"role": role, "jwt": jwt}),
            "Kubernetes login response",
        )
        auth = _mapping(payload.get("auth"), "Kubernetes login auth")
        token = auth.get("client_token")
        if not isinstance(token, str) or not token:
            raise OpenBaoError("OpenBao Kubernetes login did not return a token")
        return token

    def create_recovery_root_token(self, recovery_keys: tuple[str, str]) -> str:
        """Generate a temporary root token after two distinct recovery-share contributions."""
        if recovery_keys[0] == recovery_keys[1] or any(not key for key in recovery_keys):
            raise OpenBaoError("Two distinct recovery shares are required")
        nonce, otp = self._start_recovery_root_attempt()
        try:
            encoded_token = self._contribute_recovery_shares(nonce, recovery_keys)
            token = self._decode_root_token(encoded_token, otp)
        except OpenBaoError:
            with suppress(OpenBaoError):
                self._request("DELETE", "sys/generate-root/attempt")
            raise
        else:
            return token

    def _start_recovery_root_attempt(self) -> tuple[str, str]:
        attempt = _mapping(
            self._request("POST", "sys/generate-root/attempt", {}),
            "root token generation attempt",
        )
        nonce = attempt.get("nonce")
        otp = attempt.get("otp")
        if (
            attempt.get("started") is not True
            or not isinstance(nonce, str)
            or not nonce
            or not isinstance(otp, str)
            or not otp
            or attempt.get("required") != 2
        ):
            raise OpenBaoError("OpenBao has an active or invalid root token generation attempt")
        return nonce, otp

    def _contribute_recovery_shares(self, nonce: str, recovery_keys: tuple[str, str]) -> str:
        first = _mapping(
            self._request(
                "POST", "sys/generate-root/update", {"key": recovery_keys[0], "nonce": nonce}
            ),
            "root token generation response",
        )
        if first.get("complete") is not False:
            raise OpenBaoError("OpenBao did not require a second recovery share")
        second = _mapping(
            self._request(
                "POST", "sys/generate-root/update", {"key": recovery_keys[1], "nonce": nonce}
            ),
            "root token generation response",
        )
        encoded_token = second.get("encoded_token")
        if (
            second.get("complete") is not True
            or not isinstance(encoded_token, str)
            or not encoded_token
        ):
            raise OpenBaoError("OpenBao did not return a temporary root token")
        return encoded_token

    def _decode_root_token(self, encoded_token: str, otp: str) -> str:
        try:
            encoded = base64.b64decode(
                encoded_token + "=" * (-len(encoded_token) % 4), validate=True
            )
        except (binascii.Error, ValueError):
            raise OpenBaoError("OpenBao returned an invalid encoded temporary root token") from None
        otp_bytes = otp.encode()
        if len(encoded) != len(otp_bytes):
            raise OpenBaoError("OpenBao returned an invalid encoded temporary root token")
        try:
            token = bytes(
                left ^ right for left, right in zip(encoded, otp_bytes, strict=True)
            ).decode()
        except UnicodeDecodeError:
            raise OpenBaoError("OpenBao returned an invalid encoded temporary root token") from None
        if not token:
            raise OpenBaoError("OpenBao did not decode a temporary root token")
        return token

    def list_token_accessors(self) -> tuple[str, ...]:
        """List token accessors without exposing token identifiers."""
        payload = _mapping(self._request("LIST", "auth/token/accessors"), "token accessors")
        values = _mapping(payload.get("data"), "token accessors data").get("keys")
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise OpenBaoError("OpenBao returned invalid token accessors")
        return tuple(cast(str, value) for value in values)

    def lookup_accessor(self, accessor: str) -> dict[str, JsonValue]:
        """Return non-sensitive metadata for one token accessor."""
        payload = _mapping(
            self._request("POST", "auth/token/lookup-accessor", {"accessor": accessor}),
            "token accessor lookup",
        )
        return _mapping(payload.get("data"), "token accessor metadata")

    def self_accessor(self) -> str:
        """Return the accessor for this client token without returning the token."""
        payload = _mapping(self._request("GET", "auth/token/lookup-self"), "token lookup")
        accessor = _mapping(payload.get("data"), "token lookup data").get("accessor")
        if not isinstance(accessor, str) or not accessor:
            raise OpenBaoError("OpenBao did not return the current token accessor")
        return accessor

    def revoke_accessor(self, accessor: str) -> None:
        """Revoke a token using its non-secret accessor."""
        self._write("auth/token/revoke-accessor", {"accessor": accessor})

    def revoke_self(self) -> None:
        """Revoke the token used by this client."""
        self._write("auth/token/revoke-self", {})

    def ensure_kubernetes_auth(self) -> bool:
        """Enable the kubernetes/ auth method when absent."""
        methods = self._system_entries("sys/auth")
        existing = methods.get("kubernetes/")
        if existing is None:
            self._write("sys/auth/kubernetes", {"type": "kubernetes"})
            return True
        if _mapping(existing, "kubernetes auth method").get("type") != "kubernetes":
            raise OpenBaoError("OpenBao kubernetes/ auth path has an unexpected type")
        return False

    def configure_kubernetes_auth(self) -> None:
        """Configure Kubernetes auth to use pod-local reviewer credentials and CA."""
        self._write(
            "auth/kubernetes/config",
            {
                "kubernetes_host": "https://kubernetes.default.svc:443",
                "disable_local_ca_jwt": False,
            },
        )

    def write_policy(self, name: str, policy: str) -> None:
        """Create or replace an ACL policy."""
        self._write(f"sys/policies/acl/{name}", {"policy": policy})

    def write_kubernetes_role(self, namespace: str) -> None:
        """Create or replace a namespace-scoped External Secrets role."""
        self._write(
            f"auth/kubernetes/role/{namespace}",
            {
                "bound_service_account_names": [f"{namespace}-external-secrets"],
                "bound_service_account_namespaces": [namespace],
                "audience": "openbao",
                "token_policies": [namespace],
                "token_ttl": "10m",
                "token_max_ttl": "30m",
            },
        )

    def read_secret(self, path: str) -> SecretRecord | None:
        """Read a KV v2 record, returning None when it does not exist."""
        payload = self._request("GET", f"secret/data/{path}", allow_not_found=True)
        if payload is None:
            return None
        outer = _mapping(payload, "KV response")
        data = _mapping(outer.get("data"), "KV response data")
        values = _mapping(data.get("data"), "KV secret data")
        metadata = _mapping(data.get("metadata"), "KV secret metadata")
        version = metadata.get("version")
        if type(version) is not int:
            raise OpenBaoError("OpenBao returned an invalid KV secret version")
        return SecretRecord(values=values, version=version)

    def write_secret(self, path: str, values: dict[str, JsonValue], cas: int) -> None:
        """Conditionally write a complete KV v2 record."""
        self._write(f"secret/data/{path}", {"options": {"cas": cas}, "data": values})

    def _system_entries(self, path: str) -> dict[str, JsonValue]:
        payload = self._request("GET", path)
        values = _mapping(payload, f"{path} response")
        wrapped = values.get("data")
        return _mapping(wrapped, f"{path} response data") if isinstance(wrapped, dict) else values

    def _write(self, path: str, body: dict[str, JsonValue]) -> None:
        self._request("POST", path, body=body)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, JsonValue] | None = None,
        *,
        allow_not_found: bool = False,
        timeout: tuple[float, float] = _TIMEOUT,
    ) -> JsonValue:
        try:
            response = self._session.request(
                method,
                f"{self._address}/v1/{path}",
                headers=self._headers,
                json=body,
                timeout=timeout,
                verify=self._ca_cert,
            )
        except requests.exceptions.RequestException:
            if method == "POST" and path == "sys/init":
                raise OpenBaoError(
                    "OpenBao initialization connection failed; outcome is unknown. "
                    "Do not retry until initialization state and the local checkpoint are checked"
                ) from None
            raise OpenBaoError(f"OpenBao request {method} /v1/{path} failed") from None
        if response.status_code == 404 and allow_not_found:
            return None
        if response.status_code not in (200, 204):
            raise OpenBaoError(
                f"OpenBao request {method} /v1/{path} failed with HTTP {response.status_code}"
            )
        if not response.content:
            return None
        try:
            payload: object = response.json()
        except ValueError:
            raise OpenBaoError(
                f"OpenBao request {method} /v1/{path} returned invalid JSON"
            ) from None
        return _json_value(payload, f"{method} /v1/{path} response")


def _mapping(value: JsonValue | object, location: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise OpenBaoError(f"OpenBao returned an invalid {location}")
    return {cast(str, key): _json_value(item, location) for key, item in value.items()}


def _json_value(value: object, location: str) -> JsonValue:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, list):
        return [_json_value(item, location) for item in value]
    if isinstance(value, dict):
        return _mapping(value, location)
    raise OpenBaoError(f"OpenBao returned an invalid {location}")
