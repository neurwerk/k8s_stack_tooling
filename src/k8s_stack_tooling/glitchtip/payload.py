"""Run only in the unchanged GlitchTip 6.2.6 image, with its native dependencies.

Source contracts: upstream v6.2.6 apps/{users,organizations_ext,teams,projects,
alerts,uptime}/{models,schema}.py. Never import this module in the Tooling runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from apps.organizations_ext.models import Organization, OrganizationUser
    from apps.projects.models import Project, ProjectKey
    from apps.uptime.models import Monitor
    from django.db.models import Model, QuerySet


class ProvisionError(RuntimeError):
    """Reject unsafe state without including application data in the message."""


def _diagnostic_identifier(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
        return value
    return "unknown"


def _failure_locations(traceback: TracebackType | None) -> list[str]:
    tail: deque[str] = deque(maxlen=8)
    project = []
    walked = 0
    while traceback is not None and walked < 256:
        code = traceback.tb_frame.f_code
        basename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        if basename == "<stdin>":
            basename = "stdin.py"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}\.py", basename):
            basename = "unknown.py"
        location = f"  {basename}:{_diagnostic_identifier(code.co_name)}:{traceback.tb_lineno}"
        tail.append(location)
        if code.co_filename == __file__ and len(project) < 3:
            project.append(location)
        traceback = traceback.tb_next
        walked += 1
    lines = project + [location for location in tail if location not in project]
    if traceback is not None:
        lines.append("  additional frames omitted")
    return lines


def _failure_details(error: BaseException, limit: int = 4096) -> str:
    """Emit only bounded code metadata; never format exception values or source lines."""
    pending = deque([("root", error)])
    seen: set[int] = set()
    lines = []
    while pending and len(seen) < 4:
        relation, exception = pending.popleft()
        if id(exception) in seen:
            continue
        seen.add(id(exception))
        lines.append(relation + ": " + _diagnostic_identifier(type(exception).__name__))
        lines.extend(_failure_locations(exception.__traceback__))
        for relation, related in (
            ("cause", exception.__cause__),
            ("context", exception.__context__),
        ):
            if related is not None and id(related) not in seen:
                pending.append((relation, related))
    if pending:
        lines.append("additional exception links omitted")
    rendered = "\n".join(lines)
    omitted = "\nadditional diagnostic details omitted"
    return rendered if len(rendered) <= limit else rendered[: limit - len(omitted)] + omitted


def text(value: object) -> str:
    """Validate nonblank text without reflecting its value in errors."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ProvisionError("Required nonblank text is missing or invalid")
    return value


def slug(value: object) -> str:
    """Validate fixed resource slugs."""
    value = text(value)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise ProvisionError("Invalid fixed resource slug")
    return value


def fields(value: object, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    """Reject unknown fields instead of silently ignoring an intended setting."""
    if not isinstance(value, dict) or not required <= value.keys():
        raise ProvisionError("Invalid provisioning configuration")
    if value.keys() - required - (optional or set()):
        raise ProvisionError("Unknown provisioning configuration fields")
    return value


def validate_config(value: object) -> dict[str, Any]:
    """Validate the small fixed-identity configuration before any write."""
    config = fields(value, {"organization", "team", "project", "alert"}, {"monitors", "statusPage"})
    for key in ("organization", "project"):
        row = fields(config[key], {"slug", "name"})
        slug(row["slug"])
        text(row["name"])
    slug(fields(config["team"], {"slug"})["slug"])
    alert = fields(config["alert"], {"name", "alertRecipients"}, {"timespanMinutes"})
    text(alert["name"])
    if (
        type(alert.get("timespanMinutes", 1)) is not int
        or not 1 <= alert.get("timespanMinutes", 1) <= 1440
    ):
        raise ProvisionError("Invalid alert timespan")
    if not isinstance(alert["alertRecipients"], list) or not alert["alertRecipients"]:
        raise ProvisionError("At least one alert recipient is required")
    monitors = config.get("monitors", [])
    if not isinstance(monitors, list) or len(monitors) > 100:
        raise ProvisionError("Invalid monitor configuration")
    names = []
    for monitor in monitors:
        row = fields(
            monitor,
            {"name", "url"},
            {"interval", "timeout", "expectedStatus", "expectedBody", "allowPrivateIPs"},
        )
        names.append(text(row["name"]))
        if type(row.get("allowPrivateIPs", False)) is not bool:
            raise ProvisionError("Invalid private monitor selection")
    if len(set(names)) != len(names):
        raise ProvisionError("Duplicate configured monitor names")
    if "statusPage" in config:
        page = fields(config["statusPage"], {"name", "isPublic", "monitorNames"})
        text(page["name"])
        wanted = page["monitorNames"]
        if (
            type(page["isPublic"]) is not bool
            or not isinstance(wanted, list)
            or any(not isinstance(name, str) for name in wanted)
            or len(set(wanted)) != len(wanted)
            or not set(wanted) <= set(names)
        ):
            raise ProvisionError("Invalid status page monitor selection")
    return config


def initialize() -> None:
    """Check native runtime and pending migrations; never run migrations here."""
    import django
    from django.conf import settings
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    if sys.version_info[:2] != (3, 14):
        raise ProvisionError("Payload requires the pinned Python 3.14 upstream runtime")
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "glitchtip.settings":
        raise ProvisionError("Payload requires the native GlitchTip settings")
    django.setup()
    if settings.GLITCHTIP_VERSION != "6.2.6" or connection.vendor != "postgresql":
        raise ProvisionError("Payload requires GlitchTip 6.2.6 and PostgreSQL")
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        raise ProvisionError("Complete upstream application initialization before provisioning")


def lock(organization: str, *, read_only: bool = False) -> None:
    """Serialize fixed identities with one deterministic transaction advisory lock."""
    from django.db import connection

    key = int.from_bytes(
        hashlib.sha256(("neurwerk/glitchtip/" + organization).encode()).digest()[:8],
        "big",
        signed=True,
    )
    with connection.cursor() as cursor:
        if read_only:
            cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET LOCAL lock_timeout = '15s'")
        cursor.execute("SET LOCAL statement_timeout = '60s'")
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def one[T: Model](query: QuerySet[T]) -> T | None:
    """Never resolve nonunique names with first() or silently choose a collision."""
    rows = list(query[:2])
    if len(rows) > 1:
        raise ProvisionError("Ambiguous resource collision")
    return rows[0] if rows else None


def expect(row: Model, values: dict[str, Any]) -> None:
    """Validate existing records without overwriting unmanaged or changed state."""
    if any(getattr(row, key) != value for key, value in values.items()):
        raise ProvisionError("Existing resource conflicts with fixed configuration")


def create[T: Model](model: type[T], values: dict[str, Any]) -> T:
    """Use native validation and save hooks rather than bulk SQL writes."""
    from apps.projects.models import Project
    from apps.uptime.models import Monitor

    row = model(**values)
    # These native server-managed lifecycle fields start NULL but lack blank=True.
    exclude = (
        {"first_event"}
        if model is Project
        else ({"cached_is_up", "cached_last_change"} if model is Monitor else set())
    )
    row.full_clean(exclude=exclude)
    row.save()
    return row


def organization_and_owner(
    config: dict[str, Any], email: str, password: str
) -> tuple[Organization, OrganizationUser]:
    """Create a normal local owner only for a new organization and new account."""
    from apps.organizations_ext.constants import OrganizationUserRole
    from apps.organizations_ext.models import Organization, OrganizationOwner, OrganizationUser
    from apps.users.models import User
    from django.core.validators import validate_email
    from django.db.models import Q

    validate_email(email)
    desired = config["organization"]
    organization = one(
        Organization.objects.filter(Q(slug=desired["slug"]) | Q(name=desired["name"]))
    )
    user = one(User.objects.filter(email__iexact=email))
    if organization is None:
        if user is not None:
            raise ProvisionError("Existing account is not owned by this configuration")
        user = User.objects.create_user(email=email, password=password)
        organization = create(Organization, desired)
        membership = organization.add_user(user)
    else:
        expect(organization, desired | {"is_deleted": False})
        if user is None:
            raise ProvisionError("Existing organization has no expected local owner")
        membership = one(OrganizationUser.objects.filter(organization=organization, user=user))
        if membership is None:
            raise ProvisionError("Existing account is not a member; refusing promotion")
        expect(membership, {"role": OrganizationUserRole.OWNER})
    if not user.is_active or user.is_staff or user.is_superuser or not user.has_usable_password():
        raise ProvisionError("Expected owner must remain an active normal local account")
    owner = one(OrganizationOwner.objects.filter(organization=organization))
    if owner is None or owner.organization_user_id != membership.pk:
        raise ProvisionError("Organization ownership conflicts with fixed configuration")
    # No password reset, privilege change, EmailAddress verification or invitation adoption.
    return organization, membership


def team_and_project(
    config: dict[str, Any], organization: Organization, membership: OrganizationUser
) -> Project:
    """Create only the selected team/project; preserve every unrelated membership."""
    from apps.projects.models import Project
    from apps.teams.models import Team
    from django.db.models import Q

    team = one(Team.objects.filter(organization=organization, slug=config["team"]["slug"]))
    if team is None:
        team = create(Team, {"organization": organization, **config["team"]})
    if (
        team.projects.exclude(organization=organization).exists()
        or team.members.exclude(organization=organization).exists()
    ):
        raise ProvisionError("Cross-organization team association")
    team.members.add(membership)
    desired = config["project"]
    project = one(
        Project.objects.filter(organization=organization).filter(
            Q(slug=desired["slug"]) | Q(name=desired["name"])
        )
    )
    if project is None:
        project = create(Project, {"organization": organization, **desired})
    else:
        expect(project, desired | {"is_deleted": False})
    if project.teams.exclude(organization=organization).exists():
        raise ProvisionError("Cross-organization project association")
    project.teams.add(team)
    active_key(project)  # Native Project.save creates its key; never replace it.
    return project


def active_key(project: Project) -> ProjectKey:
    """Require one existing active native key without generating or rotating keys."""
    from apps.projects.models import ProjectKey

    key = one(ProjectKey.objects.filter(project=project, is_active=True))
    if key is None:
        raise ProvisionError("Project has no unambiguous active key")
    return key


def alert(config: dict[str, Any], project: Project) -> None:
    """Create or validate one quantity-one alert and its fixed recipients."""
    from apps.alerts.models import AlertRecipient, ProjectAlert
    from apps.alerts.schema import ProjectAlertIn

    desired = config["alert"]
    validated = ProjectAlertIn(
        name=desired["name"],
        quantity=1,
        uptime=True,
        timespanMinutes=desired.get("timespanMinutes", 1),
        alertRecipients=desired["alertRecipients"],
    ).model_dump()
    recipients = validated.pop("alert_recipients")
    row = one(ProjectAlert.objects.filter(project=project, name=desired["name"]))
    if row is None:
        row = create(ProjectAlert, {"project": project, **validated})
    else:
        expect(row, validated)
    wanted = set()
    for recipient in recipients:
        kind = recipient["recipient_type"]
        url = str(recipient.get("url") or "")
        identity = (kind, url)
        if identity in wanted:
            raise ProvisionError("Duplicate configured alert recipients")
        wanted.add(identity)
        values = {
            "recipient_type": kind,
            "url": url,
            "tags_to_add": recipient.get("tags_to_add", []),
            "config": {
                field: recipient[field]
                for field in ("bot_email", "api_key", "channel", "topic")
                if field in recipient
            },
        }
        existing = one(AlertRecipient.objects.filter(alert=row, recipient_type=kind, url=url))
        if existing is None:
            create(AlertRecipient, {"alert": row, **values})
        else:
            expect(existing, values)
    # Extra recipients may contain operational secrets or manual choices: never delete them.
    if set(row.alertrecipient_set.values_list("recipient_type", "url")) != wanted:
        raise ProvisionError("Existing alert has unmanaged recipients; refusing replacement")


def monitors(
    config: dict[str, Any], organization: Organization, project: Project
) -> dict[str, Monitor]:
    """Validate upstream SSRF rules and use native save/on_commit scheduling."""
    from apps.uptime.models import Monitor
    from apps.uptime.schema import MonitorIn
    from apps.uptime.tasks import perform_checks
    from django.conf import settings
    from django.db import transaction
    from django.test.utils import override_settings

    if config.get("monitors") and not settings.GLITCHTIP_ENABLE_UPTIME:
        raise ProvisionError("Uptime monitoring is not enabled")
    result = {}
    existing_ids = []
    for desired in config.get("monitors", []):
        private = desired.get("allowPrivateIPs", False)
        if private and not settings.GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS:
            raise ProvisionError("Private monitor destination is not enabled upstream")
        target = urlsplit(text(desired["url"]))
        if (
            target.scheme not in {"http", "https"}
            or target.username
            or target.password
            or target.fragment
        ):
            raise ProvisionError("Invalid monitor HTTP endpoint")
        with override_settings(GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS=private):
            validated = MonitorIn(
                monitorType="GET",
                name=desired["name"],
                url=desired["url"],
                interval=desired.get("interval", 60),
                timeout=desired.get("timeout", 10),
                expectedStatus=desired.get("expectedStatus", 200),
                expectedBody=desired.get("expectedBody", ""),
                confirmationThreshold=1,
                project=str(project.pk),
            ).model_dump()
        validated.pop("project")
        values = {"organization": organization, "project": project, **validated}
        row = one(Monitor.objects.filter(organization=organization, name=desired["name"]))
        if row is None:
            row = create(Monitor, values)
        else:
            expect(row, values)
            existing_ids.append(row.pk)
        if row.statuspage_set.exclude(organization=organization).exists():
            raise ProvisionError("Cross-organization monitor association")
        result[desired["name"]] = row
    if existing_ids:
        # Retry a prior committed run whose native on_commit enqueue failed.
        transaction.on_commit(lambda: perform_checks.enqueue(existing_ids))
    return result


def status_page(
    config: dict[str, Any], organization: Organization, wanted: dict[str, Monitor]
) -> None:
    """Add only selected associations and retain unrelated same-organization monitors."""
    from apps.uptime.models import StatusPage

    if "statusPage" not in config:
        return
    desired = config["statusPage"]
    values = {
        "organization": organization,
        "name": desired["name"],
        "is_public": desired["isPublic"],
    }
    row = one(StatusPage.objects.filter(organization=organization, name=desired["name"]))
    if row is None:
        row = create(StatusPage, values)
    else:
        expect(row, values)
    if row.monitors.exclude(organization=organization).exists():
        raise ProvisionError("Cross-organization status-page association")
    if (
        row.monitors.filter(project__isnull=False)
        .exclude(project__organization=organization)
        .exists()
    ):
        raise ProvisionError("Cross-organization status-page project association")
    row.monitors.add(*(wanted[name] for name in desired["monitorNames"]))
    # Native status-page responses have a 60s cache; accepting that delay avoids broad invalidation.


def provision(config: dict[str, Any], email: str, password: str) -> None:
    """Apply the entire fixed graph in one transaction; never emit its credentials."""
    from django.db import transaction

    config = validate_config(config)
    email, password = text(email), text(password)
    with transaction.atomic():
        lock(config["organization"]["slug"])
        organization, membership = organization_and_owner(config, email, password)
        project = team_and_project(config, organization, membership)
        alert(config, project)
        selected = monitors(config, organization, project)
        status_page(config, organization, selected)


def export_dsn(
    organization_slug: str, project_slug: str, name: str, team_slug: str
) -> dict[str, Any]:
    """Read a committed, owned project in a database-enforced read-only transaction."""
    from apps.organizations_ext.constants import OrganizationUserRole
    from apps.organizations_ext.models import Organization, OrganizationOwner
    from apps.projects.models import Project
    from django.conf import settings
    from django.db import transaction

    organization_slug, project_slug, team_slug = (
        slug(organization_slug),
        slug(project_slug),
        slug(team_slug),
    )
    with transaction.atomic():
        lock(organization_slug, read_only=True)
        organization = one(Organization.objects.filter(slug=organization_slug, is_deleted=False))
        if organization is None:
            raise ProvisionError("Expected organization is unavailable")
        owner = one(OrganizationOwner.objects.filter(organization=organization))
        if owner is None or owner.organization_user.organization_id != organization.pk:
            raise ProvisionError("Expected organization ownership is unavailable")
        if owner.organization_user.role != OrganizationUserRole.OWNER:
            raise ProvisionError("Expected organization owner role is unavailable")
        project = one(
            Project.objects.filter(
                organization=organization, slug=project_slug, name=name, is_deleted=False
            )
        )
        if (
            project is None
            or not project.teams.filter(organization=organization, slug=team_slug).exists()
        ):
            raise ProvisionError("Expected project/team is unavailable")
        if project.teams.exclude(organization=organization).exists():
            raise ProvisionError("Cross-organization project association")
        key = active_key(project)
        dsn = text(key.get_dsn())
        target = urlsplit(dsn)
        public = settings.GLITCHTIP_URL
        if (
            target.scheme not in {"http", "https"}
            or target.scheme != public.scheme
            or target.hostname != public.hostname
            or target.port != public.port
            or target.username != key.public_key.hex
            or target.password
            or target.query
            or target.fragment
            or target.path != public.path.rstrip("/") + "/" + str(project.pk)
        ):
            raise ProvisionError("Native project DSN is invalid")
        return {
            "schemaVersion": 1,
            "organization": organization_slug,
            "project": project_slug,
            "projectId": project.pk,
            "dsn": dsn,
        }


def main() -> int:
    """Keep all native errors and logs away from terminal and private export output."""
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("provision")
    export = commands.add_parser("export-dsn")
    for field in ("organization", "project", "project-name", "team"):
        export.add_argument(f"--{field}", required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    committed = False
    try:
        with Path(os.devnull).open("w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            initialize()
            if args.command == "provision":
                from django.db import transaction

                config = json.loads(
                    Path(text(os.environ.get("GLITCHTIP_PROVISIONING_CONFIG_FILE"))).read_text()
                )

                def mark_committed() -> None:
                    nonlocal committed
                    committed = True

                # Mark the commit before native callbacks, so failure status remains truthful.
                with transaction.atomic():
                    transaction.on_commit(mark_committed)
                    provision(
                        config,
                        text(os.environ.get("GLITCHTIP_OWNER_EMAIL")),
                        text(os.environ.get("GLITCHTIP_OWNER_PASSWORD")),
                    )
                result = None
            else:
                result = export_dsn(args.organization, args.project, args.project_name, args.team)
    except Exception as error:
        message = (
            "GlitchTip provisioning committed but post-commit work failed; rerun safely"
            if committed
            else "GlitchTip operation failed; no credentials or native errors displayed"
        )
        print(message, file=sys.stderr)
        try:
            details = _failure_details(error, 4096 - len(message) - 2)
        except Exception:
            details = "Failure metadata unavailable"
        print(details, file=sys.stderr)
        return 2 if committed else 1
    if result is not None:
        print(
            json.dumps(result)
        )  # Private exec stdout: caller MUST capture, never forward to terminal.
    else:
        print("GlitchTip provisioning committed and native scheduling completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
