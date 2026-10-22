"""Run in unchanged GlitchTip 6.2.6, against an allocated disposable PG18.6 DB.

First run guard.py successfully, then upstream bin/run-migrate.sh, then:
  GLITCHTIP_NATIVE_TEST_CONFIRM=DISPOSABLE_DATABASE \
    PYTHONPATH=/code DJANGO_SETTINGS_MODULE=glitchtip.settings \
    python /tests/acceptance.py /tests/payload.py

The database NAME must start glitchtip_provisioning_test_. No database/role,
namespace, Pod or Secret is created or removed by this harness. Tests leave their
synthetic rows in that disposable database for the allocating operator to clean up.
No pytest, Tooling install, worker, SMTP server or outbound probe is needed.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import guard as native_guard
from diagnostics import SafeResult, exception_metadata, safe_test_name

logging.disable(logging.CRITICAL)
SOURCE = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("reviewed_glitchtip_payload", SOURCE)
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)
try:
    with Path(os.devnull).open("w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        native_guard.verify()
        payload.initialize()
except Exception:
    print(exception_metadata(sys.exc_info()), file=sys.stderr)
    raise SystemExit("Native acceptance startup failed; private details hidden") from None

from allauth.account.models import EmailAddress  # noqa: E402
from apps.alerts.models import AlertRecipient, ProjectAlert  # noqa: E402
from apps.organizations_ext.constants import OrganizationUserRole  # noqa: E402
from apps.organizations_ext.models import Organization, OrganizationUser  # noqa: E402
from apps.projects.models import Project, ProjectKey  # noqa: E402
from apps.teams.models import Team  # noqa: E402
from apps.uptime.models import Monitor, MonitorCheck, StatusPage  # noqa: E402
from apps.uptime.tasks import perform_checks  # noqa: E402
from apps.users.models import User  # noqa: E402
from django.db import DatabaseError, connection, transaction  # noqa: E402
from django.db.migrations.loader import MigrationLoader  # noqa: E402
from django.test.utils import CaptureQueriesContext, override_settings  # noqa: E402
from ninja.errors import ValidationError as APIValidationError  # noqa: E402
from pydantic import ValidationError as SchemaValidationError  # noqa: E402


def preflight():
    native_guard.verify()


def fixture():
    suffix = uuid.uuid4().hex[:12]
    config = {
        "organization": {"slug": f"test-{suffix}", "name": f"Test {suffix}"},
        "team": {"slug": "platform"},
        "project": {"slug": "services", "name": "Services"},
        "alert": {"name": "Managed alert", "alertRecipients": [{"recipientType": "email"}]},
        "monitors": [{"name": "Managed probe", "url": "https://example.com/health"}],
        "statusPage": {"name": "Test health", "isPublic": True, "monitorNames": ["Managed probe"]},
    }
    return config, f"owner-{suffix}@example.invalid", uuid.uuid4().hex


def graph(config):
    organization = Organization.objects.get(slug=config["organization"]["slug"])
    project = Project.objects.get(organization=organization, slug="services")
    return organization, project


def snapshot():
    models = [
        User,
        Organization,
        OrganizationUser,
        Team,
        Project,
        ProjectKey,
        ProjectAlert,
        AlertRecipient,
        Monitor,
        StatusPage,
        Team.members.through,
        Team.projects.through,
        StatusPage.monitors.through,
        EmailAddress,
    ]
    return [list(model.objects.order_by("pk").values()) for model in models]


class NativeAcceptance(unittest.TestCase):
    def test_private_monitor_requires_both_explicit_destination_and_native_permission(self):
        config, email, password = fixture()
        config["monitors"][0]["url"] = "http://127.0.0.1:8000/api/0/internal/health/"
        before = snapshot()
        with override_settings(GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS=True):
            with self.assertRaises(
                (payload.ProvisionError, APIValidationError, SchemaValidationError)
            ):
                payload.provision(config, email, password)
        self.assertEqual(snapshot(), before)
        config["monitors"][0]["allowPrivateIPs"] = True
        with override_settings(GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS=False):
            with self.assertRaises(payload.ProvisionError):
                payload.provision(config, email, password)
        self.assertEqual(snapshot(), before)
        with override_settings(GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS=True):
            payload.provision(config, email, password)
        organization, _ = graph(config)
        self.assertEqual(Monitor.objects.filter(organization=organization).count(), 1)

    def test_ambiguous_or_missing_active_keys_fail_without_replacement(self):
        config, email, password = fixture()
        payload.provision(config, email, password)
        organization, project = graph(config)
        ProjectKey.objects.create(project=project)
        for active in (True, False):
            if not active:
                ProjectKey.objects.filter(project=project).update(is_active=False)
            before = snapshot()
            with self.assertRaises(payload.ProvisionError):
                payload.provision(config, email, password)
            with self.assertRaises(payload.ProvisionError):
                payload.export_dsn(organization.slug, project.slug, project.name, "platform")
            self.assertEqual(snapshot(), before)

    def test_fresh_retry_preserves_native_keys_password_health_history_and_manual_associations(
        self,
    ):
        config, email, password = fixture()
        task_type = type(perform_checks)
        original_enqueue = task_type.enqueue
        observed = []

        def enqueue(task, *args, **kwargs):
            if task is perform_checks:
                observed.append(connection.in_atomic_block)
            return original_enqueue(task, *args, **kwargs)

        with patch.object(task_type, "enqueue", new=enqueue):
            payload.provision(config, email, password)
        self.assertTrue(observed)
        self.assertFalse(any(observed))
        organization, project = graph(config)
        user = User.objects.get(email=email)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(organization.is_owner(user))
        self.assertTrue(user.check_password(password))
        key = ProjectKey.objects.get(project=project, is_active=True)
        self.assertTrue(key.public_key)
        self.assertEqual(ProjectAlert.objects.filter(project=project, quantity=1).count(), 1)
        team = Team.objects.get(organization=organization, slug="platform")
        other_team = Team.objects.create(organization=organization, slug="manual")
        project.teams.add(other_team)
        other_monitor = Monitor.objects.create(
            organization=organization,
            project=project,
            name="Manual monitor",
            monitor_type="GET",
            url="https://example.com/manual",
        )
        page = StatusPage.objects.get(organization=organization, name="Test health")
        page.monitors.add(other_monitor)
        monitor = Monitor.objects.get(organization=organization, name="Managed probe")
        Monitor.objects.filter(pk=monitor.pk).update(cached_is_up=True, cached_consecutive_down=2)
        check = MonitorCheck.objects.create(
            organization=organization, monitor=monitor, is_up=True, is_change=False
        )
        before = snapshot()
        payload.provision(config, email, "different-not-a-reset")
        self.assertEqual(snapshot(), before)
        self.assertEqual(ProjectKey.objects.get(project=project, is_active=True).pk, key.pk)
        self.assertTrue(MonitorCheck.objects.filter(pk=check.pk).exists())
        self.assertTrue(team.members.filter(user=user).exists())
        self.assertTrue(page.monitors.filter(pk=other_monitor.pk).exists())
        self.assertTrue(page.monitors.filter(pk=monitor.pk).exists())

    def test_ownership_nonunique_names_and_cross_org_conflicts_preserve_database(self):
        for conflict in (
            "existing-user",
            "owner-role",
            "duplicate-alert",
            "duplicate-monitor",
            "cross-team",
            "cross-page",
        ):
            with self.subTest(conflict=conflict):
                config, email, password = fixture()
                if conflict == "existing-user":
                    User.objects.create_user(email=email, password=password)
                else:
                    payload.provision(config, email, password)
                    organization, project = graph(config)
                    if conflict == "owner-role":
                        OrganizationUser.objects.filter(
                            organization=organization, user__email=email
                        ).update(role=OrganizationUserRole.MEMBER)
                    elif conflict == "duplicate-alert":
                        ProjectAlert.objects.create(project=project, name=config["alert"]["name"])
                    elif conflict == "duplicate-monitor":
                        Monitor.objects.create(
                            organization=organization,
                            project=project,
                            name="Managed probe",
                            monitor_type="GET",
                            url="https://example.com/other",
                        )
                    else:
                        other_config, other_email, other_password = fixture()
                        payload.provision(other_config, other_email, other_password)
                        other_org, other_project = graph(other_config)
                        if conflict == "cross-team":
                            team = Team.objects.get(organization=organization, slug="platform")
                            before = snapshot()
                            # The native insert trigger forbids this graph before provisioning.
                            # Catch outside the savepoint so the connection remains usable.
                            with self.assertRaises(DatabaseError), transaction.atomic():
                                team.projects.add(other_project)
                            self.assertFalse(team.projects.filter(pk=other_project.pk).exists())
                            self.assertEqual(snapshot(), before)
                            continue
                        else:
                            StatusPage.objects.get(organization=organization).monitors.add(
                                Monitor.objects.get(organization=other_org)
                            )
                before = snapshot()
                with self.assertRaises(payload.ProvisionError):
                    payload.provision(config, email, password)
                self.assertEqual(snapshot(), before)

    def test_late_precommit_failure_rolls_back_new_objects(self):
        config, email, password = fixture()
        payload.provision(config, email, password)
        config["alert"]["name"] = "New alert rolled back"
        config["monitors"][0]["name"] = "New monitor rolled back"
        config["statusPage"]["monitorNames"] = ["New monitor rolled back"]
        config["statusPage"]["isPublic"] = (
            False  # Existing page now conflicts, after earlier creates.
        )
        before = snapshot()
        with self.assertRaises(payload.ProvisionError):
            payload.provision(config, email, password)
        self.assertEqual(snapshot(), before)

    def test_readonly_export_uses_native_public_dsn_and_never_generates_keys(self):
        config, email, password = fixture()
        payload.provision(config, email, password)
        organization, project = graph(config)
        before = snapshot()
        with CaptureQueriesContext(connection) as queries:
            exported = payload.export_dsn(organization.slug, project.slug, project.name, "platform")
        self.assertEqual(exported["projectId"], project.pk)
        self.assertEqual(exported["dsn"], ProjectKey.objects.get(project=project).get_dsn())
        for query in queries:
            self.assertFalse(
                query["sql"]
                .lstrip()
                .upper()
                .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            payload.lock(organization.slug, read_only=True)
            User.objects.create_user(
                email=f"must-not-write-{uuid.uuid4().hex}@example.invalid", password="never-written"
            )
        with self.assertRaises(payload.ProvisionError):
            payload.export_dsn(organization.slug, project.slug, "Wrong project", "platform")
        self.assertEqual(snapshot(), before)
        command = [
            sys.executable,
            "-",
            "export-dsn",
            "--organization",
            organization.slug,
            "--project",
            project.slug,
            "--project-name",
            project.name,
            "--team",
            "platform",
        ]
        process = subprocess.run(
            command,
            input=SOURCE.read_bytes(),
            capture_output=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout), exported)
        self.assertEqual(snapshot(), before)

    def test_postcommit_failure_has_distinct_redacted_outcome_and_retry_recovers(self):
        config, email, password = fixture()
        task_type = type(perform_checks)
        original_enqueue = task_type.enqueue

        def fail_selected_enqueue(task, *args, **kwargs):
            if task is perform_checks:
                raise RuntimeError("sensitive-native-error")
            return original_enqueue(task, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            values = {
                "GLITCHTIP_PROVISIONING_CONFIG_FILE": str(path),
                "GLITCHTIP_OWNER_EMAIL": email,
                "GLITCHTIP_OWNER_PASSWORD": password,
            }
            output = io.StringIO()
            with (
                patch.dict(os.environ, values),
                patch.object(sys, "argv", [str(SOURCE), "provision"]),
                patch.object(task_type, "enqueue", new=fail_selected_enqueue),
                redirect_stdout(output),
                redirect_stderr(output),
            ):
                result = payload.main()
            self.assertEqual(result, 2)
            self.assertIn("committed", output.getvalue())
            self.assertNotIn(password, output.getvalue())
            self.assertNotIn("sensitive-native-error", output.getvalue())
        before = snapshot()
        payload.provision(config, email, password)
        self.assertEqual(snapshot(), before)

    def test_wrong_version_pending_migrations_and_blank_credentials_fail_before_mutation(self):
        before = snapshot()
        with (
            override_settings(GLITCHTIP_VERSION="incompatible"),
            self.assertRaises(payload.ProvisionError),
        ):
            payload.initialize()
        leaf = MigrationLoader(connection).graph.leaf_nodes("teams")
        self.assertEqual(len(leaf), 1)
        import apps.teams.migrations

        with tempfile.TemporaryDirectory() as directory:
            package = "native_pending_" + uuid.uuid4().hex
            destination = Path(directory) / package
            shutil.copytree(Path(apps.teams.migrations.__file__).parent, destination)
            (destination / "9999_native_acceptance.py").write_text(
                "from django.db import migrations\nclass Migration(migrations.Migration):\n"
                f"    dependencies = [{leaf[0]!r}]\n    operations = []\n"
            )
            sys.path.insert(0, directory)
            try:
                with (
                    override_settings(MIGRATION_MODULES={"teams": package}),
                    self.assertRaises(payload.ProvisionError),
                ):
                    payload.initialize()
            finally:
                sys.path.remove(directory)
        config, email, password = fixture()
        for blank in ("", "  "):
            with self.assertRaises(payload.ProvisionError):
                payload.provision(config, email, blank)
        self.assertEqual(snapshot(), before)


def main():
    try:
        preflight()
        # Keep fixture credentials, native exception values and DSNs out of Job logs.
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = unittest.TextTestRunner(
                stream=output, verbosity=0, resultclass=SafeResult
            ).run(unittest.defaultTestLoader.loadTestsFromTestCase(NativeAcceptance))
        print(
            f"Native GlitchTip acceptance: ran={result.testsRun} "
            f"failures={len(result.failures)} errors={len(result.errors)}"
        )
        for test, metadata in result.failures + result.errors:
            print(f"Failed native case: {safe_test_name(test)}")
            print(metadata)
        return 0 if result.wasSuccessful() else 1
    except Exception:
        print(
            "Native GlitchTip acceptance preflight failed; private connection details hidden",
            file=sys.stderr,
        )
        print(exception_metadata(sys.exc_info()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
