from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fake import FakeSession
from kubernetes.client.exceptions import ApiException

from openbao_stack_setup.catalog import contextforge_oauth_policy, namespace_policy
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.cluster import Cluster, ClusterError
from openbao_stack_setup.credentials import (
    plan_bootstrap_passwords,
    reconcile_internal_credentials,
)


def test_contextforge_selection_fails_closed() -> None:
    cluster = Cluster.__new__(Cluster)
    cluster.core = MagicMock()
    cluster.core.read_namespaced_config_map.side_effect = ApiException(status=404)
    assert cluster.contextforge_admin_email() is None
    cluster.core.read_namespaced_config_map.side_effect = ApiException(status=403)
    with pytest.raises(ClusterError, match="HTTP 403"):
        cluster.contextforge_admin_email()
    cluster.core.read_namespaced_config_map.side_effect = None
    for values in (
        "contextforge: []",
        "contextforge: {enabled: 'true'}",
        "contextforge: {enabled: true}",
    ):
        cluster.core.read_namespaced_config_map.return_value = SimpleNamespace(
            data={"values.yaml": values}
        )
        with pytest.raises(ClusterError):
            cluster.contextforge_admin_email()
    cluster.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={
            "values.yaml": "contextforge: {enabled: true, platformAdminEmail: recovery@example.com}"
        }
    )
    assert cluster.contextforge_admin_email() == "recovery@example.com"


def test_credentials_are_opt_in_preserved_and_copy_conflicts_fail(tmp_path: Path) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca.crt", session)
    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))
    assert "contextforge/internal" not in session.secrets
    reconcile_internal_credentials(api, {}, contextforge_admin_email="recovery@example.com")
    original = {path: dict(record.values) for path, record in session.secrets.items()}
    source = original["contextforge/internal"]
    assert len(set(source.values())) == len(source)
    assert (
        original["infra-postgres-operations/internal"]["contextforgePassword"]
        == source["postgresqlPassword"]
    )
    assert (
        reconcile_internal_credentials(
            api, {}, contextforge_admin_email="recovery@example.com"
        ).changed_paths
        == ()
    )
    reconcile_internal_credentials(api, {})
    assert {path: record.values for path, record in session.secrets.items()} == original
    destination = session.secrets["infra-postgres-operations/internal"].values
    destination["contextforgePassword"] = "conflicting-copy"
    with pytest.raises(OpenBaoError, match="contextforgePassword"):
        reconcile_internal_credentials(api, {}, contextforge_admin_email="recovery@example.com")
    assert destination["contextforgePassword"] == "conflicting-copy"
    assert session.secrets["contextforge/internal"].values == source


def test_native_oauth_and_eso_permissions_are_isolated() -> None:
    def capabilities(policy: str, path: str) -> set[str]:
        return {
            capability
            for pattern, rules in re.findall(r'path "([^"]+)" \{([^}]+)\}', policy)
            if fnmatchcase(path, pattern)
            for capability in re.findall(r'"([^\"]+)"', rules)
        }

    native = contextforge_oauth_policy()
    eso = namespace_policy("contextforge")
    token_path = "secret/data/contextforge/oauth/shared/0123456789abcdef/user%40example.com"
    metadata = token_path.replace("/data/", "/metadata/")
    assert capabilities(native, token_path) >= {"create", "read", "update", "delete"}
    assert capabilities(native, metadata) == {"delete"}
    assert capabilities(eso, token_path) == capabilities(eso, metadata) == set()
    assert capabilities(eso, "secret/data/contextforge/internal") == {"read"}
    for path in (
        "secret/data/contextforge/internal",
        "secret/data/forgejo/internal",
        "auth/token/create",
    ):
        assert capabilities(native, path) == set()
