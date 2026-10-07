"""Workstation registry downloads and verified offline Docker/backend delivery."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from inference_runtime_manager.downloader.config import Settings
from inference_runtime_manager.installer.docker import resource

if TYPE_CHECKING:
    from inference_runtime_manager.installer.docker import Docker


@dataclass(frozen=True)
class ImageSpec:
    name: str
    source: str | None = None
    build: str | None = None
    dockerfile: str = "Dockerfile"

    @property
    def build_directory(self) -> Path:
        if self.build is None:
            raise ValueError(f"Image {self.name} has no local build")
        return Path(str(files("inference_runtime_manager.resources").joinpath(self.build)))

    @property
    def identity(self) -> str:
        if self.source is not None:
            return self.source
        return f"build:{self.build}/{self.dockerfile}@{self.digest}"

    @property
    def digest(self) -> str:
        if self.source is not None:
            return self.source.rsplit("@", 1)[1]
        digest = hashlib.sha256()
        digest.update(self.dockerfile.encode())
        root = self.build_directory
        candidates = (
            candidate
            for candidate in root.rglob("*")
            if candidate.is_file()
            and "__pycache__" not in candidate.parts
            and candidate.suffix not in {".pyc", ".pyo"}
        )
        for path in sorted(candidates):
            if path.is_symlink():
                raise ValueError(f"Image build context contains a symlink: {path}")
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    @property
    def tag(self) -> str:
        # Skopeo writes normalized Docker Hub names into Docker-save manifests.
        return f"docker.io/local-ai-offline/{self.name}:{self.digest.removeprefix('sha256:')}"

    def directory(self, storage: Path) -> Path:
        return storage / "docker-images" / self.name / self.digest.removeprefix("sha256:")


def image_specs() -> list[ImageSpec]:
    runtime = yaml.safe_load(resource("images.yaml"))
    specs = [
        ImageSpec(name, source=value)
        if isinstance(value, str)
        else ImageSpec(
            name,
            source=value.get("source"),
            build=value.get("build"),
            dockerfile=value.get("dockerfile", "Dockerfile"),
        )
        for name, value in runtime.items()
    ]
    for spec in specs:
        if not re.fullmatch(r"[a-z0-9-]+", spec.name):
            raise ValueError("Invalid image name")
        if (spec.source is None) == (spec.build is None):
            raise ValueError(f"Image {spec.name} must have exactly one source or build")
        if spec.source is not None and not re.fullmatch(
            r"[^\s@]+@sha256:[a-f0-9]{64}", spec.source
        ):
            raise ValueError(f"Image {spec.name} must have an immutable manifest digest")
        if spec.build is not None and not re.fullmatch(r"[a-z0-9_]+", spec.build):
            raise ValueError(f"Image {spec.name} has an invalid build context")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", spec.dockerfile):
            raise ValueError(f"Image {spec.name} has an invalid Dockerfile name")
        if spec.build is not None and not (spec.build_directory / spec.dockerfile).is_file():
            raise ValueError(f"Image {spec.name} has no packaged Dockerfile")
    if len({s.name for s in specs}) != len(specs):
        raise ValueError("Duplicate runtime image name")
    return specs


RUNTIME_IMAGES = {
    "vllm": "vllm",
    "vllm-ocr-proxy": "vllm-ocr-proxy",
    "llama.cpp": "llamacpp",
    "llama.cpp-ocr-proxy": "llamacpp-ocr-proxy",
    "speaches": "speaches",
    "chatterbox": "chatterbox",
    "kokoro-onnx": "kokoro",
    "kserve": "kserve",
    "gliner": "gliner",
}


def image_names_for_runtime(runtime: str) -> list[str]:
    """Return the setup helper and selected service runtime image names."""
    try:
        name = RUNTIME_IMAGES[runtime]
    except KeyError as exc:
        raise ValueError(f"Unknown runtime: {runtime}") from exc
    return ["setup", name] if name != "setup" else ["setup"]


def selected_specs(names: list[str] | None = None) -> list[ImageSpec]:
    specs = image_specs()
    if names is None:
        return specs
    requested = set(names)
    selected = [spec for spec in specs if spec.name in requested]
    missing = requested - {spec.name for spec in selected}
    if missing:
        raise ValueError(f"Unknown runtime images: {', '.join(sorted(missing))}")
    return selected


def local_docker_command(settings: Settings) -> list[str]:
    return ["docker", "--context", settings.build_docker_context]


def inspect_docker_image(command: list[str], tag: str) -> dict[str, Any] | None:
    result = subprocess.run(
        command + ["image", "inspect", tag, "--format", "{{json .}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        if "No such image" in result.stderr:
            return None
        raise ValueError(f"Cannot inspect runtime image {tag}")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError(f"Runtime image inspection was invalid for {tag}")
    return value


def equivalent_docker_images(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Compare immutable image configuration and layers across Docker stores."""
    return bool(
        actual.get("Os") == expected.get("Os") == "linux"
        and actual.get("Architecture") == expected.get("Architecture") == "amd64"
        and actual.get("Variant", "") == expected.get("Variant", "")
        and actual.get("Config") == expected.get("Config")
        and actual.get("RootFS") == expected.get("RootFS")
    )


def valid_local_image(spec: ImageSpec, info: dict[str, Any]) -> bool:
    if (info.get("Os"), info.get("Architecture")) != ("linux", "amd64"):
        return False
    if spec.source is not None:
        digest = spec.source.rsplit("@", 1)[1]
        repo_digests = info.get("RepoDigests") or []
        return isinstance(repo_digests, list) and any(
            isinstance(item, str) and item.endswith("@" + digest) for item in repo_digests
        )
    labels = (info.get("Config") or {}).get("Labels") or {}
    return bool(
        isinstance(labels, dict) and labels.get("io.neurwerk.runtime-source") == spec.digest
    )


def require_local_context(settings: Settings) -> None:
    host = json.loads(
        subprocess.run(
            local_docker_command(settings)
            + ["context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    if not isinstance(host, str) or not host.startswith(("unix://", "npipe://")):
        raise ValueError("Runtime images may only be prepared through a local Docker context")


def prepare_local_images(settings: Settings, names: list[str] | None = None) -> None:
    """Pull or build selected Linux/AMD64 images in the workstation Docker store."""
    if shutil.which("docker") is None:
        raise ValueError("Install Docker on the workstation before preparing runtime images")
    require_local_context(settings)
    command = local_docker_command(settings)
    for spec in selected_specs(names):
        current = inspect_docker_image(command, spec.tag)
        if current is not None and valid_local_image(spec, current):
            print(f"Workstation runtime image is ready: {spec.name}")
            continue
        if current is not None:
            subprocess.run(command + ["image", "rm", spec.tag], check=True)
        if spec.source is not None:
            print(f"Downloading runtime image on workstation: {spec.name}", flush=True)
            subprocess.run(command + ["pull", "--platform", "linux/amd64", spec.source], check=True)
            subprocess.run(command + ["tag", spec.source, spec.tag], check=True)
        else:
            print(f"Building runtime image on workstation: {spec.name}", flush=True)
            subprocess.run(
                command
                + [
                    "buildx",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--pull",
                    "--load",
                    "--label",
                    f"io.neurwerk.runtime-source={spec.digest}",
                    "--file",
                    str(spec.build_directory / spec.dockerfile),
                    "--tag",
                    spec.tag,
                    str(spec.build_directory),
                ],
                check=True,
            )
        info = inspect_docker_image(command, spec.tag)
        if info is None or not valid_local_image(spec, info):
            raise ValueError(f"Prepared runtime image identity mismatch: {spec.name}")
    print("Selected runtime images are ready on the workstation.")


def local_images_ready(settings: Settings, names: list[str]) -> bool:
    try:
        require_local_context(settings)
        command = local_docker_command(settings)
        return all(
            (info := inspect_docker_image(command, spec.tag)) is not None
            and valid_local_image(spec, info)
            for spec in selected_specs(names)
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def install_local_images(
    docker: Docker, settings: Settings, names: list[str] | None = None
) -> None:
    """Stream selected workstation images into the configured offline Docker target."""
    require_local_context(settings)
    command = local_docker_command(settings)
    prepared = []
    for spec in selected_specs(names):
        local = inspect_docker_image(command, spec.tag)
        if local is None or not valid_local_image(spec, local):
            raise ValueError(f"Prepare runtime image on the workstation first: {spec.name}")
        prepared.append((spec, local))
    server = json.loads(
        subprocess.run(
            docker.docker_command + ["info", "--format", "{{json .}}"],
            env=docker.environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    if server.get("OSType") != "linux" or server.get("Architecture") not in {"amd64", "x86_64"}:
        raise ValueError("The selected Docker target must be Linux/AMD64")
    for spec, local in prepared:
        remote = inspect_docker_image(docker.docker_command, spec.tag)
        if remote is not None and equivalent_docker_images(local, remote):
            print(f"Offline target already has runtime image: {spec.name}")
            continue
        print(f"Streaming runtime image to offline target: {spec.name}", flush=True)
        source = subprocess.Popen(command + ["image", "save", spec.tag], stdout=subprocess.PIPE)
        assert source.stdout is not None
        try:
            target = subprocess.run(
                docker.docker_command + ["image", "load"],
                env=docker.environment,
                stdin=source.stdout,
                check=True,
            )
            del target
        finally:
            source.stdout.close()
            if source.wait() != 0:
                raise ValueError(f"Failed to stream runtime image: {spec.name}")
        remote = inspect_docker_image(docker.docker_command, spec.tag)
        if remote is None or not equivalent_docker_images(local, remote):
            raise ValueError(f"Installed runtime image identity mismatch: {spec.name}")


def download_images(settings: Settings) -> None:
    """Prepare all runtime images in the workstation Docker store."""
    prepare_local_images(settings)


def stage_bundle(docker: Docker, settings: Settings, names: list[str] | None = None) -> None:
    """Install selected workstation images on the offline target without archive storage."""
    install_local_images(docker, settings, names)
