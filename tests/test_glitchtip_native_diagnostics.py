"""Prove diagnostics retain useful locations without formatting private exceptions."""

import importlib.util
import io
import sys
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "native_diagnostics", Path(__file__).parent / "glitchtip_native/diagnostics.py"
)
assert spec is not None and spec.loader is not None
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)

CANARY = "https://operator:credential-canary@database.example.invalid/private-request"


class PrivateError(Exception):
    def __str__(self):
        raise AssertionError("Exception string formatting is forbidden")

    def __repr__(self):
        raise AssertionError("Exception repr formatting is forbidden")

    def __getattribute__(self, name):
        if name == "args":
            raise AssertionError("Exception argument inspection is forbidden")
        return super().__getattribute__(name)


def credential_failure():
    private_local = CANARY
    try:
        error = PrivateError(private_local)
        error.add_note(private_local)
        raise error
    except PrivateError as cause:
        raise RuntimeError(private_local) from cause


def test_exception_types_and_locations_survive_without_messages_notes_locals_or_paths():
    try:
        credential_failure()
    except RuntimeError:
        metadata = diagnostics.exception_metadata(sys.exc_info())
    assert "root: RuntimeError" in metadata
    assert "cause: PrivateError" in metadata
    assert "test_glitchtip_native_diagnostics.py:credential_failure:" in metadata
    for forbidden in (
        CANARY,
        "credential-canary",
        "database.example.invalid",
        "/home/",
        "private_local",
    ):
        assert forbidden not in metadata


def test_unittest_errors_and_subtests_never_use_raw_exception_or_parameter_formatting():
    class SyntheticCase(unittest.TestCase):
        def test_assertion_failure(self):
            self.fail(CANARY)

        def test_error(self):
            credential_failure()

        def test_subtest(self):
            with self.subTest(private_parameter=CANARY):
                credential_failure()

        def test_unapproved_subcase(self):
            with self.subTest(conflict=CANARY):
                credential_failure()

        def test_approved_subcase(self):
            with self.subTest(conflict="duplicate-alert"):
                credential_failure()

    output = io.StringIO()
    result = unittest.TextTestRunner(
        stream=output, verbosity=0, resultclass=diagnostics.SafeResult
    ).run(unittest.defaultTestLoader.loadTestsFromTestCase(SyntheticCase))
    assert len(result.errors) == 4
    assert len(result.failures) == 1
    rendered = output.getvalue() + "\n".join(
        diagnostics.safe_test_name(test) + "\n" + metadata
        for test, metadata in result.errors + result.failures
    )
    assert "SyntheticCase.test_error" in rendered
    assert "SyntheticCase.test_subtest" in rendered
    assert "SyntheticCase.test_approved_subcase [subcase=duplicate-alert]" in rendered
    assert "SyntheticCase.test_unapproved_subcase\n" in rendered
    assert "PrivateError" in rendered and "credential_failure:" in rendered
    for forbidden in (CANARY, "credential-canary", "private_parameter", "/home/"):
        assert forbidden not in rendered


def test_exception_chain_cycles_and_depth_are_bounded():
    first = RuntimeError(CANARY)
    first.__context__ = LookupError(CANARY)
    previous = first
    for _ in range(20):
        following = ValueError(CANARY)
        previous.__cause__ = following
        previous = following
    previous.__cause__ = first
    metadata = diagnostics.exception_metadata((RuntimeError, first, None))
    assert len(metadata.splitlines()) == 5
    assert "additional exception links omitted" in metadata
    assert "context: LookupError" in metadata
    assert CANARY not in metadata


def test_project_callsite_survives_deep_library_trace_without_paths_or_values():
    namespace = {}
    exec(
        compile(
            "def project_caller(callback):\n    callback()\n", "/private/path/payload.py", "exec"
        ),
        namespace,
    )

    def library_frames(depth):
        if depth:
            return library_frames(depth - 1)
        raise PrivateError(CANARY)

    try:
        namespace["project_caller"](lambda: library_frames(20))
    except PrivateError:
        metadata = diagnostics.exception_metadata(sys.exc_info())
    assert "payload.py:project_caller:2" in metadata
    assert "test_glitchtip_native_diagnostics.py:library_frames:" in metadata
    assert len(metadata.splitlines()) <= 12
    assert "/private/path" not in metadata
    assert CANARY not in metadata
