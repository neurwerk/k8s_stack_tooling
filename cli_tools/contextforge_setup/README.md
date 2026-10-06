# ContextForge Setup (source only)

Small trusted-workstation CLI for **native accounts only**, matching Studio
[PR66](https://github.com/neurwerk/k8s_stack_studio/pull/66) at
`03570e0e1c52bf4a3f968235172df857a3556187`. It uses native REST APIs pinned at
`077071bbb43599dd5ab9372ebdbb9a8e686a9816`, not Studio Connect, a broker, database
writes, an ownership/login store or a shared signing key.

API-key-only callers can have native accounts. Use the authoritative Keycloak
email/subject of the key's principal, including the **service principal**, never
a managed key's human creator. An operator approves input from Keycloak; this CLI
does not query Keycloak or grant platform `llm:invoke` or MCP permissions.
No registrations, virtual servers, tools, provider credentials, personal OAuth or
Connect operations are supported. Unsupported config fields, including integration
entries, are rejected **before any API access**. Context7 and shared Brave
registrations follow separately, not blocked by the personal OAuth issue
[Base #424](https://github.com/neurwerk/k8s_stack_base/issues/424).

## Base/operator prerequisites

Keep MCP entrypoints blocked during this operator ceremony. Prepare the **same
stable non-personal team and role IDs used by Studio**. The native provisioning
service principal owns the team and needs `admin.user_management`, `teams.read`
and `teams.manage_members`. It does not need the `is_admin` flag; native endpoints
enforce its actual permissions. The CLI reads definitions; it never creates,
updates or deletes teams/roles. Ordinary accounts must have exactly:

| Assignment | Exact native permissions |
| --- | --- |
| Configured global role, global scope, no scope ID | None |
| Configured team role, team scope, configured team ID | `tools.read`, `tools.execute`, `servers.read`, `servers.use`, `gateways.read` |

Both roles must be active, with no inheritance. Every account must be non-admin,
active, email-verified, and an active team `member`, never `owner`; assignments
must be active and non-expiring. Extra/foreign grants, removed grants/membership,
disabled users or wrong role definitions are conflicts, never automatic repairs.
Other native profile fields are left unchanged; no identity markers are added.

Pinned upstream `config.py` and `email_auth_service.py` verify these native setting
names and that defaults are looked up **by role name and scope**, not by ID:

```text
AUTO_CREATE_PERSONAL_TEAMS=false
DEFAULT_USER_ROLE=<name of the prepared empty global role>
DEFAULT_TEAM_MEMBER_ROLE=<name of the prepared limited team role>
```

Those names must resolve to the configured role IDs above. Do not guess a default
or retain `platform_viewer`/personal-team admin onboarding. Base must also enable
native email/RBAC APIs for private provisioning and enforce `REQUIRE_USER_IN_DB=true`,
`MCP_REQUIRE_AUTH=true` and `MCPGATEWAY_DIRECT_PROXY_ENABLED=false`. Public native
login/password-reset and management/credential APIs must remain unexposed.
These server settings are operator prerequisites, **not remotely verified settings**
by this CLI; effective grants are checked before activation. The configured team
should be private; its active non-personal identity and service ownership are checked.

## Input and commands

Use private operator input, not real identity manifests in public Git:

```json
{
  "origin": "https://contextforge.example.com",
  "team_id": "00000000000000000000000000000001",
  "global_role_id": "prepared-global-role-id",
  "team_role_id": "prepared-team-role-id",
  "accounts": [
    {
      "email": "caller@example.com",
      "issuer": "https://identity.example.com/realms/platform",
      "subject": "authoritative-principal-id",
      "kind": "service",
      "enabled": true
    }
  ]
}
```

`kind` is `user` or `service`. `enabled` must be `true`, attested from the approved
authoritative source; disabled/missing identities are rejected. Native email must
be lower-case ASCII; emails and issuer/subject pairs must be unique. Team/role IDs
are copied **exactly**, not re-generated or UUID-normalized. Account identity is
native email, so retries preserve it and never reset its password. Native APIs do
not store a Keycloak issuer/subject mapping: those fields catch input collisions,
not native identity-binding proof. Verify email ownership in Keycloak before use.
Existing records are checked read-only, not adopted into another ownership store.

From this package directory, after separately authorized installation:

```bash
# Local validation only; no credentials or HTTP.
uv run --frozen contextforge-setup reconcile-accounts --config <private-accounts.json>

# Explicit mutations; native service/admin token via hidden prompt
# or CONTEXTFORGE_ADMIN_TOKEN. No token/password CLI arguments.
uv run --frozen contextforge-setup reconcile-accounts --config <private-accounts.json> --apply

# Alternative native login: hidden password prompt or
# CONTEXTFORGE_ADMIN_EMAIL / CONTEXTFORGE_ADMIN_PASSWORD.
uv run --frozen contextforge-setup reconcile-accounts --config <private-accounts.json> --apply --login
```

Use an approved private HTTPS origin and `--ca-cert <trusted-ca.pem>` if needed.
TLS verification cannot be disabled. For an explicitly approved local tunnel,
use an origin such as `http://127.0.0.1:8000` and `--allow-loopback-http`;
non-loopback HTTP, origin paths, queries or credentials are rejected. Environment
proxies/netrc are ignored; redirects are never followed. Do not paste credentials
in shell commands; environment secrets must come from private operator custody.
Only counts and opaque errors are printed; payloads and raw API errors are hidden.
This native provisioning credential is distinct from caller Keycloak credentials
and the runtime Vault token. No signer key is shared.

## Safety and retry

All existing accounts are verified before **any** mutation. Valid existing accounts
receive only GETs, even when roles/membership were revoked; conflicts stop the run.
Only accounts successfully created in the **current run** can receive membership,
missing prepared roles and activation. Required bootstrap passwords are random
and discarded after native CreateAccount; native ContextForge stores their hash,
but there is no password distribution/store or secure memory-erasure claim.
New accounts start disabled; email verification, exact roles and controlled
membership must pass before the sole activation PATCH (`is_active=true`). No
display-name updates, offboarding, privilege cleanup or automatic partial-account
recovery are implemented. A lost creation response leaves an inactive existing
account requiring private operator repair; rerunning cannot regrant or enable it.
A lost activation response may be retried read-only if native state is valid.

Native calls are not one transaction across accounts. Keep routes blocked on any
failure, inspect/repair privately, then retry. Do not run competing reconcilers or
change grants/settings during the ceremony. The pinned REST APIs lack per-member
GET and complete foreign-team/history/issuer-subject metadata: the CLI reads the
whole approved team's active member list (native no-cursor/no-limit behavior) and
active grants to active roles, including expired grants for rejection. It does not
claim a complete audit of dormant grants or membership-only changes elsewhere.
Keep ordinary native management/credential APIs unreachable and do not add users
to other teams or independently reactivate dormant grants.

Future Base routes must use normal `/servers/{server_id}/mcp`, approved operator
tool membership and the existing trusted email on every handshake/list/call.
No new header layers or policy switches are introduced. Native no-auth/shared
lookups can read personal headers, so credential writes for those upstream URLs
must stay unavailable. Brave's key stays in the upstream service; native
`auth_type=none` may represent platform `shared-authentication`. The next slice
will reject duplicate URLs crossing authentication models and retain the mapping:
platform ID → approved upstream address, authentication model, native gateway/server,
public route, permission and PII/content-trace policy. No PAT fallback, plugins,
Authorization injection or individual OAuth bypass is supported here.

Publication, Base settings/routing, Secret/CA delivery and live health checks need
separate approval. Source validation is not runtime evidence.

## Validation and evidence

No repository-root or package Makefile exists. The CLI Quality matrix includes
this independent locked project in Required CI:

```bash
uv lock --check
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen pytest
uv build
```

Pinned native sources:
[email login/admin users](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/routers/email_auth.py),
[defaults and verified admin-created accounts](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/services/email_auth_service.py),
[setting names](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/config.py),
[RBAC](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/routers/rbac.py),
[membership API](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/routers/teams.py),
[unpaginated membership](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/services/team_management_service.py).
