"""Record, verify and provision the default Chatterbox voice."""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from inference_runtime_manager.installer.api_tests import request, validate_wav
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker

RECORDING_SECONDS = 80
SAMPLE_RATE = 24_000
VOICE_LIMIT = 16 * 1024 * 1024

GERMAN_SCRIPT = """Guten Tag. Ich nehme heute meine Stimme auf, damit ein Sprachsystem später klar,
ruhig und natürlich auf Deutsch sprechen kann. Ich lese diesen Text in meinem normalen Tempo
und mit einer entspannten Lautstärke. Am frühen Morgen öffne ich das Fenster und höre Vögel,
Fahrräder und einen Zug in der Ferne. Danach koche ich Kaffee und plane den kommenden Tag.

Manchmal klingt eine Frage neugierig: Kommst du morgen mit? Eine deutliche Antwort lautet:
Ja, sehr gern, aber erst nach zwölf Uhr. Zahlen und Daten gehören ebenfalls dazu. Heute ist
Mittwoch, der dreiundzwanzigste September zweitausendsechsundzwanzig. Der Preis beträgt
achtzehn Euro und fünfundvierzig Cent. Die Ziffern reichen von null bis neun.

Jetzt folgen verschiedene Wörter und Laute: München, Köln, Zürich, Straße, Größe, Bücher,
Quelle, Qualität, Psychologie, Rhythmus, Frühling und außergewöhnlich. Peter bringt Paula
ein Paket, während Franz fröhlich pfeift. Zum Schluss werde ich etwas lebendiger: Das ist
wirklich eine gute Nachricht! Dann wieder ruhig: Alles ist in Ordnung. Vielen Dank fürs
Zuhören; diese Aufnahme ist nun beendet."""


def _ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ValueError(
            "FFmpeg is required (macOS: install it yourself with `brew install ffmpeg`)"
        )
    return ffmpeg


def microphones() -> list[tuple[str, str]]:
    if sys.platform != "darwin":
        return [("System default", "default")]
    result = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
        check=False,
    )
    devices: list[tuple[str, str]] = []
    in_audio_section = False
    for line in result.stderr.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio_section = True
            continue
        if in_audio_section and (match := re.search(r"\]\s+\[(\d+)\]\s+(.+)$", line)):
            devices.append((match.group(2), match.group(1)))
    return devices or [("System default", "default")]


def _ffmpeg_command(output: Path, microphone: str = "default") -> list[str]:
    ffmpeg = _ffmpeg()
    common = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if sys.platform == "darwin":
        source = ["-f", "avfoundation", "-i", f"none:{microphone}"]
    elif sys.platform.startswith("linux"):
        source = ["-f", "pulse", "-i", "default"]
    else:
        raise ValueError("Voice recording is supported on macOS and PulseAudio Linux workstations")
    return (
        common
        + source
        + [
            "-t",
            str(RECORDING_SECONDS),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(output),
        ]
    )


def record(output: Path, microphone: str = "default") -> None:
    process = subprocess.Popen(
        _ffmpeg_command(output, microphone),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    columns = [TextColumn("Recording"), BarColumn(), TimeRemainingColumn()]
    try:
        with Progress(*columns) as progress:
            task = progress.add_task("voice", total=RECORDING_SECONDS)
            while process.poll() is None:
                progress.update(task, completed=min(time.monotonic() - started, RECORDING_SECONDS))
                time.sleep(0.1)
            progress.update(task, completed=RECORDING_SECONDS)
    except BaseException:
        process.terminate()
        process.wait()
        output.unlink(missing_ok=True)
        raise
    stderr = process.stderr.read() if process.stderr is not None else ""
    if process.returncode:
        output.unlink(missing_ok=True)
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown FFmpeg error"
        raise ValueError(f"Microphone recording failed: {detail}")


def validate_recording(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            rate = audio.getframerate()
            frames = audio.getnframes()
            if (channels, width, rate) != (1, 2, SAMPLE_RATE):
                raise ValueError("Recording must be mono 24 kHz 16-bit PCM WAV")
            duration = frames / rate
            if not 78 <= duration <= 81:
                raise ValueError(f"Recording duration is {duration:.1f}s; expected 80 seconds")
    except (EOFError, wave.Error) as exc:
        raise ValueError("FFmpeg did not produce a valid PCM WAV recording") from exc
    if frames == 0:
        raise ValueError("Recording is empty")
    return duration


def play(path: Path) -> None:
    player = shutil.which("afplay") if sys.platform == "darwin" else shutil.which("ffplay")
    if player is None:
        raise ValueError("No supported audio player found (`afplay` or `ffplay`)")
    command = [player, str(path)]
    if Path(player).name == "ffplay":
        command = [player, "-nodisp", "-autoexit", "-loglevel", "error", str(path)]
    subprocess.run(command, check=True)


def upload_candidate(docker: Docker, path: Path) -> None:
    data = path.read_bytes()
    if not data or len(data) > VOICE_LIMIT:
        raise ValueError("Recorded voice must be between 1 byte and 16 MiB")
    digest = hashlib.sha256(data).hexdigest()
    result = docker.worker(
        {
            "action": "receive_voice",
            "content": base64.b64encode(data).decode(),
            "sha256": digest,
        }
    )
    if result != {"staged": True, "sha256": digest}:
        raise ValueError("Default voice upload returned an unexpected receipt")


def _speech(docker: Docker, voice: str, text: str) -> bytes:
    audio = request(
        docker,
        "tts-german",
        "/v1/audio/speech",
        payload={
            "model": "chatterbox-multilingual",
            "input": text,
            "voice": voice,
            "response_format": "wav",
            "language": "de",
        },
    )
    validate_wav(audio)
    return audio


def provision(docker: Docker, recording: Path, output: Path) -> None:
    try:
        upload_candidate(docker, recording)
        docker.worker({"action": "configure"})
        docker.run(
            "up",
            "--pull",
            "never",
            "-d",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "300",
            "tts-german",
        )
        short_output = output.with_name("generated-short.wav")
        print("Generating the full comparison reading...")
        output.write_bytes(_speech(docker, ".candidate.wav", GERMAN_SCRIPT))
        print("Generating the one-line comparison...")
        short_output.write_bytes(
            _speech(
                docker,
                ".candidate.wav",
                "Guten Tag. So klingt meine neue Standardstimme.",
            )
        )
        for label, sample in (
            ("Original recording", recording),
            ("Generated reading of the same text", output),
            ("Generated one-line test", short_output),
        ):
            print(f"Playing: {label}")
            play(sample)
        if questionary.confirm("Use this generated voice as the default?", default=True).ask():
            docker.worker({"action": "promote_voice"})
            print("The Chatterbox default voice is now provisioned as `default`.")
        else:
            docker.worker({"action": "discard_voice"})
            print("Candidate discarded; the previous default voice is unchanged.")
    except BaseException:
        try:
            docker.worker({"action": "discard_voice"})
        except (OSError, subprocess.CalledProcessError, ValueError):
            pass
        raise


def menu() -> None:
    Console().print(
        Panel(
            GERMAN_SCRIPT,
            title="German voice recording script (80 seconds)",
            subtitle="Read naturally; the recording stops automatically",
        )
    )
    print("The recording is temporary and is deleted after this workflow exits.")
    print("Bluetooth headset microphones often sound distorted in hands-free mode.")
    print("For voice cloning, prefer the Mac's built-in microphone or a USB/wired microphone.")
    if not questionary.confirm("Select a microphone and start recording?", default=False).ask():
        return
    choices = [questionary.Choice(name, value=device) for name, device in microphones()]
    microphone = questionary.select("Microphone", choices=choices).ask()
    if microphone is None:
        return
    with tempfile.TemporaryDirectory(prefix="inference-voice-") as folder:
        recording = Path(folder) / "default.wav"
        generated = Path(folder) / "generated.wav"
        while True:
            record(recording, microphone)
            duration = validate_recording(recording)
            print(f"Recorded {duration:.1f}s.")
            play(recording)
            action = questionary.select(
                "Recording",
                choices=["Provision this recording", "Record again", "Cancel"],
            ).ask()
            if action == "Record again":
                recording.unlink(missing_ok=True)
                continue
            if action != "Provision this recording":
                return
            break
        settings = DeploymentSettings()
        if not questionary.confirm(
            f"Provision to {settings.docker_context} and recreate only tts-german?",
            default=False,
        ).ask():
            return
        provision(Docker(settings), recording, generated)
