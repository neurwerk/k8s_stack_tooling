"""Execute the mounted OIDC reconciler against a stateful mocked Keycloak API."""

import contextlib
import copy
import io
import json
import os
import runpy
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import unquote

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
        self.client_mappings: dict[str, Any] = {
            "other": {"id": "other-client-id", "mappings": [self.other]}
        }
        self.mappers = []
        self.mapper_config_readback = {}
        self.scopes = [
            {"id": name + "-id", "name": name, "protocol": "openid-connect"}
            for name in ("profile", "email", "roles", "offline_access", "basic")
        ]
        self.attached = {
            "default-client-scopes": copy.deepcopy(self.scopes[:3]),
            "optional-client-scopes": [copy.deepcopy(self.scopes[3])],
        }
        self.offline_roles = [{"id": "offline-role", "name": "offline_access", "clientRole": False}]
        self.scope_roles = []
        self.scope_client_roles = {}
        self.scope_mappers = []
        self.scope_definition_readback = {}
        self.scope_definition_missing = ()
        self.subject_mappers = [
            {
                "protocolMapper": "oidc-sub-mapper",
                "protocol": "openid-connect",
                "config": {"access.token.claim": "true"},
            }
        ]
        self.gateway_roles = [
            {"id": name + "-id", "name": name, "clientRole": True, "containerId": "gateway-id"}
            for name in ("llm:invoke", "model:Vendor/model.v1:invoke")
        ]
        self.composites = {}
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

    def test_opt_in_full_path_groups_mapper_preserves_narrow_role_scope(self):
        with patch.dict(os.environ, {"KC_GROUPS_CLAIM": "groups"}):
            self.reconcile()
            groups = next(
                m for m in self.mappers if m["protocolMapper"] == "oidc-group-membership-mapper"
            )
            self.assertEqual(
                groups["config"],
                {
                    "claim.name": "groups",
                    "full.path": "true",
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                    "userinfo.token.claim": "true",
                    "introspection.token.claim": "false",
                },
            )
            self.assertFalse(self.client["fullScopeAllowed"])
            self.assertEqual(
                {r["name"] for r in self.realm_mappings}, {"forgejo-user", "forgejo-admin"}
            )
            self.reconcile()
            self.assertEqual(
                sum(m["protocolMapper"] == "oidc-group-membership-mapper" for m in self.mappers), 1
            )
        for name in ("realm_access", "sub", "custom_groups"):
            with self.subTest(name=name), patch.dict(os.environ, {"KC_GROUPS_CLAIM": name}):
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, "Invalid OIDC groups claim"):
                    self.reconcile()
                self.assertEqual(self.calls, [])

    def request(self, url, method="GET", body: Any = None, headers=None):
        self.assertEqual(headers, {"Authorization": "Bearer fixture-token"})
        path = url.removeprefix("http://keycloak/admin/realms/test")
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.fault == "http":
            return 403, {"error": self.secret}
        if self.fault == "shared-http" and path.startswith("/client-scopes/"):
            return 503, None
        if self.fault == "mapper-http" and path == "/clients/forgejo-id/protocol-mappers/models":
            return 503, None
        if path == "/clients?clientId=" + os.environ["KC_CLIENT_ID"]:
            return 200, copy.deepcopy(self.clients)
        if path == "/clients?clientId=agentgateway":
            return 200, [{"id": "gateway-id", "clientId": "agentgateway"}]
        if path.startswith("/clients/gateway-id/roles/"):
            name = unquote(path.removeprefix("/clients/gateway-id/roles/"))
            return 200, copy.deepcopy(next(r for r in self.gateway_roles if r["name"] == name))
        if path.startswith("/roles-by-id/"):
            return 200, copy.deepcopy(self.composites[unquote(path.split("/")[2])])
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
            if path.count("/") == 2:
                definition = copy.deepcopy(
                    next(s for s in self.scopes if s["id"] == path.split("/")[2])
                )
                definition.update(self.scope_definition_readback)
                for key in self.scope_definition_missing:
                    definition.pop(key, None)
                return 200, definition
            if path.endswith("/scope-mappings/realm/composite"):
                if "/offline_access-id/" in path:
                    return 200, copy.deepcopy(self.offline_roles)
                return 200, copy.deepcopy(self.scope_roles)
            if path.endswith("/scope-mappings"):
                return 200, {"clientMappings": copy.deepcopy(self.scope_client_roles)}
            if path.endswith("/protocol-mappers/models"):
                return 200, copy.deepcopy(
                    self.scope_mappers + (self.subject_mappers if "/basic-id/" in path else [])
                )
        self.assertTrue(path.startswith("/clients/forgejo-id"), path)
        path = path.removeprefix("/clients/forgejo-id")
        if not path:
            if method == "PUT":
                if "fullScopeAllowed" in body:
                    self.assertIs(body["fullScopeAllowed"], False)
                    self.assertEqual(body["secret"], self.secret)
                self.client.update(copy.deepcopy(body))
                if self.fault == "client-readback":
                    self.client["fullScopeAllowed"] = True
                if self.fault == "enable-readback" and body.get("enabled") is True:
                    self.client["fullScopeAllowed"] = True
                if self.fault == "refresh-readback" and "attributes" in body:
                    self.client["attributes"]["client.refresh.token.rotation.enabled"] = "false"
                if self.fault == "logout-readback" and "attributes" in body:
                    self.client["attributes"]["post.logout.redirect.uris"] = (
                        "https://wrong.example.com"
                    )
                return 204, None
            result = copy.deepcopy(self.client)
            if self.fault == "reordered-urls":
                for key in ("redirectUris", "webOrigins"):
                    if key in result:
                        result[key].reverse()
            if self.fault in ("redirectUris", "webOrigins"):
                result[self.fault].append("https://unapproved.example.com")
            return 200, result
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
            uuid = path.split("/")[3]
            if path.endswith("/composite"):
                result = copy.deepcopy(self.client_mappings.get(uuid, {}).get("mappings", []))
                if self.fault == "gateway-effective":
                    result.append(copy.deepcopy(self.other))
                return 200, result
            if method == "POST":
                mapping = self.client_mappings.setdefault(uuid, {"id": uuid, "mappings": []})
                mapping["mappings"].extend(copy.deepcopy(body))
            elif method == "DELETE" and self.fault != "stale-client":
                for key, mapping in list(self.client_mappings.items()):
                    if mapping["id"] == uuid:
                        mapping["mappings"] = [r for r in mapping["mappings"] if r not in body]
                        if not mapping["mappings"]:
                            del self.client_mappings[key]
            return 204, None
        if path == "/scope-mappings/realm/composite":
            effective = copy.deepcopy(self.realm_mappings)
            if self.fault == "effective-scope":
                effective.append(copy.deepcopy(self.other))
            return 200, effective
        if path.startswith("/protocol-mappers/models"):
            if method == "DELETE":
                if self.fault != "stale-mapper":
                    self.mappers = [m for m in self.mappers if m["id"] != path.split("/")[-1]]
                return 204, None
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
                if mapper["protocolMapper"] == "oidc-usermodel-client-role-mapper":
                    config.pop("usermodel.clientRoleMapping.rolePrefix", None)
                    if self.fault == "gateway-mapper":
                        config["usermodel.clientRoleMapping.clientId"] = "other"
                config.update(self.mapper_config_readback)
            if result and self.fault == "mapper-readback":
                result[0]["config"]["userinfo.token.claim"] = "false"
            return 200, result
        parts = path.strip("/").split("/")
        if parts[0] in self.attached:
            kind = parts[0]
            if method == "GET":
                return 200, [
                    {"id": scope["id"], "name": scope["name"]} for scope in self.attached[kind]
                ]
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

    def test_canonical_identity_drift_disables_an_enabled_client(self):
        for field, wrong in (("id", "other-id"), ("name", "other-name"), ("protocol", "saml")):
            for missing in (False, True):
                with self.subTest(field=field, missing=missing):
                    self.reconcile()
                    self.assertTrue(self.client["enabled"])
                    self.scope_definition_readback = {} if missing else {field: wrong}
                    self.scope_definition_missing = (field,) if missing else ()
                    with self.assertRaisesRegex(
                        RuntimeError, "Identity client scope readback mismatch"
                    ):
                        self.reconcile()
                    self.assertFalse(self.client["enabled"])
                    self.scope_definition_readback = {}
                    self.scope_definition_missing = ()

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
            {"basic", "profile", "email"},
        )
        self.assertEqual(self.attached["optional-client-scopes"], [])
        mapper = self.mappers[0]
        self.assertEqual(mapper["protocolMapper"], "oidc-usermodel-realm-role-mapper")
        self.assertEqual(mapper["config"]["claim.name"], "forgejo_roles")
        for key in ("multivalued", "id.token.claim", "access.token.claim", "userinfo.token.claim"):
            self.assertEqual(mapper["config"][key], "true")
        self.assertEqual(mapper["config"]["introspection.token.claim"], "false")
        self.assertEqual(mapper["config"]["usermodel.realmRoleMapping.rolePrefix"], "")
        before = copy.deepcopy((self.client, self.realm_mappings, self.mappers, self.attached))
        self.calls.clear()
        self.reconcile()
        self.assertEqual((self.client, self.realm_mappings, self.mappers, self.attached), before)
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))
        self.assertTrue(
            all(
                path.startswith("/clients/forgejo-id")
                for method, path, _ in self.calls
                if method != "GET"
            )
        )

        self.attached["default-client-scopes"] = [
            scope for scope in self.attached["default-client-scopes"] if scope["name"] != "basic"
        ]
        self.reconcile()
        self.assertEqual(
            {scope["name"] for scope in self.attached["default-client-scopes"]},
            {"basic", "profile", "email"},
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
        self.assertEqual(len(writes), 3)
        self.assertTrue(all(body.get("fullScopeAllowed", False) is False for body in writes))
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
            [scope for scope in original if scope["name"] != "basic"],
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

    def test_exact_browser_lists_accept_set_order_and_enable_only_after_readback(self):
        callbacks = ["https://app.example.com/callback", "https://second.example.com/callback"]
        origins = ["https://app.example.com", "https://second.example.com"]
        self.fault = "reordered-urls"
        for fresh in (False, True):
            with (
                self.subTest(fresh=fresh),
                patch.dict(
                    os.environ,
                    {
                        "KC_REDIRECT_URIS": json.dumps(callbacks),
                        "KC_WEB_ORIGINS": json.dumps(origins),
                        "KC_REFRESH_TOKEN_ROTATION_ENABLED": "true",
                    },
                ),
            ):
                self.clients = [] if fresh else [{"id": "forgejo-id", "clientId": "forgejo"}]
                self.calls.clear()
                self.reconcile()
                self.assertEqual(self.client["redirectUris"], callbacks)
                self.assertEqual(self.client["webOrigins"], origins)
                self.assertEqual(
                    self.client["attributes"]["post.logout.redirect.uris"], "##".join(origins)
                )
                writes = [
                    (index, body)
                    for index, (method, path, body) in enumerate(self.calls)
                    if method in ("POST", "PUT") and path in ("/clients", "/clients/forgejo-id")
                ]
                self.assertTrue(all(not body["enabled"] for _, body in writes[:-1]))
                self.assertTrue(writes[-1][1]["enabled"])
                subject_read = next(
                    i
                    for i, (_, path, _) in enumerate(self.calls)
                    if path == "/client-scopes/basic-id/protocol-mappers/models"
                )
                self.assertLess(subject_read, writes[-1][0])
        for field in ("redirectUris", "webOrigins"):
            with self.subTest(widened=field):
                self.fault = field
                with self.assertRaisesRegex(RuntimeError, "Client readback mismatch"):
                    self.reconcile()
                self.assertFalse(self.client["enabled"])

    def test_explicit_logout_redirects_register_verify_correct_drift_and_remain_noop(self):
        logout_uris = ["https://app.example.com/auth", "https://second.example.com/signed-out"]
        env = {
            "KC_POST_LOGOUT_REDIRECT_URIS": json.dumps(logout_uris),
            "KC_APPROVED_POST_LOGOUT_REDIRECT_URIS": json.dumps(logout_uris),
        }
        self.clients = []
        with patch.dict(os.environ, env):
            self.reconcile()
            self.assertEqual(
                self.client["attributes"]["post.logout.redirect.uris"], "##".join(logout_uris)
            )
            self.assertTrue(self.client["enabled"])
            self.calls.clear()
            self.reconcile()
            self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

            self.client["attributes"]["post.logout.redirect.uris"] = "https://stale.example.com"
            self.calls.clear()
            self.reconcile()
            self.assertEqual(
                self.client["attributes"]["post.logout.redirect.uris"], "##".join(logout_uris)
            )
            writes = [(method, body) for method, _, body in self.calls if method != "GET"]
            self.assertEqual(writes[0], ("PUT", {"enabled": False}))
            self.assertTrue(self.client["enabled"])

            self.client["attributes"]["post.logout.redirect.uris"] = "https://stale.example.com"
            self.fault = "logout-readback"
            with self.assertRaisesRegex(RuntimeError, "attribute readback"):
                self.reconcile()
            self.assertFalse(self.client["enabled"])

        self.fault = None
        self.reconcile()
        self.assertEqual(
            self.client["attributes"]["post.logout.redirect.uris"], os.environ["KC_WEB_ORIGIN"]
        )

    def test_invalid_or_unapproved_logout_redirects_never_contact_keycloak(self):
        valid = json.dumps(["https://app.example.com/auth"])
        invalid = [
            "invalid",
            *(
                json.dumps(value)
                for value in (
                    [],
                    {},
                    [4],
                    ["https://app.example.com/auth", "https://app.example.com/auth"],
                    ["http://app.example.com/auth"],
                    ["https://app.example.com/*"],
                    ["https://user@app.example.com/auth"],
                    ["https://@app.example.com/auth"],
                    ["https://app.example.com/auth#fragment"],
                    ["https://app.example.com/auth#"],
                    ["https://app.example.com/auth "],
                    ["https://app.example.com/au\\th"],
                )
            ),
        ]
        cases = [
            {"KC_POST_LOGOUT_REDIRECT_URIS": valid},
            {
                "KC_POST_LOGOUT_REDIRECT_URIS": valid,
                "KC_APPROVED_POST_LOGOUT_REDIRECT_URIS": '["https://other.example.com/auth"]',
            },
        ]
        for value in invalid:
            cases.extend(
                (
                    {
                        "KC_POST_LOGOUT_REDIRECT_URIS": value,
                        "KC_APPROVED_POST_LOGOUT_REDIRECT_URIS": valid,
                    },
                    {
                        "KC_POST_LOGOUT_REDIRECT_URIS": valid,
                        "KC_APPROVED_POST_LOGOUT_REDIRECT_URIS": value,
                    },
                )
            )
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, env):
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.assertEqual(self.calls, [])
        self.helpers.get_admin_token.assert_not_called()

    def test_invalid_browser_gateway_and_claim_inputs_never_contact_keycloak(self):
        invalid = (
            [
                {"KC_REDIRECT_URIS": json.dumps(value)}
                for value in (
                    [],
                    {},
                    [4],
                    ["http://app.example.com/cb"],
                    ["https://app.example.com/*"],
                    ["https://app.example.com/cb#fragment"],
                    ["https://user@app.example.com/cb"],
                    ["https://app.example.com/cb", "https://app.example.com/cb"],
                )
            ]
            + [
                {"KC_WEB_ORIGINS": json.dumps([value])}
                for value in (
                    "https://app.example.com/",
                    "https://app.example.com?q=1",
                    "https://*.example.com",
                )
            ]
            + [
                {"KC_GATEWAY_ROLE_SCOPE": '["llm:invoke"]'},
                {"KC_GATEWAY_ROLE_SCOPE": '["llm:invoke", "llm:invoke"]'},
                {"KC_GATEWAY_ROLE_SCOPE": '["model:*:invoke"]'},
                {"KC_ROLE_CLAIM": "sub"},
                {"KC_ROLE_CLAIM": "resource_access.roles"},
                {
                    "KC_CLIENT_ID": "audience-demo",
                    "KC_CLIENT_AUDIENCES": '["demo-effective-realm-roles"]',
                },
                {"KC_REFRESH_TOKEN_ROTATION_ENABLED": "yes"},
            ]
        )
        for env in invalid:
            with self.subTest(env=env), patch.dict(os.environ, env):
                with self.assertRaises(ValueError):
                    self.reconcile()
        self.assertEqual(self.calls, [])
        self.helpers.get_admin_token.assert_not_called()

    def test_gateway_scopes_are_exact_and_revoke_cleanly_without_assigning_user_roles(self):
        env = {
            "KC_GATEWAY_ROLE_SCOPE": json.dumps([r["name"] for r in self.gateway_roles]),
            "KC_CLIENT_AUDIENCES": '["agentgateway", "forgejo"]',
        }
        with patch.dict(os.environ, env):
            self.reconcile()
            self.assertEqual(self.client_mappings["gateway-id"]["mappings"], self.gateway_roles)
            mapper = next(
                m
                for m in self.mappers
                if m["protocolMapper"] == "oidc-usermodel-client-role-mapper"
            )
            self.assertEqual(
                mapper["config"]["usermodel.clientRoleMapping.clientId"], "agentgateway"
            )
            self.assertEqual(mapper["config"]["claim.name"], "resource_access.agentgateway.roles")
            self.assertEqual(mapper["config"]["id.token.claim"], "false")
            self.assertFalse(self.client["fullScopeAllowed"])
            start = len(self.calls)
            self.reconcile()
            self.assertTrue(all(method == "GET" for method, _, _ in self.calls[start:]))
        self.reconcile()
        self.assertEqual(self.client_mappings, {})
        self.assertEqual(
            [m["protocolMapper"] for m in self.mappers], ["oidc-usermodel-realm-role-mapper"]
        )
        before = copy.deepcopy((self.client, self.mappers, self.realm_mappings))
        self.reconcile()
        self.assertEqual((self.client, self.mappers, self.realm_mappings), before)
        self.assertTrue(
            all(
                method == "GET" or path.startswith("/clients/forgejo-id")
                for method, path, _ in self.calls
            )
        )

    def test_gateway_composites_and_readback_fail_closed(self):
        env = {
            "KC_GATEWAY_ROLE_SCOPE": json.dumps([r["name"] for r in self.gateway_roles]),
            "KC_CLIENT_AUDIENCES": '["agentgateway"]',
        }
        for fault in ("gateway-effective", "gateway-mapper", "realm-composite", "client-composite"):
            with self.subTest(fault=fault), patch.dict(os.environ, env):
                self.fault = fault
                self.composites = {}
                for role in self.roles + self.gateway_roles:
                    role.pop("composite", None)
                if fault.endswith("composite"):
                    role = self.roles[0] if fault == "realm-composite" else self.gateway_roles[0]
                    role["composite"] = True
                    allowed_child = self.gateway_roles[1]
                    allowed_child["composite"] = True
                    self.composites[role["id"]] = [allowed_child]
                    self.composites[allowed_child["id"]] = [self.other]
                self.calls.clear()
                with self.assertRaises(RuntimeError):
                    self.reconcile()
                self.assertFalse(self.client["enabled"])
                self.assertFalse(
                    any(
                        body.get("enabled")
                        for method, _, body in self.calls
                        if method == "PUT" and isinstance(body, dict)
                    )
                )

    def test_identity_refresh_and_stale_audience_fail_closed(self):
        self.reconcile()
        original = copy.deepcopy(self.subject_mappers)
        for subjects in (
            [],
            [
                {
                    "protocolMapper": "oidc-sub-mapper",
                    "protocol": "openid-connect",
                    "config": {"access.token.claim": "false"},
                }
            ],
        ):
            self.subject_mappers = subjects
            with self.assertRaisesRegex(RuntimeError, "subject"):
                self.reconcile()
            self.assertFalse(self.client["enabled"])
        self.subject_mappers = original
        for claim in ("sub", "resource_access", "realm_access.roles", "aud"):
            self.scope_mappers = [
                {
                    "protocolMapper": "oidc-usermodel-attribute-mapper",
                    "config": {"claim.name": claim},
                }
            ]
            with self.assertRaisesRegex(RuntimeError, "Attached client scope"):
                self.reconcile()
            self.assertFalse(self.client["enabled"])
        self.scope_mappers = []
        self.fault = "refresh-readback"
        with patch.dict(os.environ, {"KC_REFRESH_TOKEN_ROTATION_ENABLED": "true"}):
            with self.assertRaisesRegex(RuntimeError, "attribute readback"):
                self.reconcile()
        self.assertFalse(self.client["enabled"])
        self.fault = None
        with patch.dict(os.environ, {"KC_CLIENT_AUDIENCES": '["agentgateway"]'}):
            self.reconcile()
        self.fault = "stale-mapper"
        with self.assertRaisesRegex(RuntimeError, "allowlist readback"):
            self.reconcile()
        self.assertFalse(self.client["enabled"])

    def test_reserved_platform_clients_are_rejected_before_authentication(self):
        for client_id in (
            "agentgateway",
            "studio",
            "keycloak-api-key-bridge",
            "account",
            "account-console",
            "admin-cli",
            "broker",
            "realm-management",
            "security-admin-console",
        ):
            with (
                self.subTest(client=client_id),
                patch.dict(os.environ, {"KC_CLIENT_ID": client_id}),
            ):
                with self.assertRaisesRegex(ValueError, "Reserved platform"):
                    self.reconcile()
        self.helpers.get_admin_token.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_product_client_ids_remain_supported(self):
        for client_id in ("forgejo", "dify", "librechat"):
            with (
                self.subTest(client=client_id),
                patch.dict(os.environ, {"KC_CLIENT_ID": client_id}),
            ):
                self.clients = [{"id": "forgejo-id", "clientId": client_id}]
                self.client = {"attributes": {}}
                self.mappers = []
                self.reconcile()
                self.assertTrue(self.client["enabled"])
                self.assertEqual(self.client["clientId"], client_id)

    def test_preflight_failures_do_not_mutate_fresh_or_existing_clients(self):
        self.reconcile()
        before = copy.deepcopy(
            (self.client, self.realm_mappings, self.client_mappings, self.mappers, self.attached)
        )
        for fresh in (False, True):
            for fault in ("missing-role", "shared-http"):
                with self.subTest(fresh=fresh, fault=fault):
                    self.clients = [] if fresh else [{"id": "forgejo-id", "clientId": "forgejo"}]
                    self.calls.clear()
                    self.fault = fault
                    with self.assertRaisesRegex(RuntimeError, "request failed"):
                        self.reconcile()
                    self.assertTrue(all(method == "GET" for method, _, _ in self.calls))
                    self.assertEqual(
                        (
                            self.client,
                            self.realm_mappings,
                            self.client_mappings,
                            self.mappers,
                            self.attached,
                        ),
                        before,
                    )
        self.clients = [{"id": "forgejo-id", "clientId": "forgejo"}]
        self.fault = "mapper-http"
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "request failed"):
            self.reconcile()
        self.assertTrue(self.client["enabled"])
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

        self.client["fullScopeAllowed"] = True
        self.fault = "missing-role"
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "request failed"):
            self.reconcile()
        self.assertFalse(self.client["enabled"])
        self.assertEqual(
            [(method, body) for method, _, body in self.calls if method != "GET"],
            [("PUT", {"enabled": False})],
        )

    def test_confirmed_drift_disables_before_correction_and_failed_enable_is_closed(self):
        self.reconcile()
        for fault in (None, "missing-scope", "enable-readback"):
            with self.subTest(fault=fault):
                self.fault = None
                self.reconcile()
                self.client["fullScopeAllowed"] = True
                self.realm_mappings = [copy.deepcopy(self.other)]
                self.fault = fault
                self.calls.clear()
                if fault:
                    with self.assertRaises(RuntimeError):
                        self.reconcile()
                    self.assertFalse(self.client["enabled"])
                else:
                    self.reconcile()
                    self.assertTrue(self.client["enabled"])
                    self.assertFalse(self.client["fullScopeAllowed"])
                writes = [
                    (method, path, body) for method, path, body in self.calls if method != "GET"
                ]
                self.assertEqual(writes[0], ("PUT", "/clients/forgejo-id", {"enabled": False}))
        self.fault = None
        self.reconcile()
        self.client["fullScopeAllowed"] = True
        self.helpers.get_client_secret.side_effect = [self.secret, SystemExit(1)]
        with self.assertRaises(SystemExit):
            self.reconcile()
        self.assertFalse(self.client["enabled"])

    def test_shared_scope_drift_disables_affected_client_but_unapplied_role_does_not(self):
        self.reconcile()
        extra = {"id": "new-role-id", "name": "new-role", "clientRole": False, "composite": True}
        self.roles.append(extra)
        self.composites[extra["id"]] = [self.other]
        self.calls.clear()
        with patch.dict(
            os.environ, {"KC_REALM_ROLE_SCOPE": '["forgejo-user", "forgejo-admin", "new-role"]'}
        ):
            with self.assertRaisesRegex(RuntimeError, "Composite role broadens"):
                self.reconcile()
        self.assertTrue(self.client["enabled"])
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))
        # An unapplied parent must not hide an unsafe descendant already scoped
        # to this client. Visit that parent first to exercise the dependency path.
        self.roles[0]["composite"] = True
        self.composites[extra["id"]] = [self.roles[0]]
        self.composites[self.roles[0]["id"]] = [self.other]
        self.calls.clear()
        with patch.dict(
            os.environ, {"KC_REALM_ROLE_SCOPE": '["new-role", "forgejo-user", "forgejo-admin"]'}
        ):
            with self.assertRaisesRegex(RuntimeError, "Composite role broadens"):
                self.reconcile()
        self.assertFalse(self.client["enabled"])
        self.roles[0].pop("composite")
        self.reconcile()
        self.scope_roles = [self.other]
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "Attached client scope broadens"):
            self.reconcile()
        self.assertFalse(self.client["enabled"])
        self.assertEqual(
            [(method, body) for method, _, body in self.calls if method != "GET"],
            [("PUT", {"enabled": False})],
        )
