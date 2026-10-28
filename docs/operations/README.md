# Operations

## Select the execution environment

Run workstation commands from their own `cli_tools/<project>/` directory with `uv run --frozen <command>`. Install the selected project's dependencies as described in [development](../development/README.md), and select the source revision through [release adoption](../releases.md). Each linked reference describes the tool's arguments and additional prerequisites.

| Workstation command | Purpose and configuration owner |
| --- | --- |
| `stack-setup` | OpenBao setup, selected-definition reconciliation, recovery verification, and supported credential updates. Follow shared [OpenBao operations](https://github.com/neurwerk/docs/blob/main/dev/operations/openbao.md) for verified source selection and [recovery custody](https://github.com/neurwerk/docs/blob/main/dev/operations/recovery-custody.md) for protected packages. |
| [maintenance](../../cli_tools/maintenance/README.md) | Inspect or switch maintenance routes for an explicit Kubernetes context, using Base's approved configuration. `kubectl-maintenance` is an alias. |
| `opensearch-logs` | Search indexed Kubernetes logs as JSON. [Source](../../cli_tools/opensearch_logs/src/opensearch_logs/main.py) defines filters and context selection; shared [Tools](https://github.com/neurwerk/docs/blob/main/dev/tools.md) provides invocation examples. It uses the Studio read-only credential and CA through a temporary loopback port-forward. |
| [ldap-directory](../../cli_tools/ldap_directory/README.md) | Inspect directory users/groups using live Active Directory configuration and a temporary in-cluster worker. Selects a published worker image from Tooling release metadata. |
| [keycloak-users](../../cli_tools/keycloak_users/README.md) | Manage local accounts in a selected realm using a non-secret local profile and browser authentication; does not use cluster bootstrap credentials. |
| [package-checker](../../cli_tools/package_checker/README.md) | Inspect published packages and build workflows using its configured GitHub credential; does not report deployments. |
| [platform-release](../../cli_tools/platform_release/README.md) | Inspect, prepare, check, and publish Base releases using an explicit trusted Base repository and its release scripts. |
| [release-manager](release-manager.md) | Target-first workstation menus for Base, Tooling and setup, using the existing `cli_tools/platform_release/` environment. |
| [openrouter-catalog-sync](../../cli_tools/openrouter_catalog_sync/README.md) | Generate or check client model values and pricing from an explicit selection policy and OpenRouter's public model API. |
| [inference-runtime-manager](../../cli_tools/inference_runtime_manager/README.md) | Download model artifacts and manage inference containers through explicitly selected Docker contexts, with saved workstation configuration. |

The shell [connection test](../../cli_tools/connection_test/connection_test.sh) runs on the target server to check network access; it is not a Python entry point or an image command.

## Container commands

Use the owning chart's workload definition for the image command, environment, mounts, and service account. The complete command mapping is in [pyproject.toml](../../pyproject.toml); component responsibilities are in [architecture](../architecture/README.md). The image has no default command and does not include `stack-setup` or the other standalone CLI projects.

For configuration failures, compare the workload's supplied keys with the selected command's parser. For example, `maintenance-server` reads `MAINTENANCE_COMPANY_NAME`, `MAINTENANCE_LOGO_PATH`, and `MAINTENANCE_RETRY_AFTER`; its logo path must be an absolute PNG or SVG path. Do not substitute workstation configuration files for workload settings.

## Verification

For approved machine identities, see [scoped service clients](scoped-service-clients.md); the legacy OIDC upsert command does not reconcile exact service-account grants.

Use the tool's operation-specific result: maintenance checks routing through HTTPS, directory inspection returns names, and Package Checker reports publication state. `stack-setup status` reports initialization, custody checkpoint, and selections; it does not prove policy, token, or workload health. Follow the shared [post-deployment checks](https://github.com/neurwerk/docs/blob/main/dev/operations/upgrades.md#after-deployment) for application readiness after an approved deployment.

Release publication and deployment selection remain separate. Follow shared [image releases](https://github.com/neurwerk/docs/blob/main/dev/operations/image-releases.md) for registry verification and image-pin changes, and [platform upgrades](https://github.com/neurwerk/docs/blob/main/dev/operations/upgrades.md) for client adoption.

The operator runs OpenBao reconciliation and release commands from their workstation at the reviewed revision. Before using frozen execution, require the exact published shared package artifact and a genuine committed lock. A local candidate checkout or passing offline check does not supply those artifacts. [Add-on helpers](../development/addon-helpers.md) and shared [add-on conventions](https://github.com/neurwerk/docs/blob/main/dev/conventions/addons.md) describe scoped product execution, not privileged setup entry points.
