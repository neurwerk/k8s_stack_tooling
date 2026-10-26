"""Local values writes preserve private-file modes and unrelated settings."""

import stat

import pytest
import yaml

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
