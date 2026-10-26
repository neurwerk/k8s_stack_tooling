"""Payload resources are data, and neither source nor destination may escape."""

import os
import stat

import pytest

from k8s_stack_tooling.api.payloads import PayloadError, stage_payloads


def source_tree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "native.py").write_text("raise RuntimeError('must never execute')\n")
    return source


def test_inert_read_only_staging_and_identical_retry(tmp_path):
    source = source_tree(tmp_path)
    destination = tmp_path / "work" / "payloads"
    (payload,) = stage_payloads(source, ["native.py"], destination=destination)
    assert payload.read_bytes() == (source / "native.py").read_bytes()
    assert stat.S_IMODE(payload.stat().st_mode) == 0o444
    inode = payload.stat().st_ino
    assert stage_payloads(source, ["native.py"], destination=destination) == (payload,)
    assert payload.stat().st_ino == inode


def test_read_only_package_source_needs_no_side_effects(tmp_path):
    source = source_tree(tmp_path)
    (source / "native.py").chmod(0o444)
    source.chmod(0o555)
    try:
        stage_payloads(source, ["native.py"], destination=tmp_path / "payloads")
        assert list(source.iterdir()) == [source / "native.py"]
        assert stat.S_IMODE(source.stat().st_mode) == 0o555
    finally:
        source.chmod(0o755)


@pytest.mark.parametrize("resource", ["../escape", "/escape", "a/../escape", "a//b", "./a", "a\\b"])
def test_traversal_rejected_before_destination_creation(tmp_path, resource):
    source = source_tree(tmp_path)
    destination = tmp_path / "payloads"
    with pytest.raises(PayloadError):
        stage_payloads(source, [resource], destination=destination)
    assert not destination.exists()


@pytest.mark.parametrize("where", ["source-file", "source-parent", "target-file", "target-parent"])
def test_symlinks_are_rejected_without_touching_target(tmp_path, where):
    source = source_tree(tmp_path)
    destination = tmp_path / "payloads"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "native.py"
    external.write_text("untouched")
    if where == "source-file":
        (source / "native.py").unlink()
        (source / "native.py").symlink_to(external)
    elif where == "source-parent":
        alias = tmp_path / "source-alias"
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif where == "target-file":
        (destination / "native.py").symlink_to(external)
    else:
        destination.rmdir()
        destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PayloadError):
        stage_payloads(source, ["native.py"], destination=destination)
    assert external.read_text() == "untouched"


def test_existing_mismatch_is_not_overwritten(tmp_path):
    source = source_tree(tmp_path)
    destination = tmp_path / "payloads"
    (payload,) = stage_payloads(source, ["native.py"], destination=destination)
    (source / "native.py").write_text("different")
    with pytest.raises(PayloadError, match="does not match"):
        stage_payloads(source, ["native.py"], destination=destination)
    assert "must never execute" in payload.read_text()


def test_nested_paths_and_nonregular_resources(tmp_path):
    source = source_tree(tmp_path)
    (source / "nested").mkdir()
    (source / "nested" / "file.py").write_text("pass")
    (payload,) = stage_payloads(source, ["nested/file.py"], destination=tmp_path / "payloads")
    assert payload.read_text() == "pass"
    os.mkfifo(source / "pipe")
    with pytest.raises(PayloadError, match="regular file"):
        stage_payloads(source, ["pipe"], destination=tmp_path / "other")


def test_existing_writable_or_hardlinked_file_is_rejected(tmp_path):
    source = source_tree(tmp_path)
    destination = tmp_path / "payloads"
    (payload,) = stage_payloads(source, ["native.py"], destination=destination)
    payload.chmod(0o644)
    with pytest.raises(PayloadError, match="read-only"):
        stage_payloads(source, ["native.py"], destination=destination)
    payload.chmod(0o444)
    os.link(payload, tmp_path / "alias")
    with pytest.raises(PayloadError, match="unlinked elsewhere"):
        stage_payloads(source, ["native.py"], destination=destination)


def test_creation_is_exclusive_even_if_destination_appears(tmp_path, monkeypatch):
    source = source_tree(tmp_path)
    destination = tmp_path / "payloads"
    destination.mkdir()
    original = os.open

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        if flags & os.O_EXCL:
            (destination / "native.py").write_text("concurrent content")
            (destination / "native.py").chmod(0o444)
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(PayloadError, match="does not match"):
        stage_payloads(source, ["native.py"], destination=destination)
    assert (destination / "native.py").read_text() == "concurrent content"
