# K8s Stack Tooling

Python runtime utilities and trusted-workstation command-line tools used to
operate the neurwerk Kubernetes stack. This repository contains one container
package and three independently locked CLI projects.

## Projects

| Project | Purpose | Execution environment |
| --- | --- | --- |
| `k8s-stack-tooling` | Idempotent Keycloak and OpenSearch initialization commands | Kubernetes Jobs in the tooling container image |
| [`package-checker`](cli_tools/package_checker/) | Reports published GHCR versions and active GitHub Actions builds | Developer or operator workstation |
| [`media-downloader-uploader`](cli_tools/media_downloader_uploader/) | Downloads verified Hugging Face artifacts and publishes immutable PII bundles | Workstation with external storage and explicit cluster access |
| [`openbao-stack-setup`](cli_tools/openbao_stack_setup/) | Bootstraps, reconciles, verifies, and updates supported OpenBao state | Trusted operator workstation only |

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

Build locally without publishing:

```bash
docker build -t k8s-stack-tooling:local .
```

The image runs as a non-root user and has no default entrypoint. Workloads must
select the required command explicitly. Tagged releases are built and published
to GHCR by GitHub Actions using the repository-scoped `GITHUB_TOKEN`.

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
- `KC_ACTIVE_DIRECTORY_CONNECTION_URL`: exactly an LDAPS endpoint of the form
  `ldaps://host:636`. Credentials, other ports, paths, queries, and fragments
  are rejected.
- `KC_ACTIVE_DIRECTORY_USERS_DN`: the Active Directory users search DN.
- `KC_ACTIVE_DIRECTORY_GROUPS_DN`: the Active Directory groups search DN.
- `KC_ACTIVE_DIRECTORY_USERNAME_ATTRIBUTE`: `sAMAccountName` or
  `userPrincipalName`.
- `KC_ACTIVE_DIRECTORY_GROUP_NAMES`: a non-empty JSON array of unique approved
  group names. Names must be lowercase, start with `neurwerk-`, match
  `^neurwerk-[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$`, and contain at most 64
  characters.
- `KC_ACTIVE_DIRECTORY_BIND_DN`: either a DN-like bind principal containing `=`
  or a whitespace-free UPN-like principal containing one `@`. Control
  characters are rejected.
- `KC_ACTIVE_DIRECTORY_BIND_CREDENTIAL`: the bind credential.
- `KC_ACTIVE_DIRECTORY_EMAIL_VERIFIED`: must be `true`.

Disabled mode reads no bind variables. Enabled reconciliation tests the LDAPS
connection and bind, requires every corresponding `/access/<group>` path,
reconciles the managed provider and mappers, verifies every mutation by
readback, and synchronizes the approved group mapper. The provider is
read-only, uses `NO_CACHE`, disables scheduled full and changed-user sync, and
uses the standard Microsoft Active Directory account-control mapper. Group
sync must process every approved group, and each resulting non-brief group
representation must expose one case-insensitively exact expected Active
Directory DN in `attributes.LDAP_ENTRY_DN`. Missing or ambiguous LDAP metadata
fails reconciliation. Bind credentials remain write-only; Keycloak's
`**********` component-secret readback is accepted only for `bindCredential`.

## Local CLI Configuration

### Package Checker

Copy `cli_tools/package_checker/.env.example` to `.env` and provide a GitHub
personal access token through `PACKAGE_CHECKER_GITHUB_PAT`. The token needs only
the package and Actions read permissions required for the repositories being
inspected. See the [package checker README](cli_tools/package_checker/README.md)
for inventory and output details.

### Media Downloader Uploader

The media tool requires explicit external storage paths. It has no
machine-specific storage default. Configure both
`MEDIA_DOWNLOADER_UPLOADER_STORAGE_ROOT` and `HF_HOME` to paths on the same
non-root mounted volume.

Uploading a PII bundle additionally requires
`MEDIA_DOWNLOADER_UPLOADER_KUBE_CONTEXT`. Every `kubectl` process receives that
context through `--context`; the tool never relies on the active context.
Deleting a pre-existing incomplete object prefix requires typing the selected
context. See the [media tool README](cli_tools/media_downloader_uploader/README.md)
before downloading or publishing artifacts.

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
