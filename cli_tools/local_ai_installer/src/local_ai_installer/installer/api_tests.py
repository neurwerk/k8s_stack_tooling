"""User-driven API smoke tests transported through the remote Docker context."""

from __future__ import annotations

import base64
import io
import json
import shlex
import struct
import subprocess
import tempfile
import time
import wave
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import questionary

from local_ai_installer.installer.docker import DeploymentSettings, Docker

LIMIT = 16 * 1024 * 1024
SENTENCE = "Guten Tag. Dies ist ein kurzer Test."
TESTS = {
    "ready": ("Readiness", "/readyz", None),
    "models": ("List models", "/v1/models", None),
    "chat": ("Chat", "/v1/chat/completions", "llm-general"),
    "documents": ("Document vision", "/v1/chat/completions", "vlm-documents"),
    "vision": ("General vision", "/v1/chat/completions", "vlm-general"),
    "tts": ("Text-to-speech", "/v1/audio/speech", "tts-german"),
    "stt": ("Speech-to-text", "/v1/audio/transcriptions", "stt-general"),
    "vad": ("Speech activity detection (VAD)", "/v1/vad", "vad-general"),
    "ner": ("PII / NER", "/api/pii/analyze", "ner-german"),
}


@dataclass
class Response:
    body: bytes
    status: int
    elapsed: float


class Unavailable(ValueError):
    """An endpoint cannot yet be exercised with its configured model."""


def remote_python(docker: Docker, script: str, payload: Any = None) -> Any:
    result = subprocess.run(
        docker.command + ["exec", "-T", "localai", "python3", "-c", script],
        env=docker.environment,
        input=(json.dumps(payload) + "\n").encode(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace")[-2000:])
    return json.loads(result.stdout)


def runtime_status(docker: Docker) -> None:
    settings = remote_python(
        docker,
        """
import json, os
keys = ["LOCALAI_MAX_ACTIVE_BACKENDS", "LOCALAI_VRAM_BUDGET",
        "LOCALAI_WATCHDOG_IDLE_TIMEOUT", "LOCALAI_FORCE_EVICTION_WHEN_BUSY"]
print(json.dumps({k: os.environ.get(k, "not set") for k in keys}))
""",
    )
    print("\nLive runtime settings:")
    for key, value in settings.items():
        print(f"  {key}: {value}")
    state = json.loads(curl(docker, "/system", show=False).body)
    print("Loaded models: " + json.dumps(state.get("loaded_models", [])))
    print("Enabled models load on demand; LocalAI evicts idle models at the backend limit.")


def model_preflight(docker: Docker, alias: str) -> None:
    config = json.loads(curl(docker, f"/api/models/config-json/{alias}", show=False).body)
    if not isinstance(config, dict) or not isinstance(config.get("parameters"), dict):
        raise ValueError(f"{alias}: invalid live configuration")
    model = config["parameters"].get("model", "")
    problems = []
    if config.get("disabled"):
        problems.append("disabled")
    if not model or "not-staged/" in model:
        problems.append("placeholder configuration; upload/assign/apply this model")
    else:
        missing = remote_python(
            docker,
            """
import json, sys
from pathlib import Path
c = json.loads(sys.stdin.readline())
root = Path("/models")
model = c["parameters"]["model"]
path = root / model
missing = []
if not path.exists():
    cache = root / "cache/hub" / ("models--" + model.replace("/", "--"))
    ref = cache / "refs/main"
    if not ref.is_file() or not (cache / "snapshots" / ref.read_text().strip()).is_dir():
        missing.append("model files/cache absent: " + model)
projector = c.get("mmproj")
if projector and not (root / projector).is_file():
    missing.append("vision projector absent: " + projector)
if c.get("name") in ("vlm-general", "vlm-documents") and not projector:
    missing.append("vision projector not configured")
print(json.dumps(missing))
""",
            config,
        )
        problems.extend(missing)
    print(f"{alias}: backend={config.get('backend')} model={model}")
    if problems:
        raise Unavailable(f"{alias}: " + "; ".join(problems))


def curl(
    docker: Docker,
    path: str,
    payload: dict[str, Any] | None = None,
    audio: bytes | None = None,
    *,
    show: bool = True,
) -> Response:
    """Stream inputs and outputs; paths on the workstation are never remote mounts."""
    args = [
        "curl",
        "-sS",
        "--connect-timeout",
        "10",
        "--max-time",
        "300",
        "--max-filesize",
        str(LIMIT),
        "-H",
    ]
    script = shlex.join(args) + ' "Authorization: Bearer $LOCALAI_API_KEY" '
    data = b""
    if audio is not None:
        script += "-F 'file=@-;filename=sample.wav;type=audio/wav' -F model=stt-general "
        data = audio
    elif payload is not None:
        script += "-H 'Content-Type: application/json' --data-binary @- "
        data = json.dumps(payload).encode()
    script += '--write-out "\\n%{http_code}" "http://127.0.0.1:8080$1"'
    if show:
        print(f"Target: {docker.settings.docker_context} (curl inside localai)")
        print(script.replace("$1", path))
        if payload is not None:
            preview = json.dumps(payload, ensure_ascii=False)
            print(f"JSON on stdin: {preview[:1200]}" + (" …" if len(preview) > 1200 else ""))
        if audio is not None:
            print(f"WAV on stdin: {len(audio)} bytes")
    started = time.monotonic()
    result = subprocess.run(
        docker.command + ["exec", "-T", "localai", "sh", "-c", script, "probe", path],
        env=docker.environment,
        input=data,
        capture_output=True,
        timeout=330,
        check=False,
    )
    elapsed = time.monotonic() - started
    body, _, code = result.stdout.rpartition(b"\n")
    status = int(code) if code.isdigit() else 0
    if show:
        print(f"HTTP {status or 'unavailable'} · {elapsed:.1f}s")
    if result.returncode:
        detail = result.stderr.decode(errors="replace")[-2000:]
        raise ValueError(f"Docker/SSH/curl failed (exit {result.returncode}): {detail}")
    if not 200 <= status < 300:
        raise ValueError(f"HTTP {status}: {body.decode(errors='replace')[:2000]}")
    return Response(body, status, elapsed)


def sample_image(document: bool = False) -> bytes:
    """Offline PNG: a readable test page or red/blue vision sample."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    width = height = 128
    pixels = (b"\0" + b"\xff\0\0" * 64 + b"\0\0\xff" * 64) * height
    if document:
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
        + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def read_sample(path: str) -> bytes:
    with Path(path).expanduser().open("rb") as file:
        data = file.read(LIMIT + 1)
    if not data or len(data) > LIMIT:
        raise ValueError("Choose a nonempty sample of at most 16 MiB")
    return data


def vad_samples(data: bytes) -> list[float]:
    """Downmix/resample a short PCM16 WAV for LocalAI's 16 kHz float API."""
    validate("tts", data)
    with wave.open(io.BytesIO(data)) as audio:
        channels, rate, frames = audio.getnchannels(), audio.getframerate(), audio.getnframes()
        if audio.getsampwidth() != 2 or frames > 30 * rate:
            raise ValueError("VAD requires PCM16 WAV audio of at most 30 seconds")
        samples = [int(s[0]) for s in struct.iter_unpack("<h", audio.readframes(frames))]
    mono: list[float] = [
        sum(samples[i : i + channels]) / (channels * 32768)
        for i in range(0, len(samples), channels)
    ]
    if rate == 16000:
        return mono + [0.0] * 8000
    converted: list[float] = []
    for i in range(int(frames * 16000 / rate)):
        position = i * rate / 16000
        left = int(position)
        fraction = position - left
        converted.append(mono[left] * (1 - fraction) + mono[min(left + 1, frames - 1)] * fraction)
    # The stock detector leaves end=0 while speech is open. Trailing silence
    # lets a finite sample close its last segment without changing the backend.
    return converted + [0.0] * 8000


def validate(kind: str, body: bytes) -> None:
    if kind == "ready":
        return
    if kind == "tts":
        try:
            with wave.open(io.BytesIO(body)) as audio:
                frames = audio.readframes(audio.getnframes())
                expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                if not frames or len(frames) != expected:
                    raise ValueError("WAV audio is empty or truncated")
        except (wave.Error, EOFError) as exc:
            raise ValueError("Expected valid WAV audio") from exc
        return
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    if kind == "models":
        valid = isinstance(value.get("data"), list)
    elif kind == "ner":
        valid = isinstance(value.get("entities"), list)
    elif kind == "stt":
        valid = isinstance(value.get("text"), str) and bool(value["text"].strip())
    elif kind == "vad":
        segments = value.get("segments")
        valid = isinstance(segments, list) and all(
            isinstance(s, dict)
            and isinstance(s.get("start"), (int, float))
            and isinstance(s.get("end"), (int, float))
            and 0 <= s["start"] <= s["end"]
            for s in segments
        )
    else:
        choices = value.get("choices")
        valid = (
            isinstance(choices, list)
            and bool(choices)
            and isinstance(choices[0], dict)
            and isinstance(choices[0].get("message"), dict)
            and isinstance(choices[0]["message"].get("content"), str)
            and bool(choices[0]["message"]["content"].strip())
        )
    if not valid:
        raise ValueError("Response is missing the expected output field")


def run_test(
    docker: Docker, kind: str, *, interactive: bool = True, audio: bytes | None = None
) -> bytes | None:
    label, path, alias = TESTS[kind]
    print(f"\n{label}")
    if alias:
        model_preflight(docker, alias)
    payload = None
    if kind in {"chat", "vision", "documents"}:
        text = "Say hello briefly."
        content: str | list[dict[str, Any]] = text
        if kind != "chat":
            sample = (
                questionary.path("Local PNG/JPEG (blank: built-in sample):").ask()
                if interactive
                else ""
            )
            if sample is None:
                return None
            image = read_sample(sample) if sample else sample_image(document=kind == "documents")
            if image.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif image.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            else:
                raise ValueError("Choose a PNG or JPEG image")
            text = (
                "Convert this page to docling."
                if kind == "documents"
                else ("Describe the colors in this image.")
            )
            content = [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"},
                },
            ]
        elif interactive:
            text = questionary.text("Prompt:", default=text).ask()
            if text is None:
                return None
            content = text
        payload = {
            "model": alias,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 256,
            "stream": False,
        }
    elif kind in {"tts", "ner"}:
        text = (
            SENTENCE
            if kind == "tts"
            else (
                "Anna Beispiel wohnt in der Musterstraße 12 in Berlin. "
                "Ihre E-Mail ist anna@example.com."
            )
        )
        if interactive:
            text = questionary.text("Sample text:", default=text).ask()
            if text is None:
                return None
        payload = (
            {"model": alias, "input": text, "voice": "default", "response_format": "wav"}
            if kind == "tts"
            else {"text": text, "detectors": [alias]}
        )
    elif kind in {"stt", "vad"}:
        if interactive:
            sample = questionary.path("Local WAV (blank: generate using tts-german):").ask()
            if sample is None:
                return None
            audio = read_sample(sample) if sample else run_test(docker, "tts", interactive=False)
        if audio is None:
            print("SKIP: no speech sample; run TTS or choose a local WAV.")
            return None
        validate("tts", audio)
        if kind == "vad":
            payload = {"model": alias, "audio": vad_samples(audio)}
    response = curl(docker, path, payload, audio if kind == "stt" else None)
    if kind != "tts":
        print(response.body.decode(errors="replace")[:12000])
    validate(kind, response.body)
    if kind == "tts":
        with tempfile.NamedTemporaryFile(
            prefix="localai-speech-", suffix=".wav", delete=False
        ) as f:
            f.write(response.body)
            print(f"Audio saved on workstation: {f.name}")
    print("PASS: successful HTTP response with expected structure. Review output quality manually.")
    return response.body


def menu() -> None:
    docker = Docker(DeploymentSettings())
    print("Tests run on the remote target through Docker/SSH; inference can load models into VRAM.")
    choices = [questionary.Choice(label, value=kind) for kind, (label, _, _) in TESTS.items()]
    choices += [
        questionary.Choice("Live runtime / loaded models", value="runtime"),
        questionary.Choice("Run all available tests", value="all"),
        questionary.Choice("Back", value="back"),
    ]
    while True:
        kind = questionary.select("Test API endpoints", choices=choices).ask()
        if kind in (None, "back"):
            return
        if not questionary.confirm(
            f"Run {kind} on {docker.settings.docker_context}?", default=False
        ).ask():
            continue
        try:
            if kind == "runtime":
                runtime_status(docker)
            elif kind == "all":
                sample = questionary.path(
                    "Local speech WAV for STT/VAD (blank: use TTS result):"
                ).ask()
                if sample is None:
                    continue
                audio = read_sample(sample) if sample else None
                if audio is not None:
                    validate("tts", audio)
                run_all(docker, audio=audio)
            else:
                run_test(docker, kind)
        except Unavailable as exc:
            print(f"SKIP: {exc}")
        except (OSError, ValueError, wave.Error, subprocess.SubprocessError) as exc:
            print(f"FAIL: {exc}")


def run_all(docker: Docker, *, audio: bytes | None = None) -> None:
    results = []
    available: set[str] | None = None
    try:
        runtime_status(docker)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Runtime status unavailable: {exc}")
    for kind, (label, _, alias) in TESTS.items():
        if alias and (available is None or alias not in available):
            reason = "model listing failed" if available is None else f"{alias} not advertised"
            results.append(f"SKIP {label}: {reason}")
            print(results[-1])
            continue
        try:
            body = run_test(docker, kind, interactive=False, audio=audio)
            if kind == "models" and body is not None:
                available = {
                    item["id"]
                    for item in json.loads(body)["data"]
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
            if kind == "tts" and audio is None:
                audio = body
            results.append(f"{'PASS' if body is not None else 'SKIP'} {label}")
        except Unavailable as exc:
            results.append(f"SKIP {label}: {exc}")
            print(results[-1])
        except (OSError, ValueError, wave.Error, subprocess.SubprocessError) as exc:
            results.append(f"FAIL {label}: {exc}")
            print(results[-1])
    print("\nSummary\n" + "\n".join(results))
