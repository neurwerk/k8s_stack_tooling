# Scoped service clients

`upsert-scoped-service-client` is a container command for explicitly approved Pipecat machine identities. Add-on charts own Jobs, approval values, Secret delivery and managed-key registration; Tooling owns reconciliation. Select a reviewed Tooling image containing this command before enabling those Jobs; changing source alone does not publish an image.

## Inputs

| Environment variable | Value |
| --- | --- |
| `KC_INTERNAL_URL`, `KC_REALM` | Keycloak URL and realm |
| `KC_ADMIN_USER`, `KC_ADMIN_PASSWORD` | Provisioning administrator; password from a Secret |
| `KC_CLIENT_ID` | `pipecat-bot-<id>`; ID starts with a lowercase letter, contains only lowercase letters, digits and hyphens, ends with a letter or digit, and is at most 40 characters |
| `KC_CLIENT_SECRET` or `KC_CLIENT_SECRET_FILE` | Exactly one source: Secret-backed environment value or mounted UTF-8 file containing the exact secret without trailing newline |
| `KC_REALM_ROLE_SCOPE` | JSON array of exact realm permissions; defaults to `[]` and should be empty for Pipecat |
| `KC_GATEWAY_ROLE_SCOPE` | JSON array of exact AgentGateway client permissions |
| `KC_CLIENT_AUDIENCES` | Exactly `["agentgateway"]` |
| `KC_CLIENT_APPROVAL` | Trusted JSON object with exactly `clientId`, `realmRoles`, `gatewayPermissions`, `audiences`, matching the requested identity and permission sets |

Chart `clientApproval` values must be delivered separately from untrusted bot configuration. The helper checks exact equality, not a subset. For example:

```json
{"clientId":"pipecat-bot-example","realmRoles":[],"gatewayPermissions":["llm:invoke","model:example:invoke"],"audiences":["agentgateway"]}
```

Permission arrays must be unique and contain only `llm:invoke`, `model:<canonical-id>:invoke` or `provider:<id>:company-use`. Gateway permissions require `llm:invoke` and at least one model invocation permission. Each role must already exist and must not be composite. Platform client names and reserved bot IDs (`pipecat`, `shared`, `turn`, `oidc` and platform client names) are rejected. The add-on additionally checks the selected canonical model and approved role catalog.

## Credentials and ownership

Setup's machine credential is `addons/pipecat/machines/<id>:clientSecret`, delivered only to `auth-keycloak/pipecat-bot-<id>-oidc:clientSecret`. Runtime `addons/pipecat/bots/<id>:gatewayApiKey` is a different credential. Never send the machine secret through command arguments or print either credential.

New clients are created disabled with the `neurwerk.scoped-service-client=v1:<client-id>` ownership attribute. An existing client without that exact attribute is rejected without mutation, even if its ID matches. A mismatched existing secret is also rejected without rotation; resolving that mismatch is a separate operator action. The helper never modifies other clients, shared scopes, groups or ordinary users.

Clients have service accounts enabled, browser/password/device/CIBA/standard token-exchange flows disabled, no redirects, no refresh tokens and `fullScopeAllowed=false`. No shared default or optional scopes remain attached. Dedicated subject, exact audience and permission mappers preserve the service-account identity. Both direct role assignments and role scopes are reconciled exactly, removing obsolete roles on this owned account, including realm-default roles. Group inheritance is rejected; effective role mappings and all configuration are read back before enabling issuance. A verified reapply performs no writes. Failed reconciliation never reports success, and failures after disabling leave the client disabled.

Managed API-key grants still pass through the bridge's intersection with the service account's current permissions. Removing a grant from the client does not immediately invalidate already issued tokens or cached bridge permissions. Deleting clients or persistent credentials is not part of this command.
