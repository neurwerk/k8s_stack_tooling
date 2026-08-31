"""Tests for Keycloak API helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from k8s_stack_tooling.api.keycloak import (
    _find_or_create_group_path,
    add_audience_mapper,
    assign_service_account_realm_roles,
    assign_service_account_role,
    assign_user_groups_api,
    send_user_actions_email_api,
    upsert_client,
    upsert_client_roles,
    upsert_composite_roles_api,
    upsert_groups_api,
    upsert_realm_role_composites_api,
    upsert_realm_roles_api,
    upsert_user_api,
)


def test_find_group_path_requests_all_subgroups() -> None:
    """Do not hide an existing subgroup behind Keycloak's pagination default."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [
            (200, [{"id": "access-id", "name": "access"}]),
            (200, [{"id": "studio-id", "name": "studio-users"}]),
        ]

        group_id = _find_or_create_group_path(
            "http://keycloak", "token", "realm", "/access/studio-users"
        )

    assert group_id == "studio-id"
    assert request.call_args_list[1].args[0].endswith("/groups/access-id/children?max=1000")


def test_find_group_path_accepts_concurrent_creation_after_reread() -> None:
    """Treat a create conflict as success only when the desired sibling now exists."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [
            (200, [{"id": "access-id", "name": "access"}]),
            (200, []),
            (409, {"errorMessage": "Sibling group already exists"}),
            (200, [{"id": "studio-id", "name": "studio-users"}]),
        ]

        group_id = _find_or_create_group_path(
            "http://keycloak", "token", "realm", "/access/studio-users"
        )

    assert group_id == "studio-id"


@pytest.mark.parametrize("status", [201, 204])
def test_upsert_client_sends_desired_secret_on_create_and_update(status: int) -> None:
    """Include the caller-owned secret in both Keycloak client representations."""
    with (
        patch("k8s_stack_tooling.api.keycloak.client_exists") as exists,
        patch("k8s_stack_tooling.api.keycloak.request", return_value=(status, {})) as request,
    ):
        exists.side_effect = [(status == 204, "uuid" if status == 204 else None), (True, "uuid")]
        result = upsert_client(
            "http://keycloak",
            "token",
            "realm",
            "client",
            ["https://app/callback"],
            ["https://app"],
            "confidential",
            "desired-secret",
        )

    assert result == "uuid"
    body = request.call_args.kwargs["body"]
    assert body["secret"] == "desired-secret"
    assert body["fullScopeAllowed"] is True


def test_upsert_client_uses_exact_url_lists_without_secret() -> None:
    """Preserve each URL and add every origin to the logout allowlist."""
    redirect_uris = [
        "https://app/callback",
        "https://admin.app/api/admin/callback",
    ]
    web_origins = ["https://app", "https://admin.app"]
    with (
        patch("k8s_stack_tooling.api.keycloak.client_exists", return_value=(True, "uuid")),
        patch("k8s_stack_tooling.api.keycloak.request", return_value=(204, {})) as request,
    ):
        upsert_client(
            "http://keycloak",
            "token",
            "realm",
            "client",
            redirect_uris,
            web_origins,
        )

    body = request.call_args.kwargs["body"]
    assert "secret" not in body
    assert body["fullScopeAllowed"] is True
    assert body["redirectUris"] == redirect_uris
    assert body["webOrigins"] == web_origins
    assert body["attributes"]["post.logout.redirect.uris"] == ("https://app/*##https://admin.app/*")


def test_add_audience_mapper_creates_missing_audience() -> None:
    """Use a unique mapper name so each configured audience is added."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [
            (
                200,
                [
                    {
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {"included.client.audience": "realm-management"},
                    }
                ],
            ),
            (201, {}),
        ]

        add_audience_mapper(
            "http://keycloak", "token", "realm", "client", "keycloak-api-key-bridge"
        )

    assert request.call_count == 2
    assert request.call_args.kwargs["body"] == {
        "name": "audience-keycloak-api-key-bridge",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": "keycloak-api-key-bridge",
            "id.token.claim": "false",
            "access.token.claim": "true",
        },
    }


def test_add_audience_mapper_skips_existing_audience() -> None:
    """Do not create a duplicate when a mapper already targets the audience."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.return_value = (
            200,
            [
                {
                    "protocolMapper": "oidc-audience-mapper",
                    "config": {"included.client.audience": "keycloak-api-key-bridge"},
                }
            ],
        )

        add_audience_mapper(
            "http://keycloak", "token", "realm", "client", "keycloak-api-key-bridge"
        )

    request.assert_called_once()


def test_add_audience_mapper_exits_when_existing_mappers_cannot_be_listed() -> None:
    """Fail the Job so Helm retries instead of silently omitting an audience."""
    with patch("k8s_stack_tooling.api.keycloak.request", return_value=(500, {})):
        with pytest.raises(SystemExit):
            add_audience_mapper(
                "http://keycloak", "token", "realm", "client", "keycloak-api-key-bridge"
            )


def test_add_audience_mapper_accepts_a_concurrent_creation() -> None:
    """Accept a conflict only after re-reading the exact required mapper."""
    mapper = {
        "protocolMapper": "oidc-audience-mapper",
        "config": {"included.client.audience": "keycloak-api-key-bridge"},
    }
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [(200, []), (409, {}), (200, [mapper])]

        add_audience_mapper(
            "http://keycloak", "token", "realm", "client", "keycloak-api-key-bridge"
        )

    assert request.call_count == 3


def test_add_audience_mapper_exits_when_conflict_does_not_create_required_mapper() -> None:
    """Do not report success when a conflicting mapper targets another audience."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request, pytest.raises(SystemExit):
        request.side_effect = [
            (200, []),
            (409, {}),
            (
                200,
                [
                    {
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {"included.client.audience": "different-client"},
                    }
                ],
            ),
        ]

        add_audience_mapper(
            "http://keycloak", "token", "realm", "client", "keycloak-api-key-bridge"
        )


def test_upsert_client_roles_creates_only_missing_roles() -> None:
    """Client role reconciliation must not duplicate an existing permission."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [
            (200, [{"id": "llm", "name": "llm:invoke"}]),
            (201, {}),
        ]

        upsert_client_roles(
            "http://keycloak",
            "token",
            "realm",
            "agentgateway-id",
            "agentgateway",
            ["llm:invoke", "model:private:invoke"],
        )

    assert request.call_count == 2
    assert request.call_args.kwargs["body"] == {"name": "model:private:invoke"}


def test_assign_service_account_role_verifies_the_mapping() -> None:
    """An accepted assignment must be visible before the Job can succeed."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [
            (200, {"id": "service-account"}),
            (200, [{"id": "realm-management"}]),
            (200, [{"id": "view-users", "name": "view-users"}]),
            (204, {}),
            (200, [{"id": "view-users"}]),
        ]

        assign_service_account_role(
            "http://keycloak", "token", "realm", "client", "realm-management", "view-users"
        )


def test_assign_service_account_role_fails_when_the_mapping_is_missing() -> None:
    """A successful write response alone is not authorization convergence."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request, pytest.raises(SystemExit):
        request.side_effect = [
            (200, {"id": "service-account"}),
            (200, [{"id": "realm-management"}]),
            (200, [{"id": "view-users", "name": "view-users"}]),
            (204, {}),
            (200, []),
        ]
        assign_service_account_role(
            "http://keycloak", "token", "realm", "client", "realm-management", "view-users"
        )


def test_assign_service_account_realm_roles_verifies_all_mappings() -> None:
    """Every declared realm role must be read back after assignment."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [
            (200, {"id": "service-account"}),
            (200, [{"id": "admin", "name": "api-key-admin"}]),
            (204, {}),
            (200, [{"id": "admin", "name": "api-key-admin"}]),
        ]
        assign_service_account_realm_roles(
            "http://keycloak", "token", "realm", "client", ["api-key-admin"]
        )


def test_upsert_composite_roles_fails_when_mutation_does_not_converge() -> None:
    """Composite role updates are producers and must fail closed."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request, pytest.raises(SystemExit):
        request.side_effect = [
            (200, [{"id": "realm-management"}]),
            (200, {"id": "parent"}),
            (200, []),
            (200, {"id": "view-users", "name": "view-users"}),
            (204, {}),
            (200, []),
        ]
        upsert_composite_roles_api(
            "http://keycloak",
            "token",
            "realm",
            "keycloak-admin",
            "realm-management",
            ["view-users"],
        )


def test_upsert_realm_roles_verifies_created_roles() -> None:
    """Role creation must be observable before reconciliation reports success."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [
            (200, [{"id": "existing", "name": "existing-role"}]),
            (201, {}),
            (200, [{"id": "role", "name": "studio-user"}]),
        ]
        upsert_realm_roles_api("http://keycloak", "token", "realm", ["studio-user"])


def test_upsert_realm_role_composites_replaces_stale_realm_composites() -> None:
    """Composite roles must converge instead of accumulating stale grants."""
    parent = {"id": "parent-id", "name": "platform-admin"}
    child = {"id": "child-id", "name": "studio-user"}
    stale = {
        "id": "stale-id",
        "name": "obsolete-role",
        "clientRole": False,
        "containerId": "realm",
    }
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [
            (200, [parent, child, {"id": "stale-id", "name": "obsolete-role"}]),
            (200, [stale]),
            (204, {}),
            (204, {}),
        ]

        upsert_realm_role_composites_api(
            "http://keycloak",
            "token",
            "realm",
            {"platform-admin": ["studio-user"]},
        )

    assert request.call_args_list[2].kwargs["body"] == [stale]
    assert request.call_args_list[3].kwargs["body"] == [child]


def test_upsert_groups_maps_realm_and_agentgateway_roles() -> None:
    """Access groups own both application roles and Gateway permissions."""
    studio_user = {"id": "studio-user-id", "name": "studio-user"}
    llm_invoke = {"id": "llm-id", "name": "llm:invoke"}
    with (
        patch("k8s_stack_tooling.api.keycloak._find_or_create_group_path", return_value="group-id"),
        patch("k8s_stack_tooling.api.keycloak.find_client_uuid", return_value="agentgateway-id"),
        patch("k8s_stack_tooling.api.keycloak.request") as request,
    ):
        request.side_effect = [
            (200, [studio_user]),
            (200, [llm_invoke]),
            (200, []),
            (204, {}),
            (200, []),
            (204, {}),
        ]

        upsert_groups_api(
            "http://keycloak",
            "token",
            "realm",
            {
                "/access/studio-users": {
                    "realmRoles": ["studio-user"],
                    "clientRoles": {"agentgateway": ["llm:invoke"]},
                }
            },
            {"studio-user"},
        )

    assert request.call_args_list[3].kwargs["body"] == [studio_user]
    assert request.call_args_list[5].kwargs["body"] == [llm_invoke]


def test_assign_user_groups_uses_group_membership_not_direct_role_mappings() -> None:
    """Human bootstrap identities are assigned only through access groups."""
    with (
        patch("k8s_stack_tooling.api.keycloak._find_or_create_group_path", return_value="group-id"),
        patch("k8s_stack_tooling.api.keycloak.request", return_value=(204, {})) as request,
    ):
        assign_user_groups_api(
            "http://keycloak",
            "token",
            "realm",
            "user-id",
            "neurwerk-admin",
            ["/access/platform-admins"],
        )

    assert request.call_args.args[0].endswith("/users/user-id/groups/group-id")
    assert request.call_args.kwargs["method"] == "PUT"


def test_upsert_user_creates_passwordless_unverified_user() -> None:
    """Initial human onboarding must not create or distribute a password."""
    with (
        patch("k8s_stack_tooling.api.keycloak._find_or_create_group_path", return_value="group-id"),
        patch("k8s_stack_tooling.api.keycloak.request") as request,
    ):
        request.side_effect = [(200, []), (201, {}), (200, [{"id": "user-id"}]), (204, {})]
        upsert_user_api(
            "http://keycloak",
            "token",
            "realm",
            "admin",
            "admin@example.test",
            "Admin",
            "User",
            ["/access/platform-admins"],
            ["VERIFY_EMAIL", "UPDATE_PASSWORD", "CONFIGURE_TOTP"],
        )

    body = request.call_args_list[1].kwargs["body"]
    assert body["emailVerified"] is False
    assert body["requiredActions"] == ["VERIFY_EMAIL", "UPDATE_PASSWORD", "CONFIGURE_TOTP"]
    assert "credentials" not in body


def test_send_user_actions_email_sends_remaining_actions_with_lifespan() -> None:
    """Resend lets Keycloak issue the token and never exposes it to tooling."""
    with patch("k8s_stack_tooling.api.keycloak.request") as request:
        request.side_effect = [
            (200, [{"id": "user-id"}]),
            (200, {"requiredActions": ["VERIFY_EMAIL", "CONFIGURE_TOTP"]}),
            (204, {}),
        ]
        send_user_actions_email_api("http://keycloak", "token", "realm", "admin", 1800)

    call = request.call_args
    assert call.args[0].endswith("/users/user-id/execute-actions-email?lifespan=1800")
    assert call.kwargs["body"] == ["VERIFY_EMAIL", "CONFIGURE_TOTP"]


def test_send_user_actions_email_refuses_users_with_no_pending_actions() -> None:
    with patch("k8s_stack_tooling.api.keycloak.request") as request, pytest.raises(SystemExit):
        request.side_effect = [(200, [{"id": "user-id"}]), (200, {"requiredActions": []})]
        send_user_actions_email_api("http://keycloak", "token", "realm", "admin", 1800)
