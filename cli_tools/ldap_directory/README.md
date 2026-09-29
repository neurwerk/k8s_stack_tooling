# LDAP Directory

Read-only diagnostic for all usernames below the configured `usersDn` and all
groups below `groupsDn`. This is a workstation CLI, not a Keycloak user import.

From this directory, install and run:

```bash
uv sync --frozen --dev
uv run --frozen ldap-directory
```

Use `--context <kube-context>` to skip the context menu. Without it, only
reachable contexts with `authKeycloak.activeDirectory.enabled: true` in the
live `auth-keycloak/keycloak-product-values` ConfigMap are offered. Choose
an eligible published Tooling image (newest first), then Users, Groups or Both.
Image choices come from the exact version and digest recorded in published
Tooling GitHub Releases; the CLI never guesses an image from an unverified tag.
The image must contain `ldap-directory-worker` (Tooling 0.7.3 or later). When
no such image has been published, the CLI explains what is missing instead of
creating a Pod. An image build or PR alone does not make the image available;
publish and verify the version first. The workstation must be able to read
public GitHub Releases.

The workstation needs `kubectl` and access to read ConfigMaps in `flux-system`
and `auth-keycloak`, and to create/get/exec/delete Pods and create/delete
NetworkPolicies in `auth-keycloak`. The Pod mounts the existing
`auth-keycloak-active-directory-secret` delivered from OpenBao; the workstation
does not fetch the password or read Kubernetes Secret data. For LDAPS, the Pod
also mounts the configured CA ConfigMap. Plain LDAP is used only if the live
configuration explicitly enables it. The temporary egress rule allows DNS and
only the configured LDAP CIDRs and port; the Pod and rule are deleted after the
query, including on normal errors or Ctrl-C. If the workstation dies suddenly,
delete any leftover `ldap-directory-*` Pod and NetworkPolicy after verifying
their names; the Pod stops itself after ten minutes.

Directory names are displayed only on the workstation. Do not save or share
the output as an ordinary log. This tool does not modify LDAP, Keycloak,
OpenBao, or existing workloads. No live cluster is contacted by installation
or quality checks.
