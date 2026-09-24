"""Manual endpoint checks executed inside each remote runtime container."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import tempfile
import time
import wave
import zlib
from datetime import datetime
from pathlib import Path
from struct import pack
from typing import Any

import questionary

from inference_runtime_manager.downloader.config import Settings
from inference_runtime_manager.installer.assignments import assigned_recipe, load_deployment
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker

LIMIT = 16 * 1024 * 1024
SENTENCE = "Guten Tag. Dies ist ein kurzer Test."

ORDER = [
    "vlm-documents",
    "llm-general",
    "vlm-general",
    "tts-german",
    "stt-general",
    "vad-general",
    "ner-german",
]


def read_sample(path: str) -> bytes:
    with Path(path).expanduser().open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if not data or len(data) > LIMIT:
        raise ValueError("Choose a nonempty WAV of at most 16 MiB")
    return data


def sample_image() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return pack("!I", len(data)) + kind + data + pack("!I", zlib.crc32(kind + data))

    width, height = 512, 192
    rows = [bytearray(b"\xff" * width * 3) for _ in range(height)]
    font = {
        "T": [31, 4, 4, 4, 4, 4, 4],
        "E": [31, 16, 16, 30, 16, 16, 31],
        "S": [15, 16, 16, 14, 1, 1, 30],
        " ": [0] * 7,
        "1": [4, 12, 4, 4, 4, 4, 14],
        "2": [14, 17, 1, 2, 4, 8, 31],
        "3": [30, 1, 1, 14, 1, 1, 30],
    }
    scale = 8
    for index, letter in enumerate("TEST 123"):
        for y, bits in enumerate(font[letter]):
            for x in range(5):
                if bits & (1 << (4 - x)):
                    left = (32 + index * 6 * scale + x * scale) * 3
                    for dy in range(scale):
                        rows[64 + y * scale + dy][left : left + scale * 3] = b"\0" * (scale * 3)
    pixels = b"".join(b"\0" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def request(
    docker: Docker,
    alias: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    audio: bytes | None = None,
) -> bytes:
    preset = assigned_recipe(Settings().storage_root, docker.settings.docker_context, alias)
    if preset is None or not preset["enabled"]:
        raise ValueError(f"{alias} is not enabled")
    service = preset["service"]
    port = preset["internal_port"]
    transport = preset["transport"]
    if transport == "curl":
        if audio is not None:
            raise ValueError("llama.cpp endpoints do not accept audio")
        script = (
            "curl -sS --max-time 300 --max-filesize 16777216 "
            '-H "Authorization: Bearer $INFERENCE_API_KEY" '
        )
        data = b""
        if payload is not None:
            script += '-H "Content-Type: application/json" --data-binary @- '
            data = json.dumps(payload).encode()
        script += f"--write-out '\n%{{http_code}}' http://127.0.0.1:{port}{path}"
        command = docker.command + [
            "exec",
            "-T",
            "-e",
            "INFERENCE_API_KEY",
            service,
            "sh",
            "-c",
            script,
        ]
    else:
        script = r"""
import json, os, sys, urllib.error, urllib.request, uuid
request = json.loads(sys.stdin.buffer.readline())
headers = {}
key = os.environ.get("INFERENCE_API_KEY")
if key:
    headers["Authorization"] = "Bearer " + key
body = None
if request["audio"] is not None:
    audio = __import__("base64").b64decode(request["audio"])
    boundary = "----irm-" + uuid.uuid4().hex
    fields = request.get("fields", {})
    parts = []
    for name, value in fields.items():
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sample.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() + audio + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
elif request["payload"] is not None:
    body = json.dumps(request["payload"]).encode()
    headers["Content-Type"] = "application/json"
req = urllib.request.Request(request["url"], data=body, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=300) as response:
        data, status = response.read(16777217), response.status
except urllib.error.HTTPError as error:
    data, status = error.read(16777217), error.code
sys.stdout.buffer.write(data + b"\n" + str(status).encode())
"""
        fields = {"model": "stt-general"} if alias == "stt-general" else {}
        data = (
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{port}{path}",
                    "payload": payload,
                    "audio": base64.b64encode(audio).decode() if audio is not None else None,
                    "fields": fields,
                }
            )
            + "\n"
        ).encode()
        command = docker.command + [
            "exec",
            "-T",
            "-e",
            "INFERENCE_API_KEY",
            service,
            transport,
            "-c",
            script,
        ]
    started = time.monotonic()
    result = subprocess.run(
        command, env=docker.environment, input=data, capture_output=True, timeout=330, check=False
    )
    body, _, code = result.stdout.rpartition(b"\n")
    status = int(code) if code.isdigit() else 0
    print(
        f"{alias} ({service}): HTTP {status or 'unavailable'} in {time.monotonic() - started:.1f}s"
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace")[-2000:])
    if not 200 <= status < 300:
        raise ValueError(f"HTTP {status}: {body.decode(errors='replace')[:2000]}")
    if len(body) > LIMIT:
        raise ValueError("Response exceeds 16 MiB")
    return body


def validate_wav(data: bytes) -> None:
    with wave.open(io.BytesIO(data)) as audio:
        if audio.getnframes() == 0:
            raise ValueError("TTS returned empty audio")


def synthesize_tts(docker: Docker, text: str) -> bytes:
    started = time.monotonic()
    speech = request(
        docker,
        "tts-german",
        "/v1/audio/speech",
        payload={
            "model": "tts-german",
            "input": text,
            "voice": "default",
            "response_format": "wav",
            "language": "de",
        },
    )
    validate_wav(speech)
    with wave.open(io.BytesIO(speech)) as audio:
        duration = audio.getnframes() / audio.getframerate()
    elapsed = time.monotonic() - started
    print(f"tts-german: {duration:.1f}s audio, real-time factor {elapsed / duration:.2f}")
    return speech


def test_service(docker: Docker, alias: str, speech: bytes | None) -> bytes | None:
    preset = assigned_recipe(Settings().storage_root, docker.settings.docker_context, alias)
    if preset is None:
        raise ValueError(f"{alias} has no saved assignment")
    request(docker, alias, preset["health_path"])
    if alias in {"llm-general", "vlm-general", "vlm-documents"}:
        content: str | list[dict[str, Any]] = "Say hello briefly."
        if alias != "llm-general":
            image = base64.b64encode(sample_image()).decode()
            content = [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                {
                    "type": "text",
                    "text": "Convert this page to docling."
                    if alias == "vlm-documents"
                    else "Describe this image.",
                },
            ]
        payload = {
            "model": alias,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 4096 if alias == "vlm-documents" else 256,
            "stream": False,
        }
        if alias == "vlm-documents":
            payload["skip_special_tokens"] = False
        value = json.loads(request(docker, alias, "/v1/chat/completions", payload=payload))
        if not value.get("choices", [{}])[0].get("message", {}).get("content"):
            raise ValueError("Chat response has no text")
    elif alias == "tts-german":
        speech = synthesize_tts(docker, SENTENCE)
        with tempfile.NamedTemporaryFile(
            prefix="inference-speech-", suffix=".wav", delete=False
        ) as file:
            file.write(speech)
            print(f"tts-german: audio saved to {file.name}")
    elif alias == "stt-general":
        if speech is None:
            raise ValueError("No speech sample is available")
        value = json.loads(request(docker, alias, "/v1/audio/transcriptions", audio=speech))
        if not isinstance(value.get("text"), str) or not value["text"].strip():
            raise ValueError("Transcription response has no text")
    elif alias == "vad-general":
        if speech is None:
            raise ValueError("No speech sample is available")
        value = json.loads(request(docker, alias, "/v1/audio/speech/timestamps", audio=speech))
        if (
            not isinstance(value, list)
            or not value
            or not all(
                isinstance(item, dict) and "start" in item and "end" in item for item in value
            )
        ):
            raise ValueError("VAD response has no timestamp list")
    elif alias == "ner-german":
        value = json.loads(
            request(
                docker,
                alias,
                "/v1/models/ner-german:predict",
                payload={"instances": ["Anna Beispiel wohnt in Berlin."]},
            )
        )
        if not isinstance(value, dict) or not isinstance(value.get("predictions"), list):
            raise ValueError("NER response has no predictions list")
    print(f"PASS {alias}; review output quality manually.")
    return speech


def enabled_docker(alias: str) -> Docker:
    root = Settings().storage_root
    docker = Docker(DeploymentSettings())
    deployment = load_deployment(root, docker.settings.docker_context)
    assignment = deployment.assignments.get(alias)
    if assignment is None or not assignment.enabled:
        raise ValueError(f"{alias} is not enabled")
    return docker


def test_individual(alias: str) -> None:
    docker = enabled_docker(alias)
    speech = None
    if alias in {"stt-general", "vad-general"}:
        sample = questionary.path("Local WAV to test:").ask()
        if sample is None:
            return
        if not sample:
            raise ValueError(f"A local WAV is required to test {alias} individually")
        speech = read_sample(sample)
    print("The request executes inside the remote service container against its loopback port.")
    test_service(docker, alias, speech)


def tts_menu() -> None:
    while True:
        action = questionary.select(
            "TTS tests",
            choices=[
                questionary.Choice("Run standard endpoint test", value="standard"),
                questionary.Choice("Generate WAV from custom text", value="custom"),
                questionary.Choice("Back", value="back"),
            ],
        ).ask()
        if action in (None, "back"):
            return
        if action == "standard":
            test_individual("tts-german")
            continue
        text = questionary.text("Text to synthesize:").ask()
        if text is None:
            return
        text = text.strip()
        if not text:
            print("Enter non-empty text.")
            continue

        docker = enabled_docker("tts-german")
        preset = assigned_recipe(
            Settings().storage_root, docker.settings.docker_context, "tts-german"
        )
        if preset is None:
            raise ValueError("tts-german has no saved assignment")
        request(docker, "tts-german", preset["health_path"])
        speech = synthesize_tts(docker, text)
        output_directory = Path("runtime")
        output_directory.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        output = output_directory / f"tts-custom-{timestamp}.wav"
        output.write_bytes(speech)
        print(f"tts-german: audio saved to {output.resolve()}")


def manual_menu() -> None:
    choices = [
        questionary.Choice("Test all enabled endpoints", value="all"),
        questionary.Separator(" "),
        questionary.Separator("── Individual Endpoints ──"),
        *[
            questionary.Choice("TTS tests", value="tts")
            if alias == "tts-german"
            else questionary.Choice(alias, value=alias)
            for alias in ORDER
        ],
        questionary.Separator(" "),
        questionary.Choice("Back", value="back"),
    ]
    while True:
        action = questionary.select("Manual tests", choices=choices).ask()
        if action in (None, "back"):
            return
        if action == "all":
            menu()
        elif action == "tts":
            tts_menu()
        else:
            test_individual(action)


def menu() -> None:
    root = Settings().storage_root
    docker = Docker(DeploymentSettings())
    deployment = load_deployment(root, docker.settings.docker_context)
    enabled_set = {
        alias for alias, assignment in deployment.assignments.items() if assignment.enabled
    }
    enabled = [alias for alias in ORDER if alias in enabled_set]
    if not enabled:
        print("No enabled services.")
        return
    sample = questionary.path("Optional local WAV for STT/VAD (blank: use the TTS result):").ask()
    if sample is None:
        return
    speech = read_sample(sample) if sample else None
    if not questionary.confirm(
        f"Test {len(enabled)} enabled services on {docker.settings.docker_context}?", default=False
    ).ask():
        return
    print("Requests execute inside each remote service container against its loopback port.")
    results = []
    for alias in enabled:
        try:
            speech = test_service(docker, alias, speech)
            results.append(f"PASS {alias}")
        except (OSError, ValueError, wave.Error, subprocess.SubprocessError) as exc:
            results.append(f"FAIL {alias}: {exc}")
            print(results[-1])
    print("\nSummary\n" + "\n".join(results))
    if any(result.startswith("FAIL ") for result in results):
        raise ValueError("One or more endpoint checks failed")
