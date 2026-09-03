# OpenRouter Catalog Sync

Generates deterministic YAML model catalog and JSON pricing files from OpenRouter's public model
API. The command uses no credential and is non-interactive. A policy file preserves intentional
public names and excludes models that must not be published.

## Setup

```bash
uv sync --dev
```

## Policy

Both fields are optional. Unknown fields and malformed values fail closed.

```json
{
  "publicNameOverrides": {
    "openai/gpt-4o": "remote/openrouter/openai/legacy-gpt-4o"
  },
  "excludedModels": ["example/private-model"]
}
```

## Usage

Write both outputs after fetching and validating every page and generating both complete files:

```bash
uv run openrouter-catalog-sync \
  --catalog-output generated/openrouter-catalog.yaml \
  --pricing-output generated/openrouter-pricing.json \
  --policy openrouter-policy.json \
  --write
```

Check committed files byte-for-byte without modifying them:

```bash
uv run openrouter-catalog-sync \
  --catalog-output generated/openrouter-catalog.yaml \
  --pricing-output generated/openrouter-pricing.json \
  --policy openrouter-policy.json \
  --check
```

`--source-url` defaults to `https://openrouter.ai/api/v1/models` and must remain on the
`https://openrouter.ai` origin. Every page must provide consistent `total_count`, `data`, and
explicit `links.next` fields. Relative next links are resolved, but pagination cannot change the
initial origin, redirect, exceed 20 pages, or exceed 5,000 raw records.

Models are sorted by exact upstream ID. The command rejects duplicate IDs and public-name
collisions, and excludes temporary (`~`), batch, policy-excluded, expired, non-text, and
parameterless models. It also excludes routing or pseudo-models whose required base `prompt` or
`completion` price is a negative sentinel; malformed base prices fail instead. Pricing is emitted
as USD per million tokens with at most six fractional places. At most 512 compatible models may
be generated, and each output has a 900,000-byte safety limit. Threshold tiers contain complete
effective rate maps: each tier inherits the base
and lower-threshold changes, while same-threshold overrides apply in source order. Base rates are
retained when unsupported conditional pricing overrides are ignored.

Both outputs are generated, validated, and staged before either destination is replaced. Each
replacement is atomic for its file; same-directory backups restore the original pair, including an
originally absent file, if replacing either destination fails. Output paths may not alias each
other or the policy path by resolved path or existing file identity.

## Output Schemas

```yaml
openrouterCatalog:
  models:
  - name: remote/openrouter/openai/gpt-4o
    upstreamModel: openai/gpt-4o
    label: GPT-4o
    group: Remote-OpenRouter-OpenAI
```

```json
{
  "providers": {
    "openrouter": {
      "models": {
        "openai/gpt-4o": {
          "rates": {
            "input": "2.5",
            "output": "10"
          },
          "tiers": [
            {
              "contextOver": 128000,
              "rates": {
                "input": "5",
                "output": "10"
              }
            }
          ]
        }
      }
    }
  }
}
```

## Quality Gates

```bash
uv lock --check
uv sync --frozen --dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen pytest
uv build
```
