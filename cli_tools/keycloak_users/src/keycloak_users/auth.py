"""Browser-only OIDC code flow with strict endpoint and callback validation."""

import base64
import hashlib
import secrets
import ssl
import time
import webbrowser
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, override
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

from keycloak_users.profile import Profile, SafeError

Json = dict[str, Any]


def trusted_endpoint(profile: Profile, value: object) -> str:
    """Accept only HTTPS endpoints on the configured issuer origin."""
    if not isinstance(value, str):
        raise SafeError("Missing OIDC endpoint.")
    endpoint, issuer = urlsplit(value), urlsplit(profile.issuer)
    if (
        endpoint.scheme != "https"
        or endpoint.netloc != issuer.netloc
        or endpoint.username is not None
        or endpoint.fragment
        or endpoint.query
        or "\\" in value
        or not value.isprintable()
        or any(c.isspace() for c in value)
    ):
        raise SafeError("Untrusted OIDC endpoint; check the configured issuer.")
    return value


def json_response(response: httpx.Response, operation: str = "HTTPS request") -> Json:
    """Bound upstream errors and refuse redirects without revealing response content."""
    if not 200 <= response.status_code < 300:
        raise SafeError(f"{operation} rejected (HTTP {response.status_code}); no automatic retry.")
    try:
        value = response.json()
    except ValueError:
        raise SafeError(f"{operation}: invalid JSON response from identity server.") from None
    if not isinstance(value, dict):
        raise SafeError(f"{operation}: unexpected identity server response shape.")
    return value


def callback_code(path: str, host: str | None, profile: Profile, state: str) -> str:
    """Validate the exact callback, singleton query values and CSRF state."""
    parsed = urlsplit(path)
    if parsed.path != "/callback" or parsed.scheme or parsed.netloc or parsed.fragment:
        raise SafeError("Unexpected browser callback path.")
    if host != f"127.0.0.1:{profile.port}":
        raise SafeError("Unexpected browser callback host.")
    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=12)
    if any(len(values) != 1 for values in query.values()):
        raise SafeError("Ambiguous browser callback.")
    if not secrets.compare_digest(query.get("state", [""])[0].encode(), state.encode()):
        raise SafeError("Browser callback state mismatch.")
    if "iss" in query and query["iss"] != [profile.issuer]:
        raise SafeError("Browser callback issuer mismatch.")
    if "error" in query:
        raise SafeError("Browser authorization was denied or cancelled.")
    code = query.get("code", [""])[0]
    if not code or len(code) > 8192:
        raise SafeError("Browser callback did not contain an authorization code.")
    return code


def wait_for_code(profile: Profile, authorization_url: str, state: str, timeout: int = 180) -> str:
    """Bind before opening the browser, silence HTTP logs, and always release the port."""
    result: list[str | SafeError] = []

    class Callback(BaseHTTPRequestHandler):
        @override
        def setup(self) -> None:
            self.request.settimeout(1)
            super().setup()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            try:
                code = callback_code(self.path, self.headers.get("Host"), profile, state)
                result.append(code)
            except (SafeError, ValueError) as exc:
                error = (
                    exc if isinstance(exc, SafeError) else SafeError("Invalid browser callback.")
                )
                result.append(error)
            with suppress(OSError):
                self.send_response(200 if isinstance(result[-1], str) else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(b"Return to the terminal. You may close this tab.")

    try:
        with HTTPServer(("127.0.0.1", profile.port), Callback) as server:
            server.timeout = 0.5
            if not webbrowser.open(authorization_url):
                raise SafeError("Unable to open a browser; run from a browser-capable workstation.")
            deadline = time.monotonic() + timeout
            while not result and time.monotonic() < deadline:
                server.handle_request()
    except OSError:
        raise SafeError("Loopback callback unavailable; check the configured port.") from None
    if not result:
        raise SafeError("Browser login timed out; no session was saved.")
    if isinstance(result[0], SafeError):
        raise result[0]
    return result[0]


class BrowserSession:
    """Hold verified browser credentials only in memory; never replay a failed request."""

    def __init__(self, profile: Profile) -> None:
        """Create a verified transport without proxy, netrc or environment overrides."""
        self.profile = profile
        self.http = httpx.Client(
            verify=ssl.create_default_context(cafile=profile.ca_path),
            follow_redirects=False,
            trust_env=False,
            timeout=20,
        )
        self.tokens: Json = {}
        self.discovery: Json = {}
        self.expires_at = 0.0
        self._subject: str | None = None
        self._roles: frozenset[str] = frozenset()
        self._verified_token: str | None = None
        self._token_expiry = 0.0

    def _clear_tokens(self) -> None:
        self.tokens = {}
        self._roles = frozenset()
        self._verified_token = None
        self.expires_at = self._token_expiry = 0.0

    def close(self) -> None:
        """Release transport and discard local token references, without server mutation."""
        self._clear_tokens()
        self._subject = None
        self.http.close()

    def _request(self, operation: str, endpoint: object, data: Json | None = None) -> Json:
        url = trusted_endpoint(self.profile, endpoint)
        try:
            response = self.http.get(url) if data is None else self.http.post(url, data=data)
        except httpx.HTTPError:
            raise SafeError(
                f"{operation} failed; check connectivity and TLS trust. No automatic retry."
            ) from None
        return json_response(response, operation)

    def login(self) -> None:
        """Discover trusted endpoints and validate a nonce-bound, signed ID token."""
        self._clear_tokens()
        self._subject = None
        self.discovery = self._request(
            "OIDC discovery", f"{self.profile.issuer}/.well-known/openid-configuration"
        )
        if self.discovery.get("issuer") != self.profile.issuer:
            raise SafeError("Discovery issuer does not match the selected realm.")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            trusted_endpoint(self.profile, self.discovery.get(name))
        verifier, state, nonce = (secrets.token_urlsafe(48) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        query = urlencode(
            {
                "client_id": self.profile.client_id,
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": self.profile.callback,
                "code_challenge": challenge.rstrip(b"=").decode(),
                "code_challenge_method": "S256",
                "state": state,
                "nonce": nonce,
            }
        )
        code = wait_for_code(
            self.profile, f"{self.discovery['authorization_endpoint']}?{query}", state
        )
        tokens = self.exchange(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self.profile.callback,
            }
        )
        self.verify_identity(tokens.get("id_token"), nonce)
        self.accept_tokens(tokens)

    def exchange(self, data: dict[str, str]) -> Json:
        """Exchange a code or refresh token once at the prevalidated token endpoint."""
        return self._request(
            "Token refresh"
            if data.get("grant_type") == "refresh_token"
            else "Login token exchange",
            self.discovery.get("token_endpoint"),
            {"client_id": self.profile.client_id, **data},
        )

    def _browser_logout_guidance(self) -> str:
        return (
            "\nIf the browser reused the wrong account, open this logout link in the same "
            "browser/profile and confirm sign out, then rerun the CLI and sign in as the "
            "intended operator. Signing out does not grant missing permissions.\n"
            f"Browser logout: {self.profile.issuer}/protocol/openid-connect/logout"
        )

    def _verify_signed(self, token: object, audience: str, kind: str) -> Json:
        if not isinstance(token, str) or not token:
            raise SafeError(f"Missing signed {kind} token.")
        try:
            header = jwt.get_unverified_header(token)
            keys = jwt.PyJWKSet.from_dict(
                self._request("Signing key retrieval", self.discovery.get("jwks_uri"))
            )
            key = keys[header["kid"]]
            if key.public_key_use not in (None, "sig") or key.algorithm_name != "RS256":
                raise SafeError(f"{kind} token key is not an allowed signing key.")
            return jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=audience,
                issuer=self.profile.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (jwt.InvalidAudienceError, jwt.MissingRequiredClaimError) as exc:
            if kind == "Access" and (
                isinstance(exc, jwt.InvalidAudienceError) or exc.claim == "aud"
            ):
                raise SafeError(
                    "Access token needs the realm-management audience. Check the operator's "
                    "actual realm-management roles, the roles default client scope and audience "
                    "mapper, and Full Scope Allowed (or restricted scope mappings). Scope "
                    "settings expose existing roles; they do not grant permissions."
                    + self._browser_logout_guidance()
                ) from None
            raise SafeError(f"{kind} token required claims could not be verified.") from None
        except (jwt.PyJWTError, KeyError, ValueError, TypeError, AttributeError, OverflowError):
            raise SafeError(
                f"{kind} token signature or required claims could not be verified."
            ) from None

    def verify_identity(self, token: object, nonce: str) -> None:
        """Verify the RS256 signature, issuer, audience, expiry, subject and nonce."""
        self._clear_tokens()
        self._subject = None
        claims = self._verify_signed(token, self.profile.client_id, "ID")
        try:
            if (
                not isinstance(claims["sub"], str)
                or not claims["sub"]
                or claims["nonce"] != nonce
                or claims.get("azp", self.profile.client_id) != self.profile.client_id
                or (
                    isinstance(claims["aud"], list)
                    and len(claims["aud"]) > 1
                    and claims.get("azp") != self.profile.client_id
                )
            ):
                raise SafeError("ID token identity binding failed.")
        except (jwt.PyJWTError, KeyError, ValueError, TypeError, AttributeError):
            raise SafeError(
                "ID token signature or required claims could not be verified."
            ) from None
        self._subject = claims["sub"]

    def accept_tokens(self, tokens: Json) -> None:
        """Require usable short-lived credentials without ever persisting them."""
        self._clear_tokens()
        if (
            not isinstance(tokens.get("access_token"), str)
            or not tokens["access_token"]
            or type(tokens.get("expires_in")) is not int
            or tokens["expires_in"] <= 0
            or not isinstance(tokens.get("token_type"), str)
            or tokens["token_type"].lower() != "bearer"
        ):
            raise SafeError("Invalid access token response.")
        claims = self._verify_signed(tokens["access_token"], "realm-management", "Access")
        if (
            self._subject is None
            or claims["sub"] != self._subject
            or claims.get("azp") != self.profile.client_id
            or type(claims["exp"]) is not int
            or type(claims["iat"]) is not int
        ):
            raise SafeError("Access token identity or client binding failed.")
        resources = claims.get("resource_access", {})
        if not isinstance(resources, dict):
            raise SafeError("Malformed access token management roles.")
        management = resources.get("realm-management", {})
        if not isinstance(management, dict):
            raise SafeError("Malformed access token management roles.")
        roles = management.get("roles", [])
        if not isinstance(roles, list) or any(
            not isinstance(role, str) or not role or not role.isprintable() or role.strip() != role
            for role in roles
        ):
            raise SafeError("Malformed access token management roles.")
        lifetime = min(tokens["expires_in"], claims["exp"] - time.time())
        if lifetime <= 0:
            raise SafeError("Access token expired during verification; authenticate again.")
        self._roles = frozenset(roles)
        self.tokens = dict(tokens)
        self._verified_token = tokens["access_token"]
        self._token_expiry = claims["exp"]
        self.expires_at = time.monotonic() + lifetime * 0.9

    def management_roles(self) -> frozenset[str]:
        """Return verified management roles, refreshing before either expiry bound."""
        if time.monotonic() >= self.expires_at or time.time() >= self._token_expiry:
            refresh = self.tokens.get("refresh_token")
            self._clear_tokens()
            if not isinstance(refresh, str) or not refresh:
                raise SafeError("Session expired; authenticate again.")
            self.accept_tokens(
                self.exchange({"grant_type": "refresh_token", "refresh_token": refresh})
            )
        token = self.tokens.get("access_token")
        if not isinstance(token, str) or token != self._verified_token:
            self._clear_tokens()
            raise SafeError("Session has no access token; authenticate again.")
        return self._roles

    def require_management(self) -> None:
        """Block unsupported authority before prompts or admin requests, without exposing tokens."""
        roles = self.management_roles()
        if "realm-admin" in roles:
            return
        missing = []
        if "manage-users" not in roles:
            missing.append("realm-management -> manage-users")
        if not {"view-realm", "manage-realm"}.intersection(roles):
            missing.append("realm-management -> view-realm (or manage-realm)")
        if missing:
            raise SafeError(
                "Permission preflight: Blocked\n"
                "Missing required realm-management roles in the verified access token:\n  "
                + "\n  ".join(missing)
                + "\nCheck: Approved operator grants, Full Scope Allowed (or restricted scope "
                "mappings), and the default roles client scope. Log in again after changes.\n"
                "Diagnosis: Token authority is missing; the token alone cannot distinguish an "
                "unassigned role from scope exclusion.\n"
                "Supported authorization: Standard realm-management roles; fine-grained-only "
                "policies are not sufficient for this CLI's preflight.\n"
                "Result: No admin request attempted by this check."
                + self._browser_logout_guidance()
            )

    def access_token(self) -> str:
        """Recheck permissions before each admin request, without replay on failure."""
        self.require_management()
        token = self._verified_token
        if token is None:
            raise SafeError("Session has no verified access token; authenticate again.")
        return token
