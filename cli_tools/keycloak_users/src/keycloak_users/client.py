"""Narrow local-user workflow on the public python-keycloak Admin API."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, override
from urllib.parse import urljoin, urlsplit

import httpx
from keycloak import KeycloakAdmin, KeycloakOpenIDConnection
from keycloak.exceptions import KeycloakError
from requests import Response

from keycloak_users.auth import BrowserSession, Json, trusted_endpoint
from keycloak_users.profile import SafeError, identifier, text

EMAIL_LIFESPAN = 604800


class AdminError(SafeError):
    """Bounded admin failure with the observed outcome of one operation."""

    def __init__(
        self,
        realm: str,
        method: str,
        operation: str,
        outcome: str,
        remediation: str,
        status: int | None = None,
    ) -> None:
        """Expose safe metadata, never response bodies or request values."""
        self.method = method
        self.operation = operation
        self.outcome = outcome
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(
            f"Realm {realm}: {method} {operation}: {outcome}{suffix}. "
            f"{remediation} No automatic retries."
        )


def operation_name(method: str, path: str) -> str:
    """Map fixed endpoint structure to labels without retaining identity paths."""
    parts = urlsplit(path).path.split("/")
    if "realms" in parts:
        parts = parts[parts.index("realms") + 2 :]
    if method != "GET":
        if parts and parts[-1] == "execute-actions-email":
            return "send action email"
        return "create user" if method == "POST" else "assign group"
    if "required-actions" in parts:
        return "read required actions"
    if "default-groups" in parts:
        return "read default groups"
    if "components" in parts:
        return "read federation"
    if "groups" in parts:
        return "read groups"
    return "read users" if "users" in parts else "read realm"


def http_remediation(status: int, operation: str, response: httpx.Response) -> str:
    """Translate only recognized validation codes and allowlisted field names."""
    hints = {
        401: "Token expired or invalid; log in again.",
        403: (
            "Permission denied; recheck the operator's effective realm-management manage-users "
            "grant, client role scope and current server policy. Log in again after changes."
            if operation in ("create user", "assign group", "send action email")
            else "Permission denied for this read; check standard realm-management roles, "
            "client scopes and server policy for this endpoint."
        ),
        404: "Stale target; reload the realm, account and group selection before proceeding.",
        409: (
            "Duplicate identity; inspect existing accounts and use explicit resume if appropriate."
            if operation == "create user"
            else "Conflict; inspect current state before proceeding."
        ),
        429: "Throttled; wait before an explicit retry.",
    }
    if status != 400:
        return hints.get(status, "Inspect server health and current state before proceeding.")
    hint = "Validation failed; review required user-profile fields and realm policy."
    if operation == "send action email":
        hint = (
            "Validation failed; review pending actions, account email and Realm settings > Email."
        )
    try:
        body = response.json()
    except ValueError:
        return hint
    if not isinstance(body, dict):
        return hint
    errors = body.get("errors", [body])
    if not isinstance(errors, list):
        return hint
    fields = sorted(
        {
            error["field"]
            for error in errors
            if isinstance(error, dict)
            and error.get("field") in ("username", "email", "firstName", "lastName")
            and error.get("errorMessage")
            in (
                "error-user-attribute-required",
                "error-invalid-email",
                "error-invalid-length",
                "error-invalid-value",
                "error-pattern-no-match",
                "error-username-invalid-character",
                "error-person-name-invalid-character",
            )
        }
    )
    return hint + (f" Check fields: {', '.join(fields)}." if fields else "")


class Connection(KeycloakOpenIDConnection):
    """Adapt only used synchronous operations, without upstream refresh/replay defaults."""

    def __init__(self, session: BrowserSession) -> None:
        """Keep metadata required by KeycloakAdmin without opening redundant transports."""
        self.realm_name = session.profile.realm
        self.session = session
        self.method = "GET"
        self.operation = "read realm"
        self.submitted = False

    def failure(self, outcome: str, remediation: str, status: int | None = None) -> AdminError:
        """Build a sanitized error for the current operation."""
        return AdminError(
            self.session.profile.realm, self.method, self.operation, outcome, remediation, status
        )

    def reset_submission(self) -> None:
        """Forget the previous operation before a library call can fail locally."""
        self.submitted = False

    def request(self, method: str, path: str, data: object, query: dict[str, Any]) -> Response:
        """Send once, never redirect, and hide error bodies before the library sees them."""
        self.method, self.operation = method, operation_name(method, path)
        self.reset_submission()
        url = urljoin(self.session.profile.server_url.rstrip("/") + "/", path)
        trusted_endpoint(self.session.profile, url)
        if data is not None and not isinstance(data, str):
            raise SafeError("Unsupported admin request payload.")
        try:
            if method != "GET" and not {"manage-users", "realm-admin"}.intersection(
                self.session.management_roles()
            ):
                raise self.failure(
                    "not attempted",
                    "Missing standard token authority: manage-users or realm-admin is required; "
                    "ask an administrator to grant it and log in again.",
                )
            token = self.session.access_token()
        except AdminError:
            raise
        except (SafeError, httpx.HTTPError, ValueError, KeyError, TypeError):
            raise self.failure(
                "not attempted", "Could not obtain verified token authority; log in again."
            ) from None
        try:
            self.submitted = True
            response = self.session.http.request(
                method,
                url,
                params={key: value for key, value in query.items() if value is not None},
                content=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            self.submitted = False
            raise self.failure(
                "not submitted", "Check connectivity and TLS trust before trying again."
            ) from None
        except httpx.HTTPError:
            raise self.failure(
                "outcome unknown" if method != "GET" else "read failed",
                "Transport failed; inspect current state before any explicit retry.",
            ) from None
        if not 200 <= response.status_code < 300:
            raise self.failure(
                "outcome unknown"
                if method != "GET" and response.status_code >= 500
                else "rejected",
                http_remediation(response.status_code, self.operation, response),
                response.status_code,
            )
        self.confirm_response(response, url)
        result = Response()
        result.status_code = response.status_code
        result.headers.update(response.headers)
        result._content = response.content
        return result

    def confirm_response(self, response: httpx.Response, url: str) -> None:
        """Reject malformed success responses without losing operation or write uncertainty."""
        if self.method == "GET":
            try:
                value = response.json()
            except ValueError:
                raise self.failure(
                    "read failed", "Server returned malformed data; reload before proceeding."
                ) from None
            parts = urlsplit(url).path.split("/")
            parts = parts[parts.index("realms") + 2 :]
            collection = self.operation != "read realm" and not (
                len(parts) == 2 and parts[0] in ("users", "groups")
            )
            if (
                collection
                and (
                    not isinstance(value, list) or any(not isinstance(item, dict) for item in value)
                )
            ) or (not collection and not isinstance(value, dict)):
                raise self.failure(
                    "read failed", "Server returned malformed data; reload before proceeding."
                ) from None
        elif self.operation == "create user":
            try:
                location = response.headers.get("Location", "")
                resolved = trusted_endpoint(self.session.profile, urljoin(url + "/", location))
                text(location)
                user_id = identifier(urlsplit(resolved).path.split("/")[-1])
            except (SafeError, ValueError):
                raise self.failure(
                    "outcome unknown",
                    "Invalid creation Location; inspect accounts before explicit resume.",
                ) from None
            if urlsplit(resolved).path != urlsplit(url).path.rstrip("/") + "/" + user_id:
                raise self.failure(
                    "outcome unknown",
                    "Creation Location does not identify this realm's user; inspect accounts "
                    "before explicit resume.",
                )

    @override
    def raw_get(self, path: str, **kwargs: Any) -> Response:
        """Implement python-keycloak's query-parameter calling convention."""
        return self.request("GET", path, None, kwargs)

    @override
    def raw_post(self, path: str, data: object, **kwargs: Any) -> Response:
        """Submit creation once; ambiguous outcomes require explicit recovery."""
        return self.request("POST", path, data, kwargs)

    @override
    def raw_put(self, path: str, data: object, **kwargs: Any) -> Response:
        """Submit membership or email once, without 401 replay."""
        return self.request("PUT", path, data, kwargs)


def call[T](operation: Callable[..., T], *args: object, **kwargs: Any) -> T:  # noqa: ANN401
    """Translate transport/library failures without disclosing upstream details."""
    try:
        return operation(*args, **kwargs)
    except (KeycloakError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        owner = getattr(operation, "__self__", None)
        connection = owner if isinstance(owner, Connection) else getattr(owner, "connection", None)
        if isinstance(connection, Connection):
            raise connection.failure(
                "outcome unknown"
                if connection.method != "GET" and connection.submitted
                else "read failed",
                "Server returned malformed data; inspect current state before proceeding.",
            ) from None
        raise SafeError(
            "Admin operation failed or returned malformed data; outcome may be unknown."
        ) from None


def records(value: object) -> list[Json]:
    """Fail closed if a collection has an unexpected shape."""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SafeError("Malformed admin collection; cannot establish safe state.")
    return value


def pages(operation: Callable[..., object], **kwargs: Any) -> list[Json]:  # noqa: ANN401
    """Read all pages, rejecting stalled pagination instead of silently truncating."""
    result: list[Json] = []
    seen: set[str] = set()
    while True:
        page = records(call(operation, query={"first": len(result), "max": 100}, **kwargs))
        for item in page:
            item_id = text(item.get("id"))
            if item_id in seen:
                raise SafeError("Admin pagination repeated an entry; retry the read later.")
            seen.add(item_id)
        result.extend(page)
        if len(page) < 100:
            return result


def local(record: Json) -> bool:
    """Exclude known federation links and LDAP attributes, including imported groups."""
    attributes = record.get("attributes", {})
    if not isinstance(attributes, dict):
        raise SafeError("Malformed identity attributes.")
    return (
        not str(record.get("id", "")).startswith("f:")
        and not record.get("federationLink")
        and not any(
            "ldap" in key.casefold() or "federation" in key.casefold() for key in attributes
        )
    )


@dataclass(frozen=True)
class Catalog:
    """Read-only state displayed before approval and rechecked before writes."""

    groups: dict[str, str]
    actions: tuple[str, ...]
    defaults: tuple[str, ...]
    federation: bool
    duplicate_emails: bool
    smtp_ready: bool


@dataclass
class Progress:
    """Exact client-observed stages; never implies a remote transaction or rollback."""

    user_id: str | None = None
    stage: str = "prevalidation"
    memberships: list[str] = field(default_factory=list)
    email: str = "not attempted"

    def summary(self) -> str:
        """Return safe stage metadata without credentials or upstream error details."""
        remaining = (
            "Remaining group/email operations were not attempted. "
            if self.stage != "complete"
            else ""
        )
        return (
            f"Stage: {self.stage}; user ID: {self.user_id or 'account creation not confirmed'}; "
            f"memberships accepted: {len(self.memberships)}; email request: {self.email}. "
            f"{remaining}"
            "Nothing was deleted or rolled back."
        )


class Accounts:
    """Read state and perform only local creation, additive membership and action email."""

    def __init__(self, session: BrowserSession) -> None:
        """Use one explicitly selected realm and the verified browser session."""
        self.realm = session.profile.realm
        self.connection = Connection(session)
        self.admin = KeycloakAdmin(connection=self.connection)

    def access_groups(self) -> dict[str, str]:
        """Follow actual access-root ancestry, never trusting an unescaped path prefix."""
        pending = [
            (group, "")
            for group in pages(self.admin.get_groups, full_hierarchy=False)
            if group.get("name") == "access" and local(group)
        ]
        seen: set[str] = set()
        result: dict[str, str] = {}
        while pending:
            group, parent_path = pending.pop()
            group_id = identifier(group.get("id"))
            if group_id in seen:
                raise SafeError("Repeated group in hierarchy; cannot establish safe groups.")
            seen.add(group_id)
            detail = call(self.admin.get_group, group_id)
            if not isinstance(detail, dict):
                raise self.connection.failure(
                    "read failed",
                    "Server returned malformed data; reload groups before proceeding.",
                ) from None
            if detail.get("id") != group_id or detail.get("name") != group.get("name"):
                raise SafeError("Group identity changed during traversal; review again.")
            if not local(detail):
                continue
            name = text(detail.get("name"))
            if name in (".", "..") or any(separator in name for separator in ("/", "\\")):
                raise SafeError("Ambiguous group name; cannot display a safe access path.")
            path = f"{parent_path}/{name}"
            if parent_path:
                result[group_id] = path
            pending.extend(
                (child, path)
                for child in pages(self.admin.get_group_children, group_id=group_id)
                if local(child)
            )
        return dict(sorted(result.items(), key=lambda item: item[1]))

    def catalog(self) -> Catalog:
        """Probe required read capabilities and realm defaults without mutation."""
        realm = call(self.admin.get_realm, self.realm)
        if not isinstance(realm, dict):
            raise self.connection.failure(
                "read failed", "Server returned malformed data; reload realm before proceeding."
            ) from None
        smtp = realm.get("smtpServer", {})
        if not isinstance(smtp, dict):
            raise SafeError("Malformed SMTP configuration; no writes performed.")
        smtp_ready = all(
            isinstance(smtp.get(key), str) and bool(smtp[key].strip()) for key in ("host", "from")
        )
        components = records(
            call(
                self.admin.get_components,
                query={"type": "org.keycloak.storage.UserStorageProvider"},
            )
        )
        configurations = [component.get("config", {}) for component in components]
        if any(not isinstance(config, dict) for config in configurations):
            raise SafeError("Malformed user-storage configuration; creation is unsafe.")
        federation = any(config.get("enabled") != ["false"] for config in configurations)
        required_actions = records(call(self.admin.get_required_actions))
        actions = tuple(
            sorted(
                text(action.get("alias"))
                for action in required_actions
                if action.get("enabled") is True
            )
        )
        default_groups = records(
            call(self.connection.raw_get, f"admin/realms/{self.realm}/default-groups").json()
        )
        defaults = [f"group: {text(group.get('path'))}" for group in default_groups]
        defaults.extend(
            f"required action: {text(action.get('alias'))}"
            for action in required_actions
            if action.get("enabled") is True and action.get("defaultAction") is True
        )
        default_role = realm.get("defaultRole")
        if not isinstance(default_role, dict):
            raise SafeError("Cannot inspect realm default role; creation is unsafe.")
        defaults.append(f"role (including composites): {text(default_role.get('name'))}")
        if type(realm.get("duplicateEmailsAllowed")) is not bool:
            raise SafeError("Cannot inspect realm email uniqueness policy.")
        return Catalog(
            self.access_groups(),
            actions,
            tuple(sorted(defaults)),
            federation,
            realm["duplicateEmailsAllowed"],
            smtp_ready,
        )

    @staticmethod
    def require_email(catalog: Catalog) -> None:
        """Require minimally configured SMTP before any onboarding writes."""
        if not catalog.smtp_ready:
            raise SafeError(
                "SMTP configuration: Missing host or sender; no writes performed; "
                "configure Realm settings > Email, or choose no email."
            )

    def users(self) -> list[Json]:
        """Read paginated users for exact case-insensitive duplicate checks."""
        return pages(self.admin.get_users)

    def existing(self, username: str) -> Json:
        """Resolve an exact local username, never silently treating create as upsert."""
        matches = [
            user
            for user in self.users()
            if text(user.get("username")).casefold() == username.casefold()
        ]
        if len(matches) != 1:
            raise SafeError("Expected exactly one existing account with this username.")
        if not local(matches[0]):
            raise SafeError("Federated accounts cannot be resumed by this tool.")
        user = call(self.admin.get_user, identifier(matches[0].get("id")))
        if not local(user):
            raise SafeError("Federated accounts cannot be resumed by this tool.")
        return user

    def prevalidate(self, catalog: Catalog, groups: list[str], actions: list[str]) -> None:
        """Reject stale approvals and choices outside the current read-only catalog."""
        current = self.catalog()
        if current != catalog:
            raise SafeError("Realm state changed since the summary; restart and review again.")
        if not set(groups) <= current.groups.keys() or not set(actions) <= set(current.actions):
            raise SafeError("Selected groups or required actions are no longer available.")

    def create(
        self,
        user: Json,
        groups: list[str],
        actions: list[str],
        catalog: Catalog,
        progress: Progress,
        *,
        send_email: bool = True,
    ) -> None:
        """Prevalidate, create once, then add memberships and request onboarding email."""
        self.prevalidate(catalog, groups, actions)
        if send_email and actions:
            self.require_email(catalog)
        if catalog.federation or catalog.duplicate_emails:
            raise SafeError("Create requires no active user storage and unique realm emails.")
        if not {"VERIFY_EMAIL", "UPDATE_PASSWORD"} <= set(catalog.actions):
            raise SafeError("VERIFY_EMAIL and UPDATE_PASSWORD must be enabled in the realm.")
        if "UPDATE_PASSWORD" not in actions:
            raise SafeError(
                "V1 requires UPDATE_PASSWORD enrollment for a new passwordless account."
            )
        default_actions = {
            value.removeprefix("required action: ")
            for value in catalog.defaults
            if value.startswith("required action: ")
        }
        if not default_actions <= set(actions):
            raise SafeError("Explicitly select all realm default required actions before creation.")
        for existing in self.users():
            if (
                text(existing.get("username")).casefold() == user["username"].casefold()
                or str(existing.get("email", "")).casefold() == user["email"].casefold()
            ):
                raise SafeError("Username or email already exists; use explicit resume instead.")
        payload: Json = {
            key: text(user.get(key)) for key in ("username", "email", "firstName", "lastName")
        }
        payload.update(enabled=True, emailVerified=False, requiredActions=actions)
        progress.user_id = self.write(
            "create", progress, self.admin.create_user, payload, exist_ok=False
        )
        progress.stage = "created"
        self.finish(identifier(progress.user_id), groups, actions, progress, send_email=send_email)

    def resume(
        self,
        user: Json,
        groups: list[str],
        actions: list[str],
        catalog: Catalog,
        progress: Progress,
        *,
        send_email: bool = True,
    ) -> None:
        """Reconfirm local identity; add only missing groups and email still-pending actions."""
        self.prevalidate(catalog, groups, actions)
        if send_email and actions:
            self.require_email(catalog)
        current = self.existing(text(user.get("username")))
        if current != user:
            raise SafeError("Account changed since identity confirmation; review again.")
        if not set(actions) <= set(current.get("requiredActions", [])):
            raise SafeError("Only remaining required actions may be requested during resume.")
        if send_email and actions and current.get("enabled") is not True:
            raise SafeError(
                "Cannot request action email for a disabled account; no writes performed."
            )
        progress.user_id = identifier(current.get("id"))
        memberships = pages(self.admin.get_user_groups, user_id=progress.user_id)
        missing = [group for group in groups if group not in {item["id"] for item in memberships}]
        self.finish(progress.user_id, missing, actions, progress, send_email=send_email)

    def finish(
        self,
        user_id: str,
        groups: list[str],
        actions: list[str],
        progress: Progress,
        *,
        send_email: bool = True,
    ) -> None:
        """Record each independent stage, stopping at the first ambiguous or failed request."""
        for group_id in groups:
            self.write("membership", progress, self.admin.group_user_add, user_id, group_id)
            progress.memberships.append(group_id)
        progress.stage = "memberships complete"
        if send_email and actions:
            self.write(
                "email",
                progress,
                self.admin.send_update_account,
                user_id,
                actions,
                lifespan=EMAIL_LIFESPAN,
            )
            progress.email = "accepted, not proof of delivery"
        progress.stage = "complete"

    def write(
        self,
        stage: str,
        progress: Progress,
        operation: Callable[..., object],
        *args: object,
        **kwargs: Any,  # noqa: ANN401
    ) -> str | None:
        """Record a write's observed result, including malformed creation confirmations."""
        progress.stage = f"{stage} not attempted"
        self.connection.reset_submission()
        try:
            result = call(operation, *args, **kwargs)
            if stage == "create":
                try:
                    return identifier(result)
                except SafeError:
                    raise self.connection.failure(
                        "outcome unknown",
                        "Invalid creation Location; inspect accounts before explicit resume.",
                    ) from None
        except AdminError as exc:
            status = f" HTTP {exc.status}" if exc.status is not None else ""
            progress.stage = f"{stage} {exc.outcome}{status}"
            if stage == "email":
                progress.email = exc.outcome + status
            raise
        except (SafeError, KeyboardInterrupt):
            if self.connection.submitted:
                progress.stage = f"{stage} outcome unknown"
                if stage == "email":
                    progress.email = "outcome unknown"
            raise
        return None
