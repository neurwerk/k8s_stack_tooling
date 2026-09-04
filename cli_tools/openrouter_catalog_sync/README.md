# OpenRouter Catalog Sync

Interactively selects compatible models from OpenRouter's public model API and generates
deterministic client Helm values and a complete JSON model cost catalog. The command uses no
credential. Its non-interactive write and check modes are suitable for automation.

## Setup

```bash
uv sync --dev
```

## Policy

All five fields are required. `selectedModels` is an explicit allowlist of unique exact upstream
IDs; newly discovered models are not selected automatically. `grantToAccessGroups` controls
whether Base grants selected models to declared AgentGateway access groups. `publicNameOverrides`
preserves reviewed serving names. `negotiatedPricing` maps upstream IDs to complete AgentGateway
pricing schedules in USD per million tokens. A negotiated schedule fully replaces that model's
fetched schedule when selected; selected models without one use fetched pricing. Overrides and
negotiated schedules are retained when a model is deselected. `customPricing` maps provider and
model IDs to complete schedules for direct and local models. Provider IDs must be lowercase.
OpenRouter entries are allowed for direct models but must not overlap `selectedModels`; use
`negotiatedPricing` to replace fetched pricing for selected models. Unknown fields, malformed
values, and identifier collisions fail closed.

```json
{
  "selectedModels": [
    "openai/gpt-4o"
  ],
  "grantToAccessGroups": true,
  "publicNameOverrides": {
    "openai/gpt-4o": "remote/openrouter/openai/legacy-gpt-4o"
  },
  "negotiatedPricing": {
    "openai/gpt-4o": {
      "rates": {
        "input": "2",
        "output": "8"
      },
      "tiers": [
        {
          "contextOver": 128000,
          "rates": {
            "input": "4",
            "output": "12"
          }
        }
      ]
    }
  },
  "customPricing": {
    "deepseek": {
      "deepseek-chat": {
        "rates": {
          "input": "0.14",
          "output": "0.28",
          "cacheRead": "0.0028"
        }
      }
    },
    "llamacpp": {
      "local/qwen2.5:14b": {
        "rates": {
          "input": "0",
          "output": "0"
        }
      }
    }
  }
}
```

## Usage

Open the fuzzy searchable multiselect for OpenRouter. Existing IDs are preselected; stale IDs are
shown as unavailable but remain togglable so they can be removed. Type to search, press Space to
toggle a model, and press Enter to confirm. Confirmation transactionally updates the canonical
policy and both generated files with rollback on a caught failure. Ctrl-C or terminal cancellation
leaves all three files unchanged:

```bash
uv run openrouter-catalog-sync \
  --catalog-output generated/openrouter-catalog.yaml \
  --pricing-output generated/client-model-cost-catalog.json \
  --policy openrouter-policy.json \
  --select
```

Non-interactively canonicalize the policy and write all three files after fetching and validating
every page:

```bash
uv run openrouter-catalog-sync \
  --catalog-output generated/openrouter-catalog.yaml \
  --pricing-output generated/client-model-cost-catalog.json \
  --policy openrouter-policy.json \
  --write
```

Check the canonical policy and generated files byte-for-byte without modifying them:

```bash
uv run openrouter-catalog-sync \
  --catalog-output generated/openrouter-catalog.yaml \
  --pricing-output generated/client-model-cost-catalog.json \
  --policy openrouter-policy.json \
  --check
```

`--source-url` defaults to `https://openrouter.ai/api/v1/models` and must remain on the
`https://openrouter.ai` origin. Every page must provide consistent `total_count`, `data`, and
explicit `links.next` fields. Relative next links are resolved, but pagination cannot change the
initial origin, redirect, exceed 20 pages, or exceed 5,000 raw records.

Models are sorted by exact upstream ID. The command rejects duplicate IDs and public-name
collisions, and excludes temporary (`~`), batch, expired, non-text, and parameterless models from
interactive choices. OpenRouter routing pseudo-models are never selectable. Unknown or
incompatible selected IDs fail closed. The complete pricing catalog combines selected OpenRouter
pricing with every `customPricing` provider. Pricing is emitted as USD per million tokens with at
most six fractional places. Where OpenRouter publishes multiple cache rates that AgentGateway
represents as one bucket, the generator uses the highest rate. Pricing conditions that
AgentGateway cannot represent require `negotiatedPricing` instead of being silently omitted.
Per-image and web-search charges are omitted because the platform's strict model request contract
does not permit those features. Negotiated and custom base and tier rates are canonicalized by
removing plus signs, redundant leading and trailing zeros, and signed-zero differences. At most
256 OpenRouter models may be selected. The compact JSON map of public names to `true` must not
exceed 16,384 UTF-8 bytes, and each output has a 900,000-byte safety limit. Threshold tiers contain
complete effective rate maps: each tier inherits the base and lower-threshold changes, while
same-threshold overrides apply in source order.

The canonical policy and both outputs are generated, validated, and staged before any destination
is replaced. Existing destinations remain live while same-directory backups are copied. Each file
replacement is atomic, and caught replacement failures trigger rollback of files already replaced,
including removal of a newly created destination. This is a transactional rollback guarantee, not
cross-file atomic visibility: concurrent readers can briefly observe an intermediate combination.
Paths may not alias or contain one another by resolved path or existing file identity.

## Output Schemas

```yaml
openrouterCatalog:
  enabled: true
  excludedModels: []
  grantToAccessGroups: true
  models:
  - name: remote/openrouter/openai/gpt-4o
    upstreamModel: openai/gpt-4o
    label: GPT-4o
    group: Remote-OpenRouter-OpenAI
infraAgentgatewayWrapper:
  modelCatalog:
    sources:
    - configMap:
        name: client-model-cost-catalog
        key: catalog.json
```

```json
{
  "providers": {
    "deepseek": {
      "models": {
        "deepseek-chat": {
          "rates": {
            "input": "0.14",
            "output": "0.28"
          }
        }
      }
    },
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
