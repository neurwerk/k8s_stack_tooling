"""Actual Kustomize patch semantics govern ownership and effective resource reads."""

import subprocess

import pytest
import yaml

from k8s_stack_tooling.addons import ContractError, check
from k8s_stack_tooling.addons.validation import check_client, render_stage


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value))


def package(path, objects):
    path.mkdir(parents=True, exist_ok=True)
    (path / "objects.yaml").write_text(yaml.safe_dump_all(objects))
    write(path / "kustomization.yaml", {"resources": ["objects.yaml"]})


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_inline_delete_honors_ownership_handoff_but_remaining_claim_still_fails(tmp_path):
    base, client, addon = [tmp_path / name for name in ("base", "client", "addon")]
    aggregate = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"namespace": "app", "name": "aggregate"},
        "spec": {
            "secretStoreRef": {"name": "store", "kind": "SecretStore"},
            "target": {"name": "password"},
            "data": [
                {
                    "secretKey": "password",
                    "remoteRef": {"key": "platform/database", "property": "password"},
                }
            ],
        },
    }
    package(base / "releases/shared", [aggregate])
    write(base / "release/manifest.yaml", {"spec": {"version": "1.0.0"}})
    git(base, "init", "-b", "main")
    git(base, "add", ".")
    git(
        base,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "fixture",
    )
    revision = git(base, "rev-parse", "HEAD")
    write(
        addon / "release/manifest.yaml",
        {
            "apiVersion": "addons.neurwerk.com/v1alpha2",
            "kind": "AddonRelease",
            "metadata": {"name": "synthetic"},
            "spec": {
                "version": "1.0.0",
                "status": "staged",
                "sourceName": "synthetic",
                "configurationKey": "synthetic",
                "prerequisites": {"base": "1.0.0", "tooling": "0.14.0", "stackSetup": "0.7.0"},
                "packages": [{"name": "app", "path": "releases/app"}],
                "inputs": [
                    {"kind": "Secret", "namespace": "app", "name": "password", "owner": "client"}
                ],
            },
        },
    )
    package(
        addon / "releases/app",
        [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "app"}}],
    )
    dedicated = {**aggregate, "metadata": {"namespace": "app", "name": "dedicated"}}
    package(client / "resources", [dedicated])

    def source(name, commit):
        return {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "GitRepository",
            "metadata": {"namespace": "flux-system", "name": name},
            "spec": {
                "interval": "1m",
                "ref": {"commit": commit},
                "url": f"https://example.test/{name}.git",
            },
        }

    def stage(name, source_name, path):
        return {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"namespace": "flux-system", "name": name},
            "spec": {
                "interval": "1m",
                "path": path,
                "sourceRef": {
                    "kind": "GitRepository",
                    "namespace": "flux-system",
                    "name": source_name,
                },
                "wait": True,
            },
        }

    shared = stage("shared", "k8s-stack", "releases/shared")
    resources = [
        source("k8s-stack", revision),
        source("synthetic", "a" * 40),
        source("flux-system", "b" * 40),
        shared,
        stage("dedicated", "flux-system", "resources"),
        stage("app", "synthetic", "releases/app"),
    ]
    package(client / "clusters/test", resources)
    write(
        client / "config/addons.yaml",
        {
            "apiVersion": "addons.neurwerk.com/v1alpha2",
            "kind": "AddonSelection",
            "baseSourceRef": {
                "kind": "GitRepository",
                "namespace": "flux-system",
                "name": "k8s-stack",
            },
            "addons": [
                {
                    "id": "synthetic",
                    "sourceRef": {
                        "kind": "GitRepository",
                        "namespace": "flux-system",
                        "name": "synthetic",
                    },
                }
            ],
        },
    )
    definition = check(addon)
    with pytest.raises(ContractError, match="overlapping ownership"):
        check_client([(addon, definition)], client, base=base, cluster_dir="clusters/test")
    shared["spec"]["patches"] = [
        {
            "patch": yaml.safe_dump(
                {
                    "apiVersion": "external-secrets.io/v1",
                    "kind": "ExternalSecret",
                    "metadata": {"namespace": "app", "name": "aggregate"},
                    "$patch": "delete",
                }
            )
        }
    ]
    package(client / "clusters/test", resources)
    check_client([(addon, definition)], client, base=base, cluster_dir="clusters/test")
    package(
        base / "releases/shared",
        [aggregate, {**aggregate, "metadata": {"namespace": "app", "name": "leftover"}}],
    )
    with pytest.raises(ContractError, match="overlapping ownership"):
        check_client(
            [(addon, definition)],
            client,
            base=base,
            base_candidate=base,
            cluster_dir="clusters/test",
        )


def test_inline_json_patch_changes_effective_image_without_interpreting_code(tmp_path):
    package(
        tmp_path / "app",
        [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "task"},
                "spec": {
                    "template": {
                        "spec": {"containers": [{"name": "app", "image": "example.test/app:1.0.0"}]}
                    }
                },
            }
        ],
    )
    stage = {
        "spec": {
            "path": "app",
            "patches": [
                {
                    "target": {"kind": "Job", "name": "task"},
                    "patch": '[{"op":"replace","path":"/spec/template/spec/containers/0/image",'
                    '"value":"example.test/app:2.0.0"}]',
                }
            ],
        }
    }
    result = render_stage(stage, tmp_path)
    assert (
        result[0]["spec"]["template"]["spec"]["containers"][0]["image"] == "example.test/app:2.0.0"
    )
    stage["spec"]["patches"] = [{"path": "../../outside.yaml"}]
    with pytest.raises(ContractError, match="inline"):
        render_stage(stage, tmp_path)
