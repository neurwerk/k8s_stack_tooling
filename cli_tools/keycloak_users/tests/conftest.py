import copy
import json
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from keycloak_users.auth import BrowserSession
from keycloak_users.client import Accounts
from keycloak_users.profile import Profile


class Realm:
    def __init__(self):
        self.profile = Profile("test", "https://identity.example/auth", "example")
        self.users = []
        self.groups = {
            "root": {"id": "root", "path": "/access", "name": "access"},
            "app": {"id": "app", "path": "/access/app", "name": "app"},
            "nested": {"id": "nested", "path": "/access/app/admin", "name": "admin"},
            "ldap": {
                "id": "ldap",
                "path": "/access/ldap",
                "name": "ldap",
                "attributes": {"LDAP_ID": ["remote-id"]},
            },
        }
        self.children = {"root": ["app", "ldap"], "app": ["nested"]}
        self.actions = [
            {"alias": action, "enabled": True, "defaultAction": False}
            for action in ("UPDATE_PASSWORD", "VERIFY_EMAIL", "CONFIGURE_TOTP")
        ]
        self.realm = {
            "defaultRole": {"name": "default-roles-example"},
            "duplicateEmailsAllowed": False,
            "smtpServer": {"host": "smtp.example", "from": "onboarding@example.com"},
        }
        self.components = []
        self.default_groups = []
        self.memberships = []
        self.writes = []
        self.requests = []
        self.failure = None
        self.session = BrowserSession(self.profile)
        self.session.http.close()
        self.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.session.discovery = {"jwks_uri": self.profile.issuer + "/certs"}
        self.session.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.session.verify_identity(
            self.signed_token(aud=self.profile.client_id, nonce="expected"), "expected"
        )
        self.session.accept_tokens(
            {"access_token": self.signed_token(), "expires_in": 300, "token_type": "Bearer"}
        )
        self.accounts = Accounts(self.session)

    def signed_token(self, **changes):
        claims = {
            "iss": self.profile.issuer,
            "aud": "realm-management",
            "azp": self.profile.client_id,
            "sub": "operator",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "resource_access": {"realm-management": {"roles": ["manage-users", "view-realm"]}},
        }
        claims.update(changes)
        return jwt.encode(claims, self.signing_key, algorithm="RS256", headers={"kid": "key-1"})

    def handle(self, request):
        if request.url.path == urlsplit(self.profile.issuer + "/certs").path:
            serialized = jwt.get_algorithm_by_name("RS256").to_jwk(self.signing_key.public_key())
            assert isinstance(serialized, str)
            return httpx.Response(
                200, json={"keys": [{**json.loads(serialized), "kid": "key-1", "alg": "RS256"}]}
            )
        self.requests.append(request)
        path = request.url.path.removeprefix("/auth/admin/realms/example")
        if request.method != "GET":
            self.writes.append(request)
            if self.failure is not None and self.failure in path:
                return httpx.Response(500, text="SECRET server error and action link")
            if request.method == "POST" and path == "/users":
                user = {"id": "new-id", **json.loads(request.content)}
                self.users.append(user)
                return httpx.Response(201, headers={"Location": str(request.url) + "/new-id"})
            return httpx.Response(204)
        result = self.read(path, request.url.params)
        return httpx.Response(200, json=copy.deepcopy(result))

    def read(self, path, params):
        values = {
            "": self.realm,
            "/components": self.components,
            "/authentication/required-actions": self.actions,
            "/default-groups": self.default_groups,
        }
        if path in values:
            return values[path]
        if path == "/users":
            users = self.users
            if "username" in params:
                users = [user for user in users if user["username"] == params["username"]]
            return self.page(users, params)
        if path == "/groups":
            return self.page([self.groups["root"]], params)
        if path.startswith("/groups/"):
            group_id = path.split("/")[2]
            if path.endswith("/children"):
                return self.page(
                    [self.groups[key] for key in self.children.get(group_id, [])], params
                )
            return self.groups[group_id]
        if path.endswith("/groups"):
            return self.page(self.memberships, params)
        if path.startswith("/users/"):
            return next(user for user in self.users if user["id"] == path.split("/")[2])
        raise AssertionError(f"Unexpected read: {path}")

    @staticmethod
    def page(items, params):
        first = int(params.get("first", "0"))
        return items[first : first + int(params.get("max", "100"))]


@pytest.fixture
def realm() -> Iterator[Realm]:
    value = Realm()
    yield value
    value.session.close()


@pytest.fixture
def user():
    return {
        "username": "new.user",
        "firstName": "New",
        "lastName": "User",
        "email": "new@example.com",
    }
