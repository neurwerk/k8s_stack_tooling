"""Immutable acquisition, cache preservation, and private failure boundaries."""

import copy
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from k8s_stack_tooling.addons.acquisition import acquire_source, main, source_checkouts
from k8s_stack_tooling.addons.schema import ContractError, SourceRef


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def local_transport(monkeypatch):
    """Translate only explicitly registered synthetic fetch URLs inside tests."""
    run = subprocess.run
    remotes = {}
    fetched = []

    def local_fetch(command, **kwargs):
        if "fetch" in command:
            position = command.index("--") + 1
            url = command[position]
            assert url in remotes, "test attempted an unregistered network fetch"
            assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == "https:ssh"
            fetched.append(url)
            command = list(command)
            command[position] = remotes[url].as_uri()
            kwargs["env"] = {**kwargs["env"], "GIT_ALLOW_PROTOCOL": "file"}
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", local_fetch)
    return remotes, fetched


@pytest.fixture
def source(tmp_path, local_transport):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "-c", "init.templateDir=", "init", "-b", "main")
    (upstream / "release").mkdir()
    (upstream / "release/manifest.yaml").write_text("spec: {version: 1.0.0}\n")
    git(upstream, "add", ".")
    git(
        upstream,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "synthetic fixture",
    )
    revision = git(upstream, "rev-parse", "HEAD")
    reference = SourceRef(kind="GitRepository", namespace="flux-system", name="sample")
    resource = {
        "kind": "GitRepository",
        "metadata": {"namespace": "flux-system", "name": "sample"},
        "spec": {"url": "https://example.test/sample.git", "ref": {"commit": revision}},
    }
    local_transport[0][resource["spec"]["url"]] = upstream
    return upstream, reference, resource, tmp_path / "cache"


def acquire(source, **kwargs):
    _, reference, resource, destination = source
    return acquire_source(reference, resource, destination, **kwargs)


def test_exact_commit_and_clean_cache_reuse(source):
    upstream, _, resource, destination = source
    result = acquire(source)
    assert result.revision == resource["spec"]["ref"]["commit"]
    assert git(destination, "status", "--porcelain") == ""
    assert git(destination, "config", "remote.origin.url") == resource["spec"]["url"]
    assert (destination / "release/manifest.yaml").read_bytes() == (
        upstream / "release/manifest.yaml"
    ).read_bytes()
    # Verified cache reuse is offline, even when the original remote disappears.
    upstream.rename(upstream.with_name("unavailable"))
    assert acquire(source) == result


@pytest.fixture
def selected_client(source, tmp_path, local_transport):
    upstream, _, template, _ = source
    client = tmp_path / "client"
    cluster = client / "clusters/demo"
    cluster.mkdir(parents=True)
    (client / "config").mkdir()
    refs = {
        name: {"kind": "GitRepository", "namespace": "flux-system", "name": name}
        for name in ("platform", "sample", "excluded")
    }
    selection = {
        "apiVersion": "addons.neurwerk.com/v1alpha2",
        "kind": "AddonSelection",
        "baseSourceRef": refs["platform"],
        "addons": [{"id": "sample", "sourceRef": refs["sample"]}],
    }
    (client / "config/addons.yaml").write_text(yaml.safe_dump(selection))
    resources = []
    for name in refs:
        resource = copy.deepcopy(template)
        resource["apiVersion"] = "source.toolkit.fluxcd.io/v1"
        resource["metadata"]["name"] = name
        resource["spec"]["url"] = f"https://example.test/{name}.git"
        resources.append(resource)
        if name != "excluded":
            local_transport[0][resource["spec"]["url"]] = upstream
    (cluster / "sources.yaml").write_text(yaml.safe_dump_all(resources))
    (cluster / "kustomization.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": ["sources.yaml"],
            }
        )
    )
    args = [
        "--client",
        str(client),
        "--cluster-dir",
        "clusters/demo",
        "--cache",
        str(tmp_path / "selected-cache"),
    ]
    return client, args, resources


def test_cli_acquires_complete_selection_and_reuses_exact_cache(
    selected_client, source, local_transport, capsys
):
    _, args, _ = selected_client
    assert main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["base"]["id"] == "base"
    assert [row["id"] for row in result["addons"]] == ["sample"]
    assert set(local_transport[1]) == {
        "https://example.test/platform.git",
        "https://example.test/sample.git",
    }
    for row in [result["base"], *result["addons"]]:
        assert row["revision"] == source[2]["spec"]["ref"]["commit"]
        assert git(row["path"], "rev-parse", "HEAD") == row["revision"]
    assert "https://" not in captured.out
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == result
    assert len(local_transport[1]) == 2


@pytest.mark.parametrize("failure", ["missing", "duplicate"])
def test_cli_checks_all_source_identities_before_fetch(
    selected_client, local_transport, monkeypatch, capsys, failure
):
    client, args, resources = selected_client
    # Rendering itself rejects duplicate Kubernetes IDs; inject its result to also
    # protect the acquisition index against duplicate identities from any renderer.
    documents = resources[:1] if failure == "missing" else [*resources, resources[0]]
    monkeypatch.setattr(
        "k8s_stack_tooling.addons.acquisition.render", lambda *args, **kwargs: documents
    )
    assert main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "https://" not in captured.err
    assert not local_transport[1]
    with pytest.raises(ContractError):
        source_checkouts(client, cluster_dir="clusters/demo", cache=Path(args[-1]))


@pytest.fixture
def forbid_git(monkeypatch):
    run = subprocess.run

    def offline_only(command, **kwargs):
        assert command[0] != "git", "offline discovery must not invoke Git"
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", offline_only)


def test_missing_cache_discovery_never_creates_or_fetches(
    selected_client, forbid_git, local_transport
):
    client, args, _ = selected_client
    cache = Path(args[-1])
    with pytest.raises(ContractError, match="cache directory is missing"):
        source_checkouts(client, cluster_dir="clusters/demo", cache=cache)
    assert not cache.exists()
    assert not local_transport[1]


def test_discovery_reuses_acquisition_paths_without_git_or_mutation(
    selected_client, capsys, monkeypatch, local_transport
):
    client, args, _ = selected_client
    assert main(args) == 0
    acquired = json.loads(capsys.readouterr().out)
    base = Path(acquired["base"]["path"])
    addon = Path(acquired["addons"][0]["path"])
    # Discovery is not verification: retain and return even a dirty checkout.
    work = addon / "user-work"
    work.write_text("preserve")
    run = subprocess.run

    def offline_only(command, **kwargs):
        assert command[0] != "git", "offline discovery must not invoke Git"
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", offline_only)
    assert source_checkouts(client, cluster_dir="clusters/demo", cache=Path(args[-1])) == (
        base,
        {"sample": addon},
    )
    assert work.read_text() == "preserve"
    # A missing add-on must fail even when Base remains cached.
    preserved = addon.with_name("preserved-work")
    addon.rename(preserved)
    with pytest.raises(ContractError, match="cache directory is missing"):
        source_checkouts(client, cluster_dir="clusters/demo", cache=Path(args[-1]))
    assert not addon.exists()
    assert (preserved / "user-work").read_text() == "preserve"
    assert len(local_transport[1]) == 2


def test_cli_parser_errors_do_not_echo_values(capsys):
    assert main(["--unknown", "private-input"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-input" not in captured.err


def test_changed_pin_uses_new_cache_and_preserves_previous_user_work(
    selected_client, source, capsys
):
    client, args, resources = selected_client
    assert main(args) == 0
    previous = json.loads(capsys.readouterr().out)
    old_checkout = Path(previous["addons"][0]["path"])
    (old_checkout / "keep.txt").write_text("user work")
    upstream = source[0]
    (upstream / "release/manifest.yaml").write_text("spec: {version: 1.0.1}\n")
    git(upstream, "add", ".")
    git(
        upstream,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "next synthetic release",
    )
    resources[1]["spec"]["ref"]["commit"] = git(upstream, "rev-parse", "HEAD")
    (client / "clusters/demo/sources.yaml").write_text(yaml.safe_dump_all(resources))
    assert main(args) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["base"] == previous["base"]
    assert current["addons"][0]["path"] != previous["addons"][0]["path"]
    assert current["addons"][0]["revision"] == resources[1]["spec"]["ref"]["commit"]
    assert (old_checkout / "keep.txt").read_text() == "user work"
    assert git(old_checkout, "rev-parse", "HEAD") == previous["addons"][0]["revision"]


def test_base_annotated_tag_resolves_commit(source):
    upstream, _, resource, _ = source
    revision = resource["spec"]["ref"]["commit"]
    git(
        upstream,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "-c",
        "tag.gpgsign=false",
        "tag",
        "-a",
        "v1.0.0",
        "-m",
        "release",
    )
    resource["spec"]["ref"] = {"tag": "v1.0.0"}
    assert acquire(source, base=True).revision == revision
    assert acquire(source, base=True).revision == revision


@pytest.mark.parametrize(
    "change", ["dirty", "ignored", "origin", "revision", "include", "replacement"]
)
def test_existing_cache_is_rejected_without_modification(source, change):
    _, _, resource, destination = source
    acquire(source)
    if change in ("dirty", "ignored"):
        (destination / "keep.txt").write_text("unrelated work")
        if change == "ignored":
            (destination / ".git/info").mkdir(exist_ok=True)
            (destination / ".git/info/exclude").write_text("keep.txt\n")
    elif change == "origin":
        git(destination, "config", "remote.origin.url", "https://example.test/other.git")
    elif change == "revision":
        resource["spec"]["ref"] = {"commit": "f" * 40}
    elif change == "include":
        with (destination / ".git/config").open("a") as stream:
            stream.write("\n[include]\npath = /missing/private-config\n")
    else:
        revision = resource["spec"]["ref"]["commit"]
        git(destination, "update-ref", "refs/replace/" + "a" * 40, revision)
    before = {
        p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()
    }
    with pytest.raises(ContractError):
        acquire(source)
    after = {
        p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()
    }
    assert before == after


def test_unrelated_directory_is_left_intact(source):
    destination = source[3]
    destination.mkdir()
    (destination / "keep").write_text("keep")
    with pytest.raises(ContractError):
        acquire(source)
    assert list(destination.iterdir()) == [destination / "keep"]


@pytest.mark.parametrize(
    "mutation",
    ["branch", "addon-tag", "identity", "credentials", "filter", "symlink"],
)
def test_invalid_sources_fail_before_fetch(source, mutation):
    _, _, resource, destination = source
    if mutation == "branch":
        resource["spec"]["ref"] = {"branch": "main"}
    elif mutation == "addon-tag":
        resource["spec"]["ref"] = {"tag": "v1.0.0"}
    elif mutation == "identity":
        resource["metadata"]["name"] = "other"
    elif mutation == "credentials":
        resource["spec"]["url"] = "https://user:private-token@example.test/sample.git"
    elif mutation == "filter":
        resource["spec"]["ignore"] = "release/"
    else:
        destination.symlink_to(source[0], target_is_directory=True)
    with pytest.raises(ContractError) as error:
        acquire(source)
    assert "private-token" not in str(error.value)
    if mutation != "symlink":
        assert not destination.exists()


def test_fetch_failure_is_redacted_and_partial_cache_removed(source, monkeypatch, capsys):
    run = subprocess.run

    def fail_fetch(command, **kwargs):
        if "fetch" in command:
            raise subprocess.CalledProcessError(
                128, command, stderr="https://user:private-token@example.test/private-path"
            )
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_fetch)
    with pytest.raises(ContractError, match="cannot acquire selected Git source") as error:
        acquire(source)
    assert error.value.__suppress_context__
    assert "private-token" not in str(error.value)
    assert capsys.readouterr() == ("", "")
    assert not source[3].exists()
    assert not list(source[3].parent.glob(".addon-acquire-*"))


def test_checkout_does_not_execute_global_filters_or_hooks(source, tmp_path, monkeypatch):
    upstream, _, resource, destination = source
    (upstream / ".gitattributes").write_text("*.yaml filter=unsafe\n")
    git(upstream, "add", ".gitattributes")
    git(
        upstream,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "attributes",
    )
    resource["spec"]["ref"]["commit"] = git(upstream, "rev-parse", "HEAD")
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "executed"
    hooks = home / "hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (home / ".gitconfig").write_text(
        f'[core]\nhooksPath = {hooks}\n[filter "unsafe"]\nsmudge = touch {marker}\n'
    )
    monkeypatch.setenv("HOME", str(home))
    acquire(source)
    assert not marker.exists()
    assert git(destination, "status", "--porcelain") == ""
