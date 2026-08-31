from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from openbao_stack_setup.recovery import (
    RecoveryKit,
    RecoveryKitError,
    load,
    new_kit,
    update,
    with_bootstrap_passwords_acknowledged,
    with_checkpoint,
    with_initialization_material,
    with_pending_bootstrap_passwords,
    write_new,
)


def recovery_kit() -> RecoveryKit:
    return replace(
        new_kit(
            "client",
            "cluster",
            "namespace",
            b"x" * 32,
            "key-id",
            ("One", "Two", "Three"),
            ("fingerprint-1", "fingerprint-2", "fingerprint-3"),
            ("public-1", "public-2", "public-3"),
            ("private-1", "private-2", "private-3"),
        ),
        ceremony_id="ceremony",
    )


def initialized_kit() -> RecoveryKit:
    staged = with_initialization_material(
        recovery_kit(), "root-token", ("share-1", "share-2", "share-3")
    )
    return with_checkpoint(staged, "initialized")


def test_exclusive_write_completes_partial_os_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "recovery.json"
    original_write = os.write

    def partial_write(fd: int, payload: bytes | memoryview) -> int:
        return original_write(fd, payload[:7])

    monkeypatch.setattr(os, "write", partial_write)
    write_new(path, recovery_kit())

    assert load(path) == recovery_kit()


def test_failed_exclusive_write_removes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "recovery.json"

    def failed_write(fd: int, payload: bytes | memoryview) -> int:
        raise OSError("disk failure")

    monkeypatch.setattr(os, "write", failed_write)
    with pytest.raises(RecoveryKitError, match="could not be written"):
        write_new(path, recovery_kit())
    assert not path.exists()


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o700])
def test_load_requires_exact_0600(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "recovery.json"
    write_new(path, recovery_kit())
    path.chmod(mode)

    with pytest.raises(RecoveryKitError, match="permissions must be 0600"):
        load(path)


def test_recovery_schema_and_values_are_strict(tmp_path: Path) -> None:
    path = tmp_path / "recovery.json"
    path.write_text("not-json", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(RecoveryKitError, match="could not be read"):
        load(path)

    path.write_text(json.dumps({"unexpected": True}), encoding="utf-8")
    with pytest.raises(RecoveryKitError, match="invalid schema"):
        load(path)

    values = {
        "schema_version": 4,
        "client": "client",
        "cluster_id": "cluster",
        "namespace_uid": "namespace",
        "static_seal_key": "invalid",
        "static_seal_key_id": "key-id",
        "checkpoint": "seal-created",
        "pending_bootstrap_passwords": None,
        "bootstrap_passwords_acknowledged": False,
        "ceremony_id": "ceremony",
        "custodian_names": ["One", "Two", "Three"],
        "custodian_fingerprints": ["fingerprint-1", "fingerprint-2", "fingerprint-3"],
        "custodian_public_keys": ["public-1", "public-2", "public-3"],
        "custodian_private_keys": ["private-1", "private-2", "private-3"],
        "initialization_root_token": None,
        "encrypted_recovery_shares": None,
    }
    path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(RecoveryKitError, match="invalid static seal key"):
        load(path)


def test_recovery_transition_validation() -> None:
    with pytest.raises(RecoveryKitError, match="exactly 32 bytes"):
        new_kit(
            "client",
            "cluster",
            "namespace",
            b"short",
            "key-id",
            ("One", "Two", "Three"),
            ("fingerprint-1", "fingerprint-2", "fingerprint-3"),
            ("public-1", "public-2", "public-3"),
            ("private-1", "private-2", "private-3"),
        )
    with pytest.raises(RecoveryKitError, match="Invalid recovery checkpoint transition"):
        with_checkpoint(recovery_kit(), "seeded")
    with pytest.raises(RecoveryKitError, match="must be durable"):
        with_checkpoint(recovery_kit(), "initialized")
    initialized = initialized_kit()
    with pytest.raises(RecoveryKitError, match="cannot be staged"):
        with_pending_bootstrap_passwords(recovery_kit(), {"keycloak": "password"})
    staged = with_pending_bootstrap_passwords(initialized, {"keycloak": "password"})
    with pytest.raises(RecoveryKitError, match="must be acknowledged"):
        with_checkpoint(staged, "seeded")
    acknowledged = with_bootstrap_passwords_acknowledged(staged)
    assert with_checkpoint(acknowledged, "seeded").pending_bootstrap_passwords is None
    with pytest.raises(RecoveryKitError, match="Invalid recovery checkpoint transition"):
        with_checkpoint(acknowledged, "complete")


def test_initialization_material_is_staged_until_packages_are_checkpointed() -> None:
    staged = with_initialization_material(
        recovery_kit(), "root-token", ("share-1", "share-2", "share-3")
    )
    assert staged.initialization_root_token == "root-token"
    assert staged.encrypted_recovery_shares == ("share-1", "share-2", "share-3")

    initialized = with_checkpoint(staged, "initialized")
    assert initialized.initialization_root_token is None
    assert initialized.encrypted_recovery_shares is None
    assert initialized.custodian_private_keys is None
    with pytest.raises(RecoveryKitError, match="invalid at this checkpoint"):
        with_initialization_material(staged, "other", ("one", "two", "three"))


def test_recovery_preserves_pending_bootstrap_passwords(tmp_path: Path) -> None:
    path = tmp_path / "recovery.json"
    initialized = initialized_kit()
    staged = with_pending_bootstrap_passwords(
        initialized,
        {"keycloak": "keycloak-password", "grafana": "grafana-password"},
    )
    write_new(path, initialized)
    update(path, staged)

    assert load(path) == staged
    acknowledged = with_bootstrap_passwords_acknowledged(load(path))
    update(path, acknowledged)
    assert load(path).bootstrap_passwords_acknowledged is True


def test_update_requires_existing_file_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "recovery.json"
    with pytest.raises(RecoveryKitError, match="does not exist"):
        update(path, recovery_kit())

    write_new(path, recovery_kit())
    initialized = initialized_kit()

    def failed_replace(self: Path, target: Path) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(RecoveryKitError, match="could not be written"):
        update(path, initialized)
    assert load(path) == recovery_kit()
    assert list(tmp_path.glob(".recovery.json.*")) == []


def test_rejects_missing_or_unsafe_parent(tmp_path: Path) -> None:
    with pytest.raises(RecoveryKitError, match="parent directory"):
        write_new(tmp_path / "missing" / "recovery.json", recovery_kit())

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(RecoveryKitError, match="group or world writable"):
        write_new(unsafe / "recovery.json", recovery_kit())
