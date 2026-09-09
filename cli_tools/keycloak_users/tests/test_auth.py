import base64
import hashlib
import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from keycloak_users import auth
from keycloak_users.auth import (
    BrowserSession,
    callback_code,
    json_response,
    trusted_endpoint,
    wait_for_code,
)
from keycloak_users.profile import Profile, SafeError


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed_identity(profile, key, **changes):
    claims = {
        "iss": profile.issuer,
        "aud": profile.client_id,
        "sub": "operator",
        "nonce": "expected",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "key-1"})


def jwks(key):
    serialized = jwt.get_algorithm_by_name("RS256").to_jwk(key.public_key())
    assert isinstance(serialized, str)
    return {
        "keys": [
            {
                **json.loads(serialized),
                "kid": "key-1",
                "alg": "RS256",
                "use": "sig",
            }
        ]
    }


@pytest.mark.parametrize("usable_access", [True, False])
def test_full_pkce_login_and_refresh_are_memory_only(
    monkeypatch, realm, signing_key, usable_access
):
    realm.signing_key = signing_key
    first = realm.signed_token(jti="first")
    second = realm.signed_token(jti="second")
    requests = []
    authorization = {}
    nonce = []

    def browser(profile, url, state):
        authorization.update(parse_qs(urlsplit(url).query))
        assert authorization["state"] == [state]
        assert authorization["redirect_uri"] == [profile.callback]
        assert authorization["code_challenge_method"] == ["S256"]
        assert authorization["scope"] == ["openid"]
        nonce.append(authorization["nonce"][0])
        return "one-time-code"

    def transport(request):
        requests.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": realm.profile.issuer,
                    "authorization_endpoint": realm.profile.issuer
                    + "/protocol/openid-connect/auth",
                    "token_endpoint": realm.profile.issuer + "/protocol/openid-connect/token",
                    "jwks_uri": realm.profile.issuer + "/protocol/openid-connect/certs",
                },
            )
        if request.url.path.endswith("certs"):
            return httpx.Response(200, json=jwks(signing_key))
        data = parse_qs(request.content.decode())
        if data["grant_type"] == ["authorization_code"]:
            digest = hashlib.sha256(data["code_verifier"][0].encode()).digest()
            assert authorization["code_challenge"] == [
                base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            ]
            assert data["redirect_uri"] == [realm.profile.callback]
            assert "client_secret" not in data
            return httpx.Response(
                200,
                json={
                    "id_token": signed_identity(realm.profile, signing_key, nonce=nonce[0]),
                    "access_token": first if usable_access else "incomplete-token",
                    "refresh_token": "refresh",
                    "expires_in": 300,
                    "token_type": "Bearer",
                },
            )
        assert data == {
            "grant_type": ["refresh_token"],
            "refresh_token": ["refresh"],
            "client_id": ["keycloak-users"],
        }
        return httpx.Response(
            200,
            json={
                "access_token": second,
                "refresh_token": "rotated",
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    monkeypatch.setattr(auth, "wait_for_code", browser)
    realm.session.http.close()
    realm.session.http = httpx.Client(transport=httpx.MockTransport(transport))
    if not usable_access:
        with pytest.raises(SafeError, match="Access token"):
            realm.session.login()
        assert realm.session.tokens == {}
        with pytest.raises(SafeError):
            realm.session.management_roles()
        return
    realm.session.login()
    assert realm.session.management_roles() == frozenset({"manage-users", "view-realm"})
    assert realm.session.access_token() == first
    realm.session.expires_at = 0
    assert realm.session.access_token() == second
    assert len([request for request in requests if request.method == "POST"]) == 2
    realm.session.close()
    assert realm.session.tokens == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"nonce": "wrong"},
        {"iss": "https://evil.example"},
        {"aud": "another-client"},
        {"exp": 1},
        {"sub": ""},
        {"azp": "other"},
        {"aud": ["keycloak-users", "other"]},
    ],
)
def test_id_token_claim_validation(realm, signing_key, changes):
    realm.session.discovery = {"jwks_uri": "https://identity.example/keys"}
    realm.session.http.close()
    realm.session.http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(signing_key)))
    )
    with pytest.raises(SafeError):
        realm.session.verify_identity(
            signed_identity(realm.profile, signing_key, **changes), "expected"
        )


def test_missing_and_bad_signatures(realm, signing_key):
    realm.session.discovery = {"jwks_uri": "https://identity.example/keys"}
    realm.session.http.close()
    realm.session.http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(signing_key)))
    )
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (None, "garbage", signed_identity(realm.profile, other_key)):
        with pytest.raises(SafeError):
            realm.session.verify_identity(token, "expected")


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "http://identity.example/token",
        "https://evil.example/token",
        "https://identity.example:444/token",
        "https://user@identity.example/token",
        "https://identity.example/token#fragment",
        "https://identity.example/token?code=x",
        "https://identity.example/\n",
    ],
)
def test_untrusted_discovery_endpoint(endpoint, realm):
    with pytest.raises(SafeError):
        trusted_endpoint(realm.profile, endpoint)


def test_wrong_discovery_issuer_stops_before_browser(monkeypatch, realm):
    realm.session.http.close()
    realm.session.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"issuer": "https://evil.example"})
        )
    )
    monkeypatch.setattr(auth, "wait_for_code", lambda *_: pytest.fail("browser must not open"))
    with pytest.raises(SafeError, match="issuer"):
        realm.session.login()


@pytest.mark.parametrize(
    "path,host",
    [
        ("/wrong?state=expected&code=c", "127.0.0.1:8765"),
        ("/callback?state=expected&code=c", "localhost:8765"),
        ("/callback?state=wrong&code=c", "127.0.0.1:8765"),
        ("/callback?state=expected&code=c&code=d", "127.0.0.1:8765"),
        ("/callback?state=expected&error=access_denied", "127.0.0.1:8765"),
        ("/callback?state=expected", "127.0.0.1:8765"),
        ("/callback?state=expected&code=c&iss=wrong", "127.0.0.1:8765"),
    ],
)
def test_callback_rejects_bad_binding(path, host, realm):
    with pytest.raises(SafeError):
        callback_code(path, host, realm.profile, "expected")


def test_callback_success_with_issuer(realm):
    query = urlencode({"state": "expected", "code": "code", "iss": realm.profile.issuer})
    assert (
        callback_code(f"/callback?{query}", "127.0.0.1:8765", realm.profile, "expected") == "code"
    )


def loopback_profile():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return Profile("loopback", "https://identity.example", "test", port=port)


@pytest.mark.parametrize(
    "query,success",
    [("state=expected&code=test-code", True), ("state=expected&error=access_denied", False)],
)
def test_real_loopback_binds_before_browser_and_cleans_up(monkeypatch, capsys, query, success):
    profile = loopback_profile()
    threads = []
    responses = []

    def browser(_url):
        def callback():
            responses.append(httpx.get(profile.callback + "?" + query, trust_env=False))

        thread = threading.Thread(target=callback)
        threads.append(thread)
        thread.start()
        return True

    monkeypatch.setattr(auth.webbrowser, "open", browser)
    if success:
        assert wait_for_code(profile, "https://identity.example/auth", "expected") == "test-code"
    else:
        with pytest.raises(SafeError, match="denied"):
            wait_for_code(profile, "https://identity.example/auth", "expected")
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert responses[0].headers["Cache-Control"] == "no-store"
    assert "test-code" not in capsys.readouterr().err
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", profile.port))


def test_browser_failure_timeout_and_cancel_cleanup(monkeypatch):
    profile = loopback_profile()
    monkeypatch.setattr(auth.webbrowser, "open", lambda _: False)
    with pytest.raises(SafeError, match="Unable"):
        wait_for_code(profile, "https://identity.example", "state")
    monkeypatch.setattr(auth.webbrowser, "open", lambda _: True)
    with pytest.raises(SafeError, match="timed out"):
        wait_for_code(profile, "https://identity.example", "state", timeout=0)

    def cancel(_url):
        raise KeyboardInterrupt

    monkeypatch.setattr(auth.webbrowser, "open", cancel)
    with pytest.raises(KeyboardInterrupt):
        wait_for_code(profile, "https://identity.example", "state")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", profile.port))


def test_bound_port_is_safe_failure(monkeypatch):
    profile = loopback_profile()
    monkeypatch.setattr(auth.webbrowser, "open", lambda _: pytest.fail("must bind first"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", profile.port))
        with pytest.raises(SafeError, match="Loopback"):
            wait_for_code(profile, "https://identity.example", "state")


def test_bad_response_and_expired_session(realm):
    for response in (
        httpx.Response(302, text="secret"),
        httpx.Response(200, text="secret"),
        httpx.Response(200, json=[]),
    ):
        with pytest.raises(SafeError) as exc:
            json_response(response)
        assert "secret" not in str(exc.value)
    realm.session.expires_at = 0
    with pytest.raises(SafeError, match="expired"):
        realm.session.access_token()
    for tokens in ({}, {"access_token": "x", "expires_in": True, "token_type": "Bearer"}):
        with pytest.raises(SafeError):
            realm.session.accept_tokens(tokens)


def test_transport_requires_tls_and_disables_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
    session = BrowserSession(Profile("test", "https://identity.example", "test"))
    try:
        assert not session.http.follow_redirects
        assert not session.http.trust_env
    finally:
        session.close()


@pytest.mark.parametrize("keys", [[None], ["SECRET malformed key"], {"SECRET": "invalid"}])
def test_malformed_jwks_is_sanitized(realm, signing_key, keys):
    realm.session.discovery = {"jwks_uri": "https://identity.example/keys"}
    realm.session.http.close()
    realm.session.http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"keys": keys}))
    )
    with pytest.raises(SafeError, match="could not be verified") as exc:
        realm.session.verify_identity(signed_identity(realm.profile, signing_key), "expected")
    assert "SECRET" not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://evil.example"},
        {"aud": "another-client"},
        {"aud": None},
        {"azp": "another-client"},
        {"azp": None},
        {"sub": "another-operator"},
        {"sub": ""},
        {"sub": None},
        {"exp": 1},
        {"exp": None},
        {"exp": "9999999999"},
        {"iat": None},
        {"iat": True},
        {"iat": 9999999999},
        {"resource_access": None},
        {"resource_access": []},
        {"resource_access": {"realm-management": None}},
        {"resource_access": {"realm-management": {"roles": "manage-users"}}},
        {"resource_access": {"realm-management": {"roles": ["view-realm", 1]}}},
        {"resource_access": {"realm-management": {"roles": [""]}}},
        {"resource_access": {"realm-management": {"roles": [" manage-users"]}}},
        {"resource_access": {"realm-management": {"roles": ["manage-users\n"]}}},
    ],
)
def test_access_claims_fail_closed_without_stale_roles(realm, changes):
    with pytest.raises(SafeError):
        realm.session.accept_tokens(
            {
                "access_token": realm.signed_token(**changes),
                "expires_in": 300,
                "token_type": "Bearer",
            }
        )
    assert realm.session.tokens == {}
    with pytest.raises(SafeError):
        realm.session.management_roles()
    assert realm.requests == []


@pytest.mark.parametrize("roles", [[], ["view-realm", "view-users"], ["manage-users"]])
def test_read_only_roles_are_available_to_preflight_but_not_admin(realm, roles):
    realm.session.accept_tokens(
        {
            "access_token": realm.signed_token(
                resource_access={"realm-management": {"roles": roles}}
            ),
            "expires_in": 300,
            "token_type": "Bearer",
        }
    )
    assert realm.session.management_roles() == frozenset(roles)
    with pytest.raises(SafeError, match="required realm-management roles") as exc:
        realm.session.access_token()
    assert "If the browser reused the wrong account" in str(exc.value)
    assert str(exc.value).endswith(
        f"Browser logout: {realm.profile.issuer}/protocol/openid-connect/logout"
    )
    assert realm.session.tokens["access_token"] not in str(exc.value)
    with pytest.raises(SafeError):
        realm.accounts.connection.raw_get("admin/realms/example/users")
    assert realm.requests == []


@pytest.mark.parametrize(
    "roles",
    [
        ["realm-admin"],
        ["manage-users", "view-realm", "manage-users"],
        ["manage-users", "manage-realm"],
    ],
)
def test_sufficient_roles_need_no_redundant_query_roles_or_repeated_jwks(realm, monkeypatch, roles):
    realm.session.accept_tokens(
        {
            "access_token": realm.signed_token(
                aud=["account", "realm-management"],
                resource_access={"realm-management": {"roles": roles}},
            ),
            "expires_in": 300,
            "token_type": "Bearer",
        }
    )
    monkeypatch.setattr(realm.session.http, "get", lambda *_: pytest.fail("cached verification"))
    for _ in range(2):
        assert realm.session.management_roles() == frozenset(roles)
        assert realm.session.access_token()


def test_access_requires_signature_algorithm_and_nonce_verified_identity(realm, signing_key):
    for token in (
        "test-access",
        signed_identity(realm.profile, signing_key, aud="realm-management"),
        jwt.encode(
            {"sub": "operator"}, "test-only-secret" * 3, algorithm="HS256", headers={"kid": "key-1"}
        ),
    ):
        with pytest.raises(SafeError, match="could not be verified"):
            realm.session.accept_tokens(
                {"access_token": token, "expires_in": 300, "token_type": "Bearer"}
            )
    with pytest.raises(SafeError):
        realm.session.verify_identity(
            realm.signed_token(aud=realm.profile.client_id, nonce="wrong"), "expected"
        )
    with pytest.raises(SafeError, match="identity"):
        realm.session.accept_tokens(
            {"access_token": realm.signed_token(), "expires_in": 300, "token_type": "Bearer"}
        )


@pytest.mark.parametrize("prefix", ["", "/auth/"])
def test_missing_management_audience_has_safe_actionable_guidance(realm, prefix):
    realm.profile = Profile("alternate", f"https://identity.example{prefix}", "alternate")
    realm.session.profile = realm.profile
    realm.session.discovery["jwks_uri"] = realm.profile.issuer + "/certs"
    token = realm.signed_token(aud="account")
    with pytest.raises(SafeError) as exc:
        realm.session.accept_tokens(
            {"access_token": token, "expires_in": 300, "token_type": "Bearer"}
        )
    for hint in (
        "realm-management",
        "operator",
        "roles default",
        "audience mapper",
        "Full Scope Allowed",
        "do not grant",
        "wrong account",
        "same browser/profile",
        f"Browser logout: {realm.profile.issuer}/protocol/openid-connect/logout",
    ):
        assert hint in str(exc.value)
    assert token not in str(exc.value)
    assert str(exc.value).endswith(
        f"Browser logout: {realm.profile.issuer}/protocol/openid-connect/logout"
    )
    assert realm.requests == []
    assert realm.session.tokens == {}


@pytest.mark.parametrize(
    "change",
    [{"sub": "other"}, {"azp": "other"}, {"aud": "other"}, {"exp": 1}, {"resource_access": {}}],
)
def test_refresh_revalidates_and_blocks_admin_without_stale_claims(realm, monkeypatch, change):
    realm.session.tokens["refresh_token"] = "refresh"
    realm.session.discovery["token_endpoint"] = realm.profile.issuer + "/token"
    requests = []

    def refresh(url, *, data):
        requests.append((url, data))
        return httpx.Response(
            200,
            json={
                "access_token": realm.signed_token(**change),
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    monkeypatch.setattr(realm.session.http, "post", refresh)
    realm.session.expires_at = 0
    with pytest.raises(SafeError):
        realm.accounts.connection.raw_post("admin/realms/example/users", data="{}")
    assert realm.requests == []
    assert len(requests) == 1
    if "resource_access" in change:
        assert realm.session.management_roles() == frozenset()
    else:
        assert realm.session.tokens == {}
        with pytest.raises(SafeError):
            realm.session.management_roles()


def test_wall_clock_expiry_cannot_use_cached_roles(realm, monkeypatch):
    monkeypatch.setattr(auth.time, "time", lambda: time.time_ns() / 1e9 + 600)
    with pytest.raises(SafeError, match="expired"):
        realm.session.management_roles()
    assert realm.session.tokens == {}


def test_token_replacement_invalidates_cached_verification(realm):
    realm.session.tokens["access_token"] = "replacement"
    with pytest.raises(SafeError):
        realm.session.management_roles()
    assert realm.session.tokens == {}


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_refresh_http_failure_is_contextual_and_discards_credentials(realm, monkeypatch, status):
    realm.session.tokens["refresh_token"] = "SECRET refresh"
    realm.session.discovery["token_endpoint"] = realm.profile.issuer + "/token"
    realm.session.expires_at = 0
    monkeypatch.setattr(
        realm.session.http, "post", lambda *_, **__: httpx.Response(status, text="SECRET body")
    )
    with pytest.raises(SafeError, match=f"Token refresh rejected \\(HTTP {status}\\)") as exc:
        realm.session.management_roles()
    assert "SECRET" not in str(exc.value)
    assert realm.session.tokens == {}


def test_discovery_transport_error_is_safe_and_contextual(realm, monkeypatch):
    def failure(*_):
        raise httpx.ConnectError("SECRET transport")

    monkeypatch.setattr(realm.session.http, "get", failure)
    with pytest.raises(SafeError, match="OIDC discovery failed") as exc:
        realm.session.login()
    assert "SECRET" not in str(exc.value)
    assert realm.session.tokens == {}


def test_untrusted_jwks_cannot_be_used_to_accept_access_token(realm, monkeypatch):
    realm.session.discovery["jwks_uri"] = "https://evil.example/keys"
    monkeypatch.setattr(realm.session.http, "get", lambda *_: pytest.fail("untrusted request"))
    with pytest.raises(SafeError, match="Untrusted OIDC endpoint"):
        realm.session.accept_tokens(
            {"access_token": realm.signed_token(), "expires_in": 300, "token_type": "Bearer"}
        )
    assert realm.session.tokens == {}
