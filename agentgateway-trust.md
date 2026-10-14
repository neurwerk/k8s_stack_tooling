# Inspection trust publication

`agentgateway-trust-publish` runs as a bounded Kubernetes Job in the existing
Tooling image. It reads public ConfigMaps only; it never reads a signing Secret.
Base owns issuance, inventory, RBAC and deployment order. Studio owns downloads.

## Inputs

Environment variables use the prefix `AGENTGATEWAY_TRUST_`:

| Suffix | Default |
| --- | --- |
| `SOURCE_NAMESPACE` | `infra-cert-manager` |
| `TARGET_NAMESPACE` | `frontend-studio` |
| `INVENTORY_CONFIG_MAP` | `agentgateway-inspection-trust-inventory-config-map` |
| `TARGET_CONFIG_MAP` | `agentgateway-inspection-trust-config-map` |
| `DEPLOYMENT` | `frontend-studio-api-deployment` |
| `SERVICE` | `frontend-studio-api-service` |
| `POD_PORT` | `4010` |

The inventory ConfigMap is in the target namespace. Its `inventory.json` has
`schema_version: 1` and ordered `roots`. Every root has `config_map`, `key`
(default `ca.crt`), reviewed lowercase DER `fingerprint_sha256`, and
`state: active|transitional`. Sources must contain exactly one self-signed signing
CA. A changed source fingerprint requires explicit inventory review.

The output `snapshots.json` contains `schema_version: 1`, `current_version` and
ordered `versions`. Each version has `version`, `published_at`, `bundle_sha256`
and ordered `certificates`; each certificate has its DER `fingerprint_sha256`,
canonical public `pem`, `purpose: inspection-root` and transition `state`.
Version and bundle digest are the SHA-256 of the concatenated canonical PEM.
States can change without changing immutable bundle bytes or their version.

## Operations

1. Before Studio advertises inspection trust, run `stage --initialize`. This
   creates a complete initial publication with a valid current version. It never
   creates an empty current pointer. Change the chart operation to `stage` after
   initialization; normal operations cannot silently recreate a lost archive.
2. To add or retire current trust, review the ordered inventory, then run `stage`.
   It retains the existing current version and every historical snapshot.
3. Read `required_version` from the command's small JSON result. Configure the
   Studio Deployment with that literal `STUDIO_GATEWAY_TRUST_REQUIRED_VERSION`.
   Each replica must refuse trust requests and readiness until that version is
   present in its validated projected document. Mount the directory without
   `subPath`. The floor is the newest *added* version, even when choosing a
   previously retained bundle as current.
4. After Studio converges, run `promote`. The command verifies the Deployment,
   Pod ownership/readiness/floor, and every serving EndpointSlice target; draining
   endpoints block promotion. It compares all retained bundle and certificate
   bytes on every replica and checks membership again before advancing current.

Output includes `version`, `current_version`, `required_version`,
`retained_versions` and `changed`. It contains no certificate material or request
diagnostics. ConfigMap resourceVersion conflicts trigger a fresh read and repeat
validation, including replica verification for promotion.

Newly selected roots must be currently valid. Expired historical roots remain
valid public download assets: archive validation checks their CA capability,
self-signature and content hashes, not current validity. Operational certificate
health is separate from historical download availability.

Current URLs use `no-store`; immutable version/fingerprint URLs use
`public, max-age=86400, immutable` in Studio. The command never prunes history,
satisfying at least 31 days after last-current, or the workstation update window
plus one day if longer. It fails before exceeding 1 MiB, 64 versions or 16 roots
per bundle. Removing current trust is explicit; archive pruning requires a separately
reviewed operation and must not be used to bypass retention.
