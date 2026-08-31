# Media Downloader Uploader

Manage a manually curated Hugging Face catalog and verified model files on external storage. The
application does not discover or synchronize catalog entries from Hugging Face; new choices are
reviewed and added to the packaged `src/media_downloader_uploader/available.yaml` deliberately.

## Files

```text
tooling/cli_tools/media_downloader_uploader/
└── src/media_downloader_uploader/
    └── available.yaml                     # packaged curated NER and LLM catalog

/path/to/external-media/
├── download.yaml                          # selected catalog variants waiting for download
├── installed.yaml                         # verified model variants present on this drive
├── huggingface/                           # HF cache and user authentication
└── models/<category>/<owner>/<model>/<revision>/<variant>/
```

PII releases are staged temporarily and published to
`s3://pii-models/<bundle-version>/` in the Rook Ceph RGW store.
The uploader retrieves the publisher credentials from the Kubernetes Secret with
`kubectl`, starts a temporary port-forward to the internal RGW Service, and
removes the port-forward after the upload. Credentials are never written to the
external drive or `.env`.

`available.yaml` holds reviewed metadata for each model and variant:

- Hugging Face source and revision
- Category, description, and recommended status
- License name, upstream URL, commercial-use status, and notice requirement
- Architecture, parameters, context length, languages, and intended use
- Variant format, quantization, precision, estimated download size, and file filters

The current Presidio NER models are marked `(recommended)` in the download selector. Each PII
catalog entry also defines a stable runtime alias and reviewed language metadata.

`download.yaml` and `installed.yaml` are external-drive state files. They travel with the model
disk and must not be committed to the application repository.

## Setup

1. Install the development environment:

```bash
uv sync --dev
```

2. Mount the external drive at a location appropriate for the workstation.
3. Copy `.env.example` to `.env` and set both
   `MEDIA_DOWNLOADER_UPLOADER_STORAGE_ROOT` and `HF_HOME` to explicit paths inside that external
   volume. There are no machine-specific defaults.
4. Set `MEDIA_DOWNLOADER_UPLOADER_KUBE_CONTEXT` before using upload behavior. Downloads do not
   require cluster access.

## Usage

```bash
uv run media-downloader-uploader
```

Select `Hugging Face authentication` in the menu. The application runs `hf auth login` with the
configured external `HF_HOME`, so both the token and cache remain off the laptop disk.

`Select models for download` is a terminal selector: use arrow keys to navigate, Space to
select/deselect, and Enter to write `<storage-root>/download.yaml`. Each selectable row
displays category, format, quantization, recommendation/gated status, estimated size, and license.
Gated models require explicit confirmation and still require the user to accept upstream access and
license terms on Hugging Face.

`Download selected models` processes only entries in `download.yaml`. Successful verified downloads
are removed from the queue and recorded in `installed.yaml`; failed selections remain queued for a
retry. The app resolves the selected revision, downloads into a temporary sibling directory, asks
the Hugging Face CLI to verify the local snapshot, calculates SHA-256 checksums for every stored
model file, and atomically publishes only complete content.

GGUF variants use explicit include patterns, so only the selected quantization is downloaded rather
than all files in the source repository. The local Hugging Face `.cache` metadata directory is not
considered model content and is excluded from the application checksum manifest.

## Integrity

Every installed model variant contains:

```text
artifact.yaml       # source, catalog IDs, requested/resolved revision, file metadata
checksums.sha256    # SHA-256 for all stored model files
```

The app reuses an existing variant only if its stored metadata and checksums remain valid.

## Uploading A PII Bundle

Download any PII transformer models needed by the client, then select
`Upload PII model bundle to Ceph RGW`. Enter a new
bundle version and use Space to explicitly select one or more of the installed PII variants. Only
those variants are released. The selector shows each stable runtime alias and its language codes;
it does not assume a fixed four-model set or fixed aggregate size.

The tool verifies the original installed downloads, hard-links the selected files below their
stable aliases in a temporary directory on the external drive, and generates the release manifest
and checksums. Hard links avoid retaining a second full copy of the model data. Existing RGW
versions are immutable and cannot be overwritten. An atomic conditional-write lock gives one
publisher ownership of each version, and the owner refreshes it between uploads. Model files and
checksums upload first;
`manifest.yaml` uploads last as the completion marker. Remote object sizes and SHA-256 metadata are
then verified. Temporary staging is removed after both successful and failed uploads; the original
verified downloads remain installed for future per-client releases.

The release manifest is `schemaVersion: 2`. It binds the exact bytes, size,
file count, and aggregate model size of `checksums.sha256`; the checksum index
then binds every model file. The Git/Helm manifest digest is therefore the
complete bundle identity rather than only model metadata. The tool prints the
manifest digest to pin after successful publication. Its `models` mapping can
contain any non-empty subset of configured PII transformer aliases. Every
selected entry retains `catalogId`, `variantId`, `path`, immutable upstream
revision, license details, and `supportedLanguages`, which `pii_engine` uses to
resolve per-language model aliases. Consumers must not assume all four
historical aliases are present; client configuration must reference aliases
included in that client's release.

The explicitly configured Kubernetes context must reach the cluster and the Rook publisher Secret
must exist. The tool passes `--context` to every `kubectl` command and never relies on the active
context. If an incomplete prefix from a prior attempt already exists, cleanup requires typing that
selected context before any objects are deleted. Failed locks are immediately recoverable;
abandoned locks become recoverable after the configured stale interval. Every uploaded object
records its publisher ID, and rollback deletes only objects that still belong to that failed
publisher. If `kubectl`
or the local `127.0.0.1` port-forward is
unavailable, the operation stops with an instruction to enable port-forwarding
and retry.

## Quality gates

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
uv build
```
