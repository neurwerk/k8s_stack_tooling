# Release manager

Run releases from the trusted workstation, not an operations VM or cluster. This CLI uses the existing lightweight `platform-release` environment; help does not require the shared SDK or setup dependency lock.

## Start

```bash
cd /absolute/path/to/k8s_stack/tooling/cli_tools/platform_release
cp -n .env.example .env
# Edit .env once: WORKSPACE_ROOT=/absolute/path/to/k8s_stack
uv run release-manager
uv run release-manager base
uv run release-manager tooling
uv run release-manager setup
```

In a terminal, the bare command shows a short introduction and a Base/Tooling/Setup/Exit target menu. Each target offers Show status, Check, Sign and publish, Verify publication, and Back. Choosing a target or Back does not access repositories or the network. Outside a terminal, the bare command prints help and exits successfully; explicit `--help` is always offline and noninteractive. Direct commands remain available.

The optional `.env` is loaded automatically from this CLI project's current working directory, identified by its `platform-release` project metadata. This works with installed and editable packages; it does not depend on the package's installation location. Missing `.env` is fine. The CLI does not search ancestors, inspect sibling configuration/credentials, or load another directory's `.env`. No `uv --env-file`, `UV_ENV_FILE`, or export step is required. Keep `.env` local and ignored; `.env.example` contains only nonsecret location configuration. The original `platform-release` command's startup behavior is unchanged.

Location precedence is `--repo` first, then an existing process `WORKSPACE_ROOT`, then `.env`'s `WORKSPACE_ROOT`. Existing process variables are never overwritten. Use an absolute workspace root containing `base/` and `tooling/`. Without a known location, an interactive action asks for the repository path; a noninteractive action fails clearly instead of guessing the current Git repository. No source code is imported from sibling repositories. Use existing `platform-release` commands for Base release preparation, notes, and PR selection.

## Explicit actions

```bash
uv run release-manager tooling status
uv run release-manager tooling check --commit <reviewed-full-SHA>
uv run release-manager tooling publish --commit <reviewed-full-SHA>
uv run release-manager tooling verify --commit <reviewed-full-SHA>
uv run release-manager setup check --commit <reviewed-full-SHA>
uv run release-manager setup publish --commit <reviewed-full-SHA>
uv run release-manager setup verify --commit <reviewed-full-SHA>
uv run release-manager base check --pr <release-PR-number>
uv run release-manager base publish --commit <reviewed-full-SHA> --tag vX.Y.Z
uv run release-manager base verify --tag vX.Y.Z
```

Status is informational, not verification. Python targets derive their version/tag from committed project metadata and matching lockfiles: `vX.Y.Z` for Tooling and `stack-setup-vX.Y.Z` for setup. Check/publish/verify require an exact reviewed commit already in the remote default branch's history. Fetch and isolated linked worktree preparation preserve the original files/index; dirty or occupied release destinations stop without cleanup. Trusted repository checks run as local code, not in a sandbox.

Base delegates to the existing protected staging/publication flow. Its publication requires the reviewed current remote default tip and retains Base's release-PR, signature, provenance, and full check gates. Base verification delegates to its existing publication verifier.

Tooling/setup check runs the target's existing lock, lint, format, type, test, and build commands (or repository `make check` when supplied), then requires full repository `Required CI` from GitHub Actions on that exact commit. Tooling checks need the pinned render/Node tools used by repository CI. Setup additionally requires its canonical published SDK wheel URL and a lock hash matching the actual published wheel and `SHA256SUMS`; candidate overrides cannot establish release readiness.

## Signing, publication and retries

Use the approved SSH-agent signer in the shared [release procedure](https://github.com/neurwerk/docs/blob/main/dev/operations/image-releases.md). Only its public `.pub` file is accepted; the default is `~/.ssh/neurwerk_base_release_ed25519.pub`, overridden by `--signing-public-key`. The private key stays in the SSH agent.

Publication displays the exact repository, tag, and reviewed commit and requires `PUBLISH OWNER/REPO TAG COMMIT`. Noninteractive publication must supply that phrase with `--confirm`; the command itself authorizes trusted local fetch/check preparation. It pushes only the signed immutable tag, which triggers the existing owning workflow. It does not merge PRs, move branches, publish from local uncommitted files, or deploy anything.

An existing remote tag is never replaced or pushed again. Inspect its exact workflow and choose verify; failed publication retries use the same immutable GitHub workflow/source, not a new tag object. An unpushed local tag may be reused only after exact commit and approved-signature verification. A conflicting tag or changed repository stops the operation.

Verification checks the exact local/remote signed tag object, successful exact-tag/commit workflow, wheel/source metadata and published asset checksums. Tooling also verifies the image's immutable digest, Linux/amd64 platform, and source/revision/version labels; Docker Buildx is required for that read-only registry inspection. Setup verifies its published SDK dependency again. Publication and verification never imply client adoption or cluster reconciliation.
