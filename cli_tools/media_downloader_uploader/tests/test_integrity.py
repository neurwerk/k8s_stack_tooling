from pathlib import Path

import pytest

from media_downloader_uploader.errors import IntegrityError
from media_downloader_uploader.integrity import calculate_checksums, verify_checksums


def test_calculate_checksums_excludes_huggingface_and_application_metadata(tmp_path: Path) -> None:
    (tmp_path / "weights.bin").write_bytes(b"weights")
    (tmp_path / "artifact.yaml").write_text("metadata")
    (tmp_path / ".cache" / "huggingface").mkdir(parents=True)
    (tmp_path / ".cache" / "huggingface" / "download").write_text("cache")

    checksums = calculate_checksums(tmp_path)

    assert [item.path for item in checksums] == ["weights.bin"]


def test_verify_checksums_rejects_modified_content(tmp_path: Path) -> None:
    model = tmp_path / "weights.bin"
    model.write_bytes(b"original")
    checksums = calculate_checksums(tmp_path)
    model.write_bytes(b"changed")

    with pytest.raises(IntegrityError):
        verify_checksums(tmp_path, checksums)
