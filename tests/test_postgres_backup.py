"""Protect whole-server coverage, fail-closed publication, and safe downloads."""

import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from importlib.resources import files
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from k8s_stack_tooling.postgres import backup


@pytest.fixture(scope="module")
def recipient(tmp_path_factory):
    directory = tmp_path_factory.mktemp("recipient")
    key, cert = directory / "key.pem", directory / "certificate.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=backup-test",
            "-days",
            "1",
        ],
        check=True,
        capture_output=True,
    )
    return key, cert


@pytest.fixture
def native(tmp_path, monkeypatch):
    runtime, output, binaries = (tmp_path / name for name in ("runtime", "output", "bin"))
    for directory in runtime, output, binaries:
        directory.mkdir()
    client = binaries / "client"
    client.write_text(
        f"#!{sys.executable}\n"
        "import io,json,os,pathlib,sys,tarfile\n"
        "mode=os.environ.get('FAIL_BACKUP','')\n"
        "name=pathlib.Path(sys.argv[0]).name\n"
        "if name=='pg_basebackup':\n"
        " with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:\n"
        "  for name,data in [('global/1260',b'example role'),('base/16384/data',b'new db;$')]:\n"
        "   info=tarfile.TarInfo(name); info.size=len(data)\n"
        "   archive.addfile(info,io.BytesIO(data))\n"
        " sys.exit(1 if mode=='dump' else 0)\n"
        "after=any('json_agg' in a for a in sys.argv)\n"
        "if not after: sys.stdin.read()\n"
        "if mode=='discovery': sys.exit(1)\n"
        "databases=['new db;$','postgres','template0','template1']\n"
        "if after:\n"
        " print(json.dumps(databases+(['added_during_dump'] if mode=='changed' else [])))\n"
        "elif mode=='malformed': print('not json')\n"
        "else: print(json.dumps({'databases':databases,'globalsIncluded':True}))\n"
    )
    client.chmod(0o755)
    for name in "pg_basebackup", "psql":
        (binaries / name).symlink_to(client)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    monkeypatch.setenv("BACKUP_RUNTIME", str(runtime))
    monkeypatch.setenv("BACKUP_WAIT_SECONDS", "5")
    return runtime, output


def execute(native, cert):
    runtime, output = native
    with ThreadPoolExecutor() as pool:
        collecting = pool.submit(backup.collect, runtime, output, cert, "operations", 10)
        result = subprocess.run(
            ["/bin/bash", str(files("k8s_stack_tooling.postgres").joinpath("backup.sh"))],
            capture_output=True,
            timeout=15,
        )
        return result, collecting.result()


def decrypt(path, key, cert):
    return subprocess.run(
        [
            "openssl",
            "cms",
            "-decrypt",
            "-binary",
            "-inform",
            "DER",
            "-in",
            str(path),
            "-recip",
            str(cert),
            "-inkey",
            str(key),
        ],
        capture_output=True,
        check=True,
    ).stdout


def test_all_databases_globals_and_standard_encryption(native, recipient, tmp_path):
    key, cert = recipient
    result, archive = execute(native, cert)
    assert result.returncode == 0
    extracted = tmp_path / "extracted"
    with tarfile.open(archive) as source:
        source.extractall(extracted, filter="data")
    manifest = json.loads(decrypt(extracted / "manifest.json.cms", key, cert))
    assert manifest["databases"] == ["new db;$", "postgres", "template0", "template1"]
    assert manifest["globalsIncluded"] is True
    physical = gzip.decompress(decrypt(extracted / "cluster.tar.gz.cms", key, cert))
    with tarfile.open(fileobj=io.BytesIO(physical)) as source:
        role, data = source.extractfile("global/1260"), source.extractfile("base/16384/data")
        assert role is not None and role.read() == b"example role"
        assert data is not None and data.read() == b"new db;$"
    assert (archive.parent / f"{archive.name}.sha256").read_text().split()[0] == backup.digest(
        archive
    )
    for line in (extracted / "SHA256SUMS").read_text().splitlines():
        expected, member = line.split()
        assert backup.digest(extracted / member) == expected
    ciphertext = extracted / "cluster.tar.gz.cms"
    damaged = bytearray(ciphertext.read_bytes())
    damaged[len(damaged) // 2] ^= 1
    ciphertext.write_bytes(damaged)
    with pytest.raises(subprocess.CalledProcessError):
        decrypt(ciphertext, key, cert)


@pytest.mark.parametrize("failure", ["discovery", "dump", "malformed", "changed"])
def test_failure_never_publishes_or_replaces_previous_backup(
    native,
    recipient,
    monkeypatch,
    failure,
):
    _, cert = recipient
    runtime, output = native
    previous = output / "previous.tar"
    previous.write_bytes(b"previous encrypted backup")
    monkeypatch.setenv("FAIL_BACKUP", failure)
    with pytest.raises(ValueError):
        execute(native, cert)
    assert list(output.iterdir()) == [previous]
    assert previous.read_bytes() == b"previous encrypted backup"
    assert not (runtime / "manifest.json").exists()


def test_invalid_recipient_has_no_plaintext_fallback(native, tmp_path):
    cert = tmp_path / "invalid.pem"
    cert.write_text("not a certificate")
    runtime, output = native
    with pytest.raises(ValueError):
        backup.collect(runtime, output, cert, "auth", 1)
    assert not list(output.iterdir())


def test_encryption_failure_does_not_publish(native, recipient, monkeypatch):
    _, cert = recipient
    runtime, output = native
    monkeypatch.setattr(
        backup, "encrypt", lambda *a: (_ for _ in ()).throw(ValueError("encryption"))
    )
    with pytest.raises(ValueError):
        backup.collect(runtime, output, cert, "auth", 1)
    assert not list(output.iterdir())


def test_download_serves_only_completed_encrypted_artifacts(tmp_path):
    name = "postgres-auth-2026-11-27T12-00-00Z-abcdef123456.tar"
    (tmp_path / name).write_bytes(b"encrypted archive")
    (tmp_path / "plaintext.sql").write_text("private")
    (tmp_path / f".{name}.partial").write_bytes(b"partial")
    symlink = name.replace("abcdef", "123456")
    (tmp_path / symlink).symlink_to(tmp_path / "plaintext.sql")
    with ThreadingHTTPServer(("127.0.0.1", 0), backup.handler(tmp_path)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{base}/{name}") as response:
                assert response.read() == b"encrypted archive"
            for path in (
                "",
                "plaintext.sql",
                f".{name}.partial",
                symlink,
                "../plaintext.sql",
                "%2e%2e%2fplaintext.sql",
            ):
                with pytest.raises(HTTPError) as error:
                    urlopen(f"{base}/{path}")
                assert error.value.code == 404
        finally:
            server.shutdown()
            thread.join()
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        backup.handler(alias)
