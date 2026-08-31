"""Idempotently create an OpenSearch ingest user.

Run as a Helm post-install/post-upgrade Job inside the cluster.
Creates/updates an internal user carrying the ingest role as a backend role,
using ``INGEST_PASSWORD`` from a local Secret managed by External Secrets Operator
and backed by OpenBao.

The role itself (permissions) is owned by the Helm chart's security bootstrap
(securityadmin.sh); this tool never touches role definitions.

Fully parameterized via environment variables -- no app-specific hardcoding.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import requests

from k8s_stack_tooling.api.http import log
from k8s_stack_tooling.api.kubernetes import run_with_reconciliation_lock

# ── OpenSearch REST API helpers (using requests directly) ──────────────


def _opensearch_request(
    method: str,
    url: str,
    auth: tuple[str, str] | None = None,
    json_body: Any = None,
) -> requests.Response:
    """Make a raw HTTP request to OpenSearch and return the Response."""
    kwargs: dict[str, Any] = {"timeout": 30}
    if auth:
        kwargs["auth"] = auth
    if json_body is not None:
        kwargs["json"] = json_body
    try:
        resp = requests.request(method, url, **kwargs)
        return resp
    except requests.exceptions.RequestException as exc:
        log(f"ERROR: Request to {method} {url} failed: {exc}")
        raise SystemExit(1) from exc


def _upsert_user(
    base_url: str,
    auth: tuple[str, str],
    username: str,
    password: str,
    backend_roles: list[str],
) -> None:
    """Create or update an OpenSearch internal user with backend roles."""
    url = f"{base_url}/_plugins/_security/api/internalusers/{username}"
    body = {
        "password": password,
        "backend_roles": backend_roles,
        "attributes": {},
    }
    log(f"Upserting OpenSearch internal user '{username}'...")
    resp = _opensearch_request("PUT", url, auth=auth, json_body=body)
    if resp.status_code in (200, 201):
        log(f"  User '{username}' upserted successfully (HTTP {resp.status_code}).")
    else:
        log(f"ERROR: User upsert returned HTTP {resp.status_code}: {resp.text[:300]}")
        raise SystemExit(1)


# ── Main ──────────────────────────────────────────────────────────────────


def _reconcile() -> None:
    base_url: str = os.environ["OPENSEARCH_INTERNAL_URL"].rstrip("/")
    admin_user: str = os.environ["OPENSEARCH_ADMIN_USER"]
    admin_pass: str = os.environ["OPENSEARCH_ADMIN_PASSWORD"]
    ingest_user: str = os.environ["INGEST_USER"]
    ingest_role: str = os.environ.get("INGEST_ROLE", "fluent_bit_ingest_role")
    ingest_password: str = os.environ.get("INGEST_PASSWORD", "")
    if not ingest_password:
        log("ERROR: INGEST_PASSWORD must not be empty.")
        raise SystemExit(1)

    auth = (admin_user, admin_pass)

    log("=== OpenSearch ingest user setup started ===")
    log(f"  OPENSEARCH_URL = {base_url}")
    log(f"  INGEST_USER    = {ingest_user}")
    log(f"  INGEST_ROLE    = {ingest_role}")

    _upsert_user(
        base_url,
        auth,
        ingest_user,
        ingest_password,
        [ingest_role],
    )

    log("=== OpenSearch ingest user setup complete ===")
    log(f"  User: {ingest_user}")
    log(f"  Role: {ingest_role}")


def main() -> None:
    """Reconcile an OpenSearch user while holding the optional shared Lease."""
    run_with_reconciliation_lock(_reconcile)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        log(f"UNEXPECTED ERROR: {exc}")
        log(f"Type: {type(exc).__name__}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
