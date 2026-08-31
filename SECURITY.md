# Security Policy

## Reporting A Vulnerability

Do not open a public issue for a suspected vulnerability. Report it through
[GitHub private vulnerability reporting](https://github.com/neurwerk/k8s_stack_tooling/security/advisories/new).
Include the affected component and version, reproduction steps, impact, and any
suggested mitigation. Do not include live credentials, recovery shares, static
seal material, private keys, customer data, or production endpoints.

We will acknowledge a complete report as soon as practical, investigate it,
and coordinate remediation and disclosure with the reporter. Please avoid
public disclosure until a fix or mitigation is available.

## Supported Versions

Security fixes are applied to the latest release and the `main` branch. Older
releases may require upgrading to receive a fix.

## Operational Security

- Treat Kubernetes contexts as explicit trust boundaries. Review the selected
  context before any operation that reads credentials or changes remote state.
- Keep `.env` files, provider credentials, tokens, and downloaded private data
  out of Git.
- Keep OpenBao custody roots outside every Git repository and multi-repository
  workspace. Workspace detection uses sibling Git worktrees or the explicit
  absolute `OPENBAO_STACK_SETUP_WORKSPACE_ROOT`. Custodian packages, recovery
  shares, private keys, and static seal checkpoints are sensitive even when
  encrypted or stored in an ignored path.
- Do not include secrets in command arguments, logs, issues, or vulnerability
  reports.
- The OpenBao operator CLI is intended for a trusted workstation. Its recovery
  and bootstrap commands are privileged operations and should follow the
  repository documentation and an organization-approved custody process.

Dependencies and upstream model artifacts retain their own security and
licensing considerations. Review them before use in a production environment.
