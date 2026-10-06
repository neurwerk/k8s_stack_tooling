# ContextForge Setup (source only)

Small trusted-workstation CLI for native accounts and scoped registrations, matching Studio
[PR66](https://github.com/neurwerk/k8s_stack_studio/pull/66) at
`03570e0e1c52bf4a3f968235172df857a3556187`. It uses native REST APIs pinned at
`077071bbb43599dd5ab9372ebdbb9a8e686a9816`, not Studio Connect, a broker, database
writes, an ownership/login store or a shared signing key.

API-key-only callers can have native accounts. Use the authoritative Keycloak
email/subject of the key's principal, including the **service principal**, never
a managed key's human creator. An operator approves input from Keycloak; this CLI
does not query Keycloak or grant platform `llm:invoke` or MCP permissions.
The accounts command never registers providers or changes tools. The separate
registrations command accepts generic operator-approved providers, including
no-authentication Context7 and shared Brave. Account config still rejects integration
entries **before any API access**. Individual-authentication app registrations use
approved OAuth metadata and a privately resolved operator app secret. Personal
consent, PKCE, provider tokens and token refresh remain native ContextForge work.
This does not fix OAuthTokenVault [Base #424](https://github.com/neurwerk/k8s_stack_base/issues/424)
or enable Studio Connect.

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
`auth_type=none` represents platform `shared-authentication` for Brave. The
registrations command rejects duplicate URLs crossing authentication models and retains the mapping:
platform ID → approved upstream address, authentication model, native gateway/server,
public route, permission and PII/content-trace policy. No PAT fallback, plugins,
Authorization injection or individual OAuth bypass is supported here.

Publication, Base settings/routing, Secret/CA delivery and live health checks need
separate approval. Source validation is not runtime evidence.

## Scoped registrations (source only)

Prefer Base's generated `registrations.json` instead of a second handwritten list:

```bash
contextforge-setup reconcile-registrations --config /approved/operator.json --catalog /approved/registrations.json
```

With `--catalog`, the operator config contains `origin`, `team_id`, and
`owner_email`, plus optional `operator_authentication` and `oauth_secret_env` below;
the separate JSON array comes from the opt-in Base catalog ConfigMap.
The existing inline `registrations` input remains supported. Never supply both.
Persist resolved gateway IDs in the private client catalog, then re-render its
operator/Studio projections. This CLI neither modifies Git nor wires Studio.
Client operators approve definitions; ordinary Studio users cannot administer
arbitrary destinations. Ordinary provider additions need configuration, not Base
or Tooling provider-name branches. Base approval/host/network controls still apply.

Optional registration `visibility` is `team` (legacy default) or `public` (new Base
catalog default). PUBLIC visibility behind private ingress is deliberately accepted
within one fixed internal team, **not** strict native team isolation. Ownership,
tool membership and platform invocation permissions remain mandatory. Existing
records with different visibility are conflicts, never silently migrated.

Individual-authentication may declare optional non-secret `oauth` source metadata:
`authorization_url`, `token_url`, `client_id`, `redirect_uri`, `scopes`,
`client_secret_ref: {name, key}` and `pkce: true`. Endpoints/callback must use HTTPS.
This is one operator-registered app, not dynamic client registration or PAT support.
No plaintext client secret, token or header is accepted in configuration or arguments.
Individual `--apply` requires this metadata and the fixed reference
`client_secret_ref: {name: contextforge-oauth-apps, key: <integration-id>}`.
The operator resolves that approved reference through an explicit environment
mapping or hidden input; the CLI never reads arbitrary Kubernetes Secret names.
Only native app registration receives the actual client secret. Native registration
encrypts sensitive `oauth_config` fields before PostgreSQL persistence and masks
reads; this is still a PostgreSQL copy, not OpenBao-only storage or live encryption proof.
The user accepts temporary per-email native DB tokens with gateway team context
ignored, and the known callback-transfer/browser-binding limitation. This does not
fix the Vault scope defect. Actual ciphertext/restart qualification with the persistent
`AUTH_ENCRYPTION_SECRET`, compatible images and private native configuration remain
separate rollout steps; this command does not change the default token backend.

```bash
contextforge-setup reconcile-registrations --config /approved/private-registrations.json
contextforge-setup reconcile-registrations --config /approved/private-registrations.json --apply --login
# Only when the operator deliberately changes an owned server's approved tools:
contextforge-setup reconcile-registrations --config /approved/private-registrations.json --apply --login --update-owned-tools
```

Without `--apply`, validation makes **no API calls and prompts for no credentials**.
Only an approved operator supplies this local file, never a browser/caller URL or
Studio request. Unknown fields, credentials, URL query/userinfo/fragment, duplicate
IDs/routes/URLs fail before credentials/API access. Individual apply without the
approved app metadata/fixed Secret reference also fails before credentials/API access.
No PAT/basic/header fallback, token broker or native patch is provided.
Context7 must work without a provider key. Brave's company key remains
solely in its existing upstream MCP service, never in this file or native headers.

Exact schema (example addresses and tools are placeholders, not deployment values):

```json
{
  "origin": "https://contextforge.example.com",
  "team_id": "approved-existing-team-id",
  "owner_email": "registration-admin@example.com",
  "registrations": [
    {
      "id": "context7",
      "provider": "context7",
      "authentication_model": "no-authentication",
      "upstream_url": "https://context7.example.com/mcp",
      "transport": "STREAMABLEHTTP",
      "gateway_id": null,
      "server_id": "cccccccccccccccccccccccccccccccc",
      "approved_tools": ["resolve-library-id", "query-docs"],
      "permission": "mcp:context7:invoke",
      "public_route": "/mcp/context7",
      "pii_policy": "approved-policy-id",
      "content_trace": false
    },
    {
      "id": "brave",
      "provider": "brave",
      "authentication_model": "shared-authentication",
      "upstream_url": "http://brave.example.com/sse",
      "transport": "SSE",
      "gateway_id": null,
      "server_id": "dddddddddddddddddddddddddddddddd",
      "approved_tools": ["brave_web_search"],
      "permission": "mcp:brave:invoke",
      "public_route": "/mcp/brave",
      "pii_policy": "approved-policy-id",
      "content_trace": false
    }
  ]
}
```

Select 1-200 approved registrations. Provider is a generic lower-case identifier,
not an allowlist of provider names; multiple integrations can use the same provider.
`id` is the existing platform integration ID (lower-case DNS subdomain, max 50 characters;
Base routing accepts at most 48),
not a native UUID. Permission is exactly `mcp:<id>:invoke`. Choose fixed, distinct
32-character lower-case hex `server_id` UUIDs; native server creation accepts them.
Use an approved reachable HTTP(S) upstream URL and its exact native `SSE` or
`STREAMABLEHTTP` protocol. CF's upstream SSRF allowlist/network and CA trust must
allow those addresses; the CLI never changes those controls or upstream trust.
Internal HTTP upstream addresses are separate from the private admin-origin TLS
requirement. `approved_tools` is an explicit nonempty list of upstream **original
names**, not CF-prefixed display names: missing, ambiguous or disabled tools fail.
Use the exact native-stored endpoint path: approval distinguishes `/mcp` from
`/mcp/`, even though duplicate detection rejects both spellings across registrations.

Native gateway creation does **not** accept an ID. For first creation only, set
`gateway_id=null`: lookup uses deterministic `neurwerk-contextforge-<id>` names.
The native description binds provider/platform ID/auth model; server description
also binds the resolved gateway ID. Team, visibility, owner and creator must match
this exact ceremony. A foreign same-name/ID/URL registration is rejected, never
adopted, renamed or overwritten. Success prints a non-secret JSON mapping; copy
its resolved `gateway_id` into the operator file and approved consumers before
opening routes. Later runs with that ID must find it; they never replace it. An
unset ID still permits retry after a lost first-create response using the owned
alias, with no local mapping store or invented API ID support.

Existing valid resources are read-only. `--update-owned-tools` alone permits a
PUT of `associated_tools` on a verified owned server, leaving IDs/profile/other
settings untouched. Even then, a foreign gateway/team/auth/header association
fails rather than being removed. Disabled extra tool associations are inspected
through `/servers/<id>/tools?include_inactive=true`, not hidden by server detail.
After every write, native gateway state and exact approved membership are read
again; missing persistence is failure, not success. CF may create a `pending`
gateway asynchronously: keep routes blocked, let native registration finish,
then retry the same mapping; no polling loop or automatic re-registration runs.
The command is not a multi-integration transaction and does not roll back/delete
partially created resources. Keep routes blocked on **any** error and repair
privately. Do not run competing reconcilers or concurrent native management.

### Native operator and Base consumer contract

- Use an active, email-verified native `is_admin=true` registration owner and an active
  existing non-personal team. This trusted operator is **not** an ordinary caller
  or Studio's limited account-provisioning service. Native session tokens are
  required (`token_use=session` from email login): use `--login` with the same
  hidden/environment credentials as accounts, or a native email-login session in
  `CONTEXTFORGE_ADMIN_TOKEN`; scoped API tokens are rejected. Native verification
  and `/auth/email/me` establish the active, email-verified DB administrator, not local JWT parsing.
  After proxy activation use the explicit fixed operator mode below, not Studio's identity.
- The APIs used require `teams.read`, `gateways.read/create`, `tools.read`,
  `servers.read/create`, and `servers.update` only for explicit membership updates;
  the registration administrator needs native unscoped catalog visibility. No
  team/role creation, account grants, platform permissions or offboarding occurs.
- Read operations use `limit=0&include_inactive=true` for native unpaginated catalogs,
  plus server tools/resources/prompts including inactive associations. Reject active
  A2A associations; this pinned API has no inactive per-server A2A association read.
  Operators must keep A2A associations absent and ordinary management unavailable.
  Native visibility still hides **other owners' private registrations**, and there
  is no complete saved-personal-header audit API here. An approved operator inventory
  must exclude hidden duplicate upstream URLs/aliases and personal headers before
  enabling traffic; a visible URL already owned by any other authentication model
  fails. Do not claim this CLI audits hidden registrations or credentials.
- No-auth/shared gateways use `auth_type=none`; individual gateways use native
  `auth_type=oauth`, `grant_type=authorization_code` and approved app configuration.
  All use `gateway_mode=cache`, without passthrough or identity injection.
  Servers are OAuth-disabled, with
  only approved tools and no resources/prompts/A2A. Ordinary callers retain the
  exact limited account role above; no credential-write/registration/discovery
  management routes may be exposed, even for no-auth/shared providers.
- Output `registrations[]` contains `id`, `provider`, `authentication_model`,
  `upstream_url`, `transport`, resolved `gateway_id`, fixed `server_id`,
  `native_mcp_path`, exact `tool_ids`, `permission`, `public_route`, `pii_policy`,
  `content_trace`, and `state` (`pending-consent` or `approved-tools`). These states
  describe the registration ceremony, **not runtime readiness**.
  Routing/permission/PII/content-trace values are declared
  operator metadata, **not CF enforcement settings or platform grants**. Base
  must preserve the approved platform ID/public route and policies while replacing
  its upstream with the private native origin plus `/servers/<server_id>/mcp`;
  Studio's backend catalog uses the same gateway/server/auth-model IDs and team.
- Route only normal `/servers/<id>/mcp` with tool membership enforced on list/call,
  including cached tool calls. Keep `MCPGATEWAY_DIRECT_PROXY_ENABLED=false`,
  `MCP_REQUIRE_AUTH=true`, `REQUIRE_USER_IN_DB=true`; never route global `/mcp` or a
  direct-proxy bypass. AgentGateway must require `llm:invoke` **and** the matching
  `mcp:<platform-id>:invoke`, preserve PII/trace policy and forward the existing
  trusted account email on every handshake/list/call. Neither Connect nor native
  admin endpoints are public. Registration preparation alone does not establish
  runtime isolation or remove publication/trust/account/routing prerequisites.

### Operator app preparation and native consent

1. Register one approved provider app (GitHub first) with its exact approved HTTPS
   callback. Supply its client ID in the private approved catalog and keep its client
   secret outside Git, files, arguments and logs. DCR and per-user app creation are
   not used. Select approved original tool names and the fixed Secret reference.
2. The operator runs the supported two-custodian `stack-setup reconcile` on the
   trusted workstation. `openbao-stack-setup` 0.2.26 adds the exact
   `contextforge/provider-apps` ESO read and secret-operator write policies when
   ContextForge is selected; its native personal-token policy remains separate.
   Base must supply the catalog ConfigMap and matching fixed app ExternalSecret.
3. Prepare the authoritative app secret, using hidden input:

   ```bash
   stack-setup secret set-contextforge-oauth github --context <context> --client <client>
   ```

   This selects an individual integration from
   `infra-agentgateway/infra-agentgateway-mcp-catalog:registrations.json`, CAS-writes
   only its ID field in `contextforge/provider-apps`, and refreshes
   `contextforge/contextforge-oauth-apps`. New providers need only approved client
   configuration and this command, not a new Base or Tooling provider implementation.
4. Use native-session authentication before activation, or explicitly select
   `"operator_authentication": "trusted-proxy"` afterwards. Trusted-proxy mode is
   **only** for an operator-controlled private connection/tunnel or a protected
   fixed-principal management proxy. It sends the approved `owner_email` in the fixed
   `x-contextforge-account-email` header; native `PROXY_USER_HEADER` must match.
   Do not expose this header/management connection to browsers or ordinary users.
   The server must enforce active DB users, proxy trust and private operator access.
   The CLI checks `/rbac/my/roles` for the server-resolved operator email,
   `/rbac/my/permissions` for registration authority, and the RBAC-protected
   `/auth/email/admin/users/<owner-email>` for active, email-verified DB admin status. This endpoint
   works in native proxy mode; JWT-only `/auth/email/me` does not. Prepare at least
   one native role assignment for the operator. No Studio service identity fallback
   or shared CF signing key is supported. Native-session/token environment and
   `--login` cannot be mixed with trusted-proxy mode.
5. To resolve the approved app Secret from private in-memory custody, optionally
   add this **non-secret** mapping to the operator JSON:

   ```json
   "oauth_secret_env": {"github": "CONTEXTFORGE_OAUTH_GITHUB_APP_SECRET"}
   ```

   Each integration maps to a distinct `CONTEXTFORGE_OAUTH_*` name. An approved
   operator loads the matching ESO Secret field into that environment without
   printing it; without a supplied value the CLI uses a hidden prompt. No secret
   export/read command that prints credentials is provided. Existing matching
   registrations do not re-resolve or silently rotate the app secret; changing the
   authoritative record alone does not update a native PostgreSQL copy. App rotation
   requires a separately approved native credential update, never re-creation/adoption.
6. Validate, then explicitly create the gateway and **empty** scoped server:

   ```bash
   contextforge-setup reconcile-registrations --config <operator.json> --catalog <registrations.json>
   contextforge-setup reconcile-registrations --config <operator.json> --catalog <registrations.json> --apply
   ```

   Output is `pending-consent` with `tool_ids=[]`. Native authorization-code
   registration deliberately skips discovery before consent. Even if a callback
   discovers tools later, rerunning this command does not auto-grant them.
7. Complete native `/oauth/authorize/<gateway-id>` through the protected operator
   flow as **the same native registration operator principal**. A human's Studio
   Connect saves that human's token, not the registration operator's token; it cannot
   qualify operator discovery. The CLI never accepts a browser-selected email,
   starts consent or prints authorization URLs/state/provider responses. Accepted
   transferable-callback limitations remain. Native stores/refreshes each account's
   own tokens; discovery never falls back to another principal's or a shared credential.
8. Explicitly refresh using the operator's own native saved token and approve only
   the selected original tool names:

   ```bash
   contextforge-setup reconcile-registrations --config <operator.json> --catalog <registrations.json> --apply --refresh-oauth --update-owned-tools
   ```

   `--refresh-oauth` requires an existing owned gateway/server and explicit
   `--update-owned-tools`; it calls native
   `/gateways/<id>/tools/refresh?include_resources=false&include_prompts=false`.
   Native refresh needs `gateways.update`. Opaque failures, missing operator tokens,
   empty refreshes (no advanced native `lastRefreshAt`), validation errors, or missing,
   disabled/ambiguous approved tools cannot be reported as qualification. No discovered
   extras are granted. Exact stored membership is read back before `approved-tools`
   is reported. Keep routes blocked until the supervisor completes image/settings/
   permission/PII/two-user/ciphertext/restart qualification; this state is not `ready`.

### Workstation CLI release boundary

The independent packages are `contextforge-setup` 0.1.1 and `openbao-stack-setup`
0.2.26, each with its own lockfile/build artifacts. Root Tooling is still 0.7.4.
The root Dockerfile copies only `src/`, **not** `cli_tools/`; its image does not
contain these commands. Root `v*` image CI does not publish standalone CLI wheels.
CLI Quality validates/builds each package; adoption must pin a reviewed Tooling
source commit (the existing workstation installation path), or separately publish
and verify those package artifacts. Do not claim a root image tag delivers this CLI
or bump unrelated image pins. Publication/adoption remain supervisor-owned.

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

Registration source contracts:
[gateway/server/tool REST](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/main.py),
[native schemas](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/schemas.py),
[gateway discovery and generated IDs](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/services/gateway_service.py),
[server IDs and hidden inactive detail associations](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/services/server_service.py),
[full server tool membership](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/services/tool_service.py),
[native session scope](https://github.com/IBM/mcp-context-forge/blob/077071bbb43599dd5ab9372ebdbb9a8e686a9816/mcpgateway/auth.py).
