"""Verify downloader inventory, stream artifacts, and apply selected local slots."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import yaml

from local_ai_installer.downloader.catalog import load_catalog, load_installed
from local_ai_installer.downloader.integrity import verify_checksums
from local_ai_installer.downloader.models import StoredArtifact
from local_ai_installer.installer.docker import Docker, slots
from local_ai_installer.installer.worker import safe


def prepare(root: Path, alias: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Require a completed matching artifact; never resolve/download a new revision."""
    preset = slots()[alias]
    model = load_catalog().model(preset["model_id"])
    variant = model.variant(preset["variant_id"])
    source_id = variant.source or model.source
    requested_revision = variant.revision or model.revision
    entries = [
        e
        for e in load_installed(root / "installed.yaml").installed
        if e.model_id == model.id and e.variant_id == variant.id
    ]
    if len(entries) != 1:
        raise ValueError(f"Download {model.id}/{variant.id} before provisioning {alias}")
    entry = entries[0]
    expected = f"models/{model.category}/{source_id}/{entry.revision}/{variant.id}"
    if entry.path != expected or entry.source != source_id or entry.verification != "sha256":
        raise ValueError("Installed artifact identity/path does not match the catalog")
    if requested_revision != "main" and entry.revision != requested_revision:
        raise ValueError("Installed artifact differs from the pinned catalog revision")
    source = root / expected
    if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
        raise ValueError("Artifact is outside the storage root")
    artifact = StoredArtifact.model_validate(
        yaml.safe_load(safe(source, "artifact.yaml").read_text())
    )
    if artifact.schema_version != 1 or artifact.category != model.category:
        raise ValueError("Unsupported artifact schema/category")
    if (artifact.model_id, artifact.variant_id, artifact.source, artifact.revision) != (
        entry.model_id,
        entry.variant_id,
        entry.source,
        entry.revision,
    ):
        raise ValueError("Inventory and artifact metadata disagree")
    if entry.file_count != len(artifact.files) or entry.total_bytes != sum(
        f.size for f in artifact.files
    ):
        raise ValueError("Inventory totals disagree with the artifact")
    expected_checksums = "".join(f"{f.sha256}  {f.path}\n" for f in artifact.files)
    if safe(source, "checksums.sha256").read_text() != expected_checksums:
        raise ValueError("Checksum index disagrees with artifact metadata")
    for record in artifact.files:
        safe(source, record.path)
    verify_checksums(source, artifact.files)
    records = [r.model_dump() for r in artifact.files]
    # Hash model content, not download timestamps in artifact.yaml. A repaired
    # local download of identical bytes must reuse the same remote publication.
    names = {r["path"] for r in records}
    required = {preset[k] for k in ("model_file", "projector") if k in preset}
    if alias == "stt-general":
        required |= {"model.bin", "config.json", "tokenizer.json"}
    if alias == "tts-german":
        required |= {
            "t3_mtl23ls_v2.safetensors",
            "s3gen.pt",
            "ve.pt",
            "conds.pt",
            "grapheme_mtl_merged_expanded_v1.json",
            "Cangjie5_TC.json",
        }
    if alias == "ner-german":
        required |= {"config.json", "tokenizer_config.json"}
        if not any(name.endswith((".safetensors", ".bin")) for name in names):
            raise ValueError("NER weights are missing")
        if not names.intersection({"tokenizer.json", "spm.model", "tokenizer.model"}):
            raise ValueError("NER tokenizer assets are missing")
    if not required <= names:
        raise ValueError(f"Missing model companions: {sorted(required - names)}")
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    destination = f"library/{model.id}/{entry.revision}/{variant.id}/{fingerprint}"
    manifest = {
        "destination": destination,
        "source": source_id,
        "revision": entry.revision,
        "files": records,
    }
    config = preset["config"]
    config.update(name=alias, disabled=not preset["enabled"])
    config.setdefault("parameters", {})["model"] = preset.get(
        "cache_repo", destination + ("/" + preset["model_file"] if "model_file" in preset else "")
    )
    if "projector" in preset:
        config["mmproj"] = destination + "/" + preset["projector"]
    config["limits"] = {"max_concurrent": 1, "retry_after_seconds": 2}
    application = {
        "action": "apply",
        "alias": alias,
        "manifest": manifest,
        "cache_repo": preset.get("cache_repo"),
        "yaml": yaml.safe_dump(config, sort_keys=False),
    }
    return source, manifest, application


def transfer(docker: Docker, source: Path, manifest: dict[str, Any]) -> None:
    if docker.worker({"action": "inspect", "manifest": manifest})["present"]:
        print("Verified remote bundle already present; skipping upload.")
        return
    process = subprocess.Popen(
        docker.worker_command(), env=docker.environment, stdin=subprocess.PIPE
    )
    assert process.stdin is not None
    try:
        process.stdin.write(
            (json.dumps({"action": "receive", "manifest": manifest}) + "\n").encode()
        )
        with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
            for record in manifest["files"]:
                file = source / record["path"]
                if file.is_symlink():
                    raise ValueError("Artifact symlinks are not uploaded")
                with file.open("rb") as stream:
                    info = tarfile.TarInfo(record["path"])
                    info.size = record["size"]
                    info.mode = 0o644
                    archive.addfile(info, stream)
        process.stdin.close()
        if process.wait() != 0:
            raise ValueError("Remote publication failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def provision(docker: Docker | None, root: Path, aliases: list[str], dry_run: bool = False) -> None:
    # Preflight every source before contacting/changing the server.
    prepared = [prepare(root, alias) for alias in aliases]
    for source, manifest, application in prepared:
        print(f"{application['alias']}: {source} -> /models/{manifest['destination']}")
    if dry_run:
        return
    if docker is None:
        raise ValueError("A Docker target is required for publication")
    for source, manifest, _ in prepared:
        transfer(docker, source, manifest)
    # Stock LocalAI owns all model processes. Stopping it before changing cache
    # refs also covers the native NER endpoint's different disable semantics.
    docker.run("stop", "localai")
    for _, _, application in prepared:
        docker.worker(application)
    # On failure leave the service stopped, preserving old definitions/history
    # and staged data for inspection. Do not auto-restart a partially applied set.
    docker.run("up", "-d", "--wait", "--wait-timeout", "180", "localai")
