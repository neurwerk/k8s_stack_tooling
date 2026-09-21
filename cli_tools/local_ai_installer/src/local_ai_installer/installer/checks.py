"""Explicit inference probes; receipts are scoped to target, config and artifacts."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import wave
from pathlib import Path
from typing import Any

from local_ai_installer.installer.docker import Docker, resource
from local_ai_installer.installer.upload import prepare


def request(docker: Docker, path: str, payload: dict[str, Any] | None = None) -> bytes:
    script = 'curl -fsS --max-time 300 --max-filesize 16777216 -H "Authorization: Bearer $LOCALAI_API_KEY" '
    if payload is not None:
        script += '-H "Content-Type: application/json" --data-binary @- '
    script += '"http://127.0.0.1:8080$1"'
    result = subprocess.run(
        docker.command + ["exec", "-T", "localai", "sh", "-c", script, "probe", path],
        env=docker.environment,
        input=json.dumps(payload).encode() if payload is not None else b"",
        capture_output=True,
        check=True,
    )
    return result.stdout


def signature(docker: Docker, root: Path, alias: str) -> str:
    _, manifest, application = prepare(root, alias)
    live = json.loads(request(docker, f"/api/models/edit/{alias}"))
    data = {
        "target": docker.settings.docker_context,
        "manifest": manifest,
        "planned_config": application["yaml"],
        "config": live["config"],
        "images": resource("images.yaml"),
        "backends": resource("backends.yaml"),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def probe(docker: Docker, alias: str) -> None:
    if alias == "tts-german":
        data = request(
            docker,
            "/v1/audio/speech",
            {
                "model": alias,
                "input": "Guten Tag. Dies ist ein kurzer Test.",
                "voice": "default",
                "response_format": "wav",
            },
        )
        with wave.open(io.BytesIO(data)) as audio:
            if audio.getnframes() == 0:
                raise ValueError("TTS returned empty audio")
    elif alias == "llm-general":
        result = json.loads(
            request(
                docker,
                "/v1/chat/completions",
                {
                    "model": alias,
                    "messages": [{"role": "user", "content": "Say hello briefly."}],
                    "max_tokens": 64,
                },
            )
        )
        if not result.get("choices", [{}])[0].get("message", {}).get("content"):
            raise ValueError("Chat returned no response text")
    else:
        raise ValueError("No automated inference probe for this alias; test it in LocalAI")


def verify(docker: Docker, root: Path, alias: str) -> None:
    path = root / "inference-checks.json"
    records = json.loads(path.read_text()) if path.exists() else {}
    records.pop(alias, None)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(records, indent=2) + "\n")
    temporary.replace(path)
    before = signature(docker, root, alias)
    probe(docker, alias)
    if signature(docker, root, alias) != before:
        raise ValueError("Configuration changed during the test; retry")
    records[alias] = before
    temporary.write_text(json.dumps(records, indent=2) + "\n")
    temporary.replace(path)


def status(docker: Docker, root: Path, alias: str) -> str:
    path = root / "inference-checks.json"
    records = json.loads(path.read_text()) if path.exists() else {}
    if alias not in records:
        return "Not tested"
    return "Verified" if records[alias] == signature(docker, root, alias) else "Needs retest"
