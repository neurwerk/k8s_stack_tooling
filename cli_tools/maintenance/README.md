# Maintenance CLI

Install with `uv tool install .` on a trusted workstation with kubectl and trusted
cluster/HTTPS certificates. Provides `kubectl-maintenance` and `maintenance`.

```sh
maintenance on studio --context example
maintenance on global --context example
maintenance status --context example
maintenance off global --context example
maintenance off studio --context example
```

Scope defaults to `global`; approved products are read from Base's
`maintenance-runtime` ConfigMap in namespace `maintenance`. Global and product
routes persist independently. Base owns image approval, manifests, host inventory,
Service and Traefik prerequisites. The CLI checks v1 identities/scopes and the
versioned image digest, not Kubernetes schemas or server-defaulted settings.

Every on/off atomically acquires `maintenance-operation-lock`. On reuses the
owned Deployment without changing settings, requires the Base image, waits for
rollout and a ready endpoint, then creates the selected route. HTTPS verification
requires trusted TLS, status 503 and `X-Platform-Maintenance: true` on every
selected host. No redirects, credentials or TLS bypass are used.

Off deletes only the selected owned route with UID/resourceVersion preconditions.
Remaining scopes retain the backend. After the last route, HTTPS marker absence,
a five-second grace, and a fresh all-namespace reference inventory gate deletion
of the owned Deployment. Unknown/indirect Service references or listing failures
retain it. Normal applications may return redirects, 401 or other statuses.
Off permits an older image; repeat `off global` to clean an orphaned backend.
The CLI never deletes the platform Service or Base ConfigMap.

Locks never expire or get stolen. After a crash, **independently confirm the owning
operator process is dead** before manually deleting its lock:

```sh
kubectl --context example -n maintenance get configmap maintenance-operation-lock
kubectl --context example -n maintenance delete configmap maintenance-operation-lock
```

Never remove a running operator's lock. Other kubectl writers must respect it;
Kubernetes cannot make route inventory and Deployment deletion one transaction.
On any failure, inspect status before retrying. Persistent routes do not expire.

## Checks

```sh
uv lock --check
uv sync --frozen --dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen pytest
uv build
```

Tests mock kubectl and HTTPS. Required coverage is 80%.
