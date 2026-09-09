import copy
import json
from unittest.mock import Mock

import httpx
import pytest

from keycloak_users.client import AdminError, Progress, call, local, pages, records
from keycloak_users.profile import SafeError


@pytest.fixture
def realm(realm):
    realm.realm["smtpServer"] = {"host": "smtp.example", "from": "sender@example.com"}
    return realm


def test_catalog_filters_federated_groups_and_recurses(realm):
    catalog = realm.accounts.catalog()
    assert catalog.groups == {"app": "/access/app", "nested": "/access/app/admin"}
    assert not catalog.federation
    assert "role (including composites): default-roles-example" in catalog.defaults
    assert not realm.writes


def test_create_real_library_payload_and_seven_day_email(realm, user):
    create = Mock(wraps=realm.accounts.admin.create_user)
    realm.accounts.admin.create_user = create
    progress = Progress()
    realm.accounts.create(
        user,
        ["app", "nested"],
        ["VERIFY_EMAIL", "UPDATE_PASSWORD"],
        realm.accounts.catalog(),
        progress,
    )
    assert progress.user_id == "new-id"
    assert create.call_args.kwargs == {"exist_ok": False}
    assert progress.stage == "complete"
    assert progress.memberships == ["app", "nested"]
    assert progress.email == "accepted, not proof of delivery"
    payload = json.loads(realm.writes[0].content)
    assert payload == {
        **user,
        "enabled": True,
        "emailVerified": False,
        "requiredActions": ["VERIFY_EMAIL", "UPDATE_PASSWORD"],
    }
    assert realm.writes[-1].url.params["lifespan"] == "604800"
    assert "redirect_uri" not in realm.writes[-1].url.params
    assert [request.method for request in realm.writes] == ["POST", "PUT", "PUT", "PUT"]


@pytest.mark.parametrize("field,value", [("username", "NEW.USER"), ("email", "NEW@example.com")])
def test_duplicate_never_becomes_upsert(realm, user, field, value):
    realm.users = [
        {"id": "existing", "username": "other", "email": "other@example.com", field: value}
    ]
    with pytest.raises(SafeError, match="already exists"):
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], realm.accounts.catalog(), Progress())
    assert not realm.writes


@pytest.mark.parametrize("config", [{}, {"enabled": ["true"]}, {"enabled": "false"}])
def test_active_or_unknown_federation_blocks_creation(realm, user, config):
    realm.components = [{"id": "ldap-provider", "config": config}]
    with pytest.raises(SafeError, match="no active user storage"):
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], realm.accounts.catalog(), Progress())
    assert not realm.writes


def test_disabled_federation_and_no_groups_are_allowed(realm, user):
    realm.components = [{"config": {"enabled": ["false"]}}]
    realm.accounts.create(user, [], ["UPDATE_PASSWORD"], realm.accounts.catalog(), Progress())
    assert len(realm.writes) == 2


@pytest.mark.parametrize("change", ["groups", "actions", "defaults", "duplicates"])
def test_stale_approval_fails_before_writes(realm, user, change):
    catalog = realm.accounts.catalog()
    if change == "groups":
        realm.groups["app"]["attributes"] = {"LDAP_ID": ["linked"]}
    elif change == "actions":
        realm.actions.pop()
    elif change == "defaults":
        realm.default_groups = [{"id": "default", "path": "/unexpected"}]
    else:
        realm.realm["duplicateEmailsAllowed"] = True
    with pytest.raises(SafeError, match="changed"):
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], catalog, Progress())
    assert not realm.writes


@pytest.mark.parametrize(
    "groups,actions", [(["ldap"], ["UPDATE_PASSWORD"]), ([], ["DISABLED"]), ([], [])]
)
def test_invalid_choices_fail_before_writes(realm, user, groups, actions):
    with pytest.raises(SafeError):
        realm.accounts.create(user, groups, actions, realm.accounts.catalog(), Progress())
    assert not realm.writes


@pytest.mark.parametrize(
    "failure,stage,groups,email",
    [
        ("/users", "create", 0, "not attempted"),
        ("/groups/nested", "membership", 1, "not attempted"),
        ("execute-actions-email", "email", 2, "outcome unknown"),
    ],
)
def test_partial_failure_no_retry_no_delete(realm, user, failure, stage, groups, email):
    realm.failure = failure
    progress = Progress()
    with pytest.raises(SafeError) as exc:
        realm.accounts.create(
            user, ["app", "nested"], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress
        )
    assert "SECRET" not in str(exc.value)
    assert progress.stage.startswith(stage)
    assert len(progress.memberships) == groups
    assert progress.email.startswith(email)
    assert len(realm.writes) == (1 if stage == "create" else groups + 2)
    assert not any(request.method == "DELETE" for request in realm.requests)


def test_resume_only_missing_groups_and_remaining_actions(realm, user):
    existing = {
        **user,
        "id": "existing",
        "enabled": True,
        "emailVerified": True,
        "requiredActions": ["VERIFY_EMAIL"],
    }
    realm.users = [existing]
    realm.memberships = [{"id": "app"}]
    snapshot = copy.deepcopy(existing)
    progress = Progress()
    realm.accounts.resume(
        snapshot, ["app", "nested"], ["VERIFY_EMAIL"], realm.accounts.catalog(), progress
    )
    assert existing == snapshot
    assert progress.memberships == ["nested"]
    assert len(realm.writes) == 2
    assert json.loads(realm.writes[-1].content) == ["VERIFY_EMAIL"]
    assert all(request.method == "PUT" for request in realm.writes)


def test_resume_refuses_federated_changed_or_completed_actions(realm, user):
    realm.users = [{**user, "id": "existing", "federationLink": "ldap"}]
    with pytest.raises(SafeError, match="Federated"):
        realm.accounts.existing(user["username"])
    realm.users[0].pop("federationLink")
    snapshot = copy.deepcopy(realm.users[0])
    realm.users[0]["email"] = "changed@example.com"
    with pytest.raises(SafeError, match="changed"):
        realm.accounts.resume(snapshot, [], [], realm.accounts.catalog(), Progress())
    with pytest.raises(SafeError, match="remaining"):
        realm.accounts.resume(
            realm.users[0], [], ["UPDATE_PASSWORD"], realm.accounts.catalog(), Progress()
        )
    assert not realm.writes


def test_pagination_users_and_group_children(realm):
    realm.users = [{"id": f"user-{i}", "username": f"user-{i}"} for i in range(205)]
    realm.groups.update(
        {f"g-{i}": {"id": f"g-{i}", "name": f"g-{i}", "path": f"/access/g-{i}"} for i in range(205)}
    )
    realm.children["root"] = [f"g-{i}" for i in range(205)]
    assert len(realm.accounts.users()) == 205
    assert len(realm.accounts.access_groups()) == 205


def test_repeated_pagination_and_malformed_data_fail_closed():
    def repeated(**_kwargs):
        return [{"id": f"user-{i}"} for i in range(100)]

    with pytest.raises(SafeError, match="repeated"):
        pages(repeated)
    with pytest.raises(SafeError):
        records([None])
    with pytest.raises(SafeError):
        local({"attributes": []})
    with pytest.raises(SafeError, match="outcome"):
        call(lambda: {}["missing"])


@pytest.mark.parametrize("status", [302, 401, 403, 409, 500])
def test_adapter_rejects_redirects_and_does_not_replay(realm, status):
    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(status, text="SECRET", headers={"Location": "https://evil.example"})

    realm.session.http.close()
    realm.session.http = httpx.Client(transport=httpx.MockTransport(reject))
    with pytest.raises(SafeError, match=f"HTTP {status}"):
        realm.accounts.connection.raw_post("admin/realms/example/users", "{}")
    assert len(requests) == 1
    with pytest.raises(SafeError, match="Untrusted"):
        realm.accounts.connection.raw_get("https://evil.example/users")
    with pytest.raises(SafeError, match="payload"):
        realm.accounts.connection.raw_put("admin/realms/example/users", {})


def test_realm_default_actions_require_explicit_enrollment(realm, user):
    realm.actions[2]["defaultAction"] = True
    catalog = realm.accounts.catalog()
    assert "required action: CONFIGURE_TOTP" in catalog.defaults
    with pytest.raises(SafeError, match="default required actions"):
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], catalog, Progress())
    assert not realm.writes
    realm.accounts.create(user, [], ["UPDATE_PASSWORD", "CONFIGURE_TOTP"], catalog, Progress())
    assert len(realm.writes) == 2


def test_default_action_policy_changes_invalidate_approval(realm, user):
    catalog = realm.accounts.catalog()
    realm.actions[2]["defaultAction"] = True
    with pytest.raises(SafeError, match="changed"):
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], catalog, Progress())
    assert not realm.writes


def test_transport_timeout_after_create_is_not_replayed(realm, user):
    requests = []
    original = realm.handle

    def lost_response(request):
        requests.append(request)
        response = original(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("SECRET diagnostic", request=request)
        return response

    realm.session.http.close()
    realm.session.http = httpx.Client(transport=httpx.MockTransport(lost_response))
    progress = Progress()
    with pytest.raises(SafeError) as exc:
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress)
    assert "SECRET" not in str(exc.value)
    assert len(realm.users) == 1
    assert len(realm.writes) == 1
    assert progress.user_id is None
    assert progress.email == "not attempted"
    assert "create outcome unknown" in progress.summary()


def test_malformed_federation_and_missing_defaults_fail_closed(realm):
    realm.components = [{"config": None}]
    with pytest.raises(SafeError, match="user-storage"):
        realm.accounts.catalog()
    realm.components = []
    realm.realm.pop("defaultRole")
    with pytest.raises(SafeError, match="default role"):
        realm.accounts.catalog()
    assert not realm.writes


def test_access_groups_ignore_spoofed_top_level_paths(monkeypatch, realm):
    roots = [
        realm.groups["root"],
        {"id": "spoof", "name": "access/not-a-child", "path": "/access/not-a-child"},
        {"id": "other", "name": "other", "path": "/access/other"},
    ]
    original = realm.read

    def read(path, params):
        if path == "/groups":
            return realm.page(roots, params)
        return original(path, params)

    monkeypatch.setattr(realm, "read", read)
    assert realm.accounts.access_groups() == {"app": "/access/app", "nested": "/access/app/admin"}
    assert not any(
        "spoof" in str(request.url) or "/groups/other" in str(request.url)
        for request in realm.requests
    )


def test_access_groups_derive_display_from_ancestry_not_server_path(realm):
    realm.groups["app"]["path"] = "/another-root/not-the-name"
    realm.groups["nested"]["path"] = "/access/spoof"
    assert realm.accounts.access_groups() == {"app": "/access/app", "nested": "/access/app/admin"}
    realm.groups["root"]["name"] = "not-access"
    assert realm.accounts.access_groups() == {}


@pytest.mark.parametrize("name", ["nested/spoof", "nested\\spoof", ".", ".."])
def test_ambiguous_access_child_names_fail_before_writes(realm, name):
    realm.groups["app"]["name"] = name
    with pytest.raises(SafeError, match="Ambiguous group name"):
        realm.accounts.catalog()
    assert not realm.writes


def test_opaque_federated_ids_do_not_break_local_lookup_or_resume(realm, user):
    existing = {**user, "id": "existing", "enabled": True, "requiredActions": []}
    realm.users = [{"id": "f:provider:external", "username": "federated"}, existing]
    assert len(realm.accounts.users()) == 2
    assert realm.accounts.existing(user["username"]) == existing
    progress = Progress()
    realm.accounts.resume(existing, ["app"], [], realm.accounts.catalog(), progress)
    assert progress.memberships == ["app"]
    assert not local(realm.users[0])
    with pytest.raises(SafeError, match="Federated"):
        realm.accounts.existing("federated")
    assert len(realm.writes) == 1


def test_disabled_resume_with_email_rejects_before_membership(realm, user):
    existing = {**user, "id": "existing", "enabled": False, "requiredActions": ["VERIFY_EMAIL"]}
    realm.users = [existing]
    progress = Progress()
    with pytest.raises(SafeError, match="disabled"):
        realm.accounts.resume(
            existing, ["app"], ["VERIFY_EMAIL"], realm.accounts.catalog(), progress
        )
    assert not realm.writes
    assert progress.memberships == []
    assert progress.email == "not attempted"


@pytest.mark.parametrize("actions,send_email", [([], True), (["VERIFY_EMAIL"], False)])
def test_disabled_group_only_resume_preserves_actions(realm, user, actions, send_email):
    existing = {**user, "id": "existing", "enabled": False, "requiredActions": ["VERIFY_EMAIL"]}
    realm.users = [existing]
    snapshot = copy.deepcopy(existing)
    progress = Progress()
    realm.accounts.resume(
        existing, ["app"], actions, realm.accounts.catalog(), progress, send_email=send_email
    )
    assert existing == snapshot
    assert len(realm.writes) == 1
    assert realm.writes[0].url.path.endswith("/groups/app")
    assert progress.email == "not attempted"
    assert progress.stage == "complete"


def test_create_without_email_preserves_enrollment_and_memberships(realm, user):
    actions = ["UPDATE_PASSWORD", "VERIFY_EMAIL"]
    progress = Progress()
    realm.accounts.create(
        user, ["app"], actions, realm.accounts.catalog(), progress, send_email=False
    )
    assert json.loads(realm.writes[0].content)["requiredActions"] == actions
    assert [request.method for request in realm.writes] == ["POST", "PUT"]
    assert progress.email == "not attempted"
    assert progress.stage == "complete"


def test_malformed_group_detail_is_sanitized(monkeypatch, realm):
    original = realm.read

    def read(path, params):
        return [] if path == "/groups/app" else original(path, params)

    monkeypatch.setattr(realm, "read", read)
    with pytest.raises(SafeError, match="malformed data") as exc:
        realm.accounts.catalog()
    assert exc.value.__suppress_context__
    assert not realm.writes


@pytest.mark.parametrize(
    "status,hint,outcome",
    [
        (400, "Validation", "rejected"),
        (401, "log in again", "rejected"),
        (403, "Permission", "rejected"),
        (404, "Stale target", "rejected"),
        (409, "Duplicate identity", "rejected"),
        (429, "Throttled", "rejected"),
        (500, "Inspect server health", "outcome unknown"),
        (503, "Inspect server health", "outcome unknown"),
    ],
)
def test_create_http_failure_has_safe_operation_and_exact_progress(
    realm, user, status, hint, outcome
):
    original = realm.handle

    def handle(request):
        if request.method == "POST":
            realm.writes.append(request)
            return httpx.Response(status, text="SECRET personal request or token")
        return original(request)

    realm.session.http = httpx.Client(transport=httpx.MockTransport(handle))
    progress = Progress()
    with pytest.raises(AdminError) as exc:
        realm.accounts.create(
            user, ["app"], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress
        )
    assert exc.value.method == "POST"
    assert exc.value.operation == "create user"
    assert exc.value.status == status
    assert exc.value.outcome == outcome
    assert "Realm example" in str(exc.value)
    assert hint in str(exc.value)
    assert "SECRET" not in str(exc.value)
    assert progress.stage == f"create {outcome} HTTP {status}"
    assert progress.user_id is None
    assert "account creation not confirmed" in progress.summary()
    assert progress.memberships == []
    assert progress.email == "not attempted"
    assert len(realm.writes) == 1


@pytest.mark.parametrize("stage", ["membership", "email"])
def test_later_explicit_conflict_preserves_accepted_progress(realm, user, stage):
    original = realm.handle

    def handle(request):
        suffix = "/groups/nested" if stage == "membership" else "/execute-actions-email"
        if request.method == "PUT" and request.url.path.endswith(suffix):
            realm.writes.append(request)
            return httpx.Response(409, text="SECRET")
        return original(request)

    realm.session.http = httpx.Client(transport=httpx.MockTransport(handle))
    progress = Progress()
    with pytest.raises(AdminError, match="Conflict") as exc:
        realm.accounts.create(
            user, ["app", "nested"], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress
        )
    assert "Duplicate" not in str(exc.value)
    assert progress.user_id == "new-id"
    assert progress.memberships == (["app"] if stage == "membership" else ["app", "nested"])
    assert progress.stage == f"{stage} rejected HTTP 409"
    assert "unknown" not in progress.summary()
    assert progress.email == ("not attempted" if stage == "membership" else "rejected HTTP 409")


@pytest.mark.parametrize(
    "path,operation",
    [
        ("/users", "read users"),
        ("/groups", "read groups"),
        ("/users/id/groups", "read groups"),
        ("/authentication/required-actions", "read required actions"),
        ("", "read realm"),
        ("/components", "read federation"),
        ("/default-groups", "read default groups"),
    ],
)
def test_read_errors_identify_capability_without_target_content(realm, path, operation):
    realm.session.http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, text="SECRET"))
    )
    with pytest.raises(AdminError) as exc:
        realm.accounts.connection.raw_get(f"admin/realms/example{path}")
    assert exc.value.method == "GET"
    assert exc.value.operation == operation
    assert "Permission" in str(exc.value)
    assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize(
    "error,outcome",
    [
        (httpx.ConnectError, "not submitted"),
        (httpx.ConnectTimeout, "not submitted"),
        (httpx.PoolTimeout, "not submitted"),
        (httpx.ReadTimeout, "outcome unknown"),
        (httpx.WriteTimeout, "outcome unknown"),
        (httpx.WriteError, "outcome unknown"),
        (httpx.RemoteProtocolError, "outcome unknown"),
    ],
)
def test_transport_outcomes_never_replay(realm, user, error, outcome):
    original = realm.handle
    attempts = []

    def handle(request):
        if request.method == "POST":
            attempts.append(request)
            raise error("SECRET TLS or request detail", request=request)
        return original(request)

    realm.session.http = httpx.Client(transport=httpx.MockTransport(handle))
    progress = Progress()
    with pytest.raises(AdminError) as exc:
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress)
    assert exc.value.outcome == outcome
    assert progress.stage == f"create {outcome}"
    assert progress.email == "not attempted"
    assert "SECRET" not in str(exc.value)
    assert len(attempts) == 1


@pytest.mark.parametrize("failure", ["roles", "token", "refresh"])
def test_authority_failure_before_dispatch_is_not_attempted(monkeypatch, realm, user, failure):
    catalog = realm.accounts.catalog()
    monkeypatch.setattr(realm.accounts, "prevalidate", lambda *_: None)
    monkeypatch.setattr(realm.accounts, "users", lambda: [])
    if failure == "roles":
        monkeypatch.setattr(realm.session, "management_roles", lambda: frozenset({"view-users"}))
    else:
        error = SafeError("SECRET") if failure == "token" else httpx.ReadTimeout("SECRET")
        monkeypatch.setattr(realm.session, "access_token", Mock(side_effect=error))
    progress = Progress()
    with pytest.raises(AdminError) as exc:
        realm.accounts.create(user, ["app"], ["UPDATE_PASSWORD"], catalog, progress)
    assert exc.value.outcome == "not attempted"
    assert progress.stage == "create not attempted"
    assert progress.email == "not attempted"
    assert not realm.writes
    assert "SECRET" not in str(exc.value)


def test_role_check_before_each_write_stops_after_revocation(monkeypatch, realm, user):
    original = realm.handle

    def handle(request):
        response = original(request)
        if request.method == "PUT":
            monkeypatch.setattr(realm.session, "management_roles", lambda: frozenset())
        return response

    realm.session.http = httpx.Client(transport=httpx.MockTransport(handle))
    progress = Progress()
    with pytest.raises(AdminError, match="Missing standard token authority"):
        realm.accounts.create(
            user, ["app", "nested"], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress
        )
    assert len(realm.writes) == 2
    assert progress.user_id == "new-id"
    assert progress.memberships == ["app"]
    assert progress.stage == "membership not attempted"
    assert progress.email == "not attempted"


@pytest.mark.parametrize("roles", [[], ["view-users", "view-realm"], ["keycloak-admin"]])
def test_verified_token_without_standard_write_roles_never_dispatches(realm, roles):
    realm.session.accept_tokens(
        {
            "access_token": realm.signed_token(
                resource_access={"realm-management": {"roles": roles}}
            ),
            "expires_in": 300,
            "token_type": "Bearer",
        }
    )
    with pytest.raises(AdminError, match="Missing standard token authority") as exc:
        realm.accounts.connection.raw_post("admin/realms/example/users", "{}")
    assert exc.value.outcome == "not attempted"
    assert not realm.writes


@pytest.mark.parametrize(
    "location",
    [
        None,
        "",
        "https://evil.example/users/id",
        "/users/",
        "/users/id?SECRET",
        "https://identity.example/auth/admin/realms/other/users/existing-id",
        "https://identity.example/auth/admin/realms/example/groups/existing-id",
    ],
)
def test_invalid_creation_location_stays_unknown(realm, user, location):
    original = realm.handle

    def handle(request):
        response = original(request)
        if request.method == "POST":
            return httpx.Response(201, headers={} if location is None else {"Location": location})
        return response

    realm.session.http = httpx.Client(transport=httpx.MockTransport(handle))
    progress = Progress()
    with pytest.raises(AdminError, match="outcome unknown") as exc:
        realm.accounts.create(
            user, ["app"], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress
        )
    assert progress.stage == "create outcome unknown"
    assert progress.user_id is None
    assert len(realm.writes) == 1
    assert "SECRET" not in str(exc.value)


def test_validation_errors_only_render_recognized_fields(realm):
    realm.session.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400,
                json={
                    "errors": [
                        {
                            "field": "email",
                            "errorMessage": "error-invalid-email",
                            "params": ["SECRET"],
                        },
                        {"field": "SECRET", "errorMessage": "error-user-attribute-required"},
                        {"field": "username", "errorMessage": "SECRET"},
                        {"field": ["SECRET"], "errorMessage": {"SECRET": True}},
                    ]
                },
            )
        )
    )
    with pytest.raises(AdminError) as exc:
        realm.accounts.connection.raw_post("admin/realms/example/users", "{}")
    assert "Check fields: email." in str(exc.value)
    assert "SECRET" not in str(exc.value)
    assert "username" not in str(exc.value)


@pytest.mark.parametrize(
    "smtp,ready",
    [
        ({}, False),
        ({"host": "smtp.example"}, False),
        ({"host": "  ", "from": "sender@example.com"}, False),
        ({"host": 123, "from": "sender@example.com"}, False),
        ({"host": "smtp.example", "from": "sender@example.com", "password": "SECRET"}, True),
    ],
)
def test_catalog_exposes_only_smtp_readiness(realm, smtp, ready):
    realm.realm["smtpServer"] = smtp
    catalog = realm.accounts.catalog()
    assert catalog.smtp_ready is ready
    assert "SECRET" not in repr(catalog)


@pytest.mark.parametrize("smtp", [None, [], "SECRET", 42])
def test_malformed_smtp_fails_bounded(realm, smtp):
    realm.realm["smtpServer"] = smtp
    with pytest.raises(SafeError, match="Malformed SMTP") as exc:
        realm.accounts.catalog()
    assert "SECRET" not in str(exc.value)
    assert not realm.writes


@pytest.mark.parametrize("resume", [False, True])
def test_email_requires_smtp_before_any_write(realm, user, resume):
    realm.realm.pop("smtpServer")
    existing = {**user, "id": "existing", "enabled": True, "requiredActions": ["UPDATE_PASSWORD"]}
    if resume:
        realm.users = [existing]
    operation = realm.accounts.resume if resume else realm.accounts.create
    progress = Progress()
    with pytest.raises(SafeError, match="SMTP configuration: Missing host or sender"):
        operation(
            existing if resume else user,
            ["app"],
            ["UPDATE_PASSWORD"],
            realm.accounts.catalog(),
            progress,
        )
    assert not realm.writes
    assert progress.memberships == []
    assert progress.email == "not attempted"


def test_smtp_removed_after_approval_prevents_create(realm, user):
    catalog = realm.accounts.catalog()
    realm.realm["smtpServer"] = {}
    with pytest.raises(SafeError, match="changed"):
        realm.accounts.create(user, ["app"], ["UPDATE_PASSWORD"], catalog, Progress())
    assert not realm.writes


def test_no_email_does_not_require_smtp(realm, user):
    realm.realm["smtpServer"] = {}
    realm.accounts.create(
        user, ["app"], ["UPDATE_PASSWORD"], realm.accounts.catalog(), Progress(), send_email=False
    )
    assert len(realm.writes) == 2


@pytest.mark.parametrize("path", ["/users", "/groups/app", "/default-groups", ""])
@pytest.mark.parametrize("body", ["SECRET invalid JSON", "null", '["SECRET"]'])
def test_malformed_read_success_is_operation_specific(realm, path, body):
    realm.session.http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    )
    with pytest.raises(AdminError) as exc:
        realm.accounts.connection.raw_get(f"admin/realms/example{path}")
    assert exc.value.method == "GET"
    assert exc.value.outcome == "read failed"
    assert "malformed data" in str(exc.value)
    assert "SECRET" not in str(exc.value)


def test_unexpected_success_status_keeps_creation_unknown(realm, user):
    original = realm.handle

    def handle(request):
        response = original(request)
        if request.method == "POST":
            return httpx.Response(202, headers=response.headers, text="SECRET")
        return response

    realm.session.http = httpx.Client(transport=httpx.MockTransport(handle))
    progress = Progress()
    with pytest.raises(AdminError) as exc:
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress)
    assert exc.value.outcome == "outcome unknown"
    assert progress.stage == "create outcome unknown"
    assert len(realm.writes) == 1
    assert "SECRET" not in str(exc.value)


def test_local_safe_error_before_dispatch_is_not_uncertain(monkeypatch, realm, user):
    monkeypatch.setattr(
        realm.accounts.admin, "create_user", Mock(side_effect=SafeError("Local error"))
    )
    progress = Progress()
    with pytest.raises(SafeError, match="Local error"):
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress)
    assert progress.stage == "create not attempted"
    assert not realm.writes


def test_cancellation_after_dispatch_keeps_creation_unknown(realm, user):
    original = realm.handle

    def handle(request):
        response = original(request)
        if request.method == "POST":
            raise KeyboardInterrupt
        return response

    realm.session.http = httpx.Client(transport=httpx.MockTransport(handle))
    progress = Progress()
    with pytest.raises(KeyboardInterrupt):
        realm.accounts.create(user, [], ["UPDATE_PASSWORD"], realm.accounts.catalog(), progress)
    assert progress.stage == "create outcome unknown"
    assert progress.email == "not attempted"
    assert len(realm.writes) == 1
