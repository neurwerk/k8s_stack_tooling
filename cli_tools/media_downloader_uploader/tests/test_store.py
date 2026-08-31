from pathlib import Path
from unittest.mock import Mock

from media_downloader_uploader.models import ArtifactRequest
from media_downloader_uploader.store import ArtifactStore


def _request() -> ArtifactRequest:
    return ArtifactRequest(
        model_id="model",
        variant_id="transformers",
        category="llm",
        source="owner/model",
        revision="main",
    )


def test_synchronize_reuses_valid_immutable_artifact(tmp_path: Path) -> None:
    client = Mock()
    client.resolve_revision.return_value = "a" * 40
    client.download.side_effect = lambda request, revision, destination: (
        destination / "weights.bin"
    ).write_bytes(b"weights")
    request = _request()
    store = ArtifactStore(tmp_path, client)

    first = store.synchronize(request)
    second = store.synchronize(request)

    assert first.revision == second.revision
    assert client.download.call_count == 1
    assert store.destination(request, "a" * 40).is_dir()


def test_synchronize_places_variants_in_separate_directories(tmp_path: Path) -> None:
    client = Mock()
    store = ArtifactStore(tmp_path, client)
    request = _request()
    gguf = request.model_copy(update={"variant_id": "gguf"})

    assert store.destination(request, "a" * 40) != store.destination(gguf, "a" * 40)
