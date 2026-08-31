from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openbao_stack_setup.custody import (
    CustodianKey,
    CustodianPackage,
    CustodyError,
    PackageMetadata,
    _generate_custodian_key,
    _revalidate_private_directory,
    binary_public_key,
    decrypt_package_share,
    default_custody_root,
    load_custodian_package,
    normalize_custodian_names,
    prepare_custody_paths,
    validate_package_set,
    write_custodian_package,
)


def metadata(index: int = 1) -> PackageMetadata:
    return PackageMetadata(
        schema_version=1,
        ceremony_id="ceremony",
        client="client",
        cluster_id="cluster",
        namespace_uid="namespace",
        static_seal_key_id="seal-key",
        share_index=index,
        recovery_shares=3,
        recovery_threshold=2,
        custodian_name=("One", "Two", "Three")[index - 1],
        fingerprint=f"fingerprint-{index}",
    )


def key(index: int = 1) -> CustodianKey:
    return CustodianKey(f"fingerprint-{index}", f"public-{index}", f"private-{index}")


def test_default_and_private_custody_paths(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        assert default_custody_root("client") == (
            tmp_path / ".local" / "share" / "neurwerk" / "openbao" / "client"
        )

    paths = prepare_custody_paths(tmp_path / "custody")
    assert paths.seal_file == paths.root / "operator-custody" / "openbao-seal.json"
    assert paths.package_dir.stat().st_mode & 0o777 == 0o700

    paths.root.chmod(0o755)
    with pytest.raises(CustodyError, match="mode 0700"):
        prepare_custody_paths(paths.root)


def test_custody_root_rejects_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(CustodyError, match="symbolic link"):
        prepare_custody_paths(linked)


def test_private_directory_revalidation_rejects_symlink_swap(tmp_path: Path) -> None:
    custody = tmp_path / "custody"
    replacement = tmp_path / "replacement"
    custody.mkdir(mode=0o700)
    replacement.mkdir(mode=0o700)
    custody.rmdir()
    custody.symlink_to(replacement, target_is_directory=True)

    with pytest.raises(CustodyError, match="symbolic link"):
        _revalidate_private_directory(custody, expected=custody)


def test_custody_boundaries_are_rechecked_around_directory_creation(
    tmp_path: Path,
) -> None:
    custody = tmp_path / "custody"
    observations: list[tuple[Path, bool]] = []

    def observe(path: Path) -> None:
        observations.append((path, path.exists()))

    with patch("openbao_stack_setup.custody._require_outside_git_boundaries", side_effect=observe):
        paths = prepare_custody_paths(custody)

    assert observations[0] == (custody, False)
    assert observations.count((paths.root, True)) >= 5
    assert (paths.root / "operator-custody", True) in observations
    assert (paths.package_dir, True) in observations


@pytest.mark.parametrize("worktree_marker", ["directory", "file"])
def test_custody_root_rejects_git_worktree_before_creation(
    tmp_path: Path, worktree_marker: str
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    marker = worktree / ".git"
    if worktree_marker == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: ../git-data/worktrees/example\n", encoding="utf-8")
    custody_root = worktree / "private" / "custody"

    with pytest.raises(CustodyError, match="outside Git"):
        prepare_custody_paths(custody_root)

    assert not custody_root.exists()


def test_custody_root_rejects_auto_discovered_multi_repository_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    first = workspace / "first"
    second = workspace / "second"
    working_directory = first / "cli"
    working_directory.mkdir(parents=True)
    second.mkdir()
    (first / ".git").mkdir()
    (second / ".git").write_text("gitdir: ../git-data/second\n", encoding="utf-8")
    custody_root = workspace / "custody"

    with (
        patch.object(Path, "cwd", return_value=working_directory),
        pytest.raises(CustodyError, match="multi-repository workspace"),
    ):
        prepare_custody_paths(custody_root)

    assert not custody_root.exists()


def test_custody_root_rejects_explicit_workspace_when_run_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    custody_root = workspace / "custody"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("OPENBAO_STACK_SETUP_WORKSPACE_ROOT", str(workspace))

    with (
        patch.object(Path, "cwd", return_value=elsewhere),
        pytest.raises(CustodyError, match="multi-repository workspace"),
    ):
        prepare_custody_paths(custody_root)

    assert not custody_root.exists()


def test_explicit_workspace_root_must_be_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENBAO_STACK_SETUP_WORKSPACE_ROOT", "relative/workspace")

    with pytest.raises(CustodyError, match="must be an absolute path"):
        prepare_custody_paths(tmp_path / "custody")


def test_custodian_names_are_normalized_and_distinct() -> None:
    assert normalize_custodian_names([" One ", "Two", "Three"]) == ("One", "Two", "Three")
    with pytest.raises(CustodyError, match="Exactly three"):
        normalize_custodian_names(["One"])
    with pytest.raises(CustodyError, match="distinct"):
        normalize_custodian_names(["One", "one", "Three"])
    with pytest.raises(CustodyError, match="printable"):
        normalize_custodian_names(["O\nne", "Two", "Three"])


def test_package_round_trip_and_resume_refuses_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "custodian-1.zip"
    write_custodian_package(path, metadata(), key(), b"encrypted-share")

    package = load_custodian_package(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert package.metadata == metadata()
    assert package.private_key == b"private-1"
    assert package.encrypted_share == b"encrypted-share"

    write_custodian_package(path, metadata(), key(), b"encrypted-share")
    with pytest.raises(CustodyError, match="does not match"):
        write_custodian_package(path, metadata(), key(), b"different")


def test_package_reader_rejects_permissions_and_members(tmp_path: Path) -> None:
    path = tmp_path / "custodian.zip"
    write_custodian_package(path, metadata(), key(), b"encrypted")
    path.chmod(0o644)
    with pytest.raises(CustodyError, match="permissions"):
        load_custodian_package(path)

    invalid = tmp_path / "invalid.zip"
    with zipfile.ZipFile(invalid, "w") as archive:
        archive.writestr("unexpected", "value")
    invalid.chmod(0o600)
    with pytest.raises(CustodyError, match="invalid members"):
        load_custodian_package(invalid)

    oversized = tmp_path / "oversized.zip"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(oversized, "w") as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "README.txt":
                payload = b"x" * (1024 * 1024 + 1)
            target.writestr(name, payload)
    oversized.chmod(0o600)
    with pytest.raises(CustodyError, match="invalid members"):
        load_custodian_package(oversized)


def test_package_metadata_and_set_binding_are_strict(tmp_path: Path) -> None:
    paths = [tmp_path / f"custodian-{index}.zip" for index in (1, 2)]
    for index, path in enumerate(paths, start=1):
        write_custodian_package(path, metadata(index), key(index), f"share-{index}".encode())
    packages = (load_custodian_package(paths[0]), load_custodian_package(paths[1]))
    validate_package_set(packages, metadata())

    duplicate = (packages[0], packages[0])
    with pytest.raises(CustodyError, match="distinct recovery shares"):
        validate_package_set(duplicate, metadata())
    other = replace(metadata(), cluster_id="other")
    with pytest.raises(CustodyError, match="selected cluster"):
        validate_package_set(packages, other)


def test_decryption_uses_isolated_import_and_matching_private_key() -> None:
    package = CustodianPackage(metadata(), b"public", b"private", b"encrypted")
    public_listing = b"pub:::::::::\nfpr:::::::::fingerprint-1:\n"
    secret_listing = b"sec:::::::::\nfpr:::::::::fingerprint-1:\n"
    with (
        patch("openbao_stack_setup.custody.shutil.which", return_value="/usr/bin/gpg"),
        patch(
            "openbao_stack_setup.custody._run_gpg",
            side_effect=[
                b"",
                public_listing,
                b"",
                secret_listing,
                b"plaintext-share\n",
            ],
        ) as run,
    ):
        assert decrypt_package_share(package) == "plaintext-share"
    assert run.call_args_list[0].args[2] == ["--import"]
    assert run.call_args_list[0].kwargs["input_data"] == b"public"
    assert run.call_args_list[2].kwargs["input_data"] == b"private"


def test_generated_key_uses_rsa4096_encryption_subkey() -> None:
    listing = b"sec:::::::::\nfpr:::::::::fingerprint-1:\n"
    run = MagicMock(side_effect=[b"", listing, b"", b"PUBLIC", b"PRIVATE"])
    with patch("openbao_stack_setup.custody._run_gpg", run):
        generated = _generate_custodian_key("/usr/bin/gpg", "One")
    assert generated == CustodianKey("fingerprint-1", "PUBLIC", "PRIVATE")
    assert "rsa4096" in run.call_args_list[0].args[2]
    assert "cert" in run.call_args_list[0].args[2]
    assert "encr" in run.call_args_list[2].args[2]


def test_public_key_is_dearmored_for_openbao() -> None:
    with (
        patch("openbao_stack_setup.custody.shutil.which", return_value="/usr/bin/gpg"),
        patch("openbao_stack_setup.custody._run_gpg", return_value=b"binary-key") as run,
    ):
        assert binary_public_key("armored-key") == b"binary-key"

    assert run.call_args.args[2] == ["--dearmor"]
    assert run.call_args.kwargs["input_data"] == b"armored-key"


def test_invalid_metadata_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "custodian.zip"
    write_custodian_package(path, metadata(), key(), b"encrypted")
    replacement = tmp_path / "invalid.zip"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(replacement, "w") as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "metadata.json":
                payload = json.dumps({"schema_version": 1}).encode()
            target.writestr(name, payload)
    replacement.chmod(0o600)
    with pytest.raises(CustodyError, match="metadata"):
        load_custodian_package(replacement)
