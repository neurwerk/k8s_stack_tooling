"""Generate and validate offline OpenBao custodian packages."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path


class CustodyError(RuntimeError):
    """Raised when custody material cannot be handled safely."""


@dataclass(frozen=True)
class CustodyPaths:
    """Identify the private paths owned by one client bootstrap."""

    root: Path
    seal_file: Path
    package_dir: Path


@dataclass(frozen=True)
class CustodianKey:
    """Hold one generated passwordless OpenPGP key pair in memory."""

    fingerprint: str
    public_key: str
    private_key: str


@dataclass(frozen=True)
class PackageMetadata:
    """Bind one recovery package to a cluster and recovery ceremony."""

    schema_version: int
    ceremony_id: str
    client: str
    cluster_id: str
    namespace_uid: str
    static_seal_key_id: str
    share_index: int
    recovery_shares: int
    recovery_threshold: int
    custodian_name: str
    fingerprint: str


@dataclass(frozen=True)
class CustodianPackage:
    """Contain the validated members of one custodian package."""

    metadata: PackageMetadata
    public_key: bytes
    private_key: bytes
    encrypted_share: bytes


_PACKAGE_MEMBERS = {
    "README.txt",
    "metadata.json",
    "private-key.asc",
    "public-key.asc",
    "recovery-share.pgp",
}
_MAX_PACKAGE_MEMBER_SIZE = 1024 * 1024
_WORKSPACE_ROOT_ENV = "OPENBAO_STACK_SETUP_WORKSPACE_ROOT"


def default_custody_root(client: str) -> Path:
    """Return the default client-specific custody root."""
    return Path.home() / ".local" / "share" / "neurwerk" / "openbao" / client


def prepare_custody_paths(root: Path) -> CustodyPaths:
    """Create or validate private custody directories."""
    expanded = root.expanduser()
    if expanded.is_symlink():
        raise CustodyError("Custody root must not be a symbolic link")
    try:
        resolved = expanded.resolve()
    except (OSError, RuntimeError):
        raise CustodyError("Custody directories could not be prepared") from None
    _require_outside_git_boundaries(resolved)
    try:
        if not resolved.exists():
            resolved.mkdir(mode=0o700, parents=True)
            resolved.chmod(0o700)
    except OSError:
        raise CustodyError("Custody directories could not be prepared") from None
    resolved = _revalidate_private_directory(resolved, expected=resolved)
    operator_dir = resolved / "operator-custody"
    package_dir = resolved / "custodian-packages"
    for directory in (operator_dir, package_dir):
        _revalidate_private_directory(resolved, expected=resolved)
        if directory.is_symlink():
            raise CustodyError("Custody directory must not be a symbolic link")
        try:
            if not directory.exists():
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
        except OSError:
            raise CustodyError("Custody directories could not be prepared") from None
        _revalidate_private_directory(directory, expected=directory)
        _revalidate_private_directory(resolved, expected=resolved)
    return CustodyPaths(resolved, operator_dir / "openbao-seal.json", package_dir)


def normalize_custodian_names(names: list[str]) -> tuple[str, str, str]:
    """Validate three distinct human-readable custodian names."""
    normalized = tuple(unicodedata.normalize("NFKC", name).strip() for name in names)
    if len(normalized) != 3:
        raise CustodyError("Exactly three custodian names are required")
    if any(
        not name
        or len(name) > 100
        or any(unicodedata.category(char).startswith("C") for char in name)
        for name in normalized
    ):
        raise CustodyError(
            "Custodian names must be nonblank printable text of at most 100 characters"
        )
    if len({name.casefold() for name in normalized}) != 3:
        raise CustodyError("Custodian names must be distinct")
    return normalized[0], normalized[1], normalized[2]


def generate_custodian_keys(names: tuple[str, str, str]) -> tuple[CustodianKey, ...]:
    """Generate three isolated RSA-4096 OpenPGP key pairs without passphrases."""
    gpg = shutil.which("gpg")
    if gpg is None:
        raise CustodyError("gpg is required to generate custodian packages")
    return tuple(_generate_custodian_key(gpg, name) for name in names)


def binary_public_key(public_key: str) -> bytes:
    """Convert an ASCII-armored public key to OpenBao's required binary form."""
    gpg = shutil.which("gpg")
    if gpg is None:
        raise CustodyError("gpg is required to prepare recovery public keys")
    with tempfile.TemporaryDirectory(prefix="stack-setup-gpg-") as directory:
        home = Path(directory)
        home.chmod(0o700)
        binary = _run_gpg(gpg, home, ["--dearmor"], input_data=public_key.encode("ascii"))
    if not binary:
        raise CustodyError("Custodian public key could not be converted to binary form")
    return binary


def package_path(package_dir: Path, share_index: int) -> Path:
    """Return the stable package path for one numbered share."""
    return package_dir / f"custodian-{share_index}.zip"


def write_custodian_package(
    path: Path,
    metadata: PackageMetadata,
    key: CustodianKey,
    encrypted_share: bytes,
) -> None:
    """Publish one complete package atomically without replacing an existing file."""
    expected = CustodianPackage(
        metadata=metadata,
        public_key=key.public_key.encode("utf-8"),
        private_key=key.private_key.encode("utf-8"),
        encrypted_share=encrypted_share,
    )
    if path.exists():
        if load_custodian_package(path) != expected:
            raise CustodyError("Existing custodian package does not match this bootstrap")
        return
    parent = path.parent
    _require_private_directory(parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("README.txt", _package_readme(metadata))
            archive.writestr(
                "metadata.json", json.dumps(asdict(metadata), indent=2, sort_keys=True) + "\n"
            )
            archive.writestr("private-key.asc", expected.private_key)
            archive.writestr("public-key.asc", expected.public_key)
            archive.writestr("recovery-share.pgp", encrypted_share)
        with temporary.open("rb") as output:
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if load_custodian_package(path) != expected:
                raise CustodyError(
                    "Existing custodian package does not match this bootstrap"
                ) from None
        _sync_directory(parent)
    except (OSError, zipfile.BadZipFile):
        raise CustodyError("Custodian package could not be written") from None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def load_custodian_package(path: Path) -> CustodianPackage:
    """Read and strictly validate one private custodian ZIP package."""
    try:
        is_file = path.is_file()
        mode = path.stat().st_mode & 0o777
        parent_mode = path.parent.stat().st_mode
    except OSError:
        raise CustodyError("Custodian package could not be read") from None
    if not is_file or mode != 0o600:
        raise CustodyError("Custodian package permissions must be 0600")
    if parent_mode & 0o022:
        raise CustodyError("Custodian package parent must not be group or world writable")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            _validate_package_entries(entries, names)
            readme = archive.read("README.txt")
            metadata_payload = archive.read("metadata.json")
            public_key = archive.read("public-key.asc")
            private_key = archive.read("private-key.asc")
            encrypted_share = archive.read("recovery-share.pgp")
    except (OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile):
        raise CustodyError("Custodian package could not be read") from None
    metadata = _load_metadata(metadata_payload)
    if not readme or not public_key or not private_key or not encrypted_share:
        raise CustodyError("Custodian package contains an empty member")
    return CustodianPackage(metadata, public_key, private_key, encrypted_share)


def decrypt_package_share(package: CustodianPackage) -> str:
    """Decrypt one packaged recovery share in an isolated temporary GnuPG home."""
    gpg = shutil.which("gpg")
    if gpg is None:
        raise CustodyError("gpg is required to decrypt custodian packages")
    with tempfile.TemporaryDirectory(prefix="stack-setup-gpg-") as directory:
        home = Path(directory)
        home.chmod(0o700)
        _run_gpg(gpg, home, ["--import"], input_data=package.public_key)
        if package.metadata.fingerprint not in _public_fingerprints(gpg, home):
            raise CustodyError("Custodian package public key does not match its metadata")
        _run_gpg(gpg, home, ["--import"], input_data=package.private_key)
        fingerprints = _secret_fingerprints(gpg, home)
        if package.metadata.fingerprint not in fingerprints:
            raise CustodyError("Custodian package private key does not match its metadata")
        result = _run_gpg(gpg, home, ["--decrypt"], input_data=package.encrypted_share)
    try:
        value = result.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise CustodyError("Custodian package recovery share has invalid plaintext") from None
    if not value or "\x00" in value:
        raise CustodyError("Custodian package recovery share has invalid plaintext")
    return value


def validate_package_set(
    packages: tuple[CustodianPackage, CustodianPackage], expected: PackageMetadata
) -> None:
    """Require two distinct packages bound to the expected recovery ceremony."""
    first, second = packages
    comparable = (
        "schema_version",
        "ceremony_id",
        "client",
        "cluster_id",
        "namespace_uid",
        "static_seal_key_id",
        "recovery_shares",
        "recovery_threshold",
    )
    if any(getattr(first.metadata, field) != getattr(expected, field) for field in comparable):
        raise CustodyError("Custodian package does not belong to the selected cluster")
    if any(getattr(second.metadata, field) != getattr(expected, field) for field in comparable):
        raise CustodyError("Custodian package does not belong to the selected cluster")
    if first.metadata.share_index == second.metadata.share_index:
        raise CustodyError("Custodian packages must contain distinct recovery shares")


def _generate_custodian_key(gpg: str, name: str) -> CustodianKey:
    with tempfile.TemporaryDirectory(prefix="stack-setup-gpg-") as directory:
        home = Path(directory)
        home.chmod(0o700)
        identity = f"OpenBao recovery custodian: {name}"
        _run_gpg(
            gpg,
            home,
            ["--passphrase", "", "--quick-generate-key", identity, "rsa4096", "cert", "0"],
        )
        fingerprints = _secret_fingerprints(gpg, home)
        if len(fingerprints) != 1:
            raise CustodyError("Generated custodian key has an invalid fingerprint")
        fingerprint = fingerprints[0]
        _run_gpg(
            gpg,
            home,
            ["--passphrase", "", "--quick-add-key", fingerprint, "rsa4096", "encr", "0"],
        )
        public_key = _run_gpg(gpg, home, ["--armor", "--export", fingerprint])
        private_key = _run_gpg(
            gpg,
            home,
            ["--passphrase", "", "--armor", "--export-secret-keys", fingerprint],
        )
    try:
        return CustodianKey(
            fingerprint=fingerprint,
            public_key=public_key.decode("ascii"),
            private_key=private_key.decode("ascii"),
        )
    except UnicodeDecodeError:
        raise CustodyError("Generated custodian key export is invalid") from None


def _run_gpg(gpg: str, home: Path, arguments: list[str], input_data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            [
                gpg,
                "--homedir",
                str(home),
                "--batch",
                "--yes",
                "--pinentry-mode",
                "loopback",
                *arguments,
            ],
            check=False,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise CustodyError("gpg operation failed") from None
    if result.returncode != 0:
        raise CustodyError("gpg operation failed")
    return result.stdout


def _secret_fingerprints(gpg: str, home: Path) -> tuple[str, ...]:
    return _fingerprints(gpg, home, "--list-secret-keys", "sec")


def _public_fingerprints(gpg: str, home: Path) -> tuple[str, ...]:
    return _fingerprints(gpg, home, "--list-keys", "pub")


def _fingerprints(gpg: str, home: Path, operation: str, primary_record: str) -> tuple[str, ...]:
    listing = _run_gpg(gpg, home, ["--with-colons", "--fingerprint", operation])
    fingerprints: list[str] = []
    primary_pending = False
    for line in listing.decode("utf-8", errors="replace").splitlines():
        fields = line.split(":")
        if fields[0] == primary_record:
            primary_pending = True
        elif fields[0] == "fpr" and primary_pending:
            fingerprints.append(fields[9])
            primary_pending = False
    return tuple(fingerprints)


def _load_metadata(payload: bytes) -> PackageMetadata:
    values = json.loads(payload.decode("utf-8"))
    expected = {field.name for field in PackageMetadata.__dataclass_fields__.values()}
    if not isinstance(values, dict) or set(values) != expected:
        raise CustodyError("Custodian package metadata has an invalid schema")
    try:
        metadata = PackageMetadata(**values)
    except TypeError:
        raise CustodyError("Custodian package metadata has an invalid schema") from None
    if (
        type(metadata.schema_version) is not int
        or metadata.schema_version != 1
        or type(metadata.recovery_shares) is not int
        or metadata.recovery_shares != 3
        or type(metadata.recovery_threshold) is not int
        or metadata.recovery_threshold != 2
        or type(metadata.share_index) is not int
        or metadata.share_index not in {1, 2, 3}
        or any(
            not isinstance(value, str) or not value
            for value in (
                metadata.ceremony_id,
                metadata.client,
                metadata.cluster_id,
                metadata.namespace_uid,
                metadata.static_seal_key_id,
                metadata.custodian_name,
                metadata.fingerprint,
            )
        )
    ):
        raise CustodyError("Custodian package metadata has invalid values")
    return metadata


def _validate_package_entries(entries: list[zipfile.ZipInfo], names: list[str]) -> None:
    if set(names) != _PACKAGE_MEMBERS or len(names) != len(_PACKAGE_MEMBERS):
        raise CustodyError("Custodian package has invalid members")
    if any(
        entry.is_dir()
        or stat.S_ISLNK(entry.external_attr >> 16)
        or entry.file_size <= 0
        or entry.file_size > _MAX_PACKAGE_MEMBER_SIZE
        for entry in entries
    ):
        raise CustodyError("Custodian package has invalid members")


def _package_readme(metadata: PackageMetadata) -> str:
    return (
        "OPENBAO RECOVERY CUSTODIAN PACKAGE\n\n"
        f"Custodian: {metadata.custodian_name}\n"
        f"Client: {metadata.client}\n"
        f"Share: {metadata.share_index} of {metadata.recovery_shares}\n\n"
        "This passwordless ZIP contains one private OpenPGP key and its encrypted recovery "
        "share. Possession grants control of one share. Store it offline on encrypted removable "
        "media. Never commit, email, or upload it. Any two distinct packages can authorize an "
        "OpenBao recovery-root operation through stack-setup recovery verify.\n"
    )


def _require_private_directory(path: Path) -> None:
    try:
        stat = path.stat()
        is_directory = path.is_dir()
    except OSError:
        raise CustodyError("Custody directory could not be inspected") from None
    if not is_directory or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise CustodyError("Custody directory must be owned by the current user with mode 0700")


def _revalidate_private_directory(path: Path, *, expected: Path) -> Path:
    """Re-resolve one custody directory and re-check its ownership boundaries."""
    if path.is_symlink():
        raise CustodyError("Custody directory must not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CustodyError("Custody directories could not be prepared") from None
    if resolved != expected:
        raise CustodyError("Custody directory resolved path changed during preparation")
    _require_outside_git_boundaries(resolved)
    _require_private_directory(resolved)
    return resolved


def _require_outside_git_boundaries(path: Path) -> None:
    """Reject custody below Git repositories or recognized workspace roots."""
    for candidate in (path, *path.parents):
        if _has_git_marker(candidate):
            raise CustodyError("Custody root must be outside Git repositories")
    for workspace in _workspace_roots():
        if path == workspace or workspace in path.parents:
            raise CustodyError("Custody root must be outside multi-repository workspaces")


def _workspace_roots() -> set[Path]:
    """Return explicitly configured and structurally discovered workspace roots."""
    roots: set[Path] = set()
    configured = os.environ.get(_WORKSPACE_ROOT_ENV)
    if configured is not None:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            raise CustodyError(f"{_WORKSPACE_ROOT_ENV} must be an absolute path")
        try:
            configured_root = configured_path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise CustodyError(f"{_WORKSPACE_ROOT_ENV} must be an existing directory") from None
        if not configured_root.is_dir():
            raise CustodyError(f"{_WORKSPACE_ROOT_ENV} must be an existing directory")
        roots.add(configured_root)
    discovered = _discover_multi_repository_workspace(Path.cwd())
    if discovered is not None:
        roots.add(discovered)
    return roots


def _discover_multi_repository_workspace(start: Path) -> Path | None:
    """Find a worktree parent containing at least two direct Git worktrees."""
    resolved = start.resolve()
    worktree = next(
        (candidate for candidate in (resolved, *resolved.parents) if _has_git_marker(candidate)),
        None,
    )
    if worktree is None:
        return None
    candidate = worktree.parent
    try:
        child_worktrees = sum(
            1 for child in candidate.iterdir() if child.is_dir() and _has_git_marker(child)
        )
    except OSError:
        raise CustodyError("Multi-repository workspace boundary could not be inspected") from None
    return candidate if child_worktrees >= 2 else None


def _has_git_marker(path: Path) -> bool:
    """Return whether a directory is a normal or linked Git worktree root."""
    marker = path / ".git"
    return marker.is_dir() or marker.is_file() or marker.is_symlink()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
