# Package Checker

Reports the most recently published GHCR container version for neurwerk packages and whether
the corresponding source repository currently has a queued or in-progress GitHub Actions run.

`published_at` is when GitHub Packages published the image version. This local tool does not
inspect a Kubernetes cluster, so it cannot determine when an image was deployed to a cluster.

## Setup

1. Copy `.env.example` to `.env`, then replace `PACKAGE_CHECKER_GITHUB_PAT=EXAMPLE` with a
   personal access token that has read access to GitHub Packages and Actions for the `neurwerk`
   organization.
2. Install dependencies:

```bash
uv sync --dev
```

## Usage

```bash
uv run package-checker --inventory /path/to/client/config/package-inventory.json
uv run package-checker --inventory /path/to/client/config/package-inventory.json --json
```

The default table includes the package, newest tag, the time that version was published, its
digest, and the active build status. `not building` means GitHub reported neither a queued nor an
in-progress workflow run for the mapped repository. `unknown` means the Actions check failed while
the package lookup itself succeeded.

## Client inventory

The required JSON inventory selects packages and their build repositories. No packages are selected implicitly. For example:

```json
[
  {"package_name": "k8s-stack-pii-engine", "repository": "neurwerk/k8s_stack_pii_engine", "stable_tags_only": true}
]
```

`stable_tags_only` defaults to false. When true, only full plain stable `X.Y.Z` tags are eligible; missing stable versions remain errors rather than falling back to suffixed tags, `latest`, or untagged versions. The inventory is limited to 1 MiB and 1000 unique packages.

The inventory tracks published source packages, not deployments. Failed lookups remain errors and the command exits nonzero. Package availability does not prove provenance or approval to install an image.

Packages use `k8s-stack-*` and source repositories use `neurwerk/k8s_stack_*`.

## Quality gates

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
```
