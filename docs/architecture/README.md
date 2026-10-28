# Architecture

## Packaging and ownership

The root [project definition](../../pyproject.toml) maps installed commands to modules in `src/k8s_stack_tooling/`. The [Dockerfile](../../Dockerfile) installs that package and its locked runtime dependencies, runs as a non-root user, and has no entrypoint. The calling Job, init container, or Deployment selects the command. Base and add-on charts own workload configuration, service accounts, and image pins.

Each Python project under [cli_tools/](../../cli_tools/) has its own entry points, dependencies, version, and lockfile. These projects are excluded from the root source distribution and are not copied into the image. Updating the image therefore does not update workstation tools; see [release adoption](../releases.md).

## Operations image

| Source | Responsibility and inputs |
| --- | --- |
| [keycloak/](../../src/k8s_stack_tooling/keycloak/) | Reconcile realms, OIDC clients, roles, directory federation, and users; send user-action emails. Commands read environment variables supplied by the calling workload. |
| [opensearch/](../../src/k8s_stack_tooling/opensearch/) | Reconcile OpenSearch users and role mappings from workload configuration. |
| [api/](../../src/k8s_stack_tooling/api/) | Shared HTTP, Keycloak, and Kubernetes helpers, including reconciliation locks and credential-change rollout checks. |
| [maintenance/](../../src/k8s_stack_tooling/maintenance/) | Serve preloaded maintenance responses through Gunicorn. `MAINTENANCE_` settings select company name, optional logo path, and retry interval. |
| [agentgateway_trust.py](../../src/k8s_stack_tooling/agentgateway_trust.py) | Stage or promote inspection-trust snapshots using in-cluster access and `AGENTGATEWAY_TRUST_` settings. |
| [agentgateway_observe.py](../../src/k8s_stack_tooling/agentgateway_observe.py) | Inspect gateway runtime state and publish observations using in-cluster access. |
| [ldap_directory.py](../../src/k8s_stack_tooling/ldap_directory.py) | Read directory names in a temporary worker using mounted credentials and LDAPS trust. |
| [postgres/](../../src/k8s_stack_tooling/postgres/) | Stage DocumentDB provisioning scripts into `/runtime`; the separately pinned DocumentDB client image executes them. |
| [addons/](../../src/k8s_stack_tooling/addons/) | Parse manifests and client selection with one shared model; verify selected sources, compatibility, ownership, and image consumers. Acquisition is separate from offline validation. |

## Execution flows

**Application setup:** a chart supplies configuration and Secret references → a Tooling command reconciles the target service → the calling workload reports success or failure. For example, `upsert-oidc-client` reads `KC_INTERNAL_URL`, realm/client settings, and the desired confidential-client secret; it verifies that Keycloak persisted that secret. OpenBao preparation and Secret delivery are separate responsibilities described in the shared [architecture](https://github.com/neurwerk/docs/blob/main/dev/architecture/README.md) and [authentication](https://github.com/neurwerk/docs/blob/main/dev/authentication/README.md) guides.

**Maintenance:** the workstation `maintenance` CLI reads Base-approved route configuration and manages operator-owned maintenance resources. It verifies HTTPS responses for the maintenance marker before removing the backend. The image's `maintenance-server` only serves responses; it does not switch routes.

**Directory inspection:** the workstation `ldap-directory` CLI creates a temporary Pod and NetworkPolicy, runs `ldap-directory-worker`, displays directory names, then cleans up those temporary resources. The worker reads the mounted bind credential; the workstation does not retrieve its value. Read-only directory inspection still creates Kubernetes resources.

**Release inspection:** `package-checker` reads GitHub package and workflow state. `platform-release` wraps Base's release scripts and uses the explicitly selected Base checkout. Neither publication information nor a successful setup command establishes application readiness.

Add-ons own native payloads and thin product wrappers; [public helpers](../development/addon-helpers.md) own reusable Job handling and staging. Privileged setup verifies the complete selection before custody and never gives product code root authority. See [OpenBao operations](https://github.com/neurwerk/docs/blob/main/dev/operations/openbao.md#sources-and-artifacts) for source and artifact prerequisites.
