"""Workstation registry downloads and verified offline Docker/backend delivery."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol

import yaml

from inference_runtime_manager.downloader.config import Settings, validate_storage
from inference_runtime_manager.installer.docker import resource

if TYPE_CHECKING:
    from inference_runtime_manager.installer.docker import Docker


@dataclass(frozen=True)
class ImageSpec:
    name: str
    source: str

    @property
    def digest(self) -> str:
        return self.source.rsplit("@", 1)[1]

    @property
    def tag(self) -> str:
        # Skopeo writes normalized Docker Hub names into Docker-save manifests.
        return f"docker.io/local-ai-offline/{self.name}:{self.digest.removeprefix('sha256:')}"

    def directory(self, storage: Path) -> Path:
        return storage / "docker-images" / self.name / self.digest.removeprefix("sha256:")


@dataclass(frozen=True)
class BundleImage:
    spec: ImageSpec
    archive: Path
    archive_sha256: str
    config_digest: str
    unpacked_bytes: int


def image_specs() -> list[ImageSpec]:
    runtime = yaml.safe_load(resource("images.yaml"))
    specs = [ImageSpec(name, source) for name, source in runtime.items()]
    for spec in specs:
        if not re.fullmatch(r"[a-z0-9-]+", spec.name):
            raise ValueError("Invalid image name")
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", spec.source):
            raise ValueError(f"Image {spec.name} must have an immutable manifest digest")
    if len({s.name for s in specs}) != len(specs):
        raise ValueError("Duplicate runtime image name")
    return specs


class ByteReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def stream_digest(stream: ByteReader) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(8 * 1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def file_digest(path: Path) -> tuple[str, int]:
    with path.open("rb") as stream:
        return stream_digest(stream)


def member_file(archive: tarfile.TarFile, name: str) -> IO[bytes]:
    member = archive.getmember(name)
    if not member.isfile():
        raise ValueError(f"Image archive member is not a regular file: {name}")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"Missing image archive member: {name}")
    return stream


def member_json(archive: tarfile.TarFile, name: str) -> tuple[bytes, object]:
    with member_file(archive, name) as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Image metadata exceeds its size limit")
    return raw, json.loads(raw)


def validate_archive(spec: ImageSpec, path: Path, source_manifest: bytes) -> tuple[str, int]:
    """Bind archive config and uncompressed layers to the pinned source manifest.

    Docker-save conversion can change the manifest digest, so comparing the
    converted manifest to the registry digest would be incorrect. The config
    digest and its rootfs.diff_ids preserve the image's content identity.
    """
    actual = "sha256:" + hashlib.sha256(source_manifest).hexdigest()
    if actual != spec.digest:
        raise ValueError(f"Source manifest mismatch for {spec.name}")
    source = json.loads(source_manifest)
    if not isinstance(source, dict) or not isinstance(source.get("config"), dict):
        raise ValueError("Pin a Linux/AMD64 image manifest, not a multi-platform index")
    expected_config = source["config"]["digest"]
    if not isinstance(expected_config, str) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", expected_config
    ):
        raise ValueError("Invalid source image config digest")
    unpacked = 0
    with tarfile.open(path, mode="r:") as archive:
        _, entries = member_json(archive, "manifest.json")
        if not isinstance(entries, list) or len(entries) != 1:
            raise ValueError("Expected one image per Docker archive")
        entry = entries[0]
        if entry.get("RepoTags") != [spec.tag]:
            raise ValueError("Unexpected image tags in archive")
        raw_config, config = member_json(archive, entry["Config"])
        if "sha256:" + hashlib.sha256(raw_config).hexdigest() != expected_config:
            raise ValueError("Image config differs from the pinned source")
        if not isinstance(config, dict) or (config.get("os"), config.get("architecture")) != (
            "linux",
            "amd64",
        ):
            raise ValueError("Only Linux/AMD64 images are supported")
        layers = entry["Layers"]
        diff_ids = config["rootfs"]["diff_ids"]
        if len(layers) != len(diff_ids):
            raise ValueError("Image layer count does not match its config")
        for layer, expected in zip(layers, diff_ids, strict=True):
            with member_file(archive, layer) as stream:
                magic = stream.read(2)
                stream.seek(0)
                if magic == b"\x1f\x8b":
                    with gzip.GzipFile(fileobj=stream) as decoded:
                        digest, size = stream_digest(decoded)
                else:
                    digest, size = stream_digest(stream)
            if digest != expected:
                raise ValueError(f"Image layer checksum mismatch: {spec.name}/{layer}")
            unpacked += size
    return expected_config, unpacked


def verified_image(spec: ImageSpec, directory: Path) -> BundleImage:
    archive = directory / "image.tar"
    try:
        receipt = json.loads((directory / "receipt.json").read_text())
        manifest = (directory / "source-manifest.json").read_bytes()
        checksum, size = file_digest(archive)
        if receipt != {
            "source": spec.source,
            "tag": spec.tag,
            "archive_sha256": checksum,
            "archive_bytes": size,
        }:
            raise ValueError("Image archive receipt mismatch")
        config_digest, unpacked = validate_archive(spec, archive, manifest)
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        raise ValueError(f"Offline bundle incomplete/invalid for {spec.name}: {directory}") from exc
    return BundleImage(spec, archive, checksum.removeprefix("sha256:"), config_digest, unpacked)


def download_images(settings: Settings) -> None:
    """Download directly to external media; no Docker Desktop image store is used."""
    validate_storage(settings)
    if shutil.which("skopeo") is None:
        raise ValueError("Install Skopeo on the workstation before downloading images")
    root = settings.storage_root.resolve() / "docker-images"
    temporary_root = root / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "TMPDIR": str(temporary_root)}
    command = [
        "skopeo",
        "--tmpdir",
        str(temporary_root),
        "--override-os",
        "linux",
        "--override-arch",
        "amd64",
    ]
    for spec in image_specs():
        destination = spec.directory(settings.storage_root.resolve())
        if destination.exists():
            print(f"Verifying cached image: {spec.name}", flush=True)
            verified_image(spec, destination)
            continue
        print(f"Downloading Linux/AMD64 image: {spec.name}", flush=True)
        with tempfile.TemporaryDirectory(dir=temporary_root, prefix=spec.name + "-") as folder:
            staging = Path(folder) / "complete"
            staging.mkdir()
            raw = subprocess.run(
                command + ["inspect", "--raw", "docker://" + spec.source],
                env=environment,
                capture_output=True,
                check=True,
            ).stdout
            if "sha256:" + hashlib.sha256(raw).hexdigest() != spec.digest:
                raise ValueError(f"Registry manifest mismatch for {spec.name}")
            (staging / "source-manifest.json").write_bytes(raw)
            archive = staging / "image.tar"
            subprocess.run(
                command
                + [
                    "copy",
                    "--retry-times",
                    "3",
                    "--format",
                    "v2s2",
                    "docker://" + spec.source,
                    f"docker-archive:{archive}:{spec.tag}",
                ],
                env=environment,
                check=True,
            )
            validate_archive(spec, archive, raw)
            checksum, size = file_digest(archive)
            receipt = {
                "source": spec.source,
                "tag": spec.tag,
                "archive_sha256": checksum,
                "archive_bytes": size,
            }
            (staging / "receipt.json").write_text(json.dumps(receipt, sort_keys=True))
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise ValueError("Image was published concurrently; rerun to verify it")
            os.rename(staging, destination)
    print("Offline runtime image bundle is ready.")


def verified_bundle(storage: Path) -> list[BundleImage]:
    result = []
    for spec in image_specs():
        print(f"Verifying offline archive: {spec.name}", flush=True)
        result.append(verified_image(spec, spec.directory(storage)))
    return result


def runtime_matches(info: dict[str, Any], image: BundleImage) -> bool:
    if (info.get("Os"), info.get("Architecture")) != ("linux", "amd64"):
        return False
    if info.get("Id") == image.config_digest:
        return True
    # Containerd stores expose a manifest digest as Id, not the config digest.
    # Compare the runtime configuration and ordered layer identities instead.
    descriptor = info.get("Descriptor") or {}
    if not info.get("Id") or descriptor.get("digest") != info["Id"]:
        return False
    with tarfile.open(image.archive, mode="r:") as archive:
        _, entries = member_json(archive, "manifest.json")
        if not isinstance(entries, list) or len(entries) != 1:
            return False
        raw, config = member_json(archive, entries[0]["Config"])
    if (
        not isinstance(config, dict)
        or "sha256:" + hashlib.sha256(raw).hexdigest() != image.config_digest
    ):
        return False
    expected_config = {
        key: value for key, value in config.get("config", {}).items() if value is not None
    }
    actual_config = {
        key: value for key, value in info.get("Config", {}).items() if value is not None
    }
    return bool(
        actual_config == expected_config
        and info.get("RootFS")
        == {"Type": config["rootfs"]["type"], "Layers": config["rootfs"]["diff_ids"]}
        and info.get("Variant", "") == config.get("variant", "")
        and info.get("OsVersion", "") == config.get("os.version", "")
    )


def load_runtime(docker: Docker, image: BundleImage) -> None:
    """Import using the existing context; Docker never resolves a registry here."""

    def inspect():
        result = subprocess.run(
            docker.docker_command + ["image", "inspect", image.spec.tag, "--format", "{{json .}}"],
            env=docker.environment,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            if "No such image" in result.stderr:
                return None
            raise ValueError("Cannot inspect the target Docker image store")
        return json.loads(result.stdout)

    info = inspect()
    if info is None:
        subprocess.run(
            docker.docker_command + ["image", "load", "--input", str(image.archive)],
            env=docker.environment,
            check=True,
        )
        info = inspect()
    if info is None or not runtime_matches(info, image):
        raise ValueError(f"Loaded image identity/platform mismatch for {image.spec.name}")


def stage_bundle(docker: Docker, storage: Path) -> None:
    """Preflight every local archive before importing any runtime image."""
    images = verified_bundle(storage)  # No target operations until this succeeds.
    result = subprocess.run(
        docker.docker_command + ["info", "--format", "{{json .}}"],
        env=docker.environment,
        capture_output=True,
        text=True,
        check=True,
    )
    server = json.loads(result.stdout)
    if server.get("OSType") != "linux" or server.get("Architecture") not in {"amd64", "x86_64"}:
        raise ValueError("The selected Docker target must be Linux/AMD64")
    for image in images:
        load_runtime(docker, image)
