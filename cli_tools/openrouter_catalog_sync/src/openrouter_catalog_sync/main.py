"""Run the openrouter-catalog-sync command-line application."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from openrouter_catalog_sync.sync import (
    DEFAULT_SOURCE_URL,
    CatalogSyncError,
    SelectionChoice,
    check_files,
    fetch_models,
    generate,
    load_policy,
    selection_choices,
    validate_paths,
    with_selected_models,
    write_files,
)


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate client model values and a complete cost catalog."
    )
    parser.add_argument("--catalog-output", required=True, type=Path)
    parser.add_argument("--pricing-output", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--select", action="store_true", help="Interactively select models.")
    mode.add_argument(
        "--write", action="store_true", help="Write canonical policy and generated files."
    )
    mode.add_argument("--check", action="store_true", help="Check all files byte-for-byte.")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    """Run synchronization and convert expected failures to concise CLI errors."""
    args = parse_arguments(arguments)
    try:
        validate_paths(args.policy, args.catalog_output, args.pricing_output)
        policy = load_policy(args.policy)
        models = fetch_models(args.source_url)
        if args.select:
            available = selection_choices(models, policy)
            selected = _prompt_for_models(available, policy.selected_models)
            policy = with_selected_models(policy, selected)
        generated = generate(models, policy)
        if args.write or args.select:
            write_files(generated, args.policy, args.catalog_output, args.pricing_output)
        else:
            check_files(generated, args.policy, args.catalog_output, args.pricing_output)
    except CatalogSyncError as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201 -- Explicit CLI diagnostics.
        raise SystemExit(1) from None


def _prompt_for_models(available: Sequence[SelectionChoice], selected: Sequence[str]) -> list[str]:
    """Run the fuzzy multiselect and translate terminal cancellation."""
    selected_set = set(selected)
    choices = [
        Choice(value=None, name="(No models)", enabled=not selected_set),
        *(
            Choice(
                value=choice.upstream_id,
                name=choice.display_name,
                enabled=choice.upstream_id in selected_set,
            )
            for choice in available
        ),
    ]
    try:
        answer = inquirer.fuzzy(
            message="Select OpenRouter models:",
            choices=choices,
            multiselect=True,
            mandatory=False,
            max_height=20,
            instruction="Type to search; Space toggles; Enter confirms.",
        ).execute()
    except (EOFError, KeyboardInterrupt):
        raise CatalogSyncError("model selection was cancelled") from None
    if not isinstance(answer, list):
        raise CatalogSyncError("model selection returned an invalid result")
    if None in answer and selected_set:
        return []
    selected_answer = [item for item in answer if item is not None]
    if not all(isinstance(item, str) for item in selected_answer):
        raise CatalogSyncError("model selection returned an invalid result")
    return cast("list[str]", selected_answer)


if __name__ == "__main__":
    main()
