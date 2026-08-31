"""Wrap authenticated Hugging Face CLI downloads and revision resolution."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from media_downloader_uploader.errors import HuggingFaceCommandError
from media_downloader_uploader.models import ArtifactRequest


class HuggingFaceClient:
    """Download model snapshots through the user's authenticated Hugging Face CLI.

    Args:
        environment: Environment used by CLI and API calls, including `HF_HOME`.
    """

    def __init__(self, environment: dict[str, str]) -> None:
        """Initialize the authenticated command wrapper.

        Args:
            environment: Environment used by CLI and API calls.
        """
        self._environment = environment

    def verify_authenticated(self) -> None:
        """Require an installed Hugging Face CLI with an authenticated user.

        Raises:
            HuggingFaceCommandError: If the CLI is unavailable or not authenticated.
        """
        if shutil.which("hf", path=self._environment.get("PATH")) is None:
            raise HuggingFaceCommandError(
                "Install the Hugging Face CLI with `uv tool install huggingface_hub`."
            )
        self._run(["hf", "auth", "whoami"])

    def authenticated_user(self) -> str | None:
        """Return the configured external-cache Hugging Face user, if authenticated.

        Returns:
            Authenticated Hugging Face user output, or None when no usable token exists.
        """
        try:
            return self._run_output(["hf", "auth", "whoami"]).strip()
        except HuggingFaceCommandError:
            return None

    def login(self, force: bool = False) -> None:
        """Open the Hugging Face CLI login flow for the configured external cache.

        Args:
            force: Whether to force replacement of an existing token.

        Raises:
            HuggingFaceCommandError: If Hugging Face CLI login does not complete.
        """
        command = ["hf", "auth", "login"]
        if force:
            command.append("--force")
        self._run(command)

    def resolve_revision(self, request: ArtifactRequest) -> str:
        """Resolve a requested branch, tag, or SHA to an immutable commit SHA.

        Args:
            request: Desired Hugging Face artifact.

        Returns:
            Resolved immutable repository commit SHA.

        Raises:
            HuggingFaceCommandError: If Hugging Face cannot resolve the revision.
        """
        output = self._run_output(
            ["hf", "models", "info", request.source, "--revision", request.revision]
        )
        try:
            raw = json.loads(output)
        except json.JSONDecodeError as error:
            raise HuggingFaceCommandError(
                "Hugging Face CLI returned invalid model information."
            ) from error
        revision = raw.get("sha")
        if not isinstance(revision, str) or not revision:
            raise HuggingFaceCommandError(
                f"Hugging Face did not return a commit SHA for {request.source}."
            )
        return revision

    def download(self, request: ArtifactRequest, revision: str, destination: Path) -> None:
        """Download one exact model revision into a local artifact directory.

        Args:
            request: Desired Hugging Face artifact.
            revision: Resolved immutable commit SHA.
            destination: Empty local directory for downloaded files.

        Raises:
            HuggingFaceCommandError: If the CLI download fails.
        """
        self._run(
            [
                "hf",
                "download",
                request.source,
                "--revision",
                revision,
                "--local-dir",
                str(destination),
                *[argument for pattern in request.include for argument in ("--include", pattern)],
            ]
        )

    def verify_download(self, request: ArtifactRequest, revision: str, destination: Path) -> None:
        """Verify a local download against Hugging Face's remote file metadata.

        Args:
            request: Desired Hugging Face artifact.
            revision: Resolved immutable commit SHA.
            destination: Local artifact directory downloaded by the CLI.

        Raises:
            HuggingFaceCommandError: If remote file validation fails.
        """
        self._run(
            [
                "hf",
                "cache",
                "verify",
                request.source,
                "--revision",
                revision,
                "--local-dir",
                str(destination),
                *([] if request.include else ["--fail-on-missing-files"]),
            ]
        )

    def _run(self, command: list[str]) -> None:
        """Run one Hugging Face CLI command without leaking secrets.

        Args:
            command: Command and arguments to execute.

        Raises:
            HuggingFaceCommandError: If the command exits unsuccessfully.
        """
        try:
            subprocess.run(  # noqa: S603 -- executable and source arguments are validated by this app.
                command, check=True, env=self._environment, text=True
            )
        except FileNotFoundError as error:
            raise HuggingFaceCommandError(
                "Hugging Face CLI executable `hf` was not found."
            ) from error
        except subprocess.CalledProcessError as error:
            raise HuggingFaceCommandError(
                f"Hugging Face CLI command failed: {' '.join(command[:3])}."
            ) from error

    def _run_output(self, command: list[str]) -> str:
        """Run one Hugging Face CLI command and return its standard output.

        Args:
            command: Command and arguments to execute.

        Returns:
            Standard output from the completed command.

        Raises:
            HuggingFaceCommandError: If the command does not return valid output.
        """
        try:
            result = subprocess.run(  # noqa: S603 -- executable and source arguments are validated by this app.
                command, check=True, env=self._environment, text=True, capture_output=True
            )
        except subprocess.CalledProcessError as error:
            raise HuggingFaceCommandError(
                f"Hugging Face CLI command failed: {' '.join(command[:3])}."
            ) from error
        return result.stdout
