"""Protect the standalone payload's failure output without a native database run."""

import importlib.util
import logging
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from k8s_stack_tooling.glitchtip import provision as exporter

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src/k8s_stack_tooling/glitchtip/payload.py"
spec = importlib.util.spec_from_file_location("standalone_glitchtip_payload", SOURCE)
assert spec is not None and spec.loader is not None
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)

CANARY = "https://operator:credential-canary@database.example.invalid/private-request"
SQL_CANARY = "SELECT secret_canary FROM private_canary WHERE token = 'hidden'"


class PrivateError(Exception):
    def __str__(self):
        raise AssertionError("Exception string formatting is forbidden")

    def __repr__(self):
        raise AssertionError("Exception repr formatting is forbidden")

    def __getattribute__(self, name):
        if name == "args":
            raise AssertionError("Exception arguments must not be inspected")
        return super().__getattribute__(name)


def private_failure():
    private_local = CANARY
    try:
        error = PrivateError(private_local, SQL_CANARY)
        error.add_note(private_local)
        error.add_note(SQL_CANARY)
        raise error
    except PrivateError as cause:
        raise RuntimeError(private_local) from cause


@pytest.mark.parametrize("after_commit", [False, True])
def test_main_redacts_exception_values_and_preserves_failure_classification(
    monkeypatch, tmp_path, capsys, after_commit
):
    callbacks = []

    @contextmanager
    def atomic():
        yield
        for callback in callbacks:
            callback()
        private_failure()

    database = ModuleType("django.db")
    setattr(database, "transaction", SimpleNamespace(atomic=atomic, on_commit=callbacks.append))
    monkeypatch.setitem(sys.modules, "django", ModuleType("django"))
    monkeypatch.setitem(sys.modules, "django.db", database)
    monkeypatch.setattr(payload, "initialize", lambda: None)

    def provision(*args):
        if not after_commit:
            private_failure()

    monkeypatch.setattr(payload, "provision", provision)
    config = tmp_path / "private-config.json"
    config.write_text("{}")
    monkeypatch.setenv("GLITCHTIP_PROVISIONING_CONFIG_FILE", str(config))
    monkeypatch.setenv("GLITCHTIP_OWNER_EMAIL", "synthetic@example.invalid")
    monkeypatch.setenv("GLITCHTIP_OWNER_PASSWORD", CANARY)
    monkeypatch.setattr(sys, "argv", ["glitchtip.py", "provision"])
    previous_logging = logging.root.manager.disable
    try:
        assert payload.main() == (2 if after_commit else 1)
    finally:
        logging.disable(previous_logging)
    output = capsys.readouterr()
    assert output.out == ""
    assert "root: RuntimeError" in output.err
    assert "cause: PrivateError" in output.err
    assert "payload.py:main:" in output.err
    assert "test_glitchtip_production_diagnostics.py:private_failure:" in output.err
    assert len(output.err.encode()) <= 4096
    expected = (
        "GlitchTip provisioning committed but post-commit work failed; rerun safely"
        if after_commit
        else "GlitchTip operation failed; no credentials or native errors displayed"
    )
    assert output.err.startswith(expected + "\n")
    for forbidden in (
        CANARY,
        "credential-canary",
        "database.example.invalid",
        str(tmp_path),
        "private_local",
        "private-config.json",
        SQL_CANARY,
    ):
        assert forbidden not in output.err


def test_total_failure_output_is_bounded_even_with_long_frames_and_exception_cycles(
    monkeypatch, capsys
):
    function_name = "frame_" + "x" * 120
    namespace: dict[str, object] = {"PrivateError": PrivateError, "CANARY": CANARY}
    program = (
        f"def {function_name}(depth):\n"
        f"    if depth: return {function_name}(depth - 1)\n"
        "    raise PrivateError(CANARY)\n"
    )
    exec(compile(program, "/private-path-canary/" + "m" * 80 + ".py", "exec"), namespace)
    frame_function = cast(Callable[[int], None], namespace[function_name])
    errors = []
    for _ in range(8):
        try:
            frame_function(20)
        except PrivateError as error:
            errors.append(error)
    for index, error in enumerate(errors):
        error.__cause__ = errors[(index + 1) % len(errors)]
        error.__context__ = errors[(index + 2) % len(errors)]

    def fail_initialize():
        raise errors[0]

    monkeypatch.setattr(payload, "initialize", fail_initialize)
    monkeypatch.setattr(sys, "argv", ["glitchtip.py", "provision"])
    previous_logging = logging.root.manager.disable
    try:
        assert payload.main() == 1
    finally:
        logging.disable(previous_logging)
    output = capsys.readouterr()
    assert output.out == ""
    assert len(output.err.encode()) <= 4096
    assert "additional diagnostic details omitted" in output.err
    assert "payload.py:main:" in output.err
    assert output.err.count(": PrivateError") <= 4
    assert CANARY not in output.err
    assert "private-path-canary" not in output.err


def test_tooling_export_and_standalone_stack_setup_resource_are_identical(monkeypatch, tmp_path):
    destination = tmp_path / "glitchtip.py"
    monkeypatch.setattr(
        sys, "argv", ["provision-glitchtip", "export", "--output", str(destination)]
    )
    assert exporter.main() == 0
    resource = ROOT / "cli_tools/openbao_stack_setup/src/openbao_stack_setup/glitchtip_payload.py"
    assert destination.read_bytes() == resource.read_bytes() == SOURCE.read_bytes()


def test_diagnostic_formatter_failure_cannot_expose_the_original_exception(monkeypatch, capsys):
    monkeypatch.setattr(payload, "initialize", private_failure)
    monkeypatch.setattr(payload, "_failure_details", lambda *args: private_failure())
    monkeypatch.setattr(sys, "argv", ["glitchtip.py", "provision"])
    previous_logging = logging.root.manager.disable
    try:
        assert payload.main() == 1
    finally:
        logging.disable(previous_logging)
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        "GlitchTip operation failed; no credentials or native errors displayed\n"
        "Failure metadata unavailable\n"
    )
