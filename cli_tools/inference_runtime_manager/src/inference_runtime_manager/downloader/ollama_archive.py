"""Read verified Ollama GGUF archives without treating them as deployable artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")
_BLOB = re.compile(r"sha256-[0-9a-f]{64}\Z")


def archived_models(storage_root: Path, profile: str) -> list[tuple[str, int]]:
    """Return complete archived model names and weights, or reject an invalid archive."""
    root = storage_root / "ollama-archive" / profile
    index = root / "index.json"
    if not index.exists():
        return []
    if index.is_symlink() or index.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid Ollama archive index")
    data = json.loads(index.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or (data.get("source_host"), data.get("format")) != (
        profile,
        "ollama-v2",
    ):
        raise ValueError("Invalid Ollama archive index")
    entries = data.get("models")
    blobs = data.get("verified_blobs")
    if (
        not isinstance(entries, list)
        or not isinstance(blobs, dict)
        or len(blobs) != data.get("blob_count")
    ):
        raise ValueError("Invalid Ollama archive inventory")
    result: list[tuple[str, int]] = []
    referenced: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid Ollama archive entry")
        name = entry.get("model")
        if not isinstance(name, str) or not _MODEL.fullmatch(name):
            raise ValueError("Invalid Ollama archive model name")
        relative = f"manifests/registry.ollama.ai/library/{name}"
        if entry.get("path") != relative:
            raise ValueError("Invalid Ollama archive manifest path")
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Missing Ollama archive manifest")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
            raise ValueError("Invalid Ollama archive manifest digest")
        manifest = json.loads(content)
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
            raise ValueError("Invalid Ollama archive manifest")
        layers = manifest.get("layers")
        config = manifest.get("config")
        if not isinstance(layers, list) or not isinstance(config, dict):
            raise ValueError("Invalid Ollama archive layers")
        weights = [
            layer
            for layer in layers
            if isinstance(layer, dict)
            and layer.get("mediaType") == "application/vnd.ollama.image.model"
        ]
        if len(weights) != 1:
            raise ValueError("Missing Ollama GGUF model layer")
        for layer in [config, *layers]:
            if not isinstance(layer, dict):
                raise ValueError("Invalid Ollama archive layer")
            digest = layer.get("digest")
            if not isinstance(digest, str) or not _BLOB.fullmatch(digest.replace(":", "-")):
                raise ValueError("Invalid Ollama archive blob digest")
            filename = digest.replace(":", "-")
            referenced.add(filename)
            blob = root / "blobs" / filename
            if filename not in blobs or blob.is_symlink() or not blob.is_file():
                raise ValueError("Missing Ollama archive blob")
            size = blob.stat().st_size
            if blobs[filename] not in (-1, size) or layer.get("size", size) != size:
                raise ValueError("Invalid Ollama archive blob size")
        model_file = root / "blobs" / weights[0]["digest"].replace(":", "-")
        with model_file.open("rb") as file:
            if file.read(4) != b"GGUF":
                raise ValueError("Invalid Ollama archive model format")
        result.append((name.replace("/", ":"), model_file.stat().st_size))
    if not result or referenced != set(blobs):
        raise ValueError("Incomplete Ollama archive inventory")
    return result
