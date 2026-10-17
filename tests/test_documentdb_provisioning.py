"""Exercise failure propagation and privilege cleanup without database access."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from k8s_stack_tooling.postgres import documentdb


@pytest.fixture
def runtime(tmp_path: Path):
    client = tmp_path / "client"
    client.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, signal, subprocess, sys, time\n"
        "command = pathlib.Path(sys.argv[0]).name + ' ' + ' '.join(sys.argv[1:])\n"
        "if '--command' not in command and pathlib.Path(sys.argv[0]).name == 'psql':\n"
        "    command += sys.stdin.read()\n"
        "with open(os.environ['CALLS'], 'a') as log: log.write(command + '\\n')\n"
        "if 'pg_advisory_lock' in command:\n"
        "    with open(os.environ['CALLS'], 'a') as log: log.write('LOCK_HELD\\n')\n"
        "    result = subprocess.run(\n"
        "        ['/bin/sh', os.environ['DOCUMENTDB_PROVISIONER'], '--locked'])\n"
        "    with open(os.environ['CALLS'], 'a') as log: log.write('LOCK_RELEASED\\n')\n"
        "    sys.exit(result.returncode)\n"
        "if '/runtime/provision.js' in command and os.environ.get('HOLD_MUTATION') and (\n"
        "    os.environ.get('DOCUMENTDB_PREFLIGHT') != 'true'):\n"
        "    if os.environ.get('IGNORE_TERM'): signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    pathlib.Path(os.environ['HOLD_MUTATION']).touch()\n"
        "    time.sleep(30)\n"
        "if os.environ.get('FAIL_ON') and os.environ['FAIL_ON'] in command and (\n"
        "    os.environ.get('DOCUMENTDB_PREFLIGHT') != 'true'\n"
        "    or os.environ.get('FAIL_PREFLIGHT')):\n"
        "    print('sensitive-password-driver-error', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "if 'SELECT EXISTS' in command: print('f')\n"
    )
    client.chmod(0o700)
    for name in ("psql", "mongosh"):
        (tmp_path / name).symlink_to(client)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "HOME": str(tmp_path),
        "PGUSER": "documentdb",
        "PGDATABASE": "postgres",
        "DOCUMENTDB_USER": "synthetic_product",
        "DOCUMENTDB_DATABASE": "SyntheticProduct",
        "DOCUMENTDB_ADMIN_USER": "operations_admin",
        "DOCUMENTDB_PASSWORD": "synthetic-app-password",
        "DOCUMENTDB_ADMIN_PASSWORD": "synthetic-admin-password",
        "DOCUMENTDB_HOST": "database.example.test",
        "DOCUMENTDB_PORT": "10260",
        "DOCUMENTDB_CA_FILE": "/synthetic/ca.crt",
    }
    return env, tmp_path / "calls"


def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(Path(documentdb.__file__).with_name("provision.sh")), "--locked"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_mongo_identity_and_probe_contracts():
    subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("documentdb.test.cjs"))],
        check=True,
        timeout=10,
    )


def test_successful_synthetic_product_revokes_before_verification(runtime):
    env, calls = runtime
    result = run(env)
    assert result.returncode == 0
    commands = calls.read_text()
    assert commands.index("NOCREATEROLE") < commands.index("/runtime/verify.js")
    assert "--tls --tlsCAFile=/synthetic/ca.crt" in commands
    assert env["DOCUMENTDB_PASSWORD"] not in commands


@pytest.mark.parametrize(
    "failure",
    [
        "ALTER ROLE operations_admin CREATEROLE",
        "GRANT documentdb_admin_role TO operations_admin WITH ADMIN OPTION",
        "/runtime/provision.js",
        "REVOKE ADMIN OPTION",
        "ALTER ROLE operations_admin NOCREATEROLE",
        "/runtime/verify.js",
        "SELECT current_user",
    ],
)
def test_failures_never_report_success_and_both_revokes_are_attempted(runtime, failure):
    env, calls = runtime
    result = run({**env, "FAIL_ON": failure})
    assert result.returncode != 0
    commands = calls.read_text()
    assert "REVOKE ADMIN OPTION" in commands
    assert "ALTER ROLE operations_admin NOCREATEROLE" in commands
    assert "sensitive-password-driver-error" not in result.stdout + result.stderr
    assert "verified" not in result.stdout


@pytest.mark.parametrize("user", ["documentdb", "operations_admin", "pg_test", "bad;sql", ""])
def test_unsafe_users_fail_before_database_access(runtime, user):
    env, calls = runtime
    result = run({**env, "DOCUMENTDB_USER": user})
    assert result.returncode != 0
    assert not calls.exists()


def test_conflicting_identity_is_rejected_before_privilege_changes(runtime):
    env, calls = runtime
    result = run({**env, "FAIL_ON": "/runtime/provision.js", "FAIL_PREFLIGHT": "true"})
    assert result.returncode != 0
    commands = calls.read_text()
    assert "ALTER ROLE operations_admin CREATEROLE" not in commands
    assert "GRANT documentdb_admin_role TO operations_admin" not in commands


def test_outer_entrypoint_propagates_locked_child_failure(runtime):
    env, calls = runtime
    result = subprocess.run(
        ["/bin/sh", str(Path(documentdb.__file__).with_name("provision.sh"))],
        env={**env, "FAIL_ON": "/runtime/provision.js"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "verified" not in result.stdout
    commands = calls.read_text()
    assert commands.index("NOCREATEROLE") < commands.index("LOCK_RELEASED")


@pytest.mark.parametrize("ignore_term", ["", "true"])
def test_termination_revokes_before_releasing_instance_lock(runtime, tmp_path, ignore_term):
    env, calls = runtime
    started = tmp_path / "mutation-started"
    process = subprocess.Popen(
        ["/bin/sh", str(Path(documentdb.__file__).with_name("provision.sh"))],
        env={**env, "HOLD_MUTATION": str(started), "IGNORE_TERM": ignore_term},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode != 0
        assert "verified" not in stdout
        assert "synthetic-app-password" not in stdout + stderr
        commands = calls.read_text()
        assert commands.index("REVOKE ADMIN OPTION") < commands.index("LOCK_RELEASED")
        assert commands.index("NOCREATEROLE") < commands.index("LOCK_RELEASED")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
