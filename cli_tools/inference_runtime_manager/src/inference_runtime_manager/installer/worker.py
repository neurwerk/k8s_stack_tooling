"""Standard-library-only model-volume worker sent through the Docker context."""

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path("/models")
CONFIG_ROOT = Path("/runtime-config")
VOICE_ROOT = CONFIG_ROOT / "voices"
VOICE_CANDIDATE = VOICE_ROOT / ".candidate.wav"
VOICE_DEFAULT = VOICE_ROOT / "default"
VOICE_LIMIT = 16 * 1024 * 1024


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Invalid relative path")
    if PurePosixPath(value).is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ValueError("Unsafe relative path")
    if any(ord(character) < 32 for character in value):
        raise ValueError("Control characters in path")
    return value


def safe(root, name):
    path = root
    for part in relative(name).split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Unexpected artifact symlink: {path}")
    return path


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".write-", delete=False) as out:
        temporary = Path(out.name)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    temporary.chmod(0o644)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def receive_voice(payload):
    content = payload.get("content")
    digest = payload.get("sha256")
    if not isinstance(content, str):
        raise ValueError("Invalid voice content")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid voice checksum")
    try:
        data = base64.b64decode(content, validate=True)
    except ValueError as exc:
        raise ValueError("Invalid voice encoding") from exc
    if not 0 < len(data) <= VOICE_LIMIT:
        raise ValueError("Invalid voice size")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Voice checksum mismatch")
    atomic_write(VOICE_CANDIDATE, data)
    return {"staged": True, "sha256": digest}


def promote_voice():
    if not VOICE_CANDIDATE.is_file():
        raise ValueError("No verified voice candidate exists")
    os.replace(VOICE_CANDIDATE, VOICE_DEFAULT)


def discard_voice():
    VOICE_CANDIDATE.unlink(missing_ok=True)


def validate(manifest):
    destination = relative(manifest["destination"])
    if not destination.startswith("library/"):
        raise ValueError("Artifacts must live in library/")
    names = set()
    for item in manifest["files"]:
        name = relative(item["path"])
        if name in names or name == ".receipt.json":
            raise ValueError("Duplicate or reserved artifact filename")
        if type(item["size"]) is not int or item["size"] < 0:
            raise ValueError("Invalid artifact size")
        if not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ValueError("Invalid checksum")
        names.add(name)
    if not names:
        raise ValueError("Empty artifact")


def verify(root, manifest):
    for item in manifest["files"]:
        path = safe(root, item["path"])
        if not path.is_file() or path.stat().st_size != item["size"]:
            raise ValueError(f"Missing/resized file: {item['path']}")
        if sha256(path) != item["sha256"]:
            raise ValueError(f"Checksum mismatch: {item['path']}")


def inspect(manifest):
    validate(manifest)
    target = safe(ROOT, manifest["destination"])
    if not target.exists():
        return False
    if json.loads((target / ".receipt.json").read_text()) != manifest:
        raise ValueError("Conflicting remote artifact; existing data preserved")
    verify(target, manifest)
    return True


def receive(manifest):
    validate(manifest)
    target = safe(ROOT, manifest["destination"])
    if target.exists():
        raise ValueError("Destination exists; inspect before upload")
    required = sum(record["size"] for record in manifest["files"])
    if shutil.disk_usage(ROOT).free < required + 256 * 1024 * 1024:
        raise ValueError("Insufficient remote disk space")
    with tempfile.TemporaryDirectory(dir=ROOT, prefix=".incoming-") as folder:
        stage = Path(folder) / "artifact"
        stage.mkdir()
        expected = {record["path"]: record for record in manifest["files"]}
        seen = set()
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
            for member in archive:
                name = relative(member.name)
                if not member.isfile() or name not in expected or name in seen:
                    raise ValueError("Unexpected archive member")
                if member.size != expected[name]["size"]:
                    raise ValueError("Wrong archive member size")
                path = safe(stage, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Missing archive data")
                with source, path.open("xb") as out:
                    shutil.copyfileobj(source, out, length=8 * 1024 * 1024)
                path.chmod(0o644)
                seen.add(name)
        if seen != set(expected):
            raise ValueError("Incomplete archive")
        verify(stage, manifest)
        (stage / ".receipt.json").write_text(json.dumps(manifest, sort_keys=True))
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(stage, target)


def cache_view(repo, revision, target, records, base=None):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid Hugging Face cache identity")
    cache = (base or ROOT / "cache/hub") / ("models--" + repo.replace("/", "--"))
    snapshot = cache / "snapshots" / revision
    snapshot.mkdir(parents=True, exist_ok=True)
    for record in records:
        name = relative(record["path"])
        if name in {"artifact.yaml", "checksums.sha256"}:
            continue
        link = snapshot / name
        link.parent.mkdir(parents=True, exist_ok=True)
        source = target / name
        if link.is_symlink() and link.resolve() == source.resolve():
            continue
        if link.exists() or link.is_symlink():
            raise ValueError(f"Conflicting cache snapshot: {link}")
        link.symlink_to(source)
    atomic_write(cache / "refs/main", revision.encode())
    return snapshot


def activate(payload):
    manifest = payload["manifest"]
    if not inspect(manifest):
        raise ValueError("Model must be fully staged before activation")
    alias = payload["alias"]
    if not re.fullmatch(r"[a-z][a-z0-9-]*", alias):
        raise ValueError("Invalid model alias")
    target = safe(ROOT, manifest["destination"])
    if payload.get("cache_repo"):
        snapshot = cache_view(
            payload["cache_repo"], manifest["revision"], target, manifest["files"]
        )
        if payload["cache_repo"] == "ResembleAI/chatterbox":
            records = [r for r in manifest["files"] if r["path"] == "Cangjie5_TC.json"]
            cache_view(payload["cache_repo"], manifest["revision"], target, records, snapshot)
    active = ROOT / "active"
    active.mkdir(exist_ok=True)
    link = active / alias
    if link.exists() and not link.is_symlink():
        raise ValueError("Refusing to replace unmanaged active model data")
    temporary = active / ("." + alias + "-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def configure():
    chatterbox = b"""server:\n  host: 0.0.0.0\n  port: 8004\n  use_ngrok: false\n  use_auth: false\n  log_file_path: /tmp/chatterbox.log\nmodel:\n  repo_id: chatterbox-multilingual\ntts_engine:\n  device: cuda\n  predefined_voices_path: /runtime-config/voices\n  reference_audio_path: /tmp/reference_audio\n  default_voice_id: default\npaths:\n  model_cache: /models/cache\n  output: /tmp/outputs\ngeneration_defaults:\n  temperature: 0.8\n  exaggeration: 1.0\n  cfg_weight: 0.5\n  seed: 0\n  speed_factor: 1.0\n  language: de\naudio_output:\n  format: wav\n  sample_rate: 24000\n  max_reference_duration_sec: 30\n  save_to_disk: false\nui:\n  title: Chatterbox TTS Server\n  show_language_select: true\n  max_predefined_voices_in_dropdown: 0\ndebug:\n  save_intermediate_audio: false\n"""
    VOICE_ROOT.mkdir(parents=True, exist_ok=True)
    atomic_write(CONFIG_ROOT / "chatterbox.yaml", chatterbox)
    aliases = json.dumps(
        {"stt-general": "Systran/faster-whisper-large-v3"}, sort_keys=True
    ).encode()
    atomic_write(CONFIG_ROOT / "model_aliases.json", aliases + b"\n")


def main():
    line = sys.stdin.buffer.readline(16 * 1024 * 1024)
    if not line.endswith(b"\n"):
        raise ValueError("Missing or oversized job header")
    payload = json.loads(line)
    ROOT.mkdir(parents=True, exist_ok=True)
    state = ROOT / ".installer"
    state.mkdir(exist_ok=True)
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        action = payload["action"]
        if action == "inspect":
            result = {"present": inspect(payload["manifest"])}
        elif action == "receive":
            receive(payload["manifest"])
            result = {"published": True}
        elif action == "activate":
            activate(payload)
            result = {"activated": payload["alias"]}
        elif action == "configure":
            configure()
            result = {"configured": True}
        elif action == "receive_voice":
            result = receive_voice(payload)
        elif action == "promote_voice":
            promote_voice()
            result = {"promoted": True}
        elif action == "discard_voice":
            discard_voice()
            result = {"discarded": True}
        else:
            raise ValueError("Unknown setup action")
        print(json.dumps(result))


if __name__ == "__main__":
    main()
