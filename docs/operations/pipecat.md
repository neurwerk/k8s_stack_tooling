# Pipecat credential preparation

Use the reviewed standalone `stack-setup` source revision selected for the add-on, not the operations container. An older catalog reconciliation does not prepare Pipecat. Follow the shared [OpenBao procedure](https://github.com/neurwerk/docs/blob/main/dev/operations/openbao.md) and [recovery custody](https://github.com/neurwerk/docs/blob/main/dev/operations/recovery-custody.md).

## Selection and isolation

Select `pipecat.enabled: true` in shared client values. Setup reads only `client-values` and `pipecat-product-values` in `pipecat`, in Helm value-source order. Set `pipecat.secretSync.enabled: true` for managed preparation; otherwise setup creates no Pipecat credentials or delivery targets, but refresh still reconciles active browser OIDC consumers and restarts active TURN and bot workloads using their existing credentials. Use the standard namespace-local OpenBao SecretStore names from the add-on defaults.

Bot `id` is a unique lowercase kebab-case Kubernetes identifier, at most 40 characters, ending with a letter or digit. Reserve `turn` for shared coturn Deployments, ConfigMaps and Services, and `product-values` for the required `pipecat-product-values` ConfigMap; reject these IDs even while TURN is disabled. Independent `urlSlug` accepts underscores, at most 64 characters; URL slugs must also be unique and cannot use `assets` or `healthz`. Neither value selects an arbitrary OpenBao path or command.

| Record | Contents | Allowed delivery namespace |
| --- | --- | --- |
| `addons/pipecat/oidc` | `clientSecret` | `auth-keycloak`, `pipecat` |
| `addons/pipecat/turn` | `sharedSecret`, 64 lowercase hexadecimal characters | `pipecat` |
| `addons/pipecat/bots/<id>` | Selected `gatewayApiKey`, `sttApiKey`, `ttsApiKey` | `pipecat` |
| `addons/pipecat/machines/<id>` | Machine `clientSecret` | `auth-keycloak` |
| `addons/pipecat/verifiers/<id>` | `gatewayApiKeySha256` only | `auth-keycloak-api-key-bridge` |

OIDC is required when there is a protected bot and can be explicitly enabled with `oidc.enabled: true` for optional sign-in when all bots are anonymous. The add-on must configure callbacks, runtime OIDC and delivery for the same selection. The browser client is `pipecat`; protected bots never receive a managed gateway key. TURN is active only with `turn.enabled: true`; managed secret ownership is a separate selection. Page-only bots can omit model grants while sessions are disabled. Each managed anonymous bot must select its canonical `modelId` and unique `gatewayPermissions`: `llm:invoke`, that model's invocation role, and only explicitly approved provider company-use roles. Setup does not grant permissions; the add-on's reviewed machine provisioning owns Keycloak roles and scopes.

The exact managed gateway/machine/verifier predicate is: `pipecat.enabled == true` AND `pipecat.secretSync.enabled == true` AND `bot.anonymous == true` AND nonempty validated `bot.gatewayPermissions` AND (`gatewaySecretRef` absent OR exactly `{name: pipecat-bot-<id>, key: gatewayApiKey}`). `sessionsEnabled` is not a preparation gate. External gateway Secret references select no managed gateway key, machine secret/client, grant descriptor, verifier or bridge registration, even if permissions are present; every add-on template must use the same predicate. Explicitly selected speech keys remain independent.

Speech keys are selected only by an exact `secretRef: {name: pipecat-bot-<id>, key: sttApiKey}` or the corresponding `ttsApiKey`. Setup prompts privately for missing external speech-provider keys during reconciliation; it never invents those credentials or imports keys from another application's Secret. Other existing external gateway/speech Secret references are not provisioned. OIDC and TURN managed references must match the published add-on interface.

## Operator order

1. Stage the add-on's namespace package, then matching non-secret client/product values in all required namespaces, scoped SecretStores/service accounts, and the setup package's ExternalSecrets. Keep applications and machine provisioning that need missing credentials inactive. Select the grant/verifier pairs through the bridge's `managedRegistrations`. Do not activate a machine provisioner until its reviewed Tooling image enforces the required exact role/scope reconciliation and disables full scope.
2. With both custodians approving and present, run `uv run --frozen stack-setup reconcile --context "<context>" --client "<client>" --custodian-package "<first-package>" --custodian-package "<second-package>"` from the selected CLI directory. Supply missing selected speech keys at protected prompts. Existing valid credentials are preserved using CAS; mismatched verifiers, invalid records, and concurrent-write failures stop rather than rotate credentials. Retrying after a partial write preserves the first generated key.
3. After temporary root revocation, setup verifies selected credential delivery. Check ExternalSecret readiness and target Secret metadata without reading values. Reconciliation does not activate Pipecat applications, provision Keycloak clients, or prove inference/media availability.
4. Separately approve and activate the add-on's setup consumers and app. `stack-setup addon refresh pipecat --context "<context>" --client "<client>"` refreshes existing selected Secret delivery, then compiled consumer releases, then derived `deployment/pipecat-<id>` workloads and selected TURN. It generates no credentials and performs no database work. Restarts discard in-memory sessions; use a maintenance window rather than a staged rollout when appropriate.

Deselection removes Pipecat read grants but retains stored credentials. No database, Redis, or session-signing secret is created. Public TLS, TURN routing, browser media, and model availability require separate approved acceptance checks.
