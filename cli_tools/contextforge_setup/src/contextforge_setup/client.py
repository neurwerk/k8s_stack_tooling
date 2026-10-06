"""Small native REST client with opaque errors and no credential forwarding."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import cast
from urllib.parse import quote

import requests

from contextforge_setup.config import Json, Object, SetupError, object_value


class Client:
    """Contact only the validated origin; never log payloads or follow redirects."""

    def __init__(self, origin: str, ca_cert: Path | None = None) -> None:
        """Use verified TLS and ignore environment proxies and netrc credentials."""
        self.origin = origin
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = str(ca_cert) if ca_cert else True

    def close(self) -> None:
        """Drop in-memory admin credentials and close connections."""
        self.session.headers.pop("Authorization", None)
        self.session.headers.pop("x-contextforge-account-email", None)
        self.session.close()

    def authenticate(self, token: str) -> None:
        """Keep the native admin token only in the private HTTP session."""
        if (
            not token
            or len(token) > 16384
            or not token.isascii()
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token)
        ):
            raise SetupError("Invalid native token format")
        self.session.headers["Authorization"] = f"Bearer {token}"

    def login(self, email: str, password: str) -> None:
        """Use native email login; discard the response except its session token."""
        result = object_value(
            self.request("POST", "/auth/email/login", {"email": email, "password": password})
        )
        token = result.get("access_token")
        if not isinstance(token, str):
            raise SetupError("Native login returned no valid token")
        self.authenticate(token)

    def require_registration_session(self) -> None:
        """Reject limited API tokens; the API still verifies this session and DB admin status."""
        token = self.session.headers.get("Authorization", "").removeprefix("Bearer ")
        parts = token.split(".")
        if len(parts) != 3:
            raise SetupError("Registrations require a native email-login session token")
        try:
            claims = object_value(
                json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            )
        except (ValueError, binascii.Error, SetupError):
            raise SetupError("Registrations require a native email-login session token") from None
        if claims.get("token_use") != "session":
            raise SetupError(
                "Registrations require a native email-login session, not a scoped API token"
            )

    def authenticate_operator_proxy(self, owner_email: str) -> None:
        """Use only the approved fixed identity on an operator-controlled private connection."""
        self.session.headers.pop("Authorization", None)
        self.session.headers["x-contextforge-account-email"] = owner_email

    def registration_operator(self, owner_email: str, authentication: str) -> Object:
        """Verify DB admin status and, for proxy mode, the server-resolved current principal."""
        if authentication == "native-session":
            self.require_registration_session()
            return object_value(self.request("GET", "/auth/email/me"))
        roles = self.request("GET", "/rbac/my/roles")
        if (
            not isinstance(roles, list)
            or not roles
            or any(object_value(role).get("user_email") != owner_email for role in roles)
        ):
            raise SetupError("Trusted proxy resolved a different or unprepared operator principal")
        permissions = self.request("GET", "/rbac/my/permissions")
        required = {
            "admin.user_management",
            "teams.read",
            "gateways.read",
            "gateways.create",
            "gateways.update",
            "tools.read",
            "servers.read",
            "servers.create",
            "servers.update",
        }
        if not isinstance(permissions, list) or (
            "*" not in permissions and not required <= set(permissions)
        ):
            raise SetupError("Use the registration operator, not Studio's limited service")
        return object_value(
            self.request("GET", f"/auth/email/admin/users/{quote(owner_email, safe='')}")
        )

    def request(
        self,
        method: str,
        path: str,
        payload: Object | None = None,
        *,
        missing: bool = False,
        expected_status: int | None = None,
    ) -> Json:
        """Make one request, without automatic mutation retries or raw errors."""
        try:
            response = self.session.request(
                method, self.origin + path, json=payload, timeout=30, allow_redirects=False
            )
        except requests.RequestException:
            raise SetupError(
                "Native API transport failed; outcome may be unknown. "
                "Keep routes blocked and rerun after repair"
            ) from None
        if missing and method == "GET" and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300 or (
            expected_status is not None and response.status_code != expected_status
        ):
            raise SetupError(
                f"Native API returned unexpected HTTP {response.status_code}; "
                "keep routes blocked and inspect privately before retry"
            )
        try:
            return cast("Json", json.loads(response.content))
        except ValueError:
            raise SetupError(
                "Native API returned invalid JSON; keep routes blocked and retry after repair"
            ) from None
