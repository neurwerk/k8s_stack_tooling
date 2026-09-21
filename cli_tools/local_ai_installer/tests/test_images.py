import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from local_ai_installer.installer import images, worker
from local_ai_installer.installer.docker import DeploymentSettings, Docker


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def archive_fixture(root: Path, architecture="amd64", corrupt=False):
    layer = b"verified layer contents"
    config = json.dumps(
        {"os": "linux", "architecture": architecture, "rootfs": {"diff_ids": [digest(layer)]}}
    ).encode()
    source = json.dumps({"schemaVersion": 2, "config": {"digest": digest(config)}}).encode()
    spec = images.ImageSpec("sample", "example/sample@" + digest(source))
    entries = [{"Config": "config.json", "RepoTags": [spec.tag], "Layers": ["layer.tar"]}]
    path = root / "image.tar"
    with tarfile.open(path, "w") as archive:
        for name, data in {
            "manifest.json": json.dumps(entries).encode(),
            "config.json": config,
            "layer.tar": b"tampered layer" if corrupt else layer,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return spec, path, source, digest(config)


def test_archive_identity_is_verified_against_the_source(tmp_path):
    spec, path, source, config_digest = archive_fixture(tmp_path)
    actual, size = images.validate_archive(spec, path, source)
    assert actual == config_digest
    assert size > 0


def test_tampered_layer_is_rejected_even_when_config_is_unchanged(tmp_path):
    spec, path, source, _ = archive_fixture(tmp_path, corrupt=True)
    with pytest.raises(ValueError, match="layer checksum"):
        images.validate_archive(spec, path, source)


def test_workstation_architecture_cannot_silently_select_arm_images(tmp_path):
    spec, path, source, _ = archive_fixture(tmp_path, architecture="arm64")
    with pytest.raises(ValueError, match="Linux/AMD64"):
        images.validate_archive(spec, path, source)


def test_wrong_source_manifest_is_rejected(tmp_path):
    spec, path, source, _ = archive_fixture(tmp_path)
    with pytest.raises(ValueError, match="Source manifest mismatch"):
        images.validate_archive(spec, path, source + b"\n")


def test_incomplete_bundle_does_not_contact_or_stop_the_server(tmp_path, monkeypatch):
    spec = images.ImageSpec("missing", "example/missing@sha256:" + "a" * 64)
    monkeypatch.setattr(images, "image_specs", lambda: [spec])
    docker = Mock()
    with pytest.raises(ValueError, match="bundle incomplete"):
        images.stage_bundle(docker, tmp_path)
    assert docker.mock_calls == []


def test_install_uses_native_local_archives_without_target_pulls(tmp_path, monkeypatch):
    local_uri = "ocifile:///models/library/offline-backends/sample/image.tar"
    stage = Mock(return_value=([("llama-cpp", local_uri)], {"files": []}))
    monkeypatch.setattr(images, "stage_bundle", stage)
    docker = Docker(DeploymentSettings(docker_context="example", api_key="example-key"))
    docker.run = Mock()
    docker.worker = Mock(return_value={"seeded": True})
    docker.install(tmp_path)
    stage.assert_called_once_with(docker, tmp_path)
    calls = [call.args for call in docker.run.call_args_list]
    assert not any(call[0] == "pull" for call in calls)
    install = next(call for call in calls if call[0] == "run")
    assert local_uri in install
    assert install[install.index("--pull") + 1] == "never"
    up = next(call for call in calls if call[0] == "up")
    assert up[up.index("--pull") + 1] == "never"


def auxiliary_fixture(root, name="features.msgpack"):
    target = root / "library/pkuseg/example"
    target.mkdir(parents=True)
    archive = target / "spacy_ontonotes.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, b"fixture")
        output.writestr("weights.npz", b"weights")
    manifest = {
        "destination": "library/pkuseg/example",
        "files": [
            {"path": archive.name, "size": archive.stat().st_size, "sha256": worker.sha256(archive)}
        ],
    }
    (target / ".receipt.json").write_text(json.dumps(manifest))
    return manifest


def test_stock_pkuseg_cache_has_zip_and_extracted_files(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    manifest = auxiliary_fixture(tmp_path)
    worker.pkuseg_cache(manifest)
    worker.pkuseg_cache(manifest)
    cache = tmp_path / "cache/pkuseg"
    assert (cache / "spacy_ontonotes.zip").is_file()
    assert (cache / "spacy_ontonotes/features.msgpack").read_bytes() == b"fixture"
    assert (cache / "spacy_ontonotes/weights.npz").read_bytes() == b"weights"


def test_auxiliary_extraction_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    manifest = auxiliary_fixture(tmp_path, "../outside")
    with pytest.raises(ValueError, match="Unsafe relative path"):
        worker.pkuseg_cache(manifest)
    assert not (tmp_path / "cache/pkuseg").exists()
