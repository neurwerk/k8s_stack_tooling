import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from media_downloader_uploader.errors import HuggingFaceCommandError
from media_downloader_uploader.huggingface import HuggingFaceClient
from media_downloader_uploader.models import ArtifactRequest


def _request() -> ArtifactRequest:
    return ArtifactRequest(
        model_id="model",
        variant_id="transformers",
        category="llm",
        source="openai/gpt-oss-20b",
        revision="main",
    )


def test_resolve_revision_uses_huggingface_cli_output(monkeypatch) -> None:
    client = HuggingFaceClient({"PATH": ""})
    result = Mock(stdout=json.dumps({"sha": "a" * 40}))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    request = _request()

    revision = client.resolve_revision(request)

    assert revision == "a" * 40


def test_resolve_revision_rejects_missing_sha(monkeypatch) -> None:
    client = HuggingFaceClient({"PATH": ""})
    result = Mock(stdout=json.dumps({}))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    request = _request()

    with pytest.raises(HuggingFaceCommandError, match="commit SHA"):
        client.resolve_revision(request)


def test_download_passes_resolved_revision_and_destination(monkeypatch, tmp_path: Path) -> None:
    client = HuggingFaceClient({"PATH": ""})
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    request = _request()

    client.download(request, "a" * 40, tmp_path)

    assert run.call_args.args[0][0:3] == ["hf", "download", "openai/gpt-oss-20b"]
    assert "--local-dir" in run.call_args.args[0]


def test_verify_download_requires_remote_checksum_and_all_remote_files(
    monkeypatch, tmp_path: Path
) -> None:
    client = HuggingFaceClient({"PATH": ""})
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    request = _request()

    client.verify_download(request, "a" * 40, tmp_path)

    command = run.call_args.args[0]
    assert command[0:3] == ["hf", "cache", "verify"]
    assert "--fail-on-missing-files" in command
    assert "--fail-on-extra-files" not in command


def test_verify_authenticated_rejects_missing_cli(monkeypatch) -> None:
    client = HuggingFaceClient({"PATH": ""})
    monkeypatch.setattr(
        "media_downloader_uploader.huggingface.shutil.which", lambda *args, **kwargs: None
    )

    with pytest.raises(HuggingFaceCommandError, match="Install"):
        client.verify_authenticated()


def test_authenticated_user_returns_none_when_cli_rejects_token(monkeypatch) -> None:
    client = HuggingFaceClient({"PATH": ""})
    error = subprocess.CalledProcessError(returncode=1, cmd=["hf", "auth", "whoami"])
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))

    assert client.authenticated_user() is None


def test_login_uses_external_environment_and_force_when_requested(monkeypatch) -> None:
    environment = {"PATH": "", "HF_HOME": "/external-media/huggingface"}
    client = HuggingFaceClient(environment)
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)

    client.login(force=True)

    assert run.call_args.args[0] == ["hf", "auth", "login", "--force"]
    assert run.call_args.kwargs["env"] == environment
