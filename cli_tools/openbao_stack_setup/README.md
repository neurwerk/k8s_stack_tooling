# OpenBao Stack Setup

`stack-setup` is the trusted-workstation CLI for OpenBao custody, bootstrap, credential/access preparation, recovery verification, and explicit delivery operations. It is separate from the Tooling container and shared SDK release.

Use the reviewed `stack-setup-vX.Y.Z` source with its committed lock and published SDK dependency. Run from `tooling/cli_tools/openbao_stack_setup/`:

```bash
uv run --frozen stack-setup --help
uv run --frozen stack-setup reconcile \
  --context <context> --client <identity> \
  --client-root /path/to/client --source-cache /path/to/acquired-sources \
  --cluster-dir clusters/example --operation-id <nonsecret-operation-id> \
  --custodian-package /secure/first-package.zip \
  --custodian-package /secure/second-package.zip
```

Complete installable source verification and the staged selection comparison precede custody. Optional registry inputs are checked only for selected native work or relevant explicit lifecycle targets. No credential values are command arguments. Human-password delivery uses the controlling terminal and durable acknowledgement; interrupted reconciliation requires the same operation/selection and explicit stopped-process confirmation.

Ordinary preparation preserves valid keys and does not provision or restart applications. Use separate `addon refresh`, `addon provision`, and `addon restart` commands with the verified source arguments. Data retention and custody safeguards remain in force; accepted downtime does not authorize storage resets.

The authoritative procedures are shared [OpenBao operations](https://github.com/neurwerk/docs/blob/main/dev/operations/openbao.md) and [recovery custody](https://github.com/neurwerk/docs/blob/main/dev/operations/recovery-custody.md). See [release adoption](../../docs/releases.md) for the separate SDK/setup publication boundary and the selected release's exact prerequisites. Unsupported older add-on definitions and historical add-on record migrations have no fallback.
