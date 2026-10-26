"""Local values writes preserve private-file modes and unrelated settings."""

import stat

import pytest
import yaml

from k8s_stack_tooling.addons.schema import parse_yaml
from k8s_stack_tooling.gateway_catalog import values
from k8s_stack_tooling.gateway_catalog.metadata import MetadataError


def test_ordered_merge_replaces_lists_but_preserves_sibling_settings(tmp_path):
    first, second = tmp_path / "defaults.yaml", tmp_path / "client.yaml"
    first.write_text("gateway:\n  enabled: true\n  models: [old]\n  keep: yes\n")
    second.write_text("gateway:\n  models: [selected]\n")
    assert values.read_values([first, second]) == {
        "gateway": {"enabled": True, "models": ["selected"], "keep": True}
    }


def test_atomic_write_preserves_permissions_and_new_file_is_private(tmp_path):
    path = tmp_path / "values.yaml"
    values.write_values(path, {"unrelated": {"keep": True}})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o640)
    values.write_values(path, {"gatewayModelMetadata": {"models": []}})
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert values.read_values([path]) == {
        "unrelated": {"keep": True},
        "gatewayModelMetadata": {"models": []},
    }


def test_failed_atomic_replace_preserves_original_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / "values.yaml"
    path.write_text("keep: true\n")

    def fail(*_):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(values.os, "replace", fail)
    with pytest.raises(OSError):
        values.write_values(path, {"new": "value"})
    assert path.read_text() == "keep: true\n"
    assert list(tmp_path.iterdir()) == [path]


def test_symlink_output_is_rejected_before_reading_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("not: [valid YAML")
    link = tmp_path / "values.yaml"
    link.symlink_to(target)
    with pytest.raises(MetadataError, match="symlink"):
        values.write_values(link, {"new": "value"})
    assert target.read_text() == "not: [valid YAML"


@pytest.mark.parametrize("text", ["[]", "null", "!!python/object/apply:os.system ['false']"])
def test_only_safe_yaml_objects_are_accepted(tmp_path, text):
    path = tmp_path / "values.yaml"
    path.write_text(text)
    with pytest.raises((MetadataError, yaml.YAMLError)):
        values.read_values([path])


def test_shared_references_round_trip_through_strict_parser_without_mutating_inputs(tmp_path):
    path = tmp_path / "values.yaml"
    path.write_text("kept: &kept\n  enabled: true\ncopy: *kept\n")
    roles = ["llm:invoke", "model:sample:invoke"]
    shared = {"roles": roles, "label": "référence"}
    additions = {"service": shared, "approval": shared, "roles": roles}

    values.write_values(path, additions)

    expected = {
        "kept": {"enabled": True},
        "copy": {"enabled": True},
        "service": {"roles": ["llm:invoke", "model:sample:invoke"], "label": "référence"},
        "approval": {"roles": ["llm:invoke", "model:sample:invoke"], "label": "référence"},
        "roles": ["llm:invoke", "model:sample:invoke"],
    }
    assert parse_yaml(path.read_bytes()) == expected
    assert additions == {key: expected[key] for key in ("service", "approval", "roles")}
    assert additions["service"] is additions["approval"] is shared
    assert shared["roles"] is additions["roles"] is roles
    assert not any(
        isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
        for token in yaml.scan(path.read_text())
    )
    # Repeated writes preserve the same materialized content, not new anchors.
    first = path.read_bytes()
    values.write_values(path, additions)
    assert path.read_bytes() == first


def test_distinct_nested_values_keep_types_and_existing_siblings(tmp_path):
    path = tmp_path / "values.yaml"
    path.write_text("gateway:\n  keep: untouched\nother: [false, null, 7]\n")
    additions = {
        "gateway": {"first": {"enabled": True}, "second": {"enabled": False}},
        "models": [{"id": "sample", "cost": 0.5}],
    }

    values.write_values(path, additions)

    assert parse_yaml(path.read_bytes()) == {
        "gateway": {
            "keep": "untouched",
            "first": {"enabled": True},
            "second": {"enabled": False},
        },
        "other": [False, None, 7],
        "models": [{"id": "sample", "cost": 0.5}],
    }
    assert additions == {
        "gateway": {"first": {"enabled": True}, "second": {"enabled": False}},
        "models": [{"id": "sample", "cost": 0.5}],
    }
