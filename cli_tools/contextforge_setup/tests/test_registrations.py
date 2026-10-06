import base64
import copy
import json
import sys
from typing import Any, override
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from contextforge_setup.client import Client
from contextforge_setup.config import SetupError, text
from contextforge_setup.main import main
from contextforge_setup.registration_config import load_registration_config
from contextforge_setup.registrations import reconcile_registrations

OWNER = "registration-admin@example.com"
TEAM = "approved-team"
ROWS = [
    {
        "id": "context7",
        "provider": "context7",
        "authentication_model": "no-authentication",
        "upstream_url": "https://context7.example.com/mcp",
        "transport": "STREAMABLEHTTP",
        "gateway_id": None,
        "server_id": "c" * 32,
        "approved_tools": ["lookup"],
        "permission": "mcp:context7:invoke",
        "public_route": "/mcp/context7",
        "pii_policy": "platform-default",
        "content_trace": False,
    },
    {
        "id": "brave",
        "provider": "brave",
        "authentication_model": "shared-authentication",
        "upstream_url": "http://brave.example.com/sse",
        "transport": "SSE",
        "gateway_id": None,
        "server_id": "d" * 32,
        "approved_tools": ["search"],
        "permission": "mcp:brave:invoke",
        "public_route": "/mcp/brave",
        "pii_policy": "platform-default",
        "content_trace": True,
    },
]
MANIFEST: dict[str, Any] = {
    "origin": "https://contextforge.example.com",
    "team_id": TEAM,
    "owner_email": OWNER,
    "registrations": ROWS,
}


def config(tmp_path, manifest=None):
    path = tmp_path / "registrations.json"
    path.write_text(json.dumps(manifest or MANIFEST))
    return load_registration_config(path)


class Native(Client):
    """Pinned native shapes, including camelCase and hidden inactive associations."""

    def __init__(self):
        self.gateways: dict[str, dict[str, Any]] = {}
        self.servers: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.membership: dict[str, list[str]] = {}
        self.calls = []
        self.lost_response = ""
        self.ignore_update = False
        self.email_verified = True

    @override
    def require_registration_session(self):
        pass

    @override
    def request(self, method, path, payload=None, *, missing=False, expected_status=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        uri = urlsplit(path)
        if method == "GET":
            return copy.deepcopy(self._read(uri.path, parse_qs(uri.query)))
        assert payload is not None
        if path == "/gateways":
            result = self._gateway(payload)
        elif path == "/servers":
            assert expected_status == 201
            result = self._server(payload)
        elif method == "PUT" and path.startswith("/servers/"):
            assert set(payload) == {"associated_tools"}
            if not self.ignore_update:
                self.membership[path.rsplit("/", 1)[-1]] = payload["associated_tools"]
            return {}
        else:
            raise AssertionError(path)
        if self.lost_response == path:
            self.lost_response = ""
            raise SetupError("Transport outcome unknown")
        return copy.deepcopy(result)

    def _read(self, path, query):
        if path == "/auth/email/me":
            return {
                "email": OWNER,
                "is_active": True,
                "is_admin": True,
                "email_verified": self.email_verified,
            }
        if path == f"/teams/{TEAM}":
            return {"id": TEAM, "is_active": True, "is_personal": False}
        if path in {"/gateways", "/servers", "/tools"}:
            assert query["limit"] == ["0"] and query["include_inactive"] == ["true"]
            if path == "/gateways":
                return list(self.gateways.values())
            if path == "/servers":
                return [self._server_view(key) for key in self.servers]
            return [
                row for row in self.tools.values() if row["gatewayId"] == query["gateway_id"][0]
            ]
        if path.startswith("/gateways/"):
            return self.gateways[path.rsplit("/", 1)[-1]]
        if path.endswith("/tools"):
            assert query["include_inactive"] == ["true"]
            return [self.tools[key] for key in self.membership[path.split("/")[2]]]
        if path.endswith(("/resources", "/prompts")):
            assert query["include_inactive"] == ["true"]
            return []
        if path.startswith("/servers/"):
            return self._server_view(path.rsplit("/", 1)[-1])
        raise AssertionError(path)

    def _gateway(self, payload):
        assert payload["auth_type"] == "none" and payload["passthrough_headers"] == []
        assert set(payload) == {
            "name",
            "description",
            "url",
            "transport",
            "auth_type",
            "passthrough_headers",
            "gateway_mode",
            "team_id",
            "visibility",
        }
        gateway_id = f"{len(self.gateways) + 1:032x}"
        row = {
            "id": gateway_id,
            "name": payload["name"],
            "description": payload["description"],
            "url": payload["url"],
            "transport": payload["transport"],
            "authType": "",
            "gatewayMode": payload["gateway_mode"],
            "teamId": TEAM,
            "ownerEmail": OWNER,
            "createdBy": OWNER,
            "visibility": "team",
            "enabled": True,
            "reachable": True,
            "status": "active",
        }
        self.gateways[gateway_id] = row
        for name in (
            ["lookup", "resolve"] if payload["name"].endswith("context7") else ["search", "summary"]
        ):
            tool_id = f"{len(self.tools) + 100:032x}"
            self.tools[tool_id] = {
                "id": tool_id,
                "originalName": name,
                "gatewayId": gateway_id,
                "url": payload["url"],
                "teamId": TEAM,
                "ownerEmail": OWNER,
                "visibility": "team",
                "integrationType": "MCP",
                "enabled": True,
                "headers": {},
                "auth": None,
            }
        return row

    def _server(self, payload):
        assert set(payload) == {"server", "team_id", "visibility"}
        data = payload["server"]
        server_id = data["id"]
        self.servers[server_id] = {
            "id": server_id,
            "name": data["name"],
            "description": data["description"],
            "teamId": TEAM,
            "ownerEmail": OWNER,
            "createdBy": OWNER,
            "visibility": "team",
            "enabled": True,
            "oauthEnabled": False,
            "oauthConfig": None,
            "associatedResources": [],
            "associatedPrompts": [],
            "associatedA2aAgents": [],
            "icon": "operator-owned-profile",
        }
        self.membership[server_id] = data["associated_tools"]
        return self._server_view(server_id)

    def _server_view(self, server_id):
        return {
            **self.servers[server_id],
            "associatedToolIds": [
                key for key in self.membership[server_id] if self.tools[key]["enabled"]
            ],
        }


def test_scoped_creation_reports_stable_ids_and_preserves_platform_metadata(tmp_path):
    api, cfg = Native(), config(tmp_path)
    mappings = reconcile_registrations(api, cfg)
    assert len(api.gateways) == len(api.servers) == 2
    for spec, mapping in zip(cfg.registrations, mappings, strict=True):
        assert mapping["server_id"] == spec.server_id
        assert mapping["native_mcp_path"] == f"/servers/{spec.server_id}/mcp"
        assert (
            mapping["permission"],
            mapping["public_route"],
            mapping["pii_policy"],
            mapping["content_trace"],
        ) == (spec.permission, spec.public_route, spec.pii_policy, spec.content_trace)
        assert isinstance(mapping["tool_ids"], list) and len(mapping["tool_ids"]) == 1
        assert all(
            api.tools[key]["gatewayId"] == mapping["gateway_id"]
            for key in api.membership[spec.server_id]
        )
    bound = copy.deepcopy(MANIFEST)
    for row, mapping in zip(bound["registrations"], mappings, strict=True):
        row["gateway_id"] = mapping["gateway_id"]
    api.calls.clear()
    assert reconcile_registrations(api, config(tmp_path, bound)) == mappings
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("lost", ["/gateways", "/servers"])
def test_lost_response_retry_reuses_owned_alias_and_supplied_server_id(tmp_path, lost):
    api, cfg = Native(), config(tmp_path)
    api.lost_response = lost
    with pytest.raises(SetupError, match="outcome unknown"):
        reconcile_registrations(api, cfg)
    gateway_id = next(iter(api.gateways))
    mappings = reconcile_registrations(api, cfg)
    assert mappings[0]["gateway_id"] == gateway_id
    assert len(api.gateways) == len(api.servers) == 2


def test_native_pending_registration_does_not_spin_or_create_server(tmp_path):
    api, cfg = Native(), config(tmp_path)
    api.lost_response = "/gateways"
    with pytest.raises(SetupError):
        reconcile_registrations(api, cfg)
    next(iter(api.gateways.values()))["status"] = "pending"
    api.calls.clear()
    with pytest.raises(SetupError, match="not active"):
        reconcile_registrations(api, cfg)
    assert not api.servers and all(method == "GET" for method, _, _ in api.calls)


def test_scoped_admin_token_cannot_hide_catalog_conflicts(tmp_path):
    api = Client("https://contextforge.example.com")
    claims = base64.urlsafe_b64encode(
        json.dumps({"token_use": "api", "teams": [TEAM]}).encode()
    ).decode()
    api.authenticate(f"header.{claims}.signature")
    with (
        patch.object(api, "request") as request,
        pytest.raises(SetupError, match="scoped API token"),
    ):
        reconcile_registrations(api, config(tmp_path))
    request.assert_not_called()
    session = base64.urlsafe_b64encode(b'{"token_use":"session"}').decode()
    api.authenticate(f"header.{session}.signature")
    api.require_registration_session()
    api.close()


@pytest.mark.parametrize("update", [False, True])
def test_inactive_foreign_membership_never_overwritten_even_with_update_flag(tmp_path, update):
    api, cfg = Native(), config(tmp_path)
    reconcile_registrations(api, cfg)
    foreign = next(key for key, row in api.tools.items() if row["originalName"] == "summary")
    api.tools[foreign]["enabled"] = False
    api.membership[cfg.registrations[0].server_id].append(foreign)
    before = copy.deepcopy(api.membership)
    api.calls.clear()
    with pytest.raises(SetupError, match="cross-integration"):
        reconcile_registrations(api, cfg, update_owned_tools=update)
    assert api.membership == before and all(method == "GET" for method, _, _ in api.calls)


def test_approved_owned_tool_update_requires_explicit_flag_and_preserves_ids_profile(tmp_path):
    api, cfg = Native(), config(tmp_path)
    mappings = reconcile_registrations(api, cfg)
    changed = copy.deepcopy(MANIFEST)
    changed["registrations"][0]["approved_tools"] = ["resolve"]
    new_cfg = config(tmp_path, changed)
    api.calls.clear()
    with pytest.raises(SetupError, match="--update-owned-tools"):
        reconcile_registrations(api, new_cfg)
    assert all(method == "GET" for method, _, _ in api.calls)
    updated = reconcile_registrations(api, new_cfg, update_owned_tools=True)
    assert [(row["gateway_id"], row["server_id"]) for row in updated] == [
        (row["gateway_id"], row["server_id"]) for row in mappings
    ]
    assert api.servers[cfg.registrations[0].server_id]["icon"] == "operator-owned-profile"
    assert [method for method, _, _ in api.calls if method != "GET"] == ["PUT"]


def test_failed_membership_write_is_not_reported_as_success(tmp_path):
    api, cfg = Native(), config(tmp_path)
    reconcile_registrations(api, cfg)
    changed = copy.deepcopy(MANIFEST)
    changed["registrations"][0]["approved_tools"] = ["resolve"]
    api.ignore_update = True
    with pytest.raises(SetupError, match="approval changed"):
        reconcile_registrations(api, config(tmp_path, changed), update_owned_tools=True)


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "marker",
        "credentials",
        "direct-proxy",
        "disabled-server",
        "duplicate-url",
        "wrong-id",
        "gateway-path",
        "tool-path",
        "unverified-operator",
    ],
)
def test_conflicts_preflight_before_any_mutations_and_hide_payloads(tmp_path, change):
    api, cfg = Native(), config(tmp_path)
    mappings = reconcile_registrations(api, cfg)
    row = api.gateways[text(mappings[0]["gateway_id"])]
    if change == "owner":
        row["ownerEmail"] = "foreign@example.com"
    elif change == "unverified-operator":
        api.email_verified = False
    elif change == "marker":
        row["description"] = "not managed by this CLI"
    elif change == "credentials":
        row["authValue"] = "never-print-secret"
    elif change == "direct-proxy":
        row["gatewayMode"] = "direct_proxy"
    elif change == "disabled-server":
        api.servers[cfg.registrations[0].server_id]["enabled"] = False
    elif change == "duplicate-url":
        api.gateways["f" * 32] = {
            **row,
            "id": "f" * 32,
            "name": "foreign-oauth",
            "authType": "oauth",
        }
    elif change == "gateway-path":
        row["url"] += "/"
    elif change == "tool-path":
        next(tool for tool in api.tools.values() if tool["originalName"] == "lookup")["url"] += "/"
    else:
        bound = copy.deepcopy(MANIFEST)
        bound["registrations"][0]["gateway_id"] = "f" * 32
        cfg = config(tmp_path, bound)
    api.calls.clear()
    with pytest.raises(SetupError) as caught:
        reconcile_registrations(api, cfg, update_owned_tools=True)
    assert "never-print-secret" not in str(caught.value)
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("change", ["individual", "duplicate-url", "headers", "url-secret"])
def test_unsafe_registration_config_is_rejected_before_credentials_or_api(
    tmp_path, monkeypatch, change
):
    manifest = copy.deepcopy(MANIFEST)
    if change == "individual":
        manifest["registrations"][1]["authentication_model"] = "individual-authentication"
    elif change == "duplicate-url":
        manifest["registrations"][1]["upstream_url"] = "https://CONTEXT7.example.com:443/mcp/"
    elif change == "headers":
        manifest["registrations"][0]["auth_headers"] = {"Authorization": "never-print-secret"}
    else:
        manifest["registrations"][0]["upstream_url"] += "?api_key=never-print-secret"
    path = tmp_path / "registrations.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(
        sys,
        "argv",
        ["contextforge-setup", "reconcile-registrations", "--config", str(path), "--apply"],
    )
    with (
        patch("contextforge_setup.main.Client") as api,
        patch("contextforge_setup.main.getpass.getpass") as prompt,
    ):
        assert main() == 1
    api.assert_not_called()
    prompt.assert_not_called()
