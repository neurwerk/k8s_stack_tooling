# K8s Stack Tooling

Python runtime utilities and trusted-workstation command-line tools used to
operate the neurwerk Kubernetes stack. This repository contains one container
package and seven independently locked CLI projects.

## Projects

| Project | Purpose | Execution environment |
| --- | --- | --- |
| `k8s-stack-tooling` | Keycloak/OpenSearch initialization and a maintenance page server | Kubernetes Jobs and on-demand maintenance Deployment |
| [`maintenance`](cli_tools/maintenance/) | Starts and stops maintenance pages for selected products | Authorized operator workstation |
| [`platform-release`](cli_tools/platform_release/) | Reviews, checks and stages signed Base platform releases through protected workflows | Trusted release custodian workstation |
| [`package-checker`](cli_tools/package_checker/) | Reports published GHCR versions and active GitHub Actions builds | Developer or operator workstation |
| [`local-ai-installer`](cli_tools/local_ai_installer/) | Downloads verified model artifacts and provisions a stock LocalAI Docker host | Workstation with external storage and an explicit Docker context |
| [`openbao-stack-setup`](cli_tools/openbao_stack_setup/) | Bootstraps, reconciles, verifies, and updates supported OpenBao state | Trusted operator workstation only |
| [`openrouter-catalog-sync`](cli_tools/openrouter_catalog_sync/) | Selects reviewed OpenRouter models and generates client Helm values and a complete model cost catalog | Developer or operator workstation |
| [`keycloak-users`](cli_tools/keycloak_users/) | Guides manual browser-login setup and creates local users with group membership and seven-day onboarding invitations | Authorized operator workstation |

The CLI projects under `cli_tools/` are not bundled into the Kubernetes image.
Each has its own `pyproject.toml`, `uv.lock`, environment, tests, and README.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Docker or another OCI builder for the root image
- Additional local tools documented by each CLI, such as `kubectl`, `gpg`, or
  the Hugging Face CLI

## Development

Install and validate the root package:

```bash
uv sync --frozen --dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen pytest
uv build
```

Run the same quality gates from each independent CLI directory:

```bash
cd cli_tools/<project>
uv lock --check
uv sync --frozen --dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen pytest
uv build
```

Tests are local and use fakes or mocks for external systems. Running the test
suites does not require or authorize access to a Kubernetes cluster, OpenBao,
GHCR, or Hugging Face.

## Container Image

The root `Dockerfile` creates the `k8s-stack-tooling` image. It installs the
root package from `uv.lock` with development dependencies disabled and exposes
these commands on `PATH`:

- `upsert-realm`
- `upsert-oidc-client`
- `upsert-realm-roles`
- `upsert-active-directory`
- `upsert-user`
- `send-user-actions-email`
- `upsert-composite-roles`
- `upsert-opensearch-user`
- `maintenance-server`

Build locally without publishing:

```bash
docker build -t k8s-stack-tooling:local .
```

The image runs as a non-root user and has no default entrypoint. Workloads must
select the required command explicitly. Tagged releases are built and published
to GHCR by GitHub Actions using the repository-scoped `GITHUB_TOKEN`.

### Maintenance Server

Select `maintenance-server` explicitly in the Tooling image to run the on-demand
WSGI responder on `0.0.0.0:8080`. It does not enable maintenance routing, call
Kubernetes or Keycloak, authenticate users, or contact upstream services.

| Environment variable | Default | Contract |
| --- | --- | --- |
| `MAINTENANCE_COMPANY_NAME` | `neurwerk` | HTML-escaped name, at most 512 characters |
| `MAINTENANCE_LOGO_PATH` | unset | Absolute mounted `.png` or `.svg` file |
| `MAINTENANCE_RETRY_AFTER` | `300` | Integer seconds, 0 through 2147483647 |

All ordinary paths and methods return HTML with `503`, `Retry-After`,
`Cache-Control: no-store`, and `X-Platform-Maintenance: true`. `HEAD` has no body.
`/_maintenance/healthz` returns `200` and `ok` for process health only, not
upstream readiness. Exact bundled asset paths under `/_maintenance/assets/`
return `200` for GET/HEAD; unknown paths still return the maintenance page.
All responses have no-store, a restrictive CSP, and MIME sniffing protection.

Assets and optional branding are loaded once at startup; restart to update them.
Unreadable, non-regular, oversized (over 1 MiB), invalid-signature PNG, or unsafe
SVG logos are omitted in favor of the company name. SVG supports a conservative
shape/presentation allowlist, not scripts, styles, links, embedded images,
animation, entities or external references. Relative or non-PNG/SVG configured
paths fail settings validation. Requests never select filesystem paths.

The fixed Gunicorn configuration uses two synchronous workers, a 15-second
worker timeout, 15-second graceful shutdown, a 128-connection backlog, a
4094-byte request line, at most 32 headers of at most 4096 bytes each.
Request bodies are never read, buffered or drained by the application; the
synchronous worker closes the connection after responding, including for
chunked/large uploads. The locked Gunicorn transport drains at most 64 KiB for
at most two seconds during socket closure, without application processing.
Malformed or over-limit HTTP is rejected by Gunicorn
before the page handler. Access logs are disabled; warning/error details are
suppressed so parser errors cannot log URLs, headers or credentials. No request
body, query, cookie or token is logged. Proxy access-log policy is separate.
Gunicorn config files, CLI flags and `GUNICORN_CMD_ARGS` are not loaded.

The page follows the shared Keycloak theme without a Keycloak runtime dependency:
white logo panel beside indigo content on desktop, one indigo panel with company
name at widths up to 767px. Inter 400/600 and the Neurwerk wordmark are bundled;
no client logo is a generic default. See [asset attribution](THIRD_PARTY.md).
Image versioning, publication and platform adoption are separate release steps.

### Keycloak Realm Themes

`upsert-realm` optionally accepts `KC_REALM_LOGIN_THEME` and
`KC_REALM_EMAIL_THEME`, mapped to the realm's `loginTheme` and `emailTheme`.
Omitting either variable preserves that selection on existing realms and leaves
the server default on creation. To reset explicitly, select `keycloak.v2` for
login and `keycloak` for email. Themes must be installed on the Keycloak server.
Names must match `[A-Za-z0-9][A-Za-z0-9_.-]*` without `..`; empty values,
whitespace, and path separators are rejected.

### Keycloak Action Emails

`send-user-actions-email` waits for both Keycloak's internal health endpoint and
the public realm OIDC discovery endpoint before requesting an email. The public
endpoint must use HTTPS, present a trusted certificate, and advertise the exact
issuer derived from `KC_PUBLIC_URL` and `KC_REALM`. The command then asks
Keycloak to email the user's remaining required actions. The optional
`KC_ACTION_EMAIL_LIFESPAN` setting controls how long the action link remains
valid, defaults to 30 minutes (`1800` seconds), and accepts values from 5 to 60
minutes. A Helm Job can override it when needed. The command never handles the
server-generated action token.

### Active Directory Reconciliation

`upsert-active-directory` reconciles the managed Microsoft Active Directory
user-storage provider in Keycloak. It uses the common `KC_INTERNAL_URL`,
`KC_HEALTH_PORT`, `KC_ADMIN_USER`, `KC_ADMIN_PASSWORD`, and `KC_REALM` variables
plus this provider-specific contract:

- `KC_ACTIVE_DIRECTORY_ENABLED`: `true` or `false`; defaults to `false`.
- `KC_ACTIVE_DIRECTORY_CONNECTION_URL`: `ldaps://host:636` with certificate
  verification, or `ldap://host:389` only with
  `KC_ACTIVE_DIRECTORY_ALLOW_INSECURE_LDAP=true` (default: `false`). Plain LDAP
  exposes passwords and directory data on the network. There is no StartTLS or
  automatic downgrade. Credentials, other ports, paths, queries, and fragments
  in the URL are rejected.
- `KC_ACTIVE_DIRECTORY_USERS_DN`: the Active Directory users search DN.
- `KC_ACTIVE_DIRECTORY_GROUPS_DN`: the Active Directory groups search DN.
- `KC_ACTIVE_DIRECTORY_USERNAME_ATTRIBUTE`: `sAMAccountName` or
  `userPrincipalName`.
- `KC_ACTIVE_DIRECTORY_GROUP_NAMES`: a non-empty JSON array of unique approved
  group names (legacy mode). Names must be lowercase, start with `neurwerk-`, match
  `^neurwerk-[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$`, and contain at most 64
  characters.
- `KC_ACTIVE_DIRECTORY_GROUP_MAPPINGS`: a JSON array such as
  `[{"sourceName":"CORP_USERS","targetParent":"/access/neurwerk-studio-users"}]`.
  Set exactly one of the two group lists nonempty; omit the other or use `[]`.
  Sources are AD CNs of 1-64 characters, including uppercase and underscores,
  without control characters, placeholders, or outer whitespace. LDAP filters
  and DNs are escaped separately. Sources must be unique ignoring case, and
  targets must be unique exact existing canonical `/access` group paths (the 13
  standard groups, plus the two Forgejo groups only when present).
- `KC_ACTIVE_DIRECTORY_BIND_DN`: either a DN-like bind principal containing `=`
  or a whitespace-free UPN-like principal containing one `@`. Control
  characters are rejected.
- `KC_ACTIVE_DIRECTORY_BIND_CREDENTIAL`: the bind credential.
- `KC_ACTIVE_DIRECTORY_EMAIL_VERIFIED`: must be `true`.

Disabled mode reads no bind variables. Enabled reconciliation tests the LDAP(S)
connection and bind, requires every corresponding `/access/<group>` path,
reconciles the managed provider and mappers, verifies every mutation by
readback, and synchronizes the approved group mappers. The provider is
read-only, uses `NO_CACHE`, disables scheduled full and changed-user sync, and
uses the standard Microsoft Active Directory account-control mapper. Group
sync must process every approved group. In legacy mode, each non-brief group
representation must expose one case-insensitively exact expected Active
Directory DN in `attributes.LDAP_ENTRY_DN`. Missing or ambiguous LDAP metadata
fails reconciliation. Bind credentials remain write-only; Keycloak's
`**********` component-secret readback is accepted only for `bindCredential`.

Mapping mode uses one built-in `READ_ONLY` mapper per source, with direct `member`
lookup and exact CN and distinguished-name filters. Eligibility uses direct
`memberOf` against `CN=<escaped sourceName>,<groupsDn>`; nested AD memberships
do not grant access. Each source becomes a child of its canonical target and
inherits that parent's existing roles. Keycloak 26.7.2 binds these groups by
parent and name, not `LDAP_ID` or `LDAP_ENTRY_DN`. Verification checks sync counts,
mapper settings, and the real parent/child IDs, names, and paths. A missing
source fails sync. No plugin, role rewrite, or local membership copy is used.

During mapping reconciliation and transitions to/from legacy mode, the provider
is temporarily disabled until every check succeeds. Failures after this point
leave it disabled for a safe retry; preflight failures leave the old state intact.
Ownership and pending/ready states use reserved native component `subType` values,
not custom config keys. Unrelated subtypes are rejected rather than overwritten.
An uncertain final activation triggers a best-effort disable and reports if it
cannot be verified. Run only one reconciler for a realm at a time. Cleanup
retires only reserved, owned group mappers under this provider, never groups,
manual components, roles, or memberships. An overlapping manual `/access` mapper
or a legacy mapper changed to `IMPORT` must be reviewed before retrying.
Removing a mapping removes its dynamic grants on reevaluation, but retained
groups, local memberships, already-issued tokens, and application sessions are
not revoked. Keep an independent local break-glass administrator available.

## Local CLI Configuration

### Package Checker

Copy `cli_tools/package_checker/.env.example` to `.env` and provide a GitHub
personal access token through `PACKAGE_CHECKER_GITHUB_PAT`. The token needs only
the package and Actions read permissions required for the repositories being
inspected. See the [package checker README](cli_tools/package_checker/README.md)
for inventory and output details.

### LocalAI Installer

Downloads verified models and provisions stock LocalAI over HTTP using an explicit
Docker context. Replaces the media downloader and removes its Ceph publishing.
See the [installer README](cli_tools/local_ai_installer/README.md) for setup.

### OpenBao Stack Setup

`stack-setup` requires an explicit Kubernetes context and client identity. It
can create or use high-value static seal and recovery custody material. Custody
roots are rejected when they are inside a Git worktree and must be stored in a
private directory outside source workspaces. A multi-repository workspace is
detected when the current worktree's parent contains at least two direct Git
worktree children. Set `OPENBAO_STACK_SETUP_WORKSPACE_ROOT` to an absolute path
to declare that boundary explicitly when running elsewhere. Never commit,
upload, or disclose custodian ZIPs, recovery shares, private keys, or seal
checkpoints.

Read the [OpenBao CLI README](cli_tools/openbao_stack_setup/README.md) and the
applicable operational runbook before using a mutating or recovery command.

## Safety

- Review commands and selected Kubernetes contexts before remote operations.
- Never store credentials or recovery material in tracked files.
- Do not use the OpenBao CLI as a Kubernetes Job.
- Treat PII bundle versions as immutable after their completion manifest exists.
- Validate model licenses and upstream access requirements before download or
  distribution.
- Use least-privilege GitHub and Kubernetes credentials.

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and the
repository security policy.

## Contributing

Keep changes scoped to the owning project, update tests and documentation with
behavior changes, and preserve independent lockfiles. Pull requests should pass
the root and CLI quality matrix. Do not add generated distributions, local
environments, downloaded media, credentials, or custody artifacts.

## License

This repository is licensed under the [MIT License](LICENSE).
