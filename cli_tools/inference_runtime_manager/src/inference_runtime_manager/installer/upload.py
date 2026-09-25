"""Verify, publish, activate, and restart explicit inference services."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

import yaml

from inference_runtime_manager.downloader.catalog import load_catalog, load_installed
from inference_runtime_manager.downloader.integrity import verify_checksums
from inference_runtime_manager.downloader.models import StoredArtifact
from inference_runtime_manager.installer.assignments import (
    compose_services,
    deployment_services,
    load_deployment,
)
from inference_runtime_manager.installer.docker import Docker
from inference_runtime_manager.installer.worker import safe


def prepare_artifact(
    root: Path, model_id: str, variant_id: str, *, verify: bool = True
) -> tuple[Path, dict[str, Any]]:
    model = load_catalog().model(model_id)
    variant = model.variant(variant_id)
    source_id = variant.source or model.source
    requested_revision = variant.revision or model.revision
    entries = [
        entry
        for entry in load_installed(root / "installed.yaml").installed
        if (entry.model_id, entry.variant_id) == (model.id, variant.id)
    ]
    if len(entries) != 1:
        raise ValueError(f"Download {model.id}/{variant.id} before uploading or applying it")
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
        record.size for record in artifact.files
    ):
        raise ValueError("Inventory totals disagree with the artifact")
    expected_checksums = "".join(f"{record.sha256}  {record.path}\n" for record in artifact.files)
    if safe(source, "checksums.sha256").read_text() != expected_checksums:
        raise ValueError("Checksum index disagrees with artifact metadata")
    for record in artifact.files:
        safe(source, record.path)
    if verify:
        verify_checksums(source, artifact.files)
    records = [record.model_dump() for record in artifact.files]
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return source, {
        "destination": f"library/{model.id}/{entry.revision}/{variant.id}/{fingerprint}",
        "source": source_id,
        "revision": entry.revision,
        "files": records,
    }


def prepare(root: Path, alias: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    preset = deployment_services(root)[alias]
    source, manifest = prepare_artifact(root, preset["model_id"], preset["variant_id"])
    names = {record["path"] for record in manifest["files"]}
    required = set(preset.get("required_files", []))
    required_suffixes = tuple(preset.get("required_any_suffix", []))
    if required_suffixes and not any(name.endswith(required_suffixes) for name in names):
        raise ValueError(f"{alias}: model weights are missing")
    if not required <= names:
        raise ValueError(f"Missing model companions: {sorted(required - names)}")
    return (
        source,
        manifest,
        {
            "action": "activate",
            "alias": alias,
            "service": preset["service"],
            "runtime": preset["runtime"],
            "enabled": preset["enabled"],
            "cache_repo": preset.get("cache_repo"),
            "manifest": manifest,
        },
    )


def transfer(docker: Docker, source: Path, manifest: dict[str, Any]) -> None:
    if docker.worker({"action": "present", "manifest": manifest})["present"]:
        print("Previously verified remote bundle is present; skipping upload.")
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
    if docker is not None:
        load_deployment(root, docker.settings.docker_context)
    configured = deployment_services(root)
    if any(alias not in configured for alias in aliases):
        raise ValueError("Unknown inference service")
    enabled = [alias for alias in aliases if configured[alias]["enabled"]]
    disabled = [alias for alias in aliases if not configured[alias]["enabled"]]
    states = docker.service_status() if docker is not None and not dry_run else {}
    unchanged = []
    changed = []
    for alias in enabled:
        preset = configured[alias]
        alternatives = set(compose_services(alias)) - {preset["service"]}
        state, health, image = states.get(preset["service"], ("", "", ""))
        if (
            docker is not None
            and state == "running"
            and health == "healthy"
            and docker.image_matches(preset["runtime"], image)
            and not any(
                states.get(service, ("", "", ""))[0] == "running" for service in alternatives
            )
        ):
            try:
                active = docker.active_identity(alias, preset["service"])
            except (OSError, subprocess.SubprocessError):
                active = None
            if active == (preset["model_id"], preset["variant_id"]):
                unchanged.append(alias)
                continue
        changed.append(alias)
    for alias in unchanged:
        print(f"{alias}: healthy with matching model files and image; no restart needed.")
    disabled = [
        alias
        for alias in disabled
        if docker is None
        or dry_run
        or any(
            states.get(service, ("", "", ""))[0] == "running" for service in compose_services(alias)
        )
    ]
    prepared = []
    for alias in changed:
        print(f"{alias}: verifying local model checksums...", flush=True)
        started = time.monotonic()
        prepared.append(prepare(root, alias))
        print(f"{alias}: local verification {time.monotonic() - started:.1f}s")
    for source, manifest, application in prepared:
        print(f"{application['alias']}: {source} -> /models/{manifest['destination']}")
        alternatives = set(compose_services(application["alias"])) - {application["service"]}
        if alternatives:
            print(f"{application['alias']}: stop alternatives {', '.join(sorted(alternatives))}")
    for alias in disabled:
        print(f"{alias}: stop {', '.join(compose_services(alias))}")
    if dry_run:
        return
    if docker is None:
        raise ValueError("A Docker target is required for publication")
    for source, manifest, application in prepared:
        print(f"{application['alias']}: checking remote bundle...", flush=True)
        started = time.monotonic()
        transfer(docker, source, manifest)
        print(f"Remote bundle check/upload: {time.monotonic() - started:.1f}s")
    for _, _, application in prepared:
        service = application["service"]
        alternatives = set(compose_services(application["alias"])) - {service}
        if alternatives:
            docker.run("stop", *sorted(alternatives))
        started = time.monotonic()
        docker.worker(application)
        print(f"{service}: activation {time.monotonic() - started:.1f}s")
        started = time.monotonic()
        docker.run(
            "up",
            "--pull",
            "never",
            "-d",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "300",
            service,
        )
        print(f"{service}: startup/health {time.monotonic() - started:.1f}s")
    for alias in disabled:
        docker.run("stop", *compose_services(alias))
    if not prepared and not disabled:
        print("Remote deployment already matches the saved assignments.")
