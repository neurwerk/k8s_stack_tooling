# OpenBao Stack Setup

`stack-setup` is the trusted-workstation operator interface for OpenBao initialization,
versioned reconciliation, status, recovery-kit verification, and supported provider credential
updates. It is not a Kubernetes Job and does not store secrets in command arguments or project
files.

## Setup

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
commands require typing the exact client name; managed credential values use hidden prompts.

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
uv run stack-setup status --context <context> --client <client>
uv run stack-setup recovery verify --context <context> --client <client> \
  --custodian-package /secure/custodian-1.zip \
  --custodian-package /secure/custodian-2.zip
uv run stack-setup secret set <provider> --context <context> --client <client>
```

Supported managed credentials are `openrouter`, `deepseek`, `brave`, `route53`, `smtp`, and
`active-directory`. The bootstrap command requires nonblank SMTP credentials when the client
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
fail-closed copies of the canonical Keycloak, Dify, Langfuse, and LibreChat internal fields;
LibreChat's `documentdbPassword` is the operations DocumentDB password. Existing matching copies are
preserved, while a conflicting copy prevents the schema version from advancing and remains safe to
retry after correction.

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
