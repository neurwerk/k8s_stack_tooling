# Add-on helpers

Add-on commands keep product inputs and native behavior local and call shared public helpers for reusable work. Follow the shared [ownership and dependency rules](https://github.com/neurwerk/docs/blob/main/dev/conventions/addons.md) and [tooling layout](https://github.com/neurwerk/docs/blob/main/dev/conventions/structure.md#add-on-tooling-layout).

## Definitions and source selection

The public [`k8s_stack_tooling.addons`](../../src/k8s_stack_tooling/addons/__init__.py) API exposes `load_manifest`, `load_selection`, `verify_selection`, and `selection_projection`. Validation uses one shared model for the add-on release manifest and client selection. Parsing alone does not authorize execution: check every selected source and its definitions, and compare the staged selection before privileged work. Keep privileged setup separate from product code; product helpers never receive OpenBao root credentials.

[`addon-acquire`](../../src/k8s_stack_tooling/addons/acquisition.py) takes `--client`, `--cluster-dir`, and `--cache` to populate a deterministic source cache. Acquisition uses caller Git authentication; local access does not establish CI access. Existing cache entries are verified, not updated or overwritten. Offline verification does not fetch sources, and acquisition is neither tag-signature verification nor release approval. Keep the same client selection, cluster directory, and cache when validating sources and generating the staged projection.

Use the exact shared SDK artifact and genuine dependency lock required by the consuming package. SDK publication and a passing candidate check do not establish availability of a dependent setup or product release. Follow shared [release prerequisites](https://github.com/neurwerk/docs/blob/main/dev/operations/image-releases.md#prepare-and-publish).

## One-shot Jobs

The public helpers in [`k8s_stack_tooling.api.jobs`](../../src/k8s_stack_tooling/api/jobs.py) accept caller-owned Job templates and API clients. The product owns permissions, input validation, and safe output summaries.

- `job_api_client(context=..., config_file=None)` is a context manager requiring an explicit, nonempty kubeconfig context. It disables transport retries and debugging, does not persist credentials to kubeconfig, redacts client-construction failures, and closes the client on exit. Use its client for both `CoreV1Api` and `BatchV1Api`. Injected alternatives also need transport retries disabled. Operations inside the context retain their own error handling.
- `require_configmap_versions(core, expected)` checks previously reviewed ConfigMap UIDs and resource versions. This is a preflight check, not a cluster transaction.
- `submit_snapshot_job(core, batch, job=..., snapshot_data=..., inputs=...)` creates an immutable, nonsecret input ConfigMap, submits the explicitly named and namespaced Job once, and attaches the snapshot to it. The Job template references a ConfigMap of the same name. `Submission` reports `submitted` or `uncertain`, the phase reached, and any known Job UID. An uncertain result can mean work is already running: do not automatically resubmit or delete it.
- `wait_for_job(batch, namespace=..., name=..., uid=..., seconds=...)` polls that exact Job and returns `complete`, `failed`, `timeout`, or `uncertain`. It does not retry failed API requests. The wait budget bounds request timeouts but is not a strict wall-clock guarantee.
- `read_job_summary(core, namespace=..., name=..., uid=..., container=..., summarize=...)` checks Pod ownership and passes bounded logs to the product's trusted redactor. The callback returns only allowlisted safe fields, or `None`. `LogSummary` reports `available` or `unavailable`; API and callback failures suppress exception text. Never return or print raw logs from the callback.

`JobError` carries redacted input/precondition errors before submission. These helpers do not log response bodies or retry uncertain writes. Kubernetes workload retry behavior remains part of the caller's reviewed Job template.

## Native payloads

[`k8s_stack_tooling.api.payloads.stage_payloads`](../../src/k8s_stack_tooling/api/payloads.py) copies declared files from an explicit package resource directory to `/work/payloads` by default. It does not import or execute payload code. The caller uses a separately pinned application image when execution needs native application dependencies.

Declare 1–128 unique relative resource paths, each at most 1 MiB. The helper rejects traversal, symlinks, and nonregular files. New files are read-only (`0444`); existing files are accepted only when their content matches and they are read-only with no additional hard links. Staging preserves relative paths. `PayloadError` is redacted; partial output can remain because multi-file staging is not atomic and failed writes are not retried.

### Thin product example

This synthetic wrapper stages one packaged resource. The product owns `payloads/configure.py`; shared Tooling owns copying and path checks.

```python
from importlib.resources import as_file, files
from pathlib import Path

from k8s_stack_tooling.api.payloads import stage_payloads


def stage_example_payloads(destination: Path) -> tuple[Path, ...]:
    resources = files("example_tooling").joinpath("payloads")
    with as_file(resources) as source:
        return stage_payloads(
            source, ("configure.py",), destination=destination
        )
```

Include the resource in the product package. Keep its directory available for the duration of staging, as the context manager does here. The wrapper neither submits a Job nor executes the payload.
