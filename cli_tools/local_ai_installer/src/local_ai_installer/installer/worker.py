"""Standard-library-only one-shot worker, sent through the Docker context.

This updates model DATA and configuration, never LocalAI/backend source code.
The workstation stops LocalAI before seed/apply, and uploads while it can run.
"""

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
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path("/models")


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
    required = sum(r["size"] for r in manifest["files"])
    if shutil.disk_usage(ROOT).free < required + 256 * 1024 * 1024:
        raise ValueError("Insufficient remote disk space")
    with tempfile.TemporaryDirectory(dir=ROOT, prefix=".incoming-") as folder:
        stage = Path(folder) / "artifact"
        stage.mkdir()
        expected = {r["path"]: r for r in manifest["files"]}
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


def cache_view(base, repo, revision, target, records):
    """Publish a normal Hugging Face snapshot and return its uncommitted ref."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid Hugging Face cache identity")
    cache = base / ("models--" + repo.replace("/", "--"))
    snapshot = cache / "snapshots" / revision
    snapshot.mkdir(parents=True, exist_ok=True)
    for record in records:
        name = relative(record["path"])
        # Receipts are not Hugging Face model files.
        if name in {"artifact.yaml", "checksums.sha256"}:
            continue
        link = snapshot / name
        link.parent.mkdir(parents=True, exist_ok=True)
        source = target / name
        if link.is_symlink() and link.resolve() == source.resolve():
            continue
        if (
            link.is_file()
            and link.resolve().is_relative_to(ROOT)
            and sha256(link) == record["sha256"]
        ):
            # Different variants of one pinned repo can share companion files.
            continue
        if link.exists() or link.is_symlink():
            raise ValueError(f"Conflicting cache snapshot: {link}")
        link.symlink_to(source)
    return snapshot, cache / "refs/main"


def apply(payload):
    manifest = payload["manifest"]
    if not inspect(manifest):
        raise ValueError("Model must be fully staged before applying its definition")
    alias = payload["alias"]
    if not re.fullmatch(r"[a-z][a-z0-9-]*", alias):
        raise ValueError("Invalid model alias")
    target = ROOT / manifest["destination"]
    updates = []
    repo = payload.get("cache_repo")
    if repo:
        snapshot, ref = cache_view(
            ROOT / "cache/hub", repo, manifest["revision"], target, manifest["files"]
        )
        updates.append((ref, manifest["revision"].encode()))
        if repo == "ResembleAI/chatterbox":
            # Stock Chatterbox's Chinese helper passes its snapshot directory as
            # cache_dir. Populate that extra cache view too, without patching it.
            records = [r for r in manifest["files"] if r["path"] == "Cangjie5_TC.json"]
            _, nested_ref = cache_view(snapshot, repo, manifest["revision"], target, records)
            updates.append((nested_ref, manifest["revision"].encode()))
    updates.append((ROOT / f"{alias}.yaml", payload["yaml"].encode()))
    history = ROOT / ".installer/history"
    history.mkdir(parents=True, exist_ok=True)
    backups = {path: path.read_bytes() if path.exists() else None for path, _ in updates}
    old_definition = backups[ROOT / f"{alias}.yaml"]
    if old_definition:
        digest = hashlib.sha256(old_definition).hexdigest()
        atomic_write(history / f"{alias}-{digest}.yaml", old_definition)
    try:
        for path, data in updates:
            atomic_write(path, data)
    except BaseException:
        for path, data in backups.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
        raise


def pkuseg_cache(manifest):
    """Initialize stock package data while LocalAI is stopped; no code changes."""
    if not inspect(manifest):
        raise ValueError("Auxiliary archive has not been staged")
    records = manifest["files"]
    if len(records) != 1 or records[0]["path"] != "spacy_ontonotes.zip":
        raise ValueError("Unexpected tokenizer cache artifact")
    record = records[0]
    source = safe(ROOT, manifest["destination"]) / record["path"]
    cache_root = safe(ROOT, "cache")
    cache_root.mkdir(exist_ok=True)
    version = safe(cache_root, "pkuseg-" + record["sha256"])
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        names = [relative(item.filename.rstrip("/")) for item in members]
        if (
            len(set(names)) != len(names)
            or sum(item.file_size for item in members) > 512 * 1024 * 1024
        ):
            raise ValueError("Invalid auxiliary archive layout/size")
        if any((item.external_attr >> 16) & 0o170000 == 0o120000 for item in members):
            raise ValueError("Auxiliary archive symlinks are not supported")
        if not version.exists():
            with tempfile.TemporaryDirectory(dir=cache_root, prefix=".pkuseg-") as folder:
                stage = Path(folder) / "cache"
                extracted = stage / "spacy_ontonotes"
                extracted.mkdir(parents=True)
                for item, name in zip(members, names, strict=True):
                    target = safe(extracted, name)
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(item) as data, target.open("xb") as out:
                            shutil.copyfileobj(data, out)
                # Stock download_model checks for the ZIP, then loads extracted
                # files separately. Keeping only one of the two is insufficient.
                (stage / record["path"]).symlink_to(source)
                os.rename(stage, version)
        for item, name in zip(members, names, strict=True):
            if item.is_dir():
                continue
            target = safe(version / "spacy_ontonotes", name)
            expected = hashlib.sha256()
            with archive.open(item) as data:
                while chunk := data.read(1024 * 1024):
                    expected.update(chunk)
            if sha256(target) != expected.hexdigest():
                raise ValueError("Existing tokenizer cache is corrupt")
        if sha256(version / record["path"]) != record["sha256"]:
            raise ValueError("Tokenizer ZIP cache is corrupt")
    active = cache_root / "pkuseg"
    if active.exists() and not active.is_symlink():
        raise ValueError("Refusing to replace an unmanaged tokenizer cache directory")
    temporary = cache_root / (".pkuseg-link-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(version, target_is_directory=True)
        os.replace(temporary, active)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    line = sys.stdin.buffer.readline(16 * 1024 * 1024)
    if not line.endswith(b"\n"):
        raise ValueError("Missing or oversized job header")
    payload = json.loads(line)
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / ".installer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        action = payload["action"]
        if action == "seed":
            for alias, definition in payload["definitions"].items():
                if not re.fullmatch(r"[a-z][a-z0-9-]*", alias):
                    raise ValueError("Invalid alias")
                target = ROOT / f"{alias}.yaml"
                if not target.exists():
                    atomic_write(target, definition.encode())
            result = {"seeded": True}
        elif action == "inspect":
            result = {"present": inspect(payload["manifest"])}
        elif action == "receive":
            receive(payload["manifest"])
            result = {"published": True}
        elif action == "apply":
            apply(payload)
            result = {"applied": payload["alias"]}
        elif action == "check-space":
            required = payload["required_bytes"]
            if type(required) is not int or required < 0:
                raise ValueError("Invalid unpacking space requirement")
            if shutil.disk_usage("/backends").free < required:
                raise ValueError("Insufficient free space for offline backend installation")
            model_bytes = payload.get("model_bytes", 0)
            if (
                type(model_bytes) is not int
                or model_bytes < 0
                or shutil.disk_usage(ROOT).free < model_bytes
            ):
                raise ValueError("Insufficient free space for auxiliary model cache")
            result = {"space_ready": True}
        elif action == "pkuseg-cache":
            pkuseg_cache(payload["manifest"])
            result = {"cache_ready": True}
        else:
            raise ValueError("Unknown setup action")
        print(json.dumps(result))


if __name__ == "__main__":
    main()
