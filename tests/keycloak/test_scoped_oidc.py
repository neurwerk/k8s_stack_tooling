"""Execute the mounted OIDC reconciler against a stateful mocked Keycloak API."""

import contextlib
import copy
import io
import os
import runpy
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[2] / "src/k8s_stack_tooling/keycloak/scoped_oidc.py"


class ForgejoOIDCRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.roles = [
            {"id": name + "-id", "name": name, "clientRole": False}
            for name in ("forgejo-user", "forgejo-admin")
        ]
        self.other = {"id": "other-id", "name": "platform-admin", "clientRole": False}
        self.client: dict[str, Any] = {"attributes": {}}
        self.clients = [{"id": "forgejo-id", "clientId": "forgejo"}]
        self.realm_mappings = [copy.deepcopy(self.other)]
        self.client_mappings = {"other": {"id": "other-client-id", "mappings": [self.other]}}
        self.mappers = []
        self.mapper_config_readback = {}
        self.scopes = [
            {"id": name + "-id", "name": name, "protocol": "openid-connect"}
            for name in ("profile", "email", "roles", "offline_access")
        ]
        self.attached = {
            "default-client-scopes": copy.deepcopy(self.scopes[:3]),
            "optional-client-scopes": [copy.deepcopy(self.scopes[3])],
        }
        self.offline_roles = [{"id": "offline-role", "name": "offline_access", "clientRole": False}]
        self.scope_roles = []
        self.scope_client_roles = {}
        self.scope_mappers = []
        self.calls = []
        self.fault = None
        self.secret = "fixture-secret-never-log"
        env = patch.dict(
            os.environ,
            {
                "KC_INTERNAL_URL": "http://keycloak",
                "KC_REALM": "test",
                "KC_ADMIN_USER": "admin",
                "KC_ADMIN_PASSWORD": self.secret,
                "KC_CLIENT_SECRET": self.secret,
                "KC_CLIENT_ID": "forgejo",
                "KC_REALM_ROLE_SCOPE": '["forgejo-user", "forgejo-admin"]',
                "KC_ROLE_CLAIM": "forgejo_roles",
                "KC_REDIRECT_URI": "https://forgejo.example.com/user/oauth2/keycloak/callback",
                "KC_WEB_ORIGIN": "https://forgejo.example.com",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        http = types.SimpleNamespace(request=self.request)
        self.helpers = types.SimpleNamespace(
            get_admin_token=Mock(return_value="fixture-token"),
            get_client_secret=Mock(return_value=self.secret),
        )
        modules = patch.dict(
            sys.modules,
            {
                "k8s_stack_tooling": types.ModuleType("k8s_stack_tooling"),
                "k8s_stack_tooling.api": types.ModuleType("k8s_stack_tooling.api"),
                "k8s_stack_tooling.api.http": http,
                "k8s_stack_tooling.api.keycloak": self.helpers,
            },
        )
        modules.start()
        self.addCleanup(modules.stop)
        self.reconcile = runpy.run_path(str(SCRIPT))["reconcile"]

    def request(self, url, method="GET", body: Any = None, headers=None):
        self.assertEqual(headers, {"Authorization": "Bearer fixture-token"})
        path = url.removeprefix("http://keycloak/admin/realms/test")
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.fault == "http":
            return 403, {"error": self.secret}
        if path == "/clients?clientId=forgejo":
            return 200, copy.deepcopy(self.clients)
        if path == "/clients":
            self.assertEqual(method, "POST")
            self.assertIs(body["fullScopeAllowed"], False)
            self.assertEqual(body["secret"], self.secret)
            self.client = copy.deepcopy(body)
            if self.fault == "creation":
                return 409, None
            self.clients = [{"id": "forgejo-id", "clientId": "forgejo"}]
            if self.fault == "created-duplicate":
                self.clients *= 2
            return 201, None
        if path == "/client-scopes":
            return 200, copy.deepcopy(self.scopes)
        if path.startswith("/roles/"):
            role = next(role for role in self.roles if role["name"] == path.split("/")[-1])
            return (404, None) if self.fault == "missing-role" else (200, copy.deepcopy(role))
        if path.startswith("/client-scopes/"):
            self.assertEqual(method, "GET", "shared scopes must never be modified")
            if path.endswith("/scope-mappings/realm/composite"):
                if "/offline_access-id/" in path:
                    return 200, copy.deepcopy(self.offline_roles)
                return 200, copy.deepcopy(self.scope_roles)
            if path.endswith("/scope-mappings"):
                return 200, {"clientMappings": copy.deepcopy(self.scope_client_roles)}
            if path.endswith("/protocol-mappers/models"):
                return 200, copy.deepcopy(self.scope_mappers)
        self.assertTrue(path.startswith("/clients/forgejo-id"), path)
        path = path.removeprefix("/clients/forgejo-id")
        if not path:
            if method == "PUT":
                self.assertIs(body["fullScopeAllowed"], False)
                self.assertEqual(body["secret"], self.secret)
                self.client.update(copy.deepcopy(body))
                if self.fault == "client-readback":
                    self.client["fullScopeAllowed"] = True
                return 204, None
            return 200, copy.deepcopy(self.client)
        if path == "/scope-mappings":
            return 200, {
                "realmMappings": copy.deepcopy(self.realm_mappings),
                "clientMappings": copy.deepcopy(self.client_mappings),
            }
        if path == "/scope-mappings/realm":
            if method == "DELETE" and self.fault != "stale-realm":
                self.realm_mappings = [role for role in self.realm_mappings if role not in body]
            if method == "POST" and self.fault != "missing-scope":
                self.realm_mappings.extend(copy.deepcopy(body))
            return 204, None
        if path.startswith("/scope-mappings/clients/"):
            self.assertEqual(method, "DELETE")
            if self.fault != "stale-client":
                self.client_mappings = {}
            return 204, None
        if path == "/scope-mappings/realm/composite":
            effective = copy.deepcopy(self.realm_mappings)
            if self.fault == "effective-scope":
                effective.append(copy.deepcopy(self.other))
            return 200, effective
        if path.startswith("/protocol-mappers/models"):
            if method == "POST":
                self.mappers.append({**copy.deepcopy(body), "id": f"mapper-{len(self.mappers)}"})
                return 201, None
            if method == "PUT":
                self.mappers = [
                    copy.deepcopy(body) if item["id"] == body["id"] else item
                    for item in self.mappers
                ]
                return 204, None
            result = copy.deepcopy(self.mappers)
            for mapper in result:
                config = mapper["config"]
                if config.get("usermodel.realmRoleMapping.rolePrefix") == "":
                    del config["usermodel.realmRoleMapping.rolePrefix"]
                config.setdefault("introspection.token.claim", "true")
                if mapper["protocolMapper"] == "oidc-audience-mapper":
                    config.setdefault("userinfo.token.claim", "false")
                config.update(self.mapper_config_readback)
            if result and self.fault == "mapper-readback":
                result[0]["config"]["userinfo.token.claim"] = "false"
            return 200, result
        parts = path.strip("/").split("/")
        if parts[0] in self.attached:
            kind = parts[0]
            if method == "GET":
                return 200, copy.deepcopy(self.attached[kind])
            if self.fault != "attachment-readback":
                if method == "DELETE":
                    self.attached[kind] = [
                        scope for scope in self.attached[kind] if scope["id"] != parts[1]
                    ]
                elif method == "PUT":
                    self.attached[kind].append(
                        copy.deepcopy(
                            next(scope for scope in self.scopes if scope["id"] == parts[1])
                        )
                    )
            return 204, None
        self.fail(f"Unexpected request: {method} {path}")

    def test_exact_scopes_and_effective_mapper_are_idempotent(self):
        self.attached = {
            "default-client-scopes": [copy.deepcopy(self.scopes[2])],
            "optional-client-scopes": copy.deepcopy(self.scopes[:2] + self.scopes[3:]),
        }
        self.reconcile()
        self.assertFalse(self.client["fullScopeAllowed"])
        self.assertEqual(self.realm_mappings, self.roles)
        self.assertEqual(self.client_mappings, {})
        self.assertEqual(
            {scope["name"] for scope in self.attached["default-client-scopes"]},
            {"profile", "email"},
        )
        self.assertEqual(self.attached["optional-client-scopes"], [])
        mapper = self.mappers[0]
        self.assertEqual(mapper["protocolMapper"], "oidc-usermodel-realm-role-mapper")
        self.assertEqual(mapper["config"]["claim.name"], "forgejo_roles")
        for key in ("multivalued", "id.token.claim", "access.token.claim", "userinfo.token.claim"):
            self.assertEqual(mapper["config"][key], "true")
        self.assertEqual(mapper["config"]["introspection.token.claim"], "false")
        self.assertEqual(mapper["config"]["usermodel.realmRoleMapping.rolePrefix"], "")
        before = copy.deepcopy((self.client, self.realm_mappings, self.mappers))
        self.calls.clear()
        self.reconcile()
        self.assertEqual((self.client, self.realm_mappings, self.mappers), before)
        self.assertFalse(any(method in ("POST", "DELETE") for method, _, _ in self.calls))
        self.assertTrue(
            all(
                path.startswith("/clients/forgejo-id")
                for method, path, _ in self.calls
                if method != "GET"
            )
        )

    def test_normalized_mapper_readback_clears_existing_prefix_and_introspection(self):
        self.reconcile()
        self.mappers[0]["config"]["usermodel.realmRoleMapping.rolePrefix"] = "stale-"
        self.mappers[0]["config"]["introspection.token.claim"] = "true"
        self.reconcile()
        _, readback = self.request(
            "http://keycloak/admin/realms/test/clients/forgejo-id/protocol-mappers/models",
            headers={"Authorization": "Bearer fixture-token"},
        )
        self.assertNotIn("usermodel.realmRoleMapping.rolePrefix", readback[0]["config"])
        self.assertEqual(readback[0]["config"]["introspection.token.claim"], "false")
        self.assertEqual(self.mappers[0]["config"]["usermodel.realmRoleMapping.rolePrefix"], "")

    def test_normalized_mapper_readback_rejects_config_drift(self):
        self.reconcile()
        for key in (*self.mappers[0]["config"], "unexpected.config"):
            with self.subTest(key=key):
                self.mapper_config_readback = {key: "unexpected"}
                with self.assertRaisesRegex(RuntimeError, "Role mapper readback mismatch"):
                    self.reconcile()
        for value in (None, "true"):
            with self.subTest(introspection=value):
                self.mapper_config_readback = {"introspection.token.claim": value}
                with self.assertRaisesRegex(RuntimeError, "Role mapper readback mismatch"):
                    self.reconcile()
        self.mapper_config_readback = {"usermodel.realmRoleMapping.rolePrefix": None}
        with self.assertRaisesRegex(RuntimeError, "Role mapper readback mismatch"):
            self.reconcile()

    def test_fresh_client_detaches_offline_access_without_modifying_shared_scopes(self):
        self.clients = []
        original = copy.deepcopy((self.scopes, self.offline_roles))
        self.reconcile()
        self.assertEqual((self.scopes, self.offline_roles), original)
        self.assertIn(
            ("DELETE", "/clients/forgejo-id/optional-client-scopes/offline_access-id", None),
            self.calls,
        )
        self.assertIn(
            ("DELETE", "/clients/forgejo-id/default-client-scopes/roles-id", None), self.calls
        )
        writes = [
            body
            for method, path, body in self.calls
            if method in ("POST", "PUT") and path in ("/clients", "/clients/forgejo-id")
        ]
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(body["fullScopeAllowed"] is False for body in writes))
        self.assertTrue(
            all(
                method == "GET"
                for method, path, _ in self.calls
                if path.startswith("/client-scopes")
            )
        )
        self.assertEqual(self.attached["optional-client-scopes"], [])

    def test_ambiguous_or_invalid_client_lookup_never_writes(self):
        for clients in (
            [{"id": "first", "clientId": "forgejo"}, {"id": "second", "clientId": "forgejo"}],
            [{"id": "other", "clientId": "forgejo-other"}],
            [{"clientId": "forgejo"}],
            [{"id": "", "clientId": "forgejo"}],
        ):
            with self.subTest(clients=clients):
                self.clients = clients
                self.calls.clear()
                with self.assertRaises(RuntimeError):
                    self.reconcile()
                self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

    def test_creation_and_scope_lookup_failures(self):
        for fault in ("creation", "created-duplicate"):
            with self.subTest(fault=fault):
                self.clients = []
                self.fault = fault
                with self.assertRaises(RuntimeError):
                    self.reconcile()
        self.fault = None
        self.clients = [{"id": "forgejo-id", "clientId": "forgejo"}]
        original = copy.deepcopy(self.scopes)
        for scopes in (
            original[1:],
            original + [original[0]],
            [{**original[0], "id": ""}, *original[1:]],
            [{**original[0], "protocol": "saml"}, *original[1:]],
        ):
            with self.subTest(scopes=scopes):
                self.scopes = scopes
                with self.assertRaisesRegex(RuntimeError, "scope lookup"):
                    self.reconcile()

    def test_failed_mutations_and_readback_fail_closed(self):
        for fault in (
            "http",
            "missing-role",
            "client-readback",
            "missing-scope",
            "stale-realm",
            "stale-client",
            "effective-scope",
            "mapper-readback",
            "attachment-readback",
        ):
            with self.subTest(fault=fault):
                self.fault = fault
                self.realm_mappings = [copy.deepcopy(self.other)]
                self.client_mappings = {
                    "other": {"id": "other-client-id", "mappings": [self.other]}
                }
                self.mappers = []
                self.attached["optional-client-scopes"] = [copy.deepcopy(self.scopes[3])]
                with self.assertRaises(RuntimeError):
                    self.reconcile()

    def test_extra_or_duplicate_claim_mapper_is_rejected(self):
        self.reconcile()
        managed = copy.deepcopy(self.mappers[0])
        for name in ("unexpected", managed["name"]):
            with self.subTest(name=name):
                self.mappers = [
                    copy.deepcopy(managed),
                    {**copy.deepcopy(managed), "id": "extra", "name": name},
                ]
                with self.assertRaisesRegex(RuntimeError, "role mapper|role claim mapper"):
                    self.reconcile()

    def test_attached_scopes_cannot_broaden_roles_or_override_claim(self):
        for fault in ("realm-role", "client-role", "claim-mapper"):
            with self.subTest(fault=fault):
                self.scope_roles = [self.other] if fault == "realm-role" else []
                self.scope_client_roles = (
                    {"other": {"mappings": [self.other]}} if fault == "client-role" else {}
                )
                self.scope_mappers = (
                    [{"config": {"claim.name": "forgejo_roles"}}] if fault == "claim-mapper" else []
                )
                with self.assertRaisesRegex(RuntimeError, "Attached client scope"):
                    self.reconcile()

    def test_secret_readback_and_safe_entrypoint_failure(self):
        self.helpers.get_client_secret.return_value = "wrong-secret"
        with self.assertRaisesRegex(RuntimeError, "secret readback"):
            self.reconcile()

        def unsafe_helper(*args):
            print(self.secret)
            print(self.secret, file=sys.stderr)
            raise RuntimeError(self.secret)

        self.helpers.get_admin_token.side_effect = unsafe_helper
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as failure:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(failure.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Scoped OIDC reconciliation or readback failed\n")

    def test_distinct_product_role_scope_and_nested_claim(self):
        self.roles = [
            {"id": name + "-id", "name": name, "clientRole": False}
            for name in ("dify-user", "dify-admin")
        ]
        with patch.dict(
            os.environ,
            {
                "KC_REALM_ROLE_SCOPE": '["dify-user", "dify-admin"]',
                "KC_ROLE_CLAIM": "realm_access.roles",
                "KC_CLIENT_AUDIENCES": '["dify"]',
                "KC_REFRESH_TOKEN_ROTATION_ENABLED": "true",
            },
        ):
            self.reconcile()
        self.assertEqual(self.realm_mappings, self.roles)
        self.assertEqual(self.mappers[0]["config"]["claim.name"], "realm_access.roles")
        self.assertEqual(self.mappers[1]["config"]["included.client.audience"], "dify")
        self.assertEqual(self.mappers[1]["config"]["access.token.claim"], "true")
        self.assertEqual(self.client["attributes"]["client.refresh.token.rotation.enabled"], "true")

    def test_invalid_scope_input_never_contacts_keycloak(self):
        for value in ("[]", '["duplicate", "duplicate"]', "[7]", "{}", "invalid"):
            with self.subTest(value=value), patch.dict(os.environ, {"KC_REALM_ROLE_SCOPE": value}):
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.assertEqual(self.calls, [])

    def test_audience_mapper_rejects_userinfo_claim_drift(self):
        self.mapper_config_readback = {"userinfo.token.claim": "true"}
        with patch.dict(os.environ, {"KC_CLIENT_AUDIENCES": '["dify"]'}):
            with self.assertRaisesRegex(RuntimeError, "Audience mapper readback mismatch"):
                self.reconcile()
