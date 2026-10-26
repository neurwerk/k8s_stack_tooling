"""Explicit local refresh/projection generation and manual installed-job launcher."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException
from pydantic import ValidationError

from k8s_stack_tooling.dify_model_sync.launcher import launch
from k8s_stack_tooling.dify_model_sync.projection import build_projection
from k8s_stack_tooling.gateway_catalog import GatewayCatalog
from k8s_stack_tooling.gateway_model_metadata import MetadataError, load_json
from k8s_stack_tooling.model_metadata_refresh import RefreshSources, refresh


def merge_values(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_values(target[key], value)
        else:
            target[key] = value


def read_values(paths: list[Path]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for path in paths:
        value = yaml.safe_load(path.read_text())
        if not isinstance(value, dict):
            raise MetadataError("values must be YAML objects")
        merge_values(result, value)
    return result


def write_values(path: Path, additions: dict[str, Any]) -> None:
    """Replace one reviewed values file atomically, preserving unrelated settings."""
    existing = read_values([path]) if path.exists() else {}
    merge_values(existing, additions)
    content = yaml.safe_dump(existing, allow_unicode=True, sort_keys=False)
    if path.is_symlink():
        raise MetadataError("output must not be a symlink")
    # Preserve existing permissions: reviewed values may contain private configuration.
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix=".model-metadata-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual metadata and Dify model-sync operations.")
    commands = parser.add_subparsers(dest="command", required=True)
    refresh_parser = commands.add_parser(
        "refresh", help="GET provider metadata; preview by default."
    )
    refresh_parser.add_argument("--catalog", type=Path, required=True)
    refresh_parser.add_argument("--values", action="append", type=Path, required=True)
    refresh_parser.add_argument("--sources", type=Path, required=True)
    refresh_parser.add_argument("--valid-hours", type=int, default=168)
    refresh_parser.add_argument("--output", type=Path)
    refresh_parser.add_argument("--write", action="store_true")
    project = commands.add_parser("project", help="Generate reviewed Dify projection and roles.")
    project.add_argument("--catalog", type=Path, required=True)
    project.add_argument("--metadata", type=Path, required=True)
    project.add_argument("--output", type=Path)
    project.add_argument("--write", action="store_true")
    run = commands.add_parser(
        "launch", help="Create a one-shot Job from the installed inert template."
    )
    run.add_argument("--context", required=True)
    run.add_argument("--namespace", default="frontend-dify")
    run.add_argument("--catalog-namespace", default="frontend-studio")
    run.add_argument("--mode", choices=["preview", "apply"], default="preview")
    run.add_argument("--expected-projection-revision", required=True)
    run.add_argument("--max-company-models", type=int, default=27)
    run.add_argument("--wait-seconds", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    try:
        if args.command == "launch":
            if not args.context.strip() or not 0 <= args.wait_seconds <= 86400:
                raise MetadataError("explicit context and bounded wait are required")
            # This is the only operation with cluster access; never fall back to current context.
            with config.new_client_from_config(context=args.context, persist_config=False) as api:
                result = launch(
                    client.CoreV1Api(api),
                    client.BatchV1Api(api),
                    catalog_namespace=args.catalog_namespace,
                    namespace=args.namespace,
                    mode=args.mode,
                    expected=args.expected_projection_revision,
                    max_company=args.max_company_models,
                    wait_seconds=args.wait_seconds,
                )
            print(json.dumps(result, sort_keys=True))
            return 1 if result["status"] in {"failed", "timeout", "uncertain"} else 0
        if args.write and args.output is None:
            raise MetadataError("--write requires an explicit --output values file")
        catalog = GatewayCatalog.model_validate(load_json(args.catalog.read_text()))
        if args.command == "refresh":
            sources = RefreshSources.model_validate(load_json(args.sources.read_text()))
            rows, counts = refresh(
                catalog, read_values(args.values), sources, valid_hours=args.valid_hours
            )
            additions = {"gatewayModelMetadata": {"models": rows}}
            print(
                json.dumps(
                    {
                        "mode": "write" if args.write else "preview",
                        **counts,
                        "proposed_values": additions,
                    },
                    sort_keys=True,
                )
            )
        else:
            projection = build_projection(catalog, load_json(args.metadata.read_text()))
            additions = {
                "frontendDify": {"modelSync": {"projection": projection}},
                "authKeycloak": {"difyAgentgatewayClientRoles": projection["roles"]},
            }
            print(
                json.dumps(
                    {
                        "projection_revision": projection["projection_revision"],
                        "selected": len(projection["models"]),
                        "proposed_values": additions,
                        "missing_metadata": sum(
                            m["metadata"] is None for m in projection["models"]
                        ),
                    },
                    sort_keys=True,
                )
            )
        if args.write:
            write_values(args.output, additions)
        return 0
    except MetadataError as error:
        print(f"Model-sync failed: {error}", file=sys.stderr)
        return 1
    except (
        ValidationError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        yaml.YAMLError,
        requests.RequestException,
        ApiException,
        ConfigException,
    ):
        # Exceptions may contain credentials or sensitive request/response bodies.
        print(
            "Model-sync failed; check contract, freshness, permissions and approved inputs.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
