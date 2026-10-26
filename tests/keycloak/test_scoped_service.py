"""Offline security checks against a stateful Keycloak API, not a live realm."""

import copy
import json
from urllib.parse import unquote

import pytest

from k8s_stack_tooling.keycloak import scoped_service as service


class Keycloak:
    def __init__(self):
        self.client_id = "service-example"
        self.client = None
        self.secret = "fixture-secret-do-not-print"
        self.calls = []
        self.scopes = {"default-client-scopes": [], "optional-client-scopes": []}
        self.mappers = []
        self.mapping = {
            "scope-mappings": {"realm": [], "clients/gateway": []},
            "role-mappings": {"realm": [], "clients/gateway": []},
        }
        self.roles = {
            name: {
                "id": "role-" + name,
                "name": name,
                "clientRole": True,
                "containerId": "gateway",
                "composite": False,
            }
            for name in ("llm:invoke", "model:example:invoke", "provider:example:company-use")
        }
        self.fault = None
        self.groups = []

    def request(self, url, method="GET", body=None, headers=None):
        path = unquote(url.split("/admin/realms/test")[1])
        self.calls.append((method, path, copy.deepcopy(body)))
        return 200, copy.deepcopy(self.dispatch(path, method, body))

    def dispatch(self, path, method, body):
        if path == "/clients?clientId=agentgateway":
            return [{"id": "gateway", "clientId": "agentgateway"}]
        if path == "/clients?clientId=" + self.client_id:
            return [{"id": "machine", "clientId": self.client_id}] if self.client else []
        if path.startswith("/clients/gateway/roles/"):
            if self.fault == "prerequisite":
                raise RuntimeError(self.secret)
            return self.roles[path.removeprefix("/clients/gateway/roles/")]
        if path == "/clients" and method == "POST":
            self.client = copy.deepcopy(body)
            assert self.client.pop("secret") == self.secret
            return None
        if path == "/clients/machine":
            assert self.client is not None
            if method == "PUT":
                assert "secret" not in body, "Existing secrets must never be rewritten"
                self.client.update(copy.deepcopy(body))
                if self.fault == "enable" and body.get("enabled"):
                    self.client["fullScopeAllowed"] = True
            return self.client
        if path == "/clients/machine/service-account-user":
            if self.fault == "account-mismatch":
                return {"id": "other-user", "username": "ordinary-user"}
            return {"id": "service-user", "username": "service-account-" + self.client_id}
        if path == "/users/service-user/groups":
            if self.fault == "groups":
                raise RuntimeError(self.secret)
            return self.groups
        for kind, attached in self.scopes.items():
            prefix = "/clients/machine/" + kind
            if path == prefix:
                return attached
            if path.startswith(prefix + "/"):
                assert method == "DELETE"
                attached[:] = [s for s in attached if s["id"] != path.split("/")[-1]]
                return None
        prefix = "/clients/machine/protocol-mappers/models"
        if path == prefix:
            if method == "POST":
                self.mappers.append({**copy.deepcopy(body), "id": str(len(self.mappers))})
            return self.mappers
        if path.startswith(prefix + "/"):
            assert method == "DELETE"
            self.mappers[:] = [m for m in self.mappers if m["id"] != path.split("/")[-1]]
            return None
        for kind, mapping in self.mapping.items():
            prefix = (
                "/clients/machine/scope-mappings"
                if kind == "scope-mappings"
                else "/users/service-user/role-mappings"
            )
            if path == prefix:
                return {
                    "realmMappings": mapping["realm"],
                    "clientMappings": {
                        key: {"id": key.removeprefix("clients/"), "mappings": items}
                        for key, items in mapping.items()
                        if key.startswith("clients/")
                    },
                }
            if path.startswith(prefix + "/"):
                key = path.removeprefix(prefix + "/").removesuffix("/composite")
                items = mapping.setdefault(key, [])
                if method == "POST":
                    items.extend(copy.deepcopy(body))
                elif method == "DELETE":
                    items[:] = [r for r in items if r not in body]
                return items
        raise AssertionError((method, path))

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


@pytest.fixture
def kc(monkeypatch):
    fake = Keycloak()
    for key in ("KC_CLIENT_SECRET_FILE",):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "KC_INTERNAL_URL": "http://keycloak",
        "KC_REALM": "test",
        "KC_ADMIN_USER": "admin",
        "KC_ADMIN_PASSWORD": fake.secret,
        "KC_CLIENT_ID": fake.client_id,
        "KC_CLIENT_SECRET": fake.secret,
        "KC_REALM_ROLE_SCOPE": "[]",
        "KC_GATEWAY_ROLE_SCOPE": json.dumps(list(fake.roles)),
        "KC_CLIENT_AUDIENCES": '["agentgateway"]',
        "KC_CLIENT_APPROVAL": json.dumps(
            {
                "clientId": fake.client_id,
                "realmRoles": [],
                "gatewayPermissions": list(fake.roles),
                "audiences": ["agentgateway"],
            }
        ),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(service, "request", fake.request)
    monkeypatch.setattr(service, "get_admin_token", lambda *args: "token")
    monkeypatch.setattr(service, "get_client_secret", lambda *args: fake.secret)
    return fake


@pytest.mark.parametrize(
    "key,value",
    [
        ("KC_CLIENT_ID", "studio"),
        ("KC_CLIENT_ID", "account"),
        ("KC_CLIENT_ID", "bad/identity"),
        ("KC_CLIENT_ID", "x" * 129),
        ("KC_CLIENT_ID", "service-other"),
        ("KC_GATEWAY_ROLE_SCOPE", '["realm-admin"]'),
        ("KC_REALM_ROLE_SCOPE", '["llm:invoke"]'),
        ("KC_CLIENT_AUDIENCES", '["account"]'),
        ("KC_GATEWAY_ROLE_SCOPE", '["llm:invoke", "model:example:invoke"]'),
    ],
)
def test_reject_unapproved_before_api(kc, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        service.reconcile()
    assert not kc.calls


@pytest.mark.parametrize("client_id", sorted(service.RESERVED_CLIENT_IDS))
def test_platform_client_cannot_be_approved_for_takeover(kc, monkeypatch, client_id):
    monkeypatch.setenv("KC_CLIENT_ID", client_id)
    monkeypatch.setenv(
        "KC_CLIENT_APPROVAL",
        json.dumps(
            {
                "clientId": client_id,
                "realmRoles": [],
                "gatewayPermissions": list(kc.roles),
                "audiences": ["agentgateway"],
            }
        ),
    )
    with pytest.raises(ValueError, match="reserved"):
        service.reconcile()
    assert not kc.calls


@pytest.mark.parametrize("client_id", ["service-example", "worker-jobs"])
def test_service_only_noop_and_secret_preserved(kc, monkeypatch, client_id):
    kc.client_id = client_id
    monkeypatch.setenv("KC_CLIENT_ID", client_id)
    monkeypatch.setenv(
        "KC_CLIENT_APPROVAL",
        json.dumps(
            {
                "clientId": client_id,
                "realmRoles": [],
                "gatewayPermissions": list(kc.roles),
                "audiences": ["agentgateway"],
            }
        ),
    )
    service.reconcile()
    assert kc.client["clientId"] == client_id
    assert kc.client["enabled"] is True
    assert kc.client["serviceAccountsEnabled"] is True
    for key in (
        "fullScopeAllowed",
        "standardFlowEnabled",
        "implicitFlowEnabled",
        "directAccessGrantsEnabled",
        "publicClient",
    ):
        assert kc.client[key] is False
    assert kc.client["redirectUris"] == kc.client["webOrigins"] == []
    audience = next(m for m in kc.mappers if m["protocolMapper"] == "oidc-audience-mapper")
    assert audience["config"]["included.client.audience"] == "agentgateway"
    kc.calls.clear()
    service.reconcile()
    assert not kc.writes()


def test_cannot_take_over_unowned_client(kc):
    kc.client = {"attributes": {}, "fullScopeAllowed": True}
    with pytest.raises(ValueError, match="not owned"):
        service.reconcile()
    assert not kc.writes()


def test_secret_mismatch_never_rotates(kc, monkeypatch):
    service.reconcile()
    kc.calls.clear()
    monkeypatch.setenv("KC_CLIENT_SECRET", "different-secret")
    with pytest.raises(ValueError, match="rotation"):
        service.reconcile()
    assert not kc.writes()


def test_shrink_revokes_scope_and_assignment_only_on_owned_identity(kc, monkeypatch):
    service.reconcile()
    reduced = ["llm:invoke", "model:example:invoke"]
    monkeypatch.setenv("KC_GATEWAY_ROLE_SCOPE", json.dumps(reduced))
    monkeypatch.setenv(
        "KC_CLIENT_APPROVAL",
        json.dumps(
            {
                "clientId": kc.client_id,
                "realmRoles": [],
                "gatewayPermissions": reduced,
                "audiences": ["agentgateway"],
            }
        ),
    )
    kc.scopes["default-client-scopes"] = [{"id": "roles"}]
    # Drift in this owned service account must be removed, not left latent for bridge grants.
    stale = {"id": "stale", "name": "platform-admin"}
    kc.mapping["role-mappings"]["realm"] = [stale]
    kc.mapping["scope-mappings"]["clients/other"] = [stale]
    kc.calls.clear()
    service.reconcile()
    for mapping in kc.mapping.values():
        assert {r["name"] for r in mapping["clients/gateway"]} == set(reduced)
        assert not mapping["realm"]
        assert not mapping.get("clients/other")
    assert not kc.scopes["default-client-scopes"]
    assert all(
        path.startswith(("/clients/machine", "/users/service-user")) for _, path, _ in kc.writes()
    )


def test_composite_roles_cannot_escalate(kc):
    kc.roles["llm:invoke"]["composite"] = True
    with pytest.raises(ValueError, match="non-composite"):
        service.reconcile()
    assert not kc.writes()


@pytest.mark.parametrize("unsafe", ["composite", "group"])
def test_confirmed_unsafe_owned_identity_disables_issuance(kc, unsafe, capsys):
    service.reconcile()
    assert kc.client["enabled"] is True
    if unsafe == "composite":
        kc.roles["llm:invoke"]["composite"] = True
    else:
        kc.groups = [{"id": "administrators"}]
    unchanged = copy.deepcopy((kc.mapping, kc.roles, kc.groups))
    kc.calls.clear()
    with pytest.raises(SystemExit):
        service.main()
    output = capsys.readouterr()
    assert kc.client["enabled"] is False
    assert kc.writes() == [("PUT", "/clients/machine", {"enabled": False})]
    assert (kc.mapping, kc.roles, kc.groups) == unchanged
    assert not output.out
    assert "failed" in output.err
    assert kc.secret not in output.err


@pytest.mark.parametrize("fault", ["prerequisite", "groups", "account-mismatch", "role-mismatch"])
def test_read_failure_or_lookup_mismatch_preserves_existing_identity(kc, fault):
    service.reconcile()
    kc.calls.clear()
    kc.fault = fault
    if fault == "role-mismatch":
        # An identity mismatch is not proof that the expected role became composite.
        kc.roles["llm:invoke"].update({"name": "other", "composite": True})
    with pytest.raises(RuntimeError):
        service.reconcile()
    assert kc.client["enabled"] is True
    assert not kc.writes()


@pytest.mark.parametrize("mismatch", ["ownership", "secret"])
def test_composite_drift_cannot_mutate_unverified_identity(kc, monkeypatch, mismatch):
    service.reconcile()
    kc.calls.clear()
    kc.roles["llm:invoke"]["composite"] = True
    if mismatch == "ownership":
        kc.client["attributes"].clear()
    else:
        monkeypatch.setenv("KC_CLIENT_SECRET", "different-secret")
    with pytest.raises(ValueError):
        service.reconcile()
    assert kc.client["enabled"] is True
    assert not kc.writes()


def test_prerequisite_failure_no_mutation_or_ready_output(kc, capsys):
    kc.fault = "prerequisite"
    with pytest.raises(SystemExit):
        service.main()
    output = capsys.readouterr()
    assert not kc.writes()
    assert not output.out
    assert kc.secret not in output.err
    assert "failed" in output.err


def test_failed_enable_readback_disables(kc):
    kc.fault = "enable"
    with pytest.raises(service.ScopeDriftError):
        service.reconcile()
    assert kc.client["enabled"] is False


def test_file_secret_and_success_output_never_disclose(kc, monkeypatch, tmp_path, capsys):
    secret_file = tmp_path / "machine-secret"
    secret_file.write_text(kc.secret)
    monkeypatch.delenv("KC_CLIENT_SECRET")
    monkeypatch.setenv("KC_CLIENT_SECRET_FILE", str(secret_file))
    service.main()
    output = capsys.readouterr()
    assert "verified" in output.out
    assert kc.secret not in output.out + output.err
