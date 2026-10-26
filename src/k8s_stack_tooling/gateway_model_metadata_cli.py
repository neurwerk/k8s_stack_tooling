"""Explicit, provider-neutral refresh of local gateway model metadata values."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests
import yaml
from pydantic import ValidationError

from k8s_stack_tooling.gateway_catalog.catalog import GatewayCatalog
from k8s_stack_tooling.gateway_catalog.metadata import MetadataError, load_json
from k8s_stack_tooling.gateway_catalog.values import read_values, write_values
from k8s_stack_tooling.model_metadata_refresh import RefreshSources, refresh


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("refresh", help="Read provider metadata; preview by default.")
    command.add_argument("--catalog", type=Path, required=True)
    command.add_argument("--values", action="append", type=Path, required=True)
    command.add_argument("--sources", type=Path, required=True)
    command.add_argument("--valid-hours", type=int, default=168)
    command.add_argument("--output", type=Path)
    command.add_argument("--write", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    try:
        if args.write and args.output is None:
            raise MetadataError("--write requires an explicit --output values file")
        catalog = GatewayCatalog.model_validate(load_json(args.catalog.read_text()))
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
        if args.write:
            write_values(args.output, additions)
        return 0
    except MetadataError as error:
        print(f"Metadata refresh failed: {error}", file=sys.stderr)
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
    ):
        # Upstream exceptions can contain credentials or provider response bodies.
        print(
            "Metadata refresh failed; check source, freshness and approved inputs.", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
