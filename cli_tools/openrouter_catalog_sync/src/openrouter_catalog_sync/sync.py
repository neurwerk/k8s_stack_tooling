"""Fetch, validate, and render the public OpenRouter model catalog."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urljoin, urlsplit

import requests
import yaml

DEFAULT_SOURCE_URL = "https://openrouter.ai/api/v1/models"
PUBLIC_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
TOKEN_FIELDS = {
    "input": ("prompt",),
    "output": ("completion",),
    "cacheRead": ("input_cache_read", "input_audio_cache"),
    "cacheWrite": ("input_cache_write", "input_cache_write_1h"),
    "reasoning": ("internal_reasoning",),
    "inputAudio": ("audio",),
    "outputAudio": ("audio_output",),
}
TOKEN_SOURCE_FIELDS = frozenset(source for sources in TOKEN_FIELDS.values() for source in sources)
RATE_FIELD_ORDER = tuple(TOKEN_FIELDS)
RATE_FIELDS = frozenset(RATE_FIELD_ORDER)
UNSUPPORTED_UNIT_FIELDS = {"request", "image", "image_output", "web_search"}
CONDITION_FIELDS = {"min_prompt_tokens", "utc_start", "utc_end", "utc_days"}
MAX_SELECTED_MODELS = 256
MAX_DESTINATION_METADATA_BYTES = 16_384
MAX_PAGES = 20
MAX_RAW_RECORDS = 5_000
MAX_RESPONSE_BYTES = 10_000_000
MAX_OUTPUT_BYTES = 900_000
RESPONSE_CHUNK_BYTES = 64 * 1024
MAX_RATE_DIGITS = 64
MAX_RATE_INTEGER_DIGITS = 12
MAX_CONTEXT_THRESHOLD = 2_147_483_647
DECIMAL_PATTERN = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


class CatalogSyncError(Exception):
    """Report invalid input or an unsuccessful synchronization."""


class Response(Protocol):
    """Describe the requests response operations used by this module."""

    status_code: int

    def raise_for_status(self) -> None:
        """Raise when the HTTP response is unsuccessful."""

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """Yield response body chunks."""

    def close(self) -> None:
        """Close the response body."""


class HttpClient(Protocol):
    """Describe an injectable HTTP client."""

    def get(self, url: str, *, timeout: float, allow_redirects: bool, stream: bool) -> Response:
        """Fetch a JSON endpoint."""


@dataclass(frozen=True)
class Policy:
    """Store the explicit client model and pricing policy."""

    selected_models: tuple[str, ...]
    public_name_overrides: Mapping[str, str]
    negotiated_pricing: Mapping[str, Mapping[str, object]]
    custom_pricing: Mapping[str, Mapping[str, Mapping[str, object]]]
    grant_to_access_groups: bool


@dataclass(frozen=True)
class GeneratedFiles:
    """Store complete generated file contents."""

    policy: bytes
    catalog: bytes
    pricing: bytes


@dataclass(frozen=True)
class SelectionChoice:
    """Describe one compatible model shown by the interactive selector."""

    upstream_id: str
    display_name: str


def load_policy(path: Path) -> Policy:
    """Load and validate policy JSON from path."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except CatalogSyncError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogSyncError(f"could not read policy {path}: {error}") from error
    policy = _object(raw, "policy")
    expected_fields = {
        "selectedModels",
        "publicNameOverrides",
        "negotiatedPricing",
        "customPricing",
        "grantToAccessGroups",
    }
    unknown = set(policy) - expected_fields
    if unknown:
        raise CatalogSyncError(f"policy contains unknown field: {min(unknown)}")
    missing = expected_fields - set(policy)
    if missing:
        raise CatalogSyncError(f"policy is missing required field: {min(missing)}")

    selections = _policy_selections(policy["selectedModels"])
    overrides = _policy_overrides(policy["publicNameOverrides"])
    negotiated = _policy_negotiated_pricing(policy["negotiatedPricing"])
    custom = _policy_custom_pricing(policy["customPricing"])
    grant = policy["grantToAccessGroups"]
    if not isinstance(grant, bool):
        raise CatalogSyncError("grantToAccessGroups must be a boolean")
    return Policy(selections, overrides, negotiated, custom, grant)


def _policy_selections(raw: object) -> tuple[str, ...]:
    selections_raw = raw
    if not isinstance(selections_raw, list) or not all(
        isinstance(item, str) and item for item in selections_raw
    ):
        raise CatalogSyncError("selectedModels must be a list of nonempty strings")
    selections = cast("list[str]", selections_raw)
    if len(selections) != len(set(selections)):
        raise CatalogSyncError("selectedModels must not contain duplicates")
    if len(selections) > MAX_SELECTED_MODELS:
        raise CatalogSyncError(f"selectedModels exceeds {MAX_SELECTED_MODELS} models")
    return tuple(selections)


def _policy_overrides(raw: object) -> dict[str, str]:
    overrides_raw = _object(raw, "publicNameOverrides")
    overrides: dict[str, str] = {}
    for upstream_id, public_name in overrides_raw.items():
        if not isinstance(upstream_id, str) or not upstream_id:
            raise CatalogSyncError("publicNameOverrides keys must be nonempty strings")
        if not isinstance(public_name, str) or not PUBLIC_NAME_PATTERN.fullmatch(public_name):
            raise CatalogSyncError(f"invalid public name override for {upstream_id}")
        overrides[upstream_id] = public_name
    return overrides


def _policy_negotiated_pricing(raw: object) -> dict[str, Mapping[str, object]]:
    return _policy_pricing_models(raw, "negotiatedPricing", "negotiated pricing")


def _policy_custom_pricing(
    raw: object,
) -> dict[str, Mapping[str, Mapping[str, object]]]:
    providers_raw = _object(raw, "customPricing")
    providers: dict[str, Mapping[str, Mapping[str, object]]] = {}
    for provider_id, raw_models in providers_raw.items():
        if not PROVIDER_ID_PATTERN.fullmatch(provider_id):
            raise CatalogSyncError(f"invalid customPricing provider id: {provider_id}")
        providers[provider_id] = _policy_pricing_models(
            raw_models,
            f"customPricing provider {provider_id}",
            f"custom pricing for {provider_id}",
        )
    return providers


def _policy_pricing_models(
    raw: object, object_label: str, schedule_label: str
) -> dict[str, Mapping[str, object]]:
    models = _object(raw, object_label)
    result: dict[str, Mapping[str, object]] = {}
    for model_id, raw_schedule in models.items():
        if not model_id or not MODEL_ID_PATTERN.fullmatch(model_id):
            raise CatalogSyncError(f"invalid {object_label} model id: {model_id}")
        result[model_id] = _pricing_schedule(raw_schedule, f"{schedule_label}/{model_id}")
    return result


def fetch_models(source_url: str, client: HttpClient | None = None) -> list[object]:
    """Fetch all pages from the public models endpoint."""
    origin = _safe_source_url(source_url)
    if client is None:
        session = requests.Session()
        session.auth = _without_auth
        http: HttpClient = session
    else:
        http = client
    models: list[object] = []
    next_url: str | None = source_url
    visited: set[str] = set()
    model_ids: set[str] = set()
    expected_total: int | None = None
    while next_url is not None:
        if len(visited) >= MAX_PAGES:
            raise CatalogSyncError(f"pagination exceeds {MAX_PAGES} pages")
        if next_url in visited:
            raise CatalogSyncError(f"pagination cycle detected at {next_url}")
        visited.add(next_url)
        page = _fetch_page(http, next_url)
        data, total_count, following = _page_fields(page, next_url)
        if expected_total is None:
            expected_total = total_count
        elif total_count != expected_total:
            raise CatalogSyncError(f"total_count changed during pagination at {next_url}")
        _add_page_records(data, next_url, models, model_ids, expected_total)
        if following is None:
            if len(model_ids) != expected_total:
                raise CatalogSyncError(
                    f"fetched {len(model_ids)} unique records, expected {expected_total}"
                )
            next_url = None
        else:
            resolved = urljoin(next_url, following)
            _require_same_origin(resolved, origin)
            next_url = resolved
    return models


def _fetch_page(http: HttpClient, url: str) -> Mapping[str, object]:
    try:
        response = http.get(url, timeout=30.0, allow_redirects=False, stream=True)
    except requests.RequestException as error:
        raise CatalogSyncError(f"could not fetch {url}: {error}") from error
    try:
        payload = _response_payload(response, url)
    finally:
        response.close()
    return _object(payload, f"response from {url}")


def _response_payload(response: Response, url: str) -> object:
    if 300 <= response.status_code < 400:
        raise CatalogSyncError(f"redirect response rejected from {url}")
    try:
        response.raise_for_status()
    except requests.RequestException as error:
        raise CatalogSyncError(f"could not fetch {url}: {error}") from error
    body = bytearray()
    chunks = response.iter_content(chunk_size=RESPONSE_CHUNK_BYTES)
    while True:
        try:
            chunk = next(chunks)
        except StopIteration:
            break
        except requests.RequestException as error:
            raise CatalogSyncError(f"could not fetch {url}: {error}") from error
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise CatalogSyncError(
                f"response from {url} exceeds the {MAX_RESPONSE_BYTES}-byte safety limit"
            )
    try:
        return json.loads(body)
    except ValueError as error:
        raise CatalogSyncError(f"could not fetch {url}: {error}") from error


def _add_page_records(
    data: Sequence[object],
    url: str,
    models: list[object],
    model_ids: set[str],
    expected_total: int,
) -> None:
    models.extend(data)
    if len(models) > MAX_RAW_RECORDS:
        raise CatalogSyncError(f"catalog exceeds {MAX_RAW_RECORDS} raw records")
    for index, raw_model in enumerate(data):
        model = _object(raw_model, f"record {index} from {url}")
        model_id = _required_string(model, "id", f"record {index} from {url}")
        if model_id in model_ids:
            raise CatalogSyncError(f"duplicate model id during pagination: {model_id}")
        model_ids.add(model_id)
    if len(model_ids) > expected_total:
        raise CatalogSyncError(
            f"fetched {len(model_ids)} records, exceeding total_count {expected_total}"
        )


def _page_fields(page: Mapping[str, object], url: str) -> tuple[list[object], int, str | None]:
    if "data" not in page:
        raise CatalogSyncError(f"response from {url} is missing data")
    data = page["data"]
    if not isinstance(data, list):
        raise CatalogSyncError(f"response from {url} has invalid data")
    data = cast("list[object]", data)
    total_count = page.get("total_count")
    if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
        raise CatalogSyncError(f"response from {url} has invalid total_count")
    if "links" not in page:
        raise CatalogSyncError(f"response from {url} is missing links")
    links = _object(page["links"], f"links from {url}")
    if "next" not in links:
        raise CatalogSyncError(f"links from {url} is missing next")
    following = links["next"]
    if following is not None and (not isinstance(following, str) or not following):
        raise CatalogSyncError(f"links.next from {url} must be a URL or null")
    return data, total_count, following


def generate(
    models: Sequence[object], policy: Policy, *, today: date | None = None
) -> GeneratedFiles:
    """Normalize models and render deterministic catalog and pricing bytes."""
    current_date = today or datetime.now(tz=UTC).date()
    normalized = _normalized_models(models)
    selected = _validated_selected_ids(policy, normalized)
    catalog_models, pricing_models, public_names = _selected_catalog_entries(
        normalized, selected, policy, current_date
    )
    _validate_destination_metadata(public_names)

    catalog = yaml.safe_dump(
        {
            "openrouterCatalog": {
                "enabled": True,
                "excludedModels": [],
                "grantToAccessGroups": policy.grant_to_access_groups,
                "models": catalog_models,
            },
            "infraAgentgatewayWrapper": {
                "modelCatalog": {
                    "sources": [
                        {
                            "configMap": {
                                "name": "client-model-cost-catalog",
                                "key": "catalog.json",
                            }
                        }
                    ]
                }
            },
        },
        allow_unicode=True,
        sort_keys=False,
    ).encode()
    pricing = (
        json.dumps(
            {"providers": _complete_pricing_catalog(pricing_models, policy.custom_pricing)},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    rendered_policy = render_policy(policy)
    for label, content in (
        ("policy", rendered_policy),
        ("catalog", catalog),
        ("pricing", pricing),
    ):
        if len(content) > MAX_OUTPUT_BYTES:
            raise CatalogSyncError(
                f"generated {label} exceeds the {MAX_OUTPUT_BYTES}-byte safety limit"
            )
    return GeneratedFiles(rendered_policy, catalog, pricing)


def _validated_selected_ids(
    policy: Policy, normalized: Mapping[str, Mapping[str, object]]
) -> set[str]:
    if len(policy.selected_models) > MAX_SELECTED_MODELS:
        raise CatalogSyncError(f"selectedModels exceeds {MAX_SELECTED_MODELS} models")
    selected = set(policy.selected_models)
    unknown = selected - set(normalized)
    if unknown:
        raise CatalogSyncError(f"selected model is unknown: {min(unknown)}")
    return selected


def _selected_catalog_entries(
    normalized: Mapping[str, Mapping[str, object]],
    selected: set[str],
    policy: Policy,
    current_date: date,
) -> tuple[list[dict[str, str]], dict[str, object], dict[str, str]]:
    catalog_models: list[dict[str, str]] = []
    pricing_models: dict[str, object] = {}
    public_names: dict[str, str] = {}
    for upstream_id in sorted(selected):
        model = normalized[upstream_id]
        if not _compatible(model, upstream_id, current_date):
            raise CatalogSyncError(f"selected model is incompatible: {upstream_id}")
        public_name = policy.public_name_overrides.get(
            upstream_id, _default_public_name(upstream_id)
        )
        if not PUBLIC_NAME_PATTERN.fullmatch(public_name):
            raise CatalogSyncError(
                f"invalid generated public name for {upstream_id}: {public_name}"
            )
        if public_name in public_names:
            raise CatalogSyncError(
                f"public name collision: {public_name} ({public_names[public_name]}, {upstream_id})"
            )
        public_names[public_name] = upstream_id
        label, publisher = _labels(model, upstream_id)
        catalog_models.append(
            {
                "name": public_name,
                "upstreamModel": upstream_id,
                "label": label,
                "group": f"Remote-OpenRouter-{publisher}",
            }
        )
        if upstream_id in policy.negotiated_pricing:
            pricing_models[upstream_id] = policy.negotiated_pricing[upstream_id]
        else:
            pricing_models[upstream_id] = _pricing(model, upstream_id)
    return catalog_models, pricing_models, public_names


def _validate_destination_metadata(public_names: Mapping[str, str]) -> None:
    metadata = json.dumps(
        dict.fromkeys(sorted(public_names), True), ensure_ascii=False, separators=(",", ":")
    ).encode()
    if len(metadata) > MAX_DESTINATION_METADATA_BYTES:
        raise CatalogSyncError(
            f"selected public-name metadata exceeds {MAX_DESTINATION_METADATA_BYTES} UTF-8 bytes"
        )


def selection_choices(
    models: Sequence[object], policy: Policy, *, today: date | None = None
) -> list[SelectionChoice]:
    """Return compatible choices plus removable stale existing selections."""
    current_date = today or datetime.now(tz=UTC).date()
    normalized = _normalized_models(models)
    choices: list[SelectionChoice] = []
    compatible_ids: set[str] = set()
    for upstream_id in sorted(normalized):
        model = normalized[upstream_id]
        if not _compatible(model, upstream_id, current_date):
            continue
        compatible_ids.add(upstream_id)
        label, _publisher = _labels(model, upstream_id)
        choices.append(SelectionChoice(upstream_id, f"{label} [{upstream_id}]"))
    for upstream_id in sorted(set(policy.selected_models) - compatible_ids):
        reason = "unknown" if upstream_id not in normalized else "incompatible"
        choices.append(SelectionChoice(upstream_id, f"Unavailable ({reason}) [{upstream_id}]"))
    return choices


def with_selected_models(policy: Policy, selected_models: Sequence[str]) -> Policy:
    """Create policy with canonical selections while retaining applicable review data."""
    selected = tuple(sorted(selected_models))
    if len(selected) != len(set(selected)):
        raise CatalogSyncError("selected models must not contain duplicates")
    if len(selected) > MAX_SELECTED_MODELS:
        raise CatalogSyncError(f"selected models exceeds {MAX_SELECTED_MODELS} models")
    return Policy(
        selected,
        policy.public_name_overrides,
        policy.negotiated_pricing,
        policy.custom_pricing,
        policy.grant_to_access_groups,
    )


def render_policy(policy: Policy) -> bytes:
    """Render policy in its canonical reviewed representation."""
    raw = {
        "selectedModels": sorted(policy.selected_models),
        "grantToAccessGroups": policy.grant_to_access_groups,
        "publicNameOverrides": {
            key: policy.public_name_overrides[key] for key in sorted(policy.public_name_overrides)
        },
        "negotiatedPricing": {
            key: policy.negotiated_pricing[key] for key in sorted(policy.negotiated_pricing)
        },
        "customPricing": {
            provider_id: {
                model_id: policy.custom_pricing[provider_id][model_id]
                for model_id in sorted(policy.custom_pricing[provider_id])
            }
            for provider_id in sorted(policy.custom_pricing)
        },
    }
    return (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode()


def _complete_pricing_catalog(
    openrouter_models: Mapping[str, object],
    custom_pricing: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, object]:
    direct_openrouter_models = custom_pricing.get("openrouter", {})
    collisions = set(openrouter_models) & set(direct_openrouter_models)
    if collisions:
        raise CatalogSyncError(
            "customPricing openrouter model is also selected: " + min(collisions)
        )
    models_by_provider: dict[str, Mapping[str, object]] = {
        **{
            provider_id: models
            for provider_id, models in custom_pricing.items()
            if provider_id != "openrouter"
        },
        "openrouter": {**direct_openrouter_models, **openrouter_models},
    }
    return {
        provider_id: {
            "models": {
                model_id: models_by_provider[provider_id][model_id]
                for model_id in sorted(models_by_provider[provider_id])
            }
        }
        for provider_id in sorted(models_by_provider)
    }


def _normalized_models(models: Sequence[object]) -> dict[str, Mapping[str, object]]:
    normalized: dict[str, Mapping[str, object]] = {}
    for index, raw_model in enumerate(models):
        model = _object(raw_model, f"model {index}")
        upstream_id = _required_string(model, "id", f"model {index}")
        if upstream_id in normalized:
            raise CatalogSyncError(f"duplicate model id: {upstream_id}")
        normalized[upstream_id] = model
    return normalized


def write_files(
    generated: GeneratedFiles, policy: Path, catalog_output: Path, pricing_output: Path
) -> None:
    """Stage all files and roll back the set when any replacement fails."""
    validate_paths(policy, catalog_output, pricing_output)
    _require_regular_outputs(policy, catalog_output, pricing_output)
    staged: list[tuple[Path, Path]] = []
    backups: list[tuple[Path | None, Path]] = []
    replaced: list[tuple[Path | None, Path]] = []
    preserve_backups = False
    try:
        _stage_outputs(generated, policy, catalog_output, pricing_output, staged)
        _backup_outputs(staged, backups)
        for (temporary, destination), (backup, _backup_destination) in zip(
            staged, backups, strict=True
        ):
            replaced.append((backup, destination))
            temporary.replace(destination)
    except CatalogSyncError as error:
        rollback_error = _rollback_outputs(replaced)
        preserve_backups = rollback_error is not None
        if rollback_error:
            raise CatalogSyncError(f"{error}; rollback failed: {rollback_error}") from error
        raise
    except OSError as error:
        rollback_error = _rollback_outputs(replaced)
        preserve_backups = rollback_error is not None
        suffix = f"; rollback failed: {rollback_error}" if rollback_error else ""
        raise CatalogSyncError(f"could not write output files: {error}{suffix}") from error
    except BaseException as error:
        rollback_error = _rollback_outputs(replaced)
        preserve_backups = rollback_error is not None
        if rollback_error:
            raise CatalogSyncError(
                f"write interrupted; rollback failed: {rollback_error}"
            ) from error
        raise
    finally:
        for temporary, _destination in staged:
            temporary.unlink(missing_ok=True)
        for backup, _destination in backups:
            if backup is not None and not preserve_backups:
                backup.unlink(missing_ok=True)


def _stage_outputs(
    generated: GeneratedFiles,
    policy: Path,
    catalog_output: Path,
    pricing_output: Path,
    staged: list[tuple[Path, Path]],
) -> None:
    for destination, content in (
        (policy, generated.policy),
        (catalog_output, generated.catalog),
        (pricing_output, generated.pricing),
    ):
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.stage.", dir=destination.parent
        )
        temporary = Path(temporary_name)
        staged.append((temporary, destination))
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if temporary.read_bytes() != content:
            raise CatalogSyncError(f"staged output validation failed for {destination}")


def _backup_outputs(
    staged: Sequence[tuple[Path, Path]], backups: list[tuple[Path | None, Path]]
) -> None:
    for _temporary, destination in staged:
        backup: Path | None = None
        if _lexists(destination):
            descriptor, backup_name = tempfile.mkstemp(
                prefix=f".{destination.name}.backup.", dir=destination.parent
            )
            os.close(descriptor)
            backup = Path(backup_name)
        backups.append((backup, destination))
        if backup is not None:
            shutil.copy2(destination, backup)


def check_files(
    generated: GeneratedFiles, policy: Path, catalog_output: Path, pricing_output: Path
) -> None:
    """Require policy and both output files to exactly match generated bytes."""
    validate_paths(policy, catalog_output, pricing_output)
    _require_regular_outputs(policy, catalog_output, pricing_output)
    mismatches: list[str] = []
    for label, path, expected in (
        ("policy", policy, generated.policy),
        ("catalog", catalog_output, generated.catalog),
        ("pricing", pricing_output, generated.pricing),
    ):
        try:
            actual = path.read_bytes()
        except FileNotFoundError:
            mismatches.append(f"{label} output is missing: {path}")
        except OSError as error:
            raise CatalogSyncError(f"could not read {label} output {path}: {error}") from error
        else:
            if actual != expected:
                mismatches.append(f"{label} output is out of date: {path}")
    if mismatches:
        raise CatalogSyncError("\n".join(mismatches))


def validate_paths(policy: Path, catalog_output: Path, pricing_output: Path) -> None:
    """Reject lexical, symlink, and inode aliases among input and outputs."""
    paths = (
        ("policy", policy),
        ("catalog output", catalog_output),
        ("pricing output", pricing_output),
    )
    for index, (left_label, left) in enumerate(paths):
        for right_label, right in paths[index + 1 :]:
            left_resolved = _resolved(left)
            right_resolved = _resolved(right)
            if left_resolved == right_resolved or _same_file(left, right):
                raise CatalogSyncError(f"{left_label} and {right_label} paths must differ")
            if left_resolved in right_resolved.parents or right_resolved in left_resolved.parents:
                raise CatalogSyncError(
                    f"{left_label} and {right_label} paths must not contain one another"
                )


def _compatible(model: Mapping[str, object], upstream_id: str, today: date) -> bool:
    if (
        upstream_id.startswith("~")
        or upstream_id.startswith("openrouter/")
        or upstream_id.endswith(":batch")
    ):
        return False
    architecture = _object(model.get("architecture"), f"architecture for {upstream_id}")
    output_modalities = architecture.get("output_modalities")
    if not isinstance(output_modalities, list) or not all(
        isinstance(modality, str) for modality in output_modalities
    ):
        raise CatalogSyncError(f"output modalities for {upstream_id} must be a string list")
    supported = model.get("supported_parameters")
    if not isinstance(supported, list) or not all(isinstance(item, str) for item in supported):
        raise CatalogSyncError(f"supported_parameters for {upstream_id} must be a string list")
    if "text" not in output_modalities or not supported:
        return False
    if not _has_supported_base_pricing(model, upstream_id):
        return False
    expiration = model.get("expiration_date")
    if expiration is None:
        return True
    if not isinstance(expiration, str) or not expiration:
        raise CatalogSyncError(f"expiration_date for {upstream_id} must be ISO 8601 or null")
    try:
        if len(expiration) == 10:
            expiration_date = date.fromisoformat(expiration)
        else:
            expiration_datetime = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
            expiration_date = (
                expiration_datetime.astimezone(UTC).date()
                if expiration_datetime.tzinfo is not None
                else expiration_datetime.date()
            )
    except ValueError as error:
        raise CatalogSyncError(
            f"invalid expiration_date for {upstream_id}: {expiration}"
        ) from error
    return expiration_date > today


def _has_supported_base_pricing(model: Mapping[str, object], upstream_id: str) -> bool:
    pricing = _object(model.get("pricing"), f"pricing for {upstream_id}")
    unknown = set(pricing) - TOKEN_SOURCE_FIELDS - UNSUPPORTED_UNIT_FIELDS - {"overrides"}
    if unknown:
        raise CatalogSyncError(f"pricing for {upstream_id} contains unknown field: {min(unknown)}")
    prompt = _decimal(pricing.get("prompt"), "prompt", upstream_id)
    completion = _decimal(pricing.get("completion"), "completion", upstream_id)
    return prompt >= 0 and completion >= 0


def _default_public_name(upstream_id: str) -> str:
    if "/" not in upstream_id:
        raise CatalogSyncError(f"model id must contain author and slug: {upstream_id}")
    author, slug = upstream_id.split("/", 1)
    if not author or not slug:
        raise CatalogSyncError(f"model id must contain author and slug: {upstream_id}")
    return f"remote/openrouter/{author}/{slug.replace(':', '-')}"


def _labels(model: Mapping[str, object], upstream_id: str) -> tuple[str, str]:
    raw_name = model.get("name")
    if raw_name is not None and not isinstance(raw_name, str):
        raise CatalogSyncError(f"name for {upstream_id} must be a string or null")
    name = " ".join(raw_name.split()) if raw_name else upstream_id
    prefix, separator, remainder = name.partition(":")
    publisher = prefix.strip() if separator and prefix.strip() else _publisher_fallback(upstream_id)
    label = remainder.strip() if separator and remainder.strip() else name
    return label, publisher


def _publisher_fallback(upstream_id: str) -> str:
    author = upstream_id.split("/", 1)[0]
    safe = " ".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", author) if part)
    if not safe:
        raise CatalogSyncError(f"could not derive publisher label for {upstream_id}")
    return safe


def _pricing(model: Mapping[str, object], upstream_id: str) -> dict[str, object]:
    pricing = _object(model.get("pricing"), f"pricing for {upstream_id}")
    _reject_per_request_pricing(pricing, upstream_id)
    rates = _rates(pricing, upstream_id, required=True)
    result: dict[str, object] = {"rates": rates}
    overrides = pricing.get("overrides", [])
    if not isinstance(overrides, list):
        raise CatalogSyncError(f"pricing overrides for {upstream_id} must be a list")
    threshold_overrides: list[tuple[int, Mapping[str, object]]] = []
    for index, raw_override in enumerate(overrides):
        override = _object(raw_override, f"pricing override {index} for {upstream_id}")
        fields = set(override)
        unknown_fields = fields - TOKEN_SOURCE_FIELDS - UNSUPPORTED_UNIT_FIELDS - CONDITION_FIELDS
        if unknown_fields:
            raise CatalogSyncError(
                f"pricing override {index} for {upstream_id} contains unknown field: "
                f"{min(unknown_fields)}"
            )
        other_conditions = fields & (CONDITION_FIELDS - {"min_prompt_tokens"})
        if other_conditions or "min_prompt_tokens" not in override:
            raise CatalogSyncError(
                f"pricing override {index} for {upstream_id} cannot be represented; "
                "add negotiatedPricing"
            )
        threshold = _threshold(override["min_prompt_tokens"], upstream_id)
        _reject_per_request_pricing(override, upstream_id)
        if not fields & TOKEN_SOURCE_FIELDS:
            continue
        threshold_overrides.append((threshold, override))
    if threshold_overrides:
        tiers: list[dict[str, object]] = []
        for threshold in sorted({item[0] for item in threshold_overrides}):
            effective_pricing = dict(pricing)
            for override_threshold, override in threshold_overrides:
                if override_threshold <= threshold:
                    effective_pricing.update(
                        (field, override[field])
                        for field in TOKEN_SOURCE_FIELDS
                        if field in override
                    )
            tiers.append(
                {
                    "contextOver": threshold,
                    "rates": _rates(effective_pricing, upstream_id, required=True),
                }
            )
        result["tiers"] = tiers
    return result


def _reject_per_request_pricing(pricing: Mapping[str, object], upstream_id: str) -> None:
    if "request" not in pricing:
        return
    value = _decimal(pricing["request"], "request", upstream_id)
    if not value.is_zero():
        raise CatalogSyncError(
            f"per-request pricing for {upstream_id} cannot be represented; add negotiatedPricing"
        )


def _pricing_schedule(raw: object, label: str) -> Mapping[str, object]:
    schedule = _object(raw, label)
    unknown = set(schedule) - {"rates", "tiers"}
    if unknown:
        raise CatalogSyncError(f"{label} contains unknown field: {min(unknown)}")
    if "rates" not in schedule:
        raise CatalogSyncError(f"{label} is missing rates")
    result: dict[str, object] = {"rates": _schedule_rates(schedule["rates"], label, "base rates")}
    tiers_raw = schedule.get("tiers")
    if tiers_raw is None:
        return result
    if not isinstance(tiers_raw, list):
        raise CatalogSyncError(f"{label} tiers must be a list")
    tiers: list[dict[str, object]] = []
    previous_threshold = -1
    for index, raw_tier in enumerate(tiers_raw):
        tier = _object(raw_tier, f"{label} tier {index}")
        if set(tier) != {"contextOver", "rates"}:
            raise CatalogSyncError(f"{label} tier {index} must contain only contextOver and rates")
        threshold = _threshold(tier["contextOver"], label)
        if threshold <= previous_threshold:
            raise CatalogSyncError(f"{label} tiers must have increasing thresholds")
        previous_threshold = threshold
        tiers.append(
            {
                "contextOver": threshold,
                "rates": _schedule_rates(tier["rates"], label, f"tier {index} rates"),
            }
        )
    result["tiers"] = tiers
    return result


def _schedule_rates(raw: object, schedule_label: str, rates_label: str) -> dict[str, str]:
    rates = _object(raw, f"{schedule_label} {rates_label}")
    unknown = set(rates) - RATE_FIELDS
    if unknown:
        raise CatalogSyncError(
            f"{schedule_label} {rates_label} contains unknown rate: {min(unknown)}"
        )
    missing = {"input", "output"} - set(rates)
    if missing:
        raise CatalogSyncError(f"{schedule_label} {rates_label} is missing rate: {min(missing)}")
    result: dict[str, str] = {}
    for field in RATE_FIELD_ORDER:
        if field not in rates:
            continue
        raw_value = rates[field]
        significant_fractional_digits = (
            len(raw_value.rsplit(".", 1)[1].rstrip("0"))
            if isinstance(raw_value, str) and "." in raw_value
            else 0
        )
        if significant_fractional_digits > 6:
            raise CatalogSyncError(
                f"{schedule_label} {field} must have at most six fractional digits"
            )
        value = _decimal(raw_value, field, schedule_label)
        if value < 0:
            raise CatalogSyncError(f"invalid {schedule_label} {field}: {raw_value}")
        if value.is_zero():
            result[field] = "0"
        else:
            rendered = format(value, "f")
            result[field] = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    return result


def _threshold(raw: object, upstream_id: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise CatalogSyncError(f"invalid min_prompt_tokens override for {upstream_id}")
    if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
        raise CatalogSyncError(f"invalid min_prompt_tokens override for {upstream_id}")
    threshold = int(raw)
    if not 0 <= threshold <= MAX_CONTEXT_THRESHOLD:
        raise CatalogSyncError(f"invalid min_prompt_tokens override for {upstream_id}")
    return threshold


def _rates(pricing: Mapping[str, object], upstream_id: str, *, required: bool) -> dict[str, str]:
    rates: dict[str, str] = {}
    for destination, sources in TOKEN_FIELDS.items():
        candidates = [
            (source, _decimal(pricing[source], source, upstream_id))
            for source in sources
            if source in pricing
        ]
        for source, value in candidates:
            if value < 0:
                raise CatalogSyncError(
                    f"invalid pricing {source} for {upstream_id}: {pricing[source]}"
                )
        if candidates:
            source, _value = max(candidates, key=lambda candidate: candidate[1])
            rates[destination] = _per_million(pricing[source], source, upstream_id)
    if required and not rates:
        raise CatalogSyncError(f"pricing for {upstream_id} has no supported token rates")
    return rates


def _per_million(raw: object, field: str, upstream_id: str) -> str:
    value = _decimal(raw, field, upstream_id)
    if value < 0:
        raise CatalogSyncError(f"invalid pricing {field} for {upstream_id}: {raw}")
    try:
        with localcontext() as context:
            context.prec = MAX_RATE_DIGITS + 16
            per_million = (value * Decimal(1_000_000)).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
    except (DecimalException, ArithmeticError) as error:
        raise CatalogSyncError(f"invalid pricing {field} for {upstream_id}: {raw}") from error
    if per_million.is_zero():
        return "0"
    rendered = format(per_million, "f").rstrip("0").rstrip(".")
    return rendered or "0"


def _decimal(raw: object, field: str, upstream_id: str) -> Decimal:
    unsigned = raw.lstrip("+-") if isinstance(raw, str) else ""
    integer_digits = len(unsigned.split(".", 1)[0].lstrip("0")) or 1
    if (
        not isinstance(raw, str)
        or not DECIMAL_PATTERN.fullmatch(raw)
        or sum(character.isdigit() for character in raw) > MAX_RATE_DIGITS
        or integer_digits > MAX_RATE_INTEGER_DIGITS
    ):
        raise CatalogSyncError(f"pricing {field} for {upstream_id} must be a decimal string")
    try:
        value = Decimal(raw)
    except DecimalException as error:
        raise CatalogSyncError(f"invalid pricing {field} for {upstream_id}: {raw}") from error
    if not value.is_finite():
        raise CatalogSyncError(f"invalid pricing {field} for {upstream_id}: {raw}")
    return value


def _without_auth(request: requests.PreparedRequest) -> requests.PreparedRequest:
    """Prevent ambient netrc credentials from reaching the public endpoint."""
    return request


def _object(raw: object, label: str) -> Mapping[str, object]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise CatalogSyncError(f"{label} must be a JSON object")
    return cast("dict[str, object]", raw)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogSyncError(f"policy contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _required_string(model: Mapping[str, object], field: str, label: str) -> str:
    value = model.get(field)
    if not isinstance(value, str) or not value:
        raise CatalogSyncError(f"{field} for {label} must be a nonempty string")
    return value


def _safe_source_url(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise CatalogSyncError(f"invalid source URL: {error}") from error
    if parsed.scheme.lower() != "https" or not hostname:
        raise CatalogSyncError("source URL must use HTTPS with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise CatalogSyncError("source URL must not contain userinfo")
    origin = parsed.scheme.lower(), hostname.rstrip(".").lower(), port or 443
    if origin != ("https", "openrouter.ai", 443):
        raise CatalogSyncError("source URL must use the https://openrouter.ai origin")
    return origin


def _require_same_origin(url: str, origin: tuple[str, str, int]) -> None:
    try:
        candidate = _safe_source_url(url)
    except CatalogSyncError as error:
        raise CatalogSyncError(f"pagination URL changes origin: {url}") from error
    if candidate != origin:
        raise CatalogSyncError(f"pagination URL changes origin: {url}")


def _same_file(left: Path, right: Path) -> bool:
    if not _lexists(left) or not _lexists(right):
        return False
    try:
        return left.samefile(right)
    except OSError as error:
        raise CatalogSyncError(f"could not compare paths {left} and {right}: {error}") from error


def _require_regular_outputs(*paths: Path) -> None:
    for path in paths:
        if _lexists(path) and (path.is_symlink() or not path.is_file()):
            raise CatalogSyncError(f"existing output path must be a regular file: {path}")


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError) as error:
        raise CatalogSyncError(f"could not resolve path {path}: {error}") from error


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _rollback_outputs(backups: Sequence[tuple[Path | None, Path]]) -> str | None:
    failure: str | None = None
    for backup, destination in reversed(backups):
        try:
            if backup is not None and _lexists(backup):
                backup.replace(destination)
            elif backup is None and _lexists(destination):
                destination.unlink()
        except OSError as error:
            failure = f"could not restore {backup} to {destination}: {error}"
    return failure
