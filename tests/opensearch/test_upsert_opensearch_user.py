"""Tests for OpenSearch user provisioning."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from k8s_stack_tooling.opensearch import upsert_opensearch_user


def _set_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the environment required by the provisioning entry point."""
    values = {
        "OPENSEARCH_INTERNAL_URL": "https://opensearch:9200",
        "OPENSEARCH_ADMIN_USER": "admin",
        "OPENSEARCH_ADMIN_PASSWORD": "admin-password",
        "INGEST_USER": "studio-logs-read",
        "INGEST_ROLE": "studio_logs_read_role",
        "INGEST_PASSWORD": "eso-managed-password",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_upserts_user_with_eso_managed_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the required local Secret value directly to OpenSearch."""
    _set_required_environment(monkeypatch)
    with patch.object(upsert_opensearch_user, "_upsert_user") as upsert:
        upsert_opensearch_user.main()

    upsert.assert_called_once_with(
        "https://opensearch:9200",
        ("admin", "admin-password"),
        "studio-logs-read",
        "eso-managed-password",
        ["studio_logs_read_role"],
    )


def test_rejects_empty_ingest_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail before calling OpenSearch when the local Secret value is empty."""
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("INGEST_PASSWORD", "")
    with (
        patch.object(upsert_opensearch_user, "_upsert_user") as upsert,
        pytest.raises(SystemExit),
    ):
        upsert_opensearch_user.main()

    upsert.assert_not_called()


def test_requires_ingest_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require the ESO-populated environment variable."""
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("INGEST_PASSWORD")
    with (
        patch.object(upsert_opensearch_user, "_upsert_user") as upsert,
        pytest.raises(SystemExit),
    ):
        upsert_opensearch_user.main()

    upsert.assert_not_called()
