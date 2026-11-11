"""Inspect retained Ollama archives and verify explicitly reviewed router bundles."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from inference_runtime_manager.installer.worker import safe, sha256

_MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")
_BLOB = re.compile(r"sha256-[0-9a-f]{64}\Z")

# Reviewed standard GGUF weights with embedded llama.cpp chat templates.
ARCHIVE_RECIPES = {
    "deepseek-r1/32b": (
        "deepseek-r1-32b",
        "gguf-q4-k-m",
        "6150cb382311b69f09cc0f9a1b69fc029cbd742b66bb8ec531aa5ecf5c613e93",
    ),
    "deepseek-r1/70b": (
        "deepseek-r1-70b",
        "gguf-q4-k-m",
        "4cd576d9aa16961244012223abf01445567b061f1814b57dfef699e4cf8df339",
    ),
    "llama3/latest": (
        "llama3-8b",
        "gguf-q4-0",
        "6a0746a1ec1aef3e7ec53868f220ff6e389f6f8ef87a01d77c96807de94ca2aa",
    ),
}


def prepare_archived_bundle(
    storage_root: Path, profile: str, name: str, *, verify: bool = True
) -> tuple[Path, dict[str, Any]]:
    """Verify original archive blobs and reference them without copies or edits."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", profile) or name not in ARCHIVE_RECIPES:
        raise ValueError("Unreviewed archive profile/model")
    if any(path.is_symlink() for path in (storage_root, *storage_root.parents)):
        raise ValueError("Archive storage path must not contain symlinks")
    root = safe(storage_root, "ollama-archive/" + profile)
    index_path = safe(root, "index.json")
    # Check every referenced ancestor before the existing archive inventory reader.
    if index_path.stat().st_size > 1024 * 1024:
        raise ValueError("Oversized archive index")
    index = json.loads(index_path.read_text())
    for entry in index["models"]:
        safe(root, entry["path"])
    safe(root, "blobs")
    archived_models(storage_root, profile)
    entry = next((entry for entry in index["models"] if entry["model"] == name), None)
    if entry is None:
        raise ValueError("Reviewed model is absent from archive")
    manifest = json.loads(safe(root, entry["path"]).read_text())
    model_id, variant_id, weight_hash = ARCHIVE_RECIPES[name]
    weights = [
        layer
        for layer in manifest["layers"]
        if layer["mediaType"] == "application/vnd.ollama.image.model"
    ]
    if weights[0]["digest"] != "sha256:" + weight_hash:
        raise ValueError("Archive weight differs from reviewed model identity")
    records = {}
    blobs = safe(root, "blobs")
    for layer in [manifest["config"], *manifest["layers"]]:
        digest = layer["digest"].removeprefix("sha256:")
        filename = "sha256-" + digest
        path = safe(blobs, filename)
        if verify and sha256(path) != digest:
            raise ValueError("Archive blob checksum mismatch")
        records[filename] = {"path": filename, "size": path.stat().st_size, "sha256": digest}
    files = [records[name] for name in sorted(records)]
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return blobs, {
        "destination": f"library/{model_id}/{entry['sha256']}/{variant_id}/{fingerprint}",
        "source": "ollama/" + name.split("/")[0],
        "revision": entry["sha256"],
        "files": files,
    }


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
