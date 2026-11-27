"""Stream a whole-cluster physical backup into public-key encrypted artifacts."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ARTIFACT = re.compile(r"postgres-(auth|operations)-[0-9TZ-]+-[0-9a-f]{12}\.tar(?:\.sha256)?")


def wait_for(path: Path, deadline: float) -> None:
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Backup producer did not complete before the deadline")
        time.sleep(0.2)


def encrypt(
    source: Path, destination: Path, certificate: Path, deadline: float, status: Path | None = None
) -> None:
    """Use standard CMS AuthEnvelopedData; the private key never enters the Job."""
    with destination.open("xb") as output:
        with subprocess.Popen(
            [
                "openssl",
                "cms",
                "-encrypt",
                "-binary",
                "-stream",
                "-outform",
                "DER",
                "-aes-256-gcm",
                "-in",
                str(source),
                "-recip",
                str(certificate),
                "-keyopt",
                "rsa_padding_mode:oaep",
                "-keyopt",
                "rsa_oaep_md:sha256",
                "-keyopt",
                "rsa_mgf1_md:sha256",
            ],
            stdout=output,
            stderr=subprocess.DEVNULL,
        ) as process:
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Backup encryption exceeded its deadline")
                    if status is not None and status.exists() and status.read_text().strip() != "0":
                        raise ValueError("Database export failed")
                    time.sleep(0.1)
                if process.returncode != 0:
                    raise ValueError("Backup encryption failed")
            except BaseException:
                process.kill()
                process.wait()
                raise


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def collect(runtime: Path, output: Path, certificate: Path, server: str, timeout: int) -> Path:
    """Publish only after producer success, inventory agreement, and encryption."""
    if server not in {"auth", "operations"}:
        raise ValueError("Unknown backup server")
    recipient = certificate.read_bytes()
    if b"PRIVATE KEY" in recipient:
        raise ValueError("Only the public recipient certificate belongs in the cluster")
    cert = x509.load_pem_x509_certificate(recipient)
    key = cert.public_key()
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError("Backup recipient requires an RSA certificate of at least 3072 bits")
    now = datetime.now(UTC)
    if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
        raise ValueError("Backup recipient certificate is outside its validity period")
    deadline = time.monotonic() + timeout
    fifo = runtime / "dump.fifo"
    os.mkfifo(fifo, 0o600)
    name = f"postgres-{server}-{now:%Y-%m-%dT%H-%M-%SZ}-{uuid.uuid4().hex[:12]}.tar"
    final = output / name
    try:
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=output) as temporary:
            work = Path(temporary)
            # The reader monitors producer failure even before the FIFO opens.
            encrypt(fifo, work / "cluster.tar.gz.cms", certificate, deadline, runtime / "status")
            wait_for(runtime / "status", deadline)
            if (runtime / "status").read_text().strip() != "0":
                raise ValueError("Database export failed")
            manifest = json.loads((runtime / "manifest.json").read_text())
            databases = manifest["databases"]
            if (
                not isinstance(databases, list)
                or not databases
                or not all(isinstance(name, str) for name in databases)
                or "postgres" not in databases
                or len(databases) != len(set(databases))
                or databases != json.loads((runtime / "databases-after.json").read_text())
                or manifest.get("globalsIncluded") is not True
            ):
                raise ValueError("Database inventory is incomplete or changed during backup")
            manifest["server"] = server
            manifest["nativeClientImage"] = os.environ.get("BACKUP_NATIVE_IMAGE", "unrecorded")
            manifest["toolingImage"] = os.environ.get("BACKUP_TOOLING_IMAGE", "unrecorded")
            manifest["completedAt"] = datetime.now(UTC).isoformat()
            manifest["recipientSha256"] = hashlib.sha256(
                cert.public_bytes(serialization.Encoding.DER)
            ).hexdigest()
            (runtime / "manifest.json").write_text(json.dumps(manifest))
            encrypt(runtime / "manifest.json", work / "manifest.json.cms", certificate, deadline)
            members = ["cluster.tar.gz.cms", "manifest.json.cms"]
            (work / "SHA256SUMS").write_text(
                "".join(f"{digest(work / member)}  {member}\n" for member in members)
            )
            staged = work / name
            with tarfile.open(staged, "w") as archive:
                for member in [*members, "SHA256SUMS"]:
                    archive.add(work / member, arcname=member, recursive=False)
            checksum = f"{digest(staged)}  {name}\n"
            (work / f"{name}.sha256").write_text(checksum)
            # The archive appears only when complete. Existing runs are never overwritten.
            os.replace(work / f"{name}.sha256", output / f"{name}.sha256")
            os.replace(staged, final)
        return final
    finally:
        fifo.unlink(missing_ok=True)
        for private in ("manifest.json", "databases-after.json", "dump-error"):
            (runtime / private).unlink(missing_ok=True)


def handler(root: Path) -> type[BaseHTTPRequestHandler]:
    """Serve explicit completed encrypted artifacts only; no listing or logging."""
    if any(path.is_symlink() for path in [root, *root.parents]):
        raise ValueError("Backup download directory must not contain symlinks")
    root = root.resolve(strict=True)

    class Download(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            name = unquote(urlsplit(self.path).path).removeprefix("/")
            if not ARTIFACT.fullmatch(name):
                self.send_error(404)
                return
            try:
                path = root / name
                # Checksums must have a corresponding completed archive.
                if name.endswith(".sha256") and not (root / name.removesuffix(".sha256")).is_file():
                    raise FileNotFoundError
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as source:
                    size = os.fstat(source.fileno())
                    if not stat.S_ISREG(size.st_mode):
                        raise FileNotFoundError
                    self.send_response(200)
                    self.send_header("Content-Length", str(size.st_size))
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    shutil.copyfileobj(source, self.wfile)
            except (FileNotFoundError, OSError):
                self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Download


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("--runtime", type=Path, default=Path("/runtime"))
    backup = sub.add_parser("collect")
    backup.add_argument("--runtime", type=Path, default=Path("/runtime"))
    backup.add_argument("--output", type=Path, default=Path("/backups"))
    backup.add_argument("--certificate", type=Path, default=Path("/recipient/certificate.pem"))
    backup.add_argument("--server", required=True, choices=["auth", "operations"])
    backup.add_argument("--timeout", type=int, default=3300)
    serve = sub.add_parser("serve")
    serve.add_argument("--output", type=Path, default=Path("/backups"))
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "stage":
        (args.runtime / "backup.sh").write_bytes(
            files("k8s_stack_tooling.postgres").joinpath("backup.sh").read_bytes()
        )
    elif args.command == "collect":
        try:
            result = collect(
                args.runtime,
                args.output,
                args.certificate,
                args.server,
                args.timeout,
            )
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError, KeyError):
            (args.runtime / "collector-failed").touch()
            print("Backup failed; no completed archive was published", flush=True)
            return 1
        print(f"Completed encrypted archive: {result.name}", flush=True)
    else:
        # No Service or Ingress. Kubernetes port-forward authenticates the operator.
        ThreadingHTTPServer(("127.0.0.1", 8080), handler(args.output)).serve_forever()
    return 0
