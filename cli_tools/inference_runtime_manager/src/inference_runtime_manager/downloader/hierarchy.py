"""Shared catalogue navigation and immutable artifact folder names."""

from __future__ import annotations

import re
from functools import cache
from importlib.resources import files
from pathlib import Path

import questionary
import yaml
from pydantic import TypeAdapter

from inference_runtime_manager.downloader.models import AvailableCatalog, CatalogModel, ModelVariant

Identity = tuple[str, str]


@cache
def _mapping() -> dict[str, list[str]]:
    return TypeAdapter(dict[str, list[str]]).validate_python(
        yaml.safe_load(
            files("inference_runtime_manager.resources").joinpath("hierarchy.yaml").read_text()
        ),
        strict=True,
    )


def grouping(model: CatalogModel, variant: ModelVariant) -> tuple[str, ...]:
    labels = _mapping().get(model.id)
    if not isinstance(labels, list) or len(labels) not in (3, 4):
        raise ValueError(f"Missing publisher/family/version grouping for {model.id}")
    owner = (variant.source or model.source).split("/", 1)[0]
    edition = labels[3] if len(labels) == 4 else owner
    return (*labels[:3], edition)


def component(label: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", label.lower()).strip(".-")
    if not value or value in (".", "..") or "/" in label or "\\" in label:
        raise ValueError(f"Invalid hierarchy component: {label!r}")
    return value


def artifact_path(model: CatalogModel, variant: ModelVariant, revision: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Artifact revision must be an immutable commit SHA")
    return Path(
        "models",
        model.category,
        *(component(x) for x in grouping(model, variant)),
        variant.id,
        revision,
    )


def validate_hierarchy(catalog: AvailableCatalog) -> None:
    paths: set[Path] = set()
    for model in catalog.models:
        for variant in model.variants:
            path = artifact_path(model, variant, "0" * 40)
            if path in paths:
                raise ValueError(f"Colliding catalogue hierarchy path: {path}")
            paths.add(path)


def browse(
    catalog: AvailableCatalog,
    identities: set[Identity] | None = None,
    selected: set[Identity] | None = None,
    *,
    multiple: bool = False,
) -> set[Identity] | None:
    """Browse categories and model groups, preserving edits across Back and edition changes.

    None means cancellation; only the root Save action publishes multiple selections.
    """
    from inference_runtime_manager.downloader.presentation import CATEGORY_NAMES

    leaves = {
        (model.id, variant.id): (
            (CATEGORY_NAMES.get(model.browse_group, model.browse_group), *grouping(model, variant)),
            variant,
        )
        for model in catalog.models
        for variant in model.variants
        if identities is None or (model.id, variant.id) in identities
    }
    draft = set(selected or ())
    prefix: list[str] = []
    headings = ("Category", "Publisher", "Family", "Version / size", "Edition")
    while True:
        scope = {
            key: value for key, value in leaves.items() if value[0][: len(prefix)] == tuple(prefix)
        }
        title = " / ".join(prefix)
        if len(prefix) < len(headings):
            labels = sorted({value[0][len(prefix)] for value in scope.values()}, key=str.casefold)
            options = [questionary.Choice(label, value=label) for label in labels]
            if not prefix and multiple:
                options.append(
                    questionary.Choice(f"Review selection ({len(draft)})", value="__save")
                )
            options.append(questionary.Choice("Back" if prefix else "Cancel", value="__back"))
            answer = questionary.select(
                f"{title} — {headings[len(prefix)]}" if title else headings[0], choices=options
            ).ask()
            if answer is None or (answer == "__back" and not prefix):
                return None
            if answer == "__save":
                return draft
            if not isinstance(answer, str):
                raise ValueError("Invalid hierarchy selection")
            if answer == "__back":
                prefix.pop()
            else:
                prefix.append(answer)
            continue
        options = [
            questionary.Choice(
                f"{variant.quantization if variant.quantization != 'none' else variant.precision or variant.format} — {variant.id}",
                value=identity,
                checked=identity in draft,
                description=variant.compatibility_notes,
            )
            for identity, (_, variant) in sorted(scope.items())
        ]
        if multiple:
            answer = questionary.checkbox(
                f"{title} — quantization / format (Space toggles; Enter keeps edits)",
                choices=options,
            ).ask()
            if answer is None:
                return None
            draft.difference_update(scope)
            draft.update(answer)
            prefix.pop()
        else:
            options.append(questionary.Choice("Back", value="__back"))
            answer = questionary.select(f"{title} — quantization / format", choices=options).ask()
            if answer is None:
                return None
            if answer == "__back":
                prefix.pop()
            else:
                return {answer}
