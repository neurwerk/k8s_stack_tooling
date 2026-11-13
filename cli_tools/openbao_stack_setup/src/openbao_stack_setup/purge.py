"""Permanently remove a deselected add-on's scoped credentials and owned grants."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection

from openbao_stack_setup.access import apply_access, complete_access
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.reconcile import ReconciliationIdentity, plan_removed_addon_access


def removed_addon_prefix(addon: str, selected: Collection[str]) -> str:
    """Accept one exact add-on identifier, never a caller-supplied storage path."""
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", addon):
        raise OpenBaoError("Purge requires one valid add-on identifier")
    if addon in selected:
        raise OpenBaoError("Remove the add-on from the deployed selection before purging")
    return f"addons/{addon}/"


def purge_addon(
    client: OpenBaoClient,
    identity: ReconciliationIdentity,
    addon: str,
    selected: Collection[str],
    *,
    verify_selection: Callable[[], None],
) -> int:
    """Revoke grants before deleting every KV version; retries resume remaining records."""
    prefix = removed_addon_prefix(addon, selected)
    # Enumerate the whole bounded subtree before any mutation, without reading values.
    records = _records(client, prefix[:-1])
    verify_selection()
    plan = plan_removed_addon_access(client, identity, prefix)
    apply_access(client, plan)
    complete_access(client, plan)
    verify_selection()
    for path in records:
        client.purge_secret(path)
    if _records(client, prefix[:-1]):
        raise OpenBaoError("Add-on records changed during purge; stop other writers before retry")
    return len(records)


def _records(client: OpenBaoClient, prefix: str) -> tuple[str, ...]:
    pending = [(prefix, 0)]
    records = []
    while pending:
        directory, depth = pending.pop()
        if depth > 32 or len(pending) + len(records) > 10000:
            raise OpenBaoError("Add-on metadata tree exceeds supported limits")
        for key in client.list_secret_metadata(directory):
            if key.endswith("/"):
                pending.append((f"{directory}/{key[:-1]}", depth + 1))
            else:
                records.append(f"{directory}/{key}")
    return tuple(sorted(records))
