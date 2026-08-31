"""Create and validate private offline OpenBao recovery kits."""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path


class RecoveryKitError(RuntimeError):
    """Raised when a recovery kit cannot be created or validated safely."""


@dataclass(frozen=True)
class RecoveryKit:
    """Static seal material and the immutable identity it is bound to."""

    schema_version: int
    client: str
    cluster_id: str
    namespace_uid: str
    static_seal_key: str
    static_seal_key_id: str
    checkpoint: str
    pending_bootstrap_passwords: dict[str, str] | None
    bootstrap_passwords_acknowledged: bool
    ceremony_id: str
    custodian_names: tuple[str, str, str]
    custodian_fingerprints: tuple[str, str, str]
    custodian_public_keys: tuple[str, str, str] | None
    custodian_private_keys: tuple[str, str, str] | None
    initialization_root_token: str | None
    encrypted_recovery_shares: tuple[str, str, str] | None


def new_static_seal() -> tuple[bytes, str]:
    """Generate a 32-byte static seal and stable, non-secret identifier."""
    key = secrets.token_bytes(32)
    return key, "infra-openbao-static-seal-v1"


def new_kit(
    client: str,
    cluster_id: str,
    namespace_uid: str,
    key: bytes,
    key_id: str,
    custodian_names: tuple[str, str, str],
    custodian_fingerprints: tuple[str, str, str],
    custodian_public_keys: tuple[str, str, str],
    custodian_private_keys: tuple[str, str, str],
) -> RecoveryKit:
    """Create the initial durable checkpoint before OpenBao initialization."""
    if len(key) != 32:
        raise RecoveryKitError("Static seal key must be exactly 32 bytes")
    return RecoveryKit(
        schema_version=4,
        client=client,
        cluster_id=cluster_id,
        namespace_uid=namespace_uid,
        static_seal_key=base64.b64encode(key).decode("ascii"),
        static_seal_key_id=key_id,
        checkpoint="seal-created",
        pending_bootstrap_passwords=None,
        bootstrap_passwords_acknowledged=False,
        ceremony_id=secrets.token_hex(16),
        custodian_names=custodian_names,
        custodian_fingerprints=custodian_fingerprints,
        custodian_public_keys=custodian_public_keys,
        custodian_private_keys=custodian_private_keys,
        initialization_root_token=None,
        encrypted_recovery_shares=None,
    )


def write_new(path: Path, kit: RecoveryKit) -> None:
    """Create one private kit without replacing an existing recovery file."""
    if path.exists():
        raise RecoveryKitError("Recovery file already exists; refusing to overwrite it")
    _write(path, kit, exclusive=True)


def update(path: Path, kit: RecoveryKit) -> None:
    """Atomically update an existing private kit after a durable checkpoint."""
    if not path.is_file():
        raise RecoveryKitError("Recovery file does not exist")
    _write(path, kit, exclusive=False)


def load(path: Path) -> RecoveryKit:
    """Read and strictly validate a private recovery kit."""
    try:
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise RecoveryKitError("Recovery file permissions must be 0600")
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RecoveryKitError("Recovery file could not be read") from None
    if not isinstance(values, dict) or set(values) != {
        "schema_version",
        "client",
        "cluster_id",
        "namespace_uid",
        "static_seal_key",
        "static_seal_key_id",
        "checkpoint",
        "pending_bootstrap_passwords",
        "bootstrap_passwords_acknowledged",
        "ceremony_id",
        "custodian_names",
        "custodian_fingerprints",
        "custodian_public_keys",
        "custodian_private_keys",
        "initialization_root_token",
        "encrypted_recovery_shares",
    }:
        raise RecoveryKitError("Recovery file has an invalid schema")
    try:
        for field in (
            "custodian_names",
            "custodian_fingerprints",
            "custodian_public_keys",
            "custodian_private_keys",
            "encrypted_recovery_shares",
        ):
            if isinstance(values[field], list):
                values[field] = tuple(values[field])
        kit = RecoveryKit(**values)
        key = base64.b64decode(kit.static_seal_key, validate=True)
    except (TypeError, ValueError):
        raise RecoveryKitError("Recovery file has an invalid static seal key") from None
    if (
        kit.schema_version != 4
        or not all(
            isinstance(value, str) and value
            for value in (
                kit.client,
                kit.cluster_id,
                kit.namespace_uid,
                kit.static_seal_key_id,
                kit.checkpoint,
                kit.ceremony_id,
            )
        )
        or len(key) != 32
        or kit.checkpoint not in {"seal-created", "initialized", "seeded", "complete"}
        or not isinstance(kit.bootstrap_passwords_acknowledged, bool)
        or not _valid_pending_bootstrap_passwords(kit)
        or not _valid_custody_state(kit)
    ):
        raise RecoveryKitError("Recovery file has invalid values")
    return kit


def with_checkpoint(kit: RecoveryKit, checkpoint: str) -> RecoveryKit:
    """Advance the private seal kit through one durable bootstrap checkpoint."""
    allowed = {
        "seal-created": "initialized",
        "initialized": "seeded",
        "seeded": "complete",
    }
    if allowed.get(kit.checkpoint) != checkpoint:
        raise RecoveryKitError("Invalid recovery checkpoint transition")
    if checkpoint == "initialized" and (
        kit.initialization_root_token is None or kit.encrypted_recovery_shares is None
    ):
        raise RecoveryKitError("Initialization material must be durable before checkpointing")
    if checkpoint == "seeded" and kit.pending_bootstrap_passwords is not None:
        if not kit.bootstrap_passwords_acknowledged:
            raise RecoveryKitError("Bootstrap passwords must be acknowledged before seeding")
        pending_bootstrap_passwords: dict[str, str] | None = None
    else:
        pending_bootstrap_passwords = kit.pending_bootstrap_passwords
    return RecoveryKit(
        schema_version=kit.schema_version,
        client=kit.client,
        cluster_id=kit.cluster_id,
        namespace_uid=kit.namespace_uid,
        static_seal_key=kit.static_seal_key,
        static_seal_key_id=kit.static_seal_key_id,
        checkpoint=checkpoint,
        pending_bootstrap_passwords=pending_bootstrap_passwords,
        bootstrap_passwords_acknowledged=(
            False if checkpoint == "seeded" else kit.bootstrap_passwords_acknowledged
        ),
        ceremony_id=kit.ceremony_id,
        custodian_names=kit.custodian_names,
        custodian_fingerprints=kit.custodian_fingerprints,
        custodian_public_keys=None if checkpoint == "initialized" else kit.custodian_public_keys,
        custodian_private_keys=None if checkpoint == "initialized" else kit.custodian_private_keys,
        initialization_root_token=(
            None if checkpoint == "initialized" else kit.initialization_root_token
        ),
        encrypted_recovery_shares=(
            None if checkpoint == "initialized" else kit.encrypted_recovery_shares
        ),
    )


def with_pending_bootstrap_passwords(kit: RecoveryKit, passwords: dict[str, str]) -> RecoveryKit:
    """Persist newly generated human passwords before displaying them."""
    if kit.checkpoint != "initialized" or kit.pending_bootstrap_passwords is not None:
        raise RecoveryKitError("Bootstrap passwords cannot be staged at this checkpoint")
    staged = dict(passwords)
    if not staged or not _valid_password_mapping(staged):
        raise RecoveryKitError("Bootstrap passwords are invalid")
    return RecoveryKit(
        schema_version=kit.schema_version,
        client=kit.client,
        cluster_id=kit.cluster_id,
        namespace_uid=kit.namespace_uid,
        static_seal_key=kit.static_seal_key,
        static_seal_key_id=kit.static_seal_key_id,
        checkpoint=kit.checkpoint,
        pending_bootstrap_passwords=staged,
        bootstrap_passwords_acknowledged=False,
        ceremony_id=kit.ceremony_id,
        custodian_names=kit.custodian_names,
        custodian_fingerprints=kit.custodian_fingerprints,
        custodian_public_keys=kit.custodian_public_keys,
        custodian_private_keys=kit.custodian_private_keys,
        initialization_root_token=kit.initialization_root_token,
        encrypted_recovery_shares=kit.encrypted_recovery_shares,
    )


def with_bootstrap_passwords_acknowledged(kit: RecoveryKit) -> RecoveryKit:
    """Record acknowledgement before storing staged passwords in OpenBao."""
    if kit.pending_bootstrap_passwords is None or kit.bootstrap_passwords_acknowledged:
        raise RecoveryKitError("Bootstrap passwords are not awaiting acknowledgement")
    return RecoveryKit(
        schema_version=kit.schema_version,
        client=kit.client,
        cluster_id=kit.cluster_id,
        namespace_uid=kit.namespace_uid,
        static_seal_key=kit.static_seal_key,
        static_seal_key_id=kit.static_seal_key_id,
        checkpoint=kit.checkpoint,
        pending_bootstrap_passwords=kit.pending_bootstrap_passwords,
        bootstrap_passwords_acknowledged=True,
        ceremony_id=kit.ceremony_id,
        custodian_names=kit.custodian_names,
        custodian_fingerprints=kit.custodian_fingerprints,
        custodian_public_keys=kit.custodian_public_keys,
        custodian_private_keys=kit.custodian_private_keys,
        initialization_root_token=kit.initialization_root_token,
        encrypted_recovery_shares=kit.encrypted_recovery_shares,
    )


def with_initialization_material(
    kit: RecoveryKit, root_token: str, encrypted_shares: tuple[str, str, str]
) -> RecoveryKit:
    """Persist the one-time initialization response before packaging it."""
    if (
        kit.checkpoint != "seal-created"
        or kit.initialization_root_token is not None
        or kit.encrypted_recovery_shares is not None
        or not root_token
        or len(encrypted_shares) != 3
        or any(not share for share in encrypted_shares)
    ):
        raise RecoveryKitError("OpenBao initialization material is invalid at this checkpoint")
    return RecoveryKit(
        schema_version=kit.schema_version,
        client=kit.client,
        cluster_id=kit.cluster_id,
        namespace_uid=kit.namespace_uid,
        static_seal_key=kit.static_seal_key,
        static_seal_key_id=kit.static_seal_key_id,
        checkpoint=kit.checkpoint,
        pending_bootstrap_passwords=kit.pending_bootstrap_passwords,
        bootstrap_passwords_acknowledged=kit.bootstrap_passwords_acknowledged,
        ceremony_id=kit.ceremony_id,
        custodian_names=kit.custodian_names,
        custodian_fingerprints=kit.custodian_fingerprints,
        custodian_public_keys=kit.custodian_public_keys,
        custodian_private_keys=kit.custodian_private_keys,
        initialization_root_token=root_token,
        encrypted_recovery_shares=encrypted_shares,
    )


def _valid_pending_bootstrap_passwords(kit: RecoveryKit) -> bool:
    pending = kit.pending_bootstrap_passwords
    if pending is None:
        return not kit.bootstrap_passwords_acknowledged
    return (
        isinstance(pending, dict)
        and kit.checkpoint == "initialized"
        and _valid_password_mapping(pending)
    )


def _valid_custody_state(kit: RecoveryKit) -> bool:
    identity_values = (kit.custodian_names, kit.custodian_fingerprints)
    if any(
        not isinstance(values, tuple)
        or len(values) != 3
        or any(not isinstance(value, str) or not value for value in values)
        or len(set(values)) != 3
        for values in identity_values
    ):
        return False
    staged_values = (
        kit.custodian_public_keys,
        kit.custodian_private_keys,
        kit.encrypted_recovery_shares,
    )
    if kit.checkpoint == "seal-created":
        if kit.custodian_public_keys is None or kit.custodian_private_keys is None:
            return False
        if (kit.initialization_root_token is None) != (kit.encrypted_recovery_shares is None):
            return False
    elif any(value is not None for value in (*staged_values, kit.initialization_root_token)):
        return False
    return all(
        values is None
        or (
            isinstance(values, tuple)
            and len(values) == 3
            and all(isinstance(value, str) and value for value in values)
        )
        for values in staged_values
    )


def _valid_password_mapping(passwords: dict[str, str]) -> bool:
    return (
        set(passwords).issubset({"keycloak", "dify", "langfuse", "grafana"})
        and bool(passwords)
        and all(isinstance(value, str) and value for value in passwords.values())
    )


def _write(path: Path, kit: RecoveryKit, *, exclusive: bool) -> None:
    parent = path.parent
    if not parent.is_dir():
        raise RecoveryKitError("Recovery file parent directory does not exist")
    if parent.stat().st_mode & 0o022:
        raise RecoveryKitError("Recovery file parent directory must not be group or world writable")
    payload = (json.dumps(asdict(kit), indent=2, sort_keys=True) + "\n").encode("utf-8")
    if exclusive:
        _write_exclusive(path, parent, payload)
        return
    _write_atomic(path, parent, payload)


def _write_exclusive(path: Path, parent: Path, payload: bytes) -> None:
    fd: int | None = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RecoveryKitError("Recovery file already exists; refusing to overwrite it") from None
    try:
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        _sync_directory(parent)
    except OSError:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)
        with suppress(OSError):
            path.unlink(missing_ok=True)
        with suppress(OSError):
            _sync_directory(parent)
        raise RecoveryKitError("Recovery file could not be written") from None


def _write_atomic(path: Path, parent: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        temporary_path.replace(path)
        _sync_directory(parent)
    except OSError:
        raise RecoveryKitError("Recovery file could not be written") from None
    finally:
        if fd >= 0:
            with suppress(OSError):
                os.close(fd)
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("write returned zero bytes")
        view = view[written:]


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
