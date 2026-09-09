# Keycloak Users

Standalone trusted-workstation CLI for **local accounts in one explicitly selected
realm**. Python 3.12+, Questionary prompts, browser authorization code + PKCE S256,
verified HTTPS and signed RS256 ID and access tokens. No cluster access, bootstrap credential lookup,
saved sessions, password prompts, client secrets, or platform changes.

Run from the standalone project directory, `cli_tools/keycloak_users/` in your
tooling checkout or worktree:

```bash
uv sync --frozen --dev
uv run --frozen keycloak-users
uv run --frozen keycloak-users setup
uv run --frozen keycloak-users doctor --profile example
uv run --frozen keycloak-users --profile example
```

From another directory, use an absolute project path, for example:

```bash
uv run --project /path/to/tooling/cli_tools/keycloak_users --frozen keycloak-users
```

`--project` paths resolve relative to the current directory, not the repository
or workspace root. Replace the example with your actual project path.

With no arguments, the CLI offers first-time setup or selection of a saved target.
No target is chosen implicitly. An explicit `--profile` skips only target selection,
not browser authentication or write confirmation.

## One-Time Manual Setup

`setup` guides an authorized administrator through the Admin Console. It does not
create clients or grant roles. Select the target realm, not `master` by convention.
The wizard prints the Admin Console URL and settings for your chosen realm and
callback port. In the Keycloak 26.x Admin Console:

1. Select the target realm. Open **Clients > Create client**. Set **Client type:
   OpenID Connect** and **Client ID: keycloak-users** (or your dedicated ID). Do not
   repurpose a platform-managed application client.
2. In **Capability config**, set **Client authentication: OFF**, **Standard flow:
   ON**, **Implicit flow: OFF**, **Direct access grants: OFF**, **Service accounts
   roles: OFF**, **OAuth 2.0 Device Authorization Grant: OFF**, and **OIDC CIBA
   Grant: OFF** wherever offered.
3. In **Login settings**, set **Valid redirect URIs:
   http://127.0.0.1:8765/callback** (use the exact URL printed by setup if different),
   **Web origins: empty**, **Root URL: empty**, **Home URL: empty**, and **Valid post
   logout redirect URIs: empty**. Do not enter wildcard redirects.
4. Save, then open **Settings > Capability config** and set **PKCE method: S256**.
   Only on older versions without this setting, use **Advanced > Proof Key for
   Code Exchange Code Challenge Method: S256** instead. Keep **ID Token Signature
   Algorithm: RS256**; v1 fails closed on other algorithms. Select an unused high
   callback port and use a browser on the same workstation.
5. Under **Client scopes > dedicated scope > Scope**, set **Full Scope Allowed:
   ON** for the simple setup. Under **Client scopes**, keep **roles > Assigned
   type: Default** so the operator's existing roles can appear in access tokens.
   These settings do not grant operator permissions.
6. Under **Realm settings > Email**, configure and test SMTP separately. Then return
   to the terminal, confirm the setup, authenticate in the browser as an authorized
   operator, and approve saving the non-secret profile after doctor checks pass.

The operator's verified access token must contain standard `realm-management`
client roles: **`manage-users` AND `view-realm`** (`manage-realm` is an accepted
substitute for `view-realm`), **OR `realm-admin`**. Redundant `query-users`,
`view-users`, and `query-groups` roles are not required. `view-clients` is not
needed by this CLI. An operator who already has these permissions needs no
additional group or grant. Human authority remains
assigned through approved Git-managed access groups, not direct human role grants.
Do not edit existing managed roles or group mappings manually. If existing
authority is insufficient, obtain a reviewed grant through its owner, or follow
the explicitly authorized new-group bootstrap below and persist the matching
configuration in Git. Neither the CLI nor its setup wizard performs these changes.
`manage-users` is broad realm administration, **not CLI-constrained delegation**.
This tool does not create a server-side security boundary or automatically grant
any roles. Restrict which operators may use the client and review server policies.

**Optional hardening, not a setup prerequisite:** under **Client scopes > dedicated
scope > Scope**, set **Full Scope Allowed: OFF** and add the listed
`realm-management` roles as explicit permitted scope mappings. Keep **roles >
Assigned type: Default**. Scope mappings restrict which existing grants can appear
in tokens; they do not grant roles to the operator.

Immediately after browser login, the CLI verifies the access token's RS256
signature, issuer, `exp`, `iat`, `realm-management` audience, and `azp` binding to
the configured client. Its subject must match the nonce-verified ID token. Missing
token permissions block before Admin API reads, account prompts, or profile saving.
Signed-token validity and permissions are rechecked on every Admin API request and
after refresh. Fine-grained-only admin authorization is unsupported and fails closed.

Setup saves only after browser authentication, the permission gate, all read-only
doctor probes and creation prerequisites, and a default-No confirmation. Doctor
reads visible users, group hierarchy, required actions, realm defaults, federation
components, and SMTP host/from readiness. Setup and doctor require the same four
exact local groups, enabled `VERIFY_EMAIL` and `UPDATE_PASSWORD` actions, no active
user storage, and unique-email policy as create before reporting success or saving
a profile. Missing SMTP does not block these checks: onboarding without email is
allowed. SMTP configuration is only a basic readiness signal, not proof of delivery;
the CLI sends no test emails. Configure and test SMTP independently.

These checks do not guarantee future write authorization: server permissions or
configuration can change. Use operators with realm-wide read visibility; filtered
results cannot establish global absence. No live realm or complete onboarding
lifecycle has been verified.

### Bootstrap An Operator Group

Skip this section if your CLI operator already has the required permissions.
Otherwise, have an administrator authorized to manage the target realm perform
this one-time setup for a **new** operator group. The read-only operator cannot
grant itself access. A temporary bootstrap administrator can perform these steps;
do not delete it until permanent administrator access has been verified.

1. **Realm: your target application realm**, the same realm configured in the CLI
   profile. Do not create this group in `master` for a CLI using another realm.
2. Open **Groups**, select the existing **access** parent group, and click
   **Create group**. Enter **Name: neurwerk-user-admins** and create it. Enter only
   the name, not the full path. Verify **Group path:
   /access/neurwerk-user-admins**; it must be beneath `access`, not a top-level group.
3. Select the new group. Open **Role mapping > Assign role > Filter by clients**.
   Select **Client: realm-management** and assign **Roles: manage-users,
   view-realm**. These are existing client roles, not similarly named realm roles.
4. Open **Users**, select the actual account used for the CLI's browser login, then
   open **Groups > Join Group**. Select **Group: /access/neurwerk-user-admins** and
   click **Join**. Preserve its existing memberships.
5. Exit and restart the CLI to obtain a fresh access token. Permission preflight
   should pass. If it still reports missing authority, check **Full Scope Allowed:
   ON** for the CLI client and **roles > Assigned type: Default**, then log in again.
6. Record the new group definition and its `realm-management` role mappings in the
   owning client configuration through the normal reviewed Git workflow. Follow the
   client's identity-ownership policy for ongoing operator membership management.

**Authority: broad user administration.** Add only trusted operators to this
group. Do not include it in the Normal user preset or grant these permissions to
ordinary LibreChat, MCP, LLM, or Studio user groups. This authorized bootstrap is
not permission to bypass Git ownership of existing managed mappings.

## Account Workflow

Create runs the permission gate, read probes, and creation prerequisite checks
before asking for account details. It then prompts for first/last name, email and
an editable username (email default).
Choose enabled required actions with explicit checkboxes; `VERIFY_EMAIL` and
`UPDATE_PASSWORD` default selected and must both be available. V1 requires password
enrollment via `UPDATE_PASSWORD`; alternate passwordless enrollment is not supported.
`CONFIGURE_TOTP` is recommended for privileged access when enabled. Enrollment is
**not enforcement of MFA at login**; authentication flows control that.

For **create only**, the **Normal user** preset preselects exactly these existing
local group paths in the editable membership checkbox:

- `/access/neurwerk-librechat-users`
- `/access/neurwerk-mcp-all-users`
- `/access/neurwerk-llm-all-users`
- `/access/neurwerk-studio-users`

This is a selection preset, not a new `normal-user` group. All four exact paths
must exist and resolve unambiguously; a missing or ambiguous required path fails
before account-detail prompts. There are no aliases, fallback paths, or group creation.
Operators can deselect preset memberships or select other eligible groups; other
groups are not preselected. Dify is disabled in the onboarding selector only:
the CLI does not disable the application or remove existing Dify memberships.

Only existing local descendants of `/access` are selectable, with full paths.
LDAP/federation-marked groups and their descendants are excluded. All groups may
confer privileged or inherited/composite permissions;
this tool does not expand or claim to enumerate their effective grants. Review
managed group mappings in Git. Realm default groups and default roles still apply
even when no access group is selected; they are shown before confirmation.
Realm default required actions are also shown and must be explicitly selected for
creation; changing defaults invalidates an earlier approval.
The checkbox defaults include enabled realm-default actions. The email prompt
defaults to Yes only when basic SMTP host/from readiness is present. Without it,
email defaults to **No (`False`)**, allowing account creation and group-only resume
without email. An explicit email request is blocked before any writes if SMTP is
not ready. Required actions remain on the account for later onboarding. The final
confirmation still defaults to No.

V1 refuses creation if any enabled user-storage provider exists, including LDAP/AD
and custom federation. Unknown enabled state fails closed. This prevents local
shadow accounts in federated realms. Federation-linked existing users are never
modified; resume is for explicitly identified local accounts only. The CLI never
changes roles, group definitions, clients, federated identities or credentials.

Before creation writes it revalidates defaults, federation state, enabled actions,
groups, unique-email policy and duplicate username/email. Applicable prerequisites,
including SMTP readiness when email is requested, are revalidated before writes;
signed-token permissions are checked on each request. Creation uses `exist_ok=False`, `enabled=true`,
`emailVerified=false`, and no credentials. Final confirmation defaults to **No**.
Concurrent server changes cannot be locked out by a workstation CLI. Keycloak
uniqueness constraints are authoritative; duplicate emails remain race-prone in
realms allowing duplicate emails, so v1 refuses creation in such realms.

Creation, each membership and the execute-actions email request are separate stages.
The email request uses **604800 seconds (seven days)**. This does not change the
platform initial-administrator bootstrap email's **1800-second** default. No client
redirect is attached to the email. Successful submission means **request accepted**,
not delivery. A timeout can mean an operation succeeded remotely. No automatic
create/email retries, deletion or rollback occur.

## Partial Failure And Retry

The terminal reports the completed stage, account ID when known, successful group
count, and whether email was not attempted, accepted, rejected, or has an unknown
outcome. Earlier accepted creation and memberships remain recorded when a later
stage fails; no retry, deletion, or rollback is automatic. Cancellation after
dispatch can leave the current operation uncertain.
If creation has an unknown outcome, inspect the exact username with the explicit
resume operation; do not blindly create again. Resume requires typing the existing
username to confirm identity, rejects federation links, and never changes profile,
enabled state, credentials or required actions. It adds only selected missing local
memberships and can request only still-pending enabled required actions. Resume
applies no automatic membership defaults or Normal user preset. Completed
actions are not reset. `send-actions-email` is a separate retry flow with no group
mutation; it also requires identity and final confirmation. Review earlier email
outcomes before requesting another message. No local progress or PII files are saved.
Email-bearing resume is refused for a disabled account before any membership change;
the tool never enables an existing account. Group-only resume remains available.
Resume and `send-actions-email` remain local-account-only but do not require the
create-specific four-group preset, federation-absence check, or unique-email realm
policy. The dedicated email command checks SMTP readiness before asking for an
account; missing SMTP blocks it. Group-only resume can proceed without SMTP.

```bash
uv run --frozen keycloak-users resume --profile example
uv run --frozen keycloak-users send-actions-email --profile example
```

## Diagnosis

A read-only operator whose token has `view-realm` but lacks `manage-users` is
blocked immediately after login, even for doctor, before any Admin API reads.
The message lists missing roles **in the verified token**. It does not claim the
operator has no grant: an unassigned grant and a grant excluded by client scope
cannot be distinguished from token absence alone. The CLI seeks no extra API
permissions to inspect grants or scopes. Have the owner review approved grants
and client scope settings, then log in again; do not add redundant query/view roles.

Operation errors identify the selected realm and operation: read, create,
membership, or email. Explicit HTTP 4xx responses are rejections, not unknown
outcomes. Connection or TLS-establishment failures mean the request was not
submitted; write timeouts, HTTP 5xx responses, and malformed write confirmations
leave the outcome unknown. Inspect progress before deciding what to do next.

- `400`: correct only recognized field errors reported through the safe allowlist.
- `401`: log in again.
- `403`: check permissions with the realm owner.
- `409`: inspect the conflict; do not blindly repeat creation.
- `429`: wait before trying again.

Raw upstream responses and secrets are not printed. A valid role gate or earlier
successful read is not a promise that a later write will be accepted.

## Local Configuration And Security

Named profiles live in `$XDG_CONFIG_HOME/keycloak-users/profiles.json`, defaulting
to `~/.config/keycloak-users/profiles.json`. Only name, HTTPS server URL (optional
`/auth` prefix), realm, public client ID, high loopback port and optional absolute
CA bundle path are persisted. Directory mode is 0700, file mode 0600, with atomic
replacement. Malformed/unknown fields and duplicate JSON keys fail instead of
silently dropping profiles. Do not run simultaneous setup writers. Profiles contain
no credentials but may contain private deployment identifiers; do not commit them.

Tokens and refresh tokens exist only in process memory. Discovery issuer must match
exactly; endpoints must use the configured HTTPS origin. HTTP redirects, environment
proxies, netrc authentication and TLS bypass are disabled. The callback binds only
`127.0.0.1`, before browser launch, accepts only `/callback` and the expected Host,
state and issuer, and expires after three minutes. Ctrl-C cancels and closes the
listener. Loopback access by other local processes can deny login but cannot bypass
state/PKCE. Callback URLs, server error bodies, tokens and action links are not
logged. Do not enable transport debug logging or share terminal recordings with PII.
Closing the tool discards credentials; it does not terminate the browser SSO session.

Uses the public [python-keycloak library](https://python-keycloak.readthedocs.io/en/latest/modules/admin.html)
for Admin API operations. Its connection is adapted to avoid its default POST retry,
401 replay and redirect behavior. OIDC uses HTTPX and PyJWT with cryptographic JWKS
verification. See the [Keycloak administration guide](https://www.keycloak.org/docs/latest/server_admin/index.html)
for current client settings and role semantics.

## Validation

```bash
uv lock --check
uv sync --frozen --dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen pytest
uv build
```

Tests use fake HTTP/admin transports and loopback-only callback simulations. No live
realm, SMTP delivery, browser SSO policy or full account lifecycle is claimed verified.
