# OpenBao Stack Setup

`stack-setup` is the trusted-workstation operator interface for OpenBao initialization,
versioned reconciliation, status, recovery-kit verification, and supported provider credential
updates. It is not a Kubernetes Job and does not store secrets in command arguments or project
files.

## Setup

### Staged ContextForge Catalog (Source Only)

The optional selector is `contextforge.enabled: true` with an explicit
`contextforge.platformAdminEmail` in `contextforge/contextforge-product-values`,
key `values.yaml`. Missing or disabled selection does nothing; malformed selection
fails before confirmation or OpenBao access. Stage the namespace and secret-sync
resources before an authorized bootstrap or two-custodian reconciliation. This
source change does not publish a package or enable ContextForge for any client.

Reconciliation generates only missing fields in `contextforge/internal` and
preserves existing values, with conflicting email or PostgreSQL copies rejected:

| Source field | `contextforge-runtime` environment key |
| --- | --- |
| `postgresqlPassword` | `DATABASE_URL` (ESO constructs the URL below) |
| `jwtSecretKey` | `JWT_SECRET_KEY` |
| `authEncryptionSecret` | `AUTH_ENCRYPTION_SECRET` |
| `platformAdminEmail` | `PLATFORM_ADMIN_EMAIL` |
| `platformAdminPassword` | `PLATFORM_ADMIN_PASSWORD` |
| `defaultUserPassword` | `DEFAULT_USER_PASSWORD` |

The independent secrets use 32 random bytes encoded as unpadded base64url.
The database URL is `postgresql+psycopg://contextforge:<password>@postgres-operations.infra-postgres-operations.svc.cluster.local:5432/contextforge`;
only the Secret may contain the expanded value. The exact password copy is
`infra-postgres-operations/internal:contextforgePassword`. Base must deliver it
through `contextforge-postgres-values` and provision the `contextforge` role and
database. After root revocation, the existing catalog convergence waits for
`contextforge-openbao-secret-store` and refreshes those two ExternalSecrets;
it does not start or wait for the ContextForge application.

The ESO `contextforge` policy reads only `contextforge/internal` and the fixed
operator app record `contextforge/provider-apps`, never native personal OAuth
records. The separate `contextforge-oauth` policy permits read/create/update/
delete under `secret/data/contextforge/oauth/*` and hard deletion under the matching
metadata prefix, matching upstream v1.0.11 source
[`077071b`](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/services/token_backends/vault_backend.py).
Runtime config must use `OAUTH_TOKEN_BACKEND=vault`, HTTPS `VAULT_ADDR`,
`VAULT_KV_MOUNT=secret`, `VAULT_KV_PATH_PREFIX=contextforge/oauth`,
`VAULT_TLS_VERIFY=true`, `VAULT_TOKEN_CACHE_ENABLED=false`, and
`VAULT_TOKEN_CACHE_MAX_SIZE=0`, with the OpenBao CA trusted by the application.
The native paths use team-or-shared, the first 16 hex characters of SHA-256 of the
MCP URL, and URL-encoded email; the shared path segment is not permission to fall
back to another user's connection. Do not enable the token-exchange grant.

#### Operator OAuth app secrets (0.2.26 source)

After the operator's authorized two-custodian reconciliation installs the selected
catalog policies, routine app credentials use the existing short-lived
`secret-operator` login, not recovery/root access:

```bash
uv run --frozen stack-setup secret set-contextforge-oauth <integration-id> \
  --context <context> --client <client>
```

The command verifies client/context identity and ContextForge selection, then reads
the fixed `infra-agentgateway/infra-agentgateway-mcp-catalog:registrations.json`.
Only approved `individual-authentication` IDs with
`client_secret_ref: {name: contextforge-oauth-apps, key: <integration-id>}` may be
updated. IDs are integration IDs, not a provider-name allowlist. Input is a hidden
operator app **client secret**, never a PAT, personal connection or raw file/argument.
There is no arbitrary record path. CAS preserves all sibling fields in
`contextforge/provider-apps`; the first write may create that exact record.
Base must supply the `contextforge/contextforge-oauth-apps` ExternalSecret and
same-named Secret, using reviewed exact ID-field selections from the single record.
After writing, the command requests ESO refresh and waits for Secret readiness;
refresh failure does not roll back an already successful OpenBao write.
Future approved providers need only client configuration and this command.

The compiled ESO policy can read this exact record; the compiled operator policy
can create/read/update it only when ContextForge is selected. The native token
policy cannot read it and retains only `contextforge/oauth/*`. No wildcard provider
namespace, personal-token read/export, new broker, root shortcut or native patch is
introduced. Kubernetes/operator access must keep the app Secret private.
The CLI does not deliver credentials to ContextForge itself: the separately pinned
`contextforge-setup` 0.1.1 resolves the approved Secret field through explicit private
environment custody or hidden input and sends supported native app registration.
See [native app preparation and consent](../contextforge_setup/README.md#operator-app-preparation-and-native-consent).
An OpenBao/ESO update alone does not rotate an existing native PostgreSQL app copy.

The authorized temporary per-email database token backend is separate from this app
record. Native owns personal PKCE/token refresh; the app-secret command does not
require a runtime Vault token or change its backend. The existing full catalog
reconciliation still prepares/verifies the selected foundation's Vault token; no
database-mode skip or new token-lifetime gate is added here. Actual encryption/restart
checks and the later Vault token-scope fix remain supervisor-owned rollout steps.
Both CLIs are independent workstation packages, not commands inside root Tooling
image 0.7.4. Pin a reviewed source commit/install their verified standalone artifacts;
no root image bump or publication is performed by this source patch.

Selected bootstrap and reconciliation now issue a missing `vaultToken` only in
`contextforge/internal`; ESO must deliver it as `contextforge-runtime:VAULT_TOKEN`.
The token is an orphan service token with only `contextforge-oauth`, no default
or identity policies, no renewal, unlimited uses, and a fixed 30-day TTL and
explicit maximum TTL (2,592,000 seconds). Its metadata binds it to the selected
client, cluster and OpenBao namespace UID. Server constraints that shorten the
initial lifetime are rejected, with cleanup of the new token; fix the server's
supported TTL configuration through separately authorized operations first.
The orphan survives the normal revocation of the temporary recovery root.
Plaintext is never printed, put in the local custody kit, or accepted as an argument.

Normal runs verify the persisted token's exact policy, identity binding, type,
renewability and lifetime through the privileged token lookup API, and reuse it
without rotation or renewal. They report only remaining lifetime, warn at seven
days or less, and reject expired, missing-from-OpenBao or unverifiable tokens with
rotation instructions. `status` still reports selection, not token health; disabling
selection retains credentials and does not revoke previously issued access.
Reserve a maintenance window and rotate at least seven days before expiry:

```bash
uv run --frozen stack-setup reconcile --context <context> --client <client> \
  --custodian-package <first-secure-package> \
  --custodian-package <second-secure-package> --rotate-contextforge-token
```

This is the existing full-catalog two-custodian ceremony, not a routine provider
update. Before starting it, separately suspend the ContextForge application stage
and `HelmRelease/contextforge`, scale every Deployment in its namespace to zero,
and wait for every active Pod (including migration Jobs) to disappear. The CLI
checks the suspended HelmRelease, zero desired Deployment replicas and absence of
active Pods before custody access and again immediately before token preparation;
it never stops or restarts workloads itself. Operators must keep the stop stable
through the operation, including against other controllers and manual changes.
Rotation revokes only the old stored token before issuing and CAS-persisting its
replacement, then revokes recovery root access before the existing ESO refresh.
Already absent/expired tokens can be explicitly rotated, but live unrelated or
overprivileged tokens require authorized repair rather than automatic revocation.
Keep the application stopped on failure, rerun the explicit rotation after fixing
the cause, and separately resume and verify health only after Secret readiness.
Failed validation or CAS persistence revokes the new token, including an uncertain
write outcome; a lost issuance response needs authorized accessor inspection before
retrying, since the token may have been created without being stored. Never use a
root, ESO, default-policy or indefinitely lived token as a shortcut.

**Activation remains blocked** pending publication of a reviewed tool revision and
Base qualification of Secret delivery, TLS trust and maintenance support. The
current foundation does not yet inject `VAULT_TOKEN` or mount OpenBao trust.
Native OAuth application client-registration configuration copies remain in
PostgreSQL; saved personal access/refresh credentials must use the native Vault
backend. Individual-authentication registrations and Connect must remain disabled:
v1.0.11 selects different team scopes for native proxy calls and proxy OAuth flows.
No broker, upstream patch, empty-team bypass, static fallback, plugins or passthrough
authentication are introduced. Account and no-authentication/shared-authentication
registration reconciliation remain separate source-only work.

### Dify Agent Credentials

The Dify catalog is selected by `dify.enabled: true` in the
`auth-keycloak/client-values` ConfigMap. An enabled selection also requires a
nonblank `dify.hostname`; missing selection defaults to disabled, while invalid
values stop the CLI before credentials are accessed. Base-only bootstrap creates
no Dify credentials, Keycloak Dify client fields, bridge Dify verifiers, or
PostgreSQL Dify password, and does not refresh Dify consumers. The generic
bridge still needs its own non-Dify credential record. When selected, the raw
Dify AgentGateway key stays in `frontend-dify/internal`; its primary SHA-256
verifier and optional secondary rotation verifier stay in the namespace-isolated
`auth-keycloak-api-key-bridge/internal` record. The Dify add-on owns the
`dify-managed-key-verifiers` ExternalSecret that copies only these two fields
into the bridge namespace. Stage its namespace and secret-sync resources before
authorized two-custodian reconciliation; the selected run refreshes this
ExternalSecret along with Dify's runtime secrets. Existing verifier slots are
preserved, not rotated or removed. Disabling Dify retains all existing credentials
without reading or refreshing its consumers. Kubernetes identities remain `frontend-dify`.

Package `0.2.23` adds the Agent API token, Agent Stub encryption key, and local
sandbox authentication token to `frontend-dify/internal`. `reconcile` generates
only missing fields and retains the existing Dify, database, and Keycloak
credentials. Reconcile with the reviewed package before selecting the platform
release that enables Dify Agent; never re-bootstrap an existing installation.

### Image Inspection Credential

The `image-inspection` managed credential writes only
`monitor-agentgateway-extproc/external:apiKey` and refreshes the corresponding
ExternalSecret. It does not enable the image reader or reuse the Docling inference
credential. Existing installations must complete the authorized catalog
reconciliation before setting it.

### OpenSearch Reporting Credentials

Package `0.2.21` additively provisions `queryDatasourceEncryptionKey`,
`provisionerPassword`, and `reportReaderPassword` in `monitor-opensearch/internal`.
The query data-source encryption key is exactly 32 characters. Existing values are
preserved on reconciliation.

SMTP bootstrap, reconciliation, and rotation also copy the managed credential pair
to `monitor-opensearch/external`. Rotation refreshes the existing
`monitor-opensearch-secret` ExternalSecret and relies on the workload's normal Secret
reload behavior; it does not force the OpenSearch HelmRelease. Conflicting SMTP copies
stop reconciliation without replacing data.

Package `0.2.20` tidies stale token accessors when OpenBao returns HTTP 403 while
reconciliation verifies and revokes old root tokens. Cleanup is bounded and remains
fail-closed if every listed accessor cannot be inspected after maintenance.

### Optional WireGuard Server Key

Package `0.2.13` adds a selection-gated WireGuard catalog at schema `4`, reusing
`bootstrap` and `reconcile`; it adds no command, provider or device-enrollment service.
The selector is `wireguard.enabled` in `wireguard/wireguard-product-values`, key
`values.yaml`, with `serverKeySecret: wireguard-server-key`. An absent ConfigMap or
disabled selector adds no WireGuard role, record or consumer refresh; malformed
values and errors other than HTTP 404 fail closed. There is no second selector in
shared client values or an inline HelmRelease override.

After separately authorized staging of Base's optional WireGuard namespace,
`releases/wireguard/secret-sync/` and product ConfigMap, keep the gateway unselected
or fully stopped with `replicas: 0`. Run the ordinary context/client-confirmed
`stack-setup reconcile` with two custodian packages (or `bootstrap` on a genuinely
new installation). This operates the existing full catalog, not just WireGuard;
its established infrastructure reconciliation side effects still apply.

The tool creates only missing `wireguard/internal:privateKey`, as raw base64 X25519,
using compare-and-set persistence; it never displays the key, rotates an existing
key, enrolls a peer or starts the gateway. Invalid existing keys stop reconciliation
without replacement. The `wireguard` role is bound to `wireguard-external-secrets`
in namespace `wireguard` with audience `openbao` and namespace-only read access.
After root revocation it waits for `wireguard-openbao-secret-store` and refreshes
`wireguard-server-key`; missing selected consumers fail visibly and can be retried.
The existing secret-operator provider policy does not gain key access.

ESO delivers only `privateKey` to the one namespace-local `wireguard-server-key`
Secret. Disabled selection preserves previously provisioned keys and roles; it is
not access revocation. Stop the gateway and remove its peer explicitly. Follow the
current-list-or-empty recovery procedure, and never restore historical peers with
the server key. Device private keys are generated manually in the Mac WireGuard
app and stay there; approval uses only the device public key. After authorized
gateway startup, `wg show wg0 public-key` inside its container reports only the
server public key for Mac configuration; never dump its full configuration.

Install `uv`, `kubectl`, and `gpg`, configure the intended Kubernetes context, then install the
locked development environment:

```bash
uv sync --dev
```

## Usage

Always pass the Kubernetes context and client explicitly. By default, custody material is stored
under `~/.local/share/neurwerk/openbao/<client>/`; `--custody-root` replaces that complete
client-specific path. Keep it outside the workspace and Git repositories. The CLI resolves the
prospective path and refuses any custody root below a Git worktree marker before creating custody
directories. It also protects a multi-repository workspace root when the current Git worktree's
parent contains at least two direct child Git worktrees. When running outside that checkout, set
`OPENBAO_STACK_SETUP_WORKSPACE_ROOT` to the absolute workspace path to enforce the same boundary.
An invalid or missing configured path fails closed. Repository ignores provide defense in depth for
the exact seal checkpoint, recovery share, private-key, and custodian-package artifacts, but ignored
paths inside a repository or recognized workspace remain prohibited. Mutating and privileged
commands require exact client-name confirmation. `reconcile` accepts `--confirm <client>`
instead of the interactive prompt. The value must exactly match `--client`; the cluster identity,
two-custodian package checks, and root-token revocation still apply. Managed credential
values use hidden prompts.

Each client must declare its fixed K3s API endpoint in the namespace-local
`openbao-product-values` ConfigMap. `preflight` and `bootstrap` require that endpoint to match a
Ready Node InternalIP and the ready `default/kubernetes` EndpointSlice exactly. The OpenBao chart
also blocks server startup until its restricted network path can reach the Kubernetes Service, so
bootstrap cannot submit the one-time initialization request through a broken API egress policy.
Before any custody mutation, both commands also require the External Secrets, Rook/Ceph, and
trust-manager HelmReleases and the OpenBao server Certificate to report Ready. They validate the
Keycloak and monitoring product values needed to determine whether SMTP credentials are required.
They also inspect `authKeycloak.activeDirectory.enabled` to determine whether a fresh bootstrap
must collect Active Directory bind credentials.
The OpenBao HelmRelease must exist but may remain pending until bootstrap creates its static-seal
Secret.
Operator API calls use a TLS-verified port-forward directly to the singleton OpenBao Pod's
loopback-only recovery listener. That listener is absent from every Service and NetworkPolicy and
enables the recovery-share root ceremony only for Kubernetes-authorized pod port-forward users.

```bash
uv run stack-setup preflight --context <context> --client <client>
uv run stack-setup bootstrap --context <context> --client <client>
uv run stack-setup reconcile --context <context> --client <client> \
  --custodian-package /secure/custodian-1.zip \
  --custodian-package /secure/custodian-2.zip
# For noninteractive reconciliation, add --confirm <client> to the command above.
uv run stack-setup status --context <context> --client <client>
uv run stack-setup recovery verify --context <context> --client <client> \
  --custodian-package /secure/custodian-1.zip \
  --custodian-package /secure/custodian-2.zip
uv run stack-setup secret set <provider> --context <context> --client <client>
```

Supported managed credentials are `openrouter`, `deepseek`, `brave`, `route53`, `smtp`,
`active-directory`, `librechat-stt`, `librechat-tts`, `docling-inference`, and
`image-inspection`.
The bootstrap command requires nonblank SMTP credentials when the client
Keycloak values enable SMTP or monitoring email alerting is not explicitly disabled.
The same SMTP credential is stored in the Keycloak and monitoring namespace paths. Credential
update commands refresh the corresponding ExternalSecrets and reconcile the affected HelmReleases
without printing the value.

When `authKeycloak.activeDirectory.enabled` is `true`, fresh bootstrap also requires
`activeDirectoryBindDn` and `activeDirectoryBindCredential`. It stores the exact pair only in
`auth-keycloak/external`, preserving the SMTP sibling fields. Disabled clients are not prompted
and no AD credential fields are created. Rotate the pair later with:

```bash
uv run stack-setup secret set active-directory --context <context> --client <client>
```

The rotation contract requires the `auth-keycloak-active-directory-secret` ExternalSecret and
`keycloak-active-directory` HelmRelease when federation is enabled. Missing or failed resources
remain fatal. The command rejects Active Directory rotation before prompting or opening OpenBao
when federation is disabled.

The operator-owned canonical SMTP record is `stack-setup/providers/smtp`. Runtime roles cannot
read it. Bootstrap and routine rotation copy its exact fields into the approved Keycloak and
monitoring namespace records; versioned reconciliation creates it from the legacy Keycloak copy
when upgrading an existing installation.
Reconciliation accepts only the complete AD pair as siblings in the Keycloak SMTP destination;
partial AD credentials and unknown fields fail closed.

Fresh bootstrap and post-bootstrap reconciliation use the same compiled catalog. The catalog owns
the approved namespace roles, exact provider paths, namespace-local secret consumers, and runtime
convergence targets. Reconciliation persists a compare-and-set schema record at
`stack-setup/reconciliation-state`, bound to the client, cluster identity, and OpenBao namespace
UID. It rejects unknown or conflicting records instead of accepting paths or policy rules from
command-line input.

Reconciliation schema version 3 adds the `infra-postgres-auth` and
`infra-postgres-operations` namespace roles and records. Each database record receives an
independently generated administrator password. Its application-user passwords are exact,
fail-closed copies of the canonical application fields.

Schema version 4 adds `infra-agentgateway/internal:postgresqlPassword` and copies it exactly to
`infra-postgres-operations/internal:agentgatewayPassword`. Existing matching copies are preserved,
while a conflicting operations copy prevents schema advancement and remains safe to retry after
correction. Fresh Studio records no longer receive Langfuse project credentials; existing Studio
Langfuse fields remain untouched during this additive transition, and the canonical project
credentials remain in `monitor-langfuse/internal`. Starting in the planned
`0.2.24` catalog, reconciliation copies the canonical project's keys into
new `frontend-studio/internal:llmLogsPublicKey` and `llmLogsSecretKey` fields,
checking for exact matches before accepting existing copies. The legacy Studio
Langfuse fields remain untouched. Base syncs these new fields into the Studio
API Pod only when the separate LLM activity feature is enabled.

For the API-key bridge PostgreSQL transition, bootstrap and reconciliation add
`auth-keycloak-api-key-bridge/internal:postgresqlPassword` and copy it to
`infra-postgres-operations/internal:apiKeyBridgePassword`. The bridge-owned password is
generated only when missing; existing values are preserved. A conflicting operations copy
fails reconciliation without overwriting either value, and a corrected run can be retried.
This prepares credentials only; database provisioning and bridge runtime adoption are separate.

`reconcile` requires exactly two distinct, cluster-bound custodian packages and a completed local
recovery kit. It creates a temporary recovery root, applies only cataloged additive changes,
verifies the restricted secret operator, and revokes the root before refreshing any Kubernetes
consumer. Rerunning safely converges a partial additive migration or a downstream Kubernetes
failure. Routine `secret set` operations continue to use the short-lived exact-path operator role.

On first bootstrap, newly generated Keycloak, Dify, Langfuse, and Grafana administrator
passwords are displayed only through the controlling terminal. Save them before entering
the required acknowledgement. An incomplete seal kit can temporarily contain these pending
passwords, so retain it outside the workspace with the same privileged custody as the static
seal material.

Post-seed convergence takes approximately two minutes. Bootstrap first wakes any stale
bootstrap-owned SecretStore and waits for all 15 stores to report Ready. It then waits until all
19 bootstrap-owned ExternalSecrets report a new Ready refresh and their target Secret metadata
exists, printing per-resource progress throughout. Enabled Active Directory adds its dedicated
ExternalSecret to that refresh set. Bootstrap then force-reconciles and waits for
`cert-manager-issuers`, `postgres-auth`, `postgres-operations`, `kube-prometheus-stack`,
`opensearch`, and `pii-engine`; enabled Active Directory also adds
`keycloak-active-directory`. Finally, it reconciles and waits for the `infrastructure` Flux
Kustomization so the application stage is unblocked immediately. The CLI does not read or print
the materialized Secret values during these checks.

## Optional LibreChat Speech Credentials

Package `0.2.14` adds managed-only speech credentials without bootstrap prompts or
generated records; reconciliation remains at schema `4`.

```bash
uv run stack-setup secret set librechat-stt --context <context> --client <client>
uv run stack-setup secret set librechat-tts --context <context> --client <client>
```

The canonical selector is `frontend-librechat/librechat-product-values`, key
`values.yaml`: both `frontendLibrechat.speech.<stt|tts>.enabled` and its
`auth.enabled` must be boolean `true` for the selected direction. Missing or
disabled selection rejects the command before confirmation, credential prompts,
or opening OpenBao; malformed values and non-404 read failures fail closed.

Both providers share `frontend-librechat/external`: STT updates only `sttApiKey`
and TTS only `ttsApiKey`, preserving sibling fields with compare-and-set writes.
The command refreshes only the selected `frontend-librechat-<stt|tts>-secret`
ExternalSecret in `frontend-librechat` and waits for its refresh and matching target
Secret metadata, without reconciling or waiting for the shared `librechat`
HelmRelease. Reloader owns the workload rollout, so either key can be provisioned
first when both directions require authentication. Check final application
readiness after both keys are provisioned as part of the authorized deployment
checks. Missing selected consumers fail visibly after the durable credential update.

Existing installations need an authorized full `reconcile` ceremony to install
the updated operator ACL: only this exact new external record gains `create`
alongside `read` and `update`; existing record permissions stay unchanged.
Activation waits for compatible Base chart support and separately authorized
adoption; installing this tool does not activate speech or create its consumers.
Disabling selectors does not delete stored credentials or revoke operator access.

## Optional Docling Credentials

Docling supports `internal-standard` and `private-vlm` modes at unchanged schema `4`.
Selection comes only from
`docling/docling-product-values`, key `values.yaml`: `docling.enabled` must be
boolean `true`, with `apiKeySecretRef: {name: docling-api, key: api-key}` under `docling`.
Set `docling.inference.mode: internal-standard` for in-Pod CPU extraction, which needs no inference token.
Set `docling.inference.mode: private-vlm` for the private vision-model endpoint, which requires
`inference.tokenSecretRef: {name: docling-inference, key: token}` under `docling`.
The legacy aliases `cpu` and `remote` remain supported during transition and normalize
to `internal-standard` and `private-vlm`, respectively. Omitted mode still selects
private inference (`private-vlm`), preserving the previous default.
Missing/disabled selection adds no internal records, roles or consumer refreshes;
malformed values and non-404 read errors stop before confirmation or secret access.
Disabling selection or switching modes retains existing credentials and roles without key rotation.

Stage compatible namespace and secret-sync resources first: use
`releases/docling/secret-sync/internal` for `internal-standard` (two stores and two API-key consumers),
or the unchanged `releases/docling/secret-sync` for `private-vlm` (three consumers).
Ordinary `bootstrap` or authorized full `reconcile` generates only a missing `docling/internal:apiKey`
(32 random bytes, base64url) and copies it exactly to
`monitor-agentgateway-extproc/internal:doclingApiKey`. Retries preserve values and
siblings; conflicting copies fail without rotation. Selected namespace roles use
`<namespace>-external-secrets` ServiceAccounts and namespace-only read policies.
After root revocation, the tool waits for both `<namespace>-openbao-secret-store`
stores and refreshes `docling/docling-api` and
`monitor-agentgateway-extproc/monitor-agentgateway-extproc-docling-secret`, each
with the same target Secret name. Normal infrastructure convergence still applies;
no Docling application release or inference token is awaited.

For `private-vlm` mode only (including alias `remote`), supply the external inference token through a hidden prompt:

```bash
uv run stack-setup secret set docling-inference --context <context> --client <client>
```

The command rejects `internal-standard` (including alias `cpu`) or disabled selection before confirmation, prompts or OpenBao access,
rejects CR/LF in `inferenceToken`, and
CAS-updates only `docling/external:inferenceToken`, preserving siblings. The updated
secret-operator policy allows creation of this exact external record, never either
internal record; existing installations need the authorized reconciliation first.
Only `docling/docling-inference` is refreshed, checking readiness and target Secret
metadata without forcing or waiting for application HelmReleases. Bootstrap never
prompts for this token. Reloader owns rollout; check application health after
provisioning during the separately authorized deployment.

## Optional Forgejo Catalog

Package `0.2.12` adds Forgejo as an opt-in additive catalog at reconciliation schema `4`.
The state record format stays at `schemaVersion: 1`; recovery-kit schema stays at `4`.
Existing schema-4 installations are reconciled on every run, so enabling Forgejo does not
require a global migration or invalidate the existing AgentGateway schema-4 prerequisite.
An unchanged reconciliation state retains its original `packageVersion`; that field is not
proof of optional-feature onboarding. The platform must pin the actual reviewed immutable
tooling commit after merge, not a placeholder or moving branch.

`preflight`, `bootstrap`, `reconcile`, and `status` read `forgejo.enabled` from
`auth-keycloak/client-values` (`data["values.yaml"]`). The selector defaults to `false`
when omitted and must be a boolean. Enabled clients also need a nonblank `forgejo.hostname`
in that same non-secret ConfigMap. No Forgejo namespace or workload must be Ready to detect
the selection. `status` reports selection, not credential or workload health.

Forgejo is a first-party optional package with mandatory Keycloak authentication,
`postgres-operations`, and cert-manager, not a selectable alternative authentication or
database stack. Base owns those dependencies and application readiness.

Only selected clients receive the `forgejo` policy and Kubernetes role, bound to
`forgejo/forgejo-external-secrets` with audience `openbao`. The policy reads only the
`secret/data/forgejo/*` and corresponding namespace metadata paths. Routine secret-operator
permissions and the supported `secret set` providers do not expand.

The generated `forgejo/internal` record contains exactly these initial fields:

| Field | Generation or purpose |
| --- | --- |
| `dbPassword` | 32 random bytes encoded as unpadded base64url |
| `oidcClientSecret` | Independently generated 32-byte base64url secret |
| `secretKey` | 32 random bytes encoded as 64 hexadecimal characters |
| `internalToken` | HS256 JWT with an `nbf` claim and an independent random signing key |
| `oauth2JwtSecret` | Unpadded base64url encoding of exactly 32 random bytes; HS256 |
| `lfsJwtSecret` | Independently generated unpadded base64url encoding of 32 random bytes |
| `adminPassword` | Independently generated 32-byte base64url recovery password |

JWT formats follow upstream [Forgejo secret generation](https://codeberg.org/forgejo/forgejo/src/branch/forgejo/modules/generate/generate.go)
and [Gitea secret generation](https://github.com/go-gitea/gitea/blob/main/modules/generate/generate.go).
All generated fields persist through the existing compare-and-set, preserve-on-retry flow.
Existing fields are never rotated or deleted by toggling this selector.

The exact namespace-isolated copies are:

| Canonical source | Destination |
| --- | --- |
| `forgejo/internal:dbPassword` | `infra-postgres-operations/internal:forgejoPassword` |
| `forgejo/internal:oidcClientSecret` | `auth-keycloak/internal:forgejoClientSecret` |

Conflicting copies fail closed; retry does not regenerate the canonical credentials.
No application key or password is requested from the operator. The chart fixes the recovery
username to `forgejo-recovery`; there is no `adminUsername` secret field. Unlike the original
bootstrap administrator passwords, `adminPassword` is not added to terminal delivery or the
local custody checkpoint. Its durable custody is the protected OpenBao record, delivered only
to the namespace-local runtime Secret. Human break-glass access requires a separately approved
secure recovery procedure under the existing recovery-custody controls; these commands do not
print or export the password, and do not log Secret contents.

The selected Base release or Knowledge Center add-on must provide these optional
resources before runtime convergence can complete:

| Namespace | SecretStore | ExternalSecret / target Secret |
| --- | --- | --- |
| `forgejo` | `forgejo-openbao-secret-store` | `forgejo-runtime` / `forgejo-runtime` |
| `infra-postgres-operations` | Existing `infra-postgres-operations-openbao-secret-store` | Base: `forgejo-postgres-values` / `forgejo-postgres-values`; add-on: `addon-postgres-values` / `addon-postgres-values` |
| `auth-keycloak` | Existing `auth-keycloak-openbao-secret-store` | `forgejo-oidc-values` / `forgejo-oidc-values` |

`forgejo-runtime` delivers all seven source fields with unchanged key names. The database
and OIDC ExternalSecrets consume only their namespace-owned copies. The database Secret
renders `values.yaml`; the OIDC Secret delivers `oidcClientSecret` directly. Package
`0.2.25` refreshes the add-on PostgreSQL ExternalSecret when present, falling back to
the legacy Base name. It fails if neither exists and never reads target Secret data.
Do not create shorter replacement names for the stores.

For staged onboarding, first make the non-secret selector and optional secret-sync resources
available. They may be unready until reconciliation generates credentials and authorizes the
role; preflight does not require their readiness. Run the approved `reconcile` ceremony before
expecting the optional application to start. After revoking root, the CLI waits for the
additional store and refreshes all three ExternalSecrets before forcing the existing
`postgres-operations` release and reconciling the infrastructure stage. Base's Flux dependencies
must order Keycloak OIDC provisioning and application startup; the CLI does not force an
application release before its stage is available.

If resources have not arrived yet, runtime convergence fails visibly after durable catalog
writes. Apply the missing composition and rerun reconciliation; a schema-4 marker alone does
not mean consumers converged. Disabled or omitted selection adds no records, copies, roles,
or runtime targets, and does not revoke previously provisioned credentials or authorization.

## Recovery Custody

New bootstrap prompts for three distinct custodian names. In separate temporary GnuPG homes,
`stack-setup` generates three passwordless RSA-4096 key pairs with encryption subkeys. OpenBao
creates one encrypted recovery share for each key and requires any two shares to generate a
temporary root token. The CLI then atomically publishes three `0600` packages under
`custodian-packages/`. Each ZIP contains one private and public key, its encrypted share,
cluster-binding metadata, and a short recovery README. Package filenames are numbered; the
recorded name inside each package identifies its intended custodian.

After creating the static-seal Secret, bootstrap waits for the OpenBao Service to publish a ready
HTTPS endpoint before starting its local port-forward. The PGP keys sent to OpenBao are base64 of
their binary OpenPGP exports; the packages retain readable ASCII-armored key files. Initialization
sends only recovery-share parameters because the static seal is an auto-unseal type; manual
barrier-share parameters are invalid for that seal. The CLI persists OpenBao's
`recovery_keys_base64` response values before decoding them into the package files.

The ZIP and private key have no password. Possession of one package grants control of one recovery
share. Copy each package to separate encrypted removable media, hand it to the named custodian,
and remove workstation copies only after two-package verification and recorded handover.
`recovery verify` imports each supplied package key into a separate temporary GnuPG home and
decrypts the share only in memory before contributing it over the TLS-verified OpenBao connection.
It does not print or write plaintext shares.

The static seal is separate from recovery shares. It supports normal automatic restarts and does
not generate root tokens. Until all packages are durable, the seal file temporarily checkpoints
generated private keys and the one-time OpenBao initialization response so package publication can
resume safely. Each package is decrypted successfully before that material is removed and the
checkpoint advances. Automatic package creation is provisional single-operator custody, not human
dual control, until separate people control at least two packages.

There is one unavoidable boundary: OpenBao can commit initialization before the workstation
receives and persists its one-time response. A crash or transport loss in that interval leaves an
initialized cluster without recoverable local shares. The CLI stops with an escalation error; do
not improvise recovery. The cluster must be rebuilt or reinitialized through an explicitly
authorized procedure. See `docs/dev/operations/recovery-custody.md` for the full custody procedure.

See `docs/dev/operations/openbao.md` in the workspace for the supported operational
procedures and safety requirements.

## Quality Gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
uv build
```
