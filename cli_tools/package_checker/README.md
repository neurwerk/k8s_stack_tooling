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
uv run package-checker
uv run package-checker --json
```

The default table includes the package, newest tag, the time that version was published, its
digest, and the active build status. `not building` means GitHub reported neither a queued nor an
in-progress workflow run for the mapped repository. `unknown` means the Actions check failed while
the package lookup itself succeeded.

## Repository mappings

Build state is derived from these source repositories:

| Package | Channel | Repository |
| --- | --- | --- |
| `k8s-stack-studio-api` | — | `neurwerk/k8s_stack_studio` |
| `k8s-stack-studio-web` | — | `neurwerk/k8s_stack_studio` |
| `k8s-stack-agentgateway-extproc` | — | `neurwerk/k8s_stack_agentgateway_extproc` |
| `k8s-stack-pii-engine` | `cpu` | `neurwerk/k8s_stack_pii_engine` |
| `k8s-stack-pii-engine` | `cu124` | `neurwerk/k8s_stack_pii_engine` |
| `k8s-stack-keycloak-api-key-bridge` | — | `neurwerk/k8s_stack_keycloak_api_key_bridge` |
| `addon-dify-ce-builder-api` | — | `neurwerk/dify_ce_builder` |
| `k8s-stack-tooling` | — | `neurwerk/k8s_stack_tooling` |
| `addon-dify-ce-builder-web` | — | `neurwerk/dify_ce_builder` |

The configured inventory is tested against every `ghcr.io/neurwerk/*` image in
`base/charts/**/values.yaml`. Stack images must use `k8s-stack-*`, addon images
must use `addon-*`, and source repositories must use the `k8s_stack_*` convention
or the established `dify_ce_builder` addon repository name.

## Quality gates

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
```
