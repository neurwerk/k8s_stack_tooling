"""Validate a private native DSN export and write only its approved OpenBao field."""

from __future__ import annotations

import json
from importlib.resources import files
from urllib.parse import urlsplit
from uuid import UUID

from openbao_stack_setup.client import OpenBaoClient, OpenBaoError

ADAPTER_PATH = "glitchtip/adapter"


def provisioning_payload() -> bytes:
    """Load the same reviewed source exported by the packaged Tooling command."""
    return files("openbao_stack_setup").joinpath("glitchtip_payload.py").read_bytes()


def parse_dsn_export(output: bytes, organization: str, project: str) -> str:
    """Validate the captured private envelope without reflecting its contents."""
    try:
        return _parse_dsn_export(output, organization, project)
    except (ValueError, TypeError, UnicodeError):
        raise OpenBaoError("Private GlitchTip DSN export is invalid; contents hidden") from None


def _parse_dsn_export(output: bytes, organization: str, project: str) -> str:
    if len(output) > 8192:
        raise ValueError
    envelope = json.loads(output)
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schemaVersion", "organization", "project", "projectId", "dsn"}
        or type(envelope["schemaVersion"]) is not int
        or envelope["schemaVersion"] != 1
        or envelope["organization"] != organization
        or envelope["project"] != project
        or type(envelope["projectId"]) is not int
        or envelope["projectId"] < 1
        or not isinstance(envelope["dsn"], str)
    ):
        raise ValueError
    dsn = envelope["dsn"]
    target = urlsplit(dsn)
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or (target.port is not None and target.port < 1)
        or target.password
        or target.query
        or target.fragment
        or target.path.rsplit("/", 1)[-1] != str(envelope["projectId"])
        or any(ord(char) < 33 or ord(char) == 127 for char in dsn)
    ):
        raise ValueError
    UUID(target.username or "")
    return dsn


def store_project_dsn(client: OpenBaoClient, dsn: str) -> None:
    """CAS-write only dsn on an existing catalog record, preserving every sibling."""
    if not dsn.strip() or any(ord(char) < 33 or ord(char) == 127 for char in dsn):
        raise OpenBaoError("Invalid GlitchTip DSN; contents hidden")
    current = client.read_secret(ADAPTER_PATH)
    if current is None or "dsn" not in current.values:
        raise OpenBaoError("Reconcile the approved GlitchTip catalog before DSN handoff")
    if current.values["dsn"] == dsn:
        return
    if current.values["dsn"] != "":
        raise OpenBaoError("Stored GlitchTip DSN differs; refusing implicit rotation")
    values = dict(current.values)
    values["dsn"] = dsn
    client.write_secret(ADAPTER_PATH, values, current.version)
