"""Run the openrouter-catalog-sync command-line application."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from openrouter_catalog_sync.sync import (
    DEFAULT_SOURCE_URL,
    CatalogSyncError,
    check_files,
    fetch_models,
    generate,
    load_policy,
    validate_paths,
    write_files,
)


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate catalog and pricing files from public OpenRouter metadata."
    )
    parser.add_argument("--catalog-output", required=True, type=Path)
    parser.add_argument("--pricing-output", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Write both generated files.")
    mode.add_argument("--check", action="store_true", help="Check both files byte-for-byte.")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    """Run synchronization and convert expected failures to concise CLI errors."""
    args = parse_arguments(arguments)
    try:
        validate_paths(args.policy, args.catalog_output, args.pricing_output)
        policy = load_policy(args.policy)
        generated = generate(fetch_models(args.source_url), policy)
        if args.write:
            write_files(generated, args.catalog_output, args.pricing_output)
        else:
            check_files(generated, args.catalog_output, args.pricing_output)
    except CatalogSyncError as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201 -- Explicit CLI diagnostics.
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
