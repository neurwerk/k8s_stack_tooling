# Platform Release

Standalone trusted-workstation CLI for Base's existing release workflows. It is
not included in the tooling image. Requires Python 3.12+, uv, Git, authenticated
`gh`, OpenSSH, `pre-commit`, and all tools required by the selected Base checkout's `make check`.

```bash
uv sync --frozen --dev
uv run platform-release --base-repo /path/to/trusted/base
uv run platform-release --base-repo /path/to/trusted/base --json status
uv run platform-release --base-repo /path/to/trusted/base --json status --tag v1.2.3
uv run platform-release --base-repo /path/to/trusted/base --json plan
uv run platform-release --base-repo /path/to/trusted/base status --diagnostics --tag v1.2.3
uv run platform-release --base-repo /path/to/trusted/base status --changelog --tag v1.2.3
```

Select a trusted public Base repository explicitly. Its scripts and Makefile are
executed as trusted code, not sandboxed. Before fetching or executing repository
scripts, the CLI verifies a public GitHub repository and one identical origin
fetch/push URL. Do not select untrusted repositories or workstation Git configuration.

`prepare` and `publish` ask once for permission to fetch origin's default branch
and tags and create/reuse an isolated linked checkout. The original checkout may
be dirty: its branch, index and files are never switched, reset or cleaned.
The fetch downloads Git objects, writes `FETCH_HEAD` and atomically fetches shared
tag refs without force, pruning or tag overwrites. It never updates origin tracking
refs, including through configured fetch mappings. Before fetching, it snapshots
the existing default-branch tracking commit, when present; that commit must be an
ancestor of the fetched target. Rewritten history (including a rewind behind the
tracking commit) or unverifiable ancestry blocks checkout creation. Objects,
`FETCH_HEAD` and newly fetched tags can remain after this rejection, but the
tracking ref and original working files remain untouched. A conflicting local tag
blocks the fetch for manual investigation; linked worktrees share those tags.

Checkouts are named `platform-release-OWNER-REPO-COMMIT` under a sibling `worktrees/`
directory (reuse the workspace's `worktrees/` when selected from there). Override
the parent with global `--worktree-root /chosen/directory`, outside the original
and primary checkouts. A clean registered checkout at that exact commit is reused;
a dirty or occupied destination blocks without cleanup or replacement. Advancing
main creates another checkout, leaving previous work intact. No automatic removal
occurs. Publication prepares its own exact clean release checkout the same way.

The latest published tag is verified against its remote signed annotated object.
No unreleased local VERSION is used for the next-patch default. Read-only status
and plan require that tag already be available locally; they never fetch it.

Human status shows the release PR URL, draft/ready state, and failed/pending/passing
required checks correlated with the current PR head and latest `Required CI` run.
It gives one next action. Green CI alone does not validate the release notes or
included-change record. The local checkout's behind/ahead state is not the source
included in an open release PR; full checkout details remain in diagnostics/JSON.
Status also shows publication state and URL, verified local signed identity,
remote final/staging matches, and exact-tag verifier status. A published GitHub Release
is not a claim that the artifact-correlated pipeline has been verified by this inspection.
Use `continue` for that separate check; expired artifacts can prevent verification of
an already published release. Tag presence alone never implies pending approval.
`status --diagnostics` shows detailed metadata and explicitly uncorrelated workflow
candidates; `status --changelog` shows release notes. `--json status` retains the full
report, including the release body. These views are also available in the menu.

Status and plan compare HEAD with the exact remote default-branch tip from read-only
`git ls-remote`, not a possibly stale remote-tracking ref. Equal, behind, ahead and
diverged use available local ancestry; absent remote objects, failed checks or
incomplete history report unknown with guidance. No automatic or lazy fetch occurs.
New releases automatically prepare from a clean linked worktree at **current remote
main** (or the configured default branch), not an old published-tag checkout.
The menu returns after status, diagnostics, changelog, plan, check and continue.
Cancellation exits safely; prepare and publish confirm their exact targets and exit
after one operation rather than returning to a mutation that could be repeated.

## Lifecycle

Startup prints a short guide, also available offline with `guide`. The single
interactive menu has two visual sections: **Release Steps** contains
**1. Prepare** (`prepare`), **2. Review notes** (`finish-notes`),
**3. Check** (`check`), **4. Merge on GitHub** (`merge`), and **5. Publish** (`publish`).
There is a blank separator before **Information**.
**Information** contains **Release status**
(`status`), **Changelog** (`changelog`), **Preview next release** (`plan`),
**Publication progress** (`continue`), and **Diagnostics** (`diagnostics`).
**Exit** returns `quit`. The default selection is always **Release status**, not
the first release step.
Use the same primary Base path and command for each step:

```bash
uv run platform-release --base-repo /path/to/trusted/base
```

The CLI creates the needed worktrees; do not switch your original checkout.
After preparation, finish the notes, upload them, validate, and manually merge the
reviewed draft PR on GitHub before publishing;
Release status shows PR links when available from its existing inspection.

1. `prepare`: the Questionary wizard suggests the next patch of the latest verified
   published release, or accepts a custom newer version. Its preview shows the exact
   repository, requested tag, verified published predecessor, selected date and summary,
   and full observed default-branch source SHA. Only the committed `## [Unreleased]`
   changelog section is shown, preserving subsections and fenced code and stopping at
   the next level-two heading. Missing or empty sections are reported explicitly, not
   replaced by old release notes. No predecessor manifest or migration is displayed.
   An interactive Yes/No confirmation defaults to No and authorizes only this previewed
   preparation. It dispatches
   `Prepare Release PR` with `successor` and the exact predecessor. Existing release
   branches, PRs, final tags and staging tags block preparation. The predecessor
   must equal both checkout `VERSION` and Base's latest reachable release tag;
   the CLI calls Base's existing `latest_release_tag()` through its trusted script.
   A prepared but unpublished release blocks starting another one. Preparation creates
   a draft PR; it does not sign, publish or deploy. Repository identity and the captured
   source commit are revalidated after confirmation and before dispatch.
2. **Review notes** shows existing release entries once and offers a simple
   multiline main-notes edit, then **Add special notes or upgrade instructions? [y/N]**.
   Optional notes default to None and are omitted. Local drafts are saved in the
   separate editable worktree without another save question. Review the final notes
   preview and answer **Update release PR?** (default **No**).
   Choosing Yes runs release checks and the confidentiality guard, commits only
   the reviewed evidence, and pushes to the existing PR branch. You do not need
   to stage, commit or push manually. Choosing No keeps the notes local for later.
   Proceed to step 3 only after the tool reports **UPDATED release PR**.
3. Use **3. Check** (`check`) while the draft PR is open. It discovers
   same-repository `release/vX.Y.Z` heads targeting the default branch, automatically
   selects a single PR, and requires a tag-and-PR-labelled choice when multiple are
   open. `check --tag vX.Y.Z` or `check --pr NUMBER` selects explicitly. No matching
   open PR is an actionable error, never a fallback to the original checkout's VERSION.
    Notes, summaries and migration files are optional; no headings are required.
    Explicit policy declarations must agree with Base's manifest. When
   ready, both full checks run. After success, follow the printed PR URL, resolve
   reviews, wait for required CI and approvals, and merge manually. This is a
   required handoff, not an automatic step. Then `publish` prepares the exact
   merged checkout at the remote default tip automatically.
4. **Merge on GitHub** (`merge --tag vX.Y.Z`) shows the actual release PR URL,
   draft/ready state and current checks. Choose **Ready for review** on GitHub,
   wait for green checks, resolve required reviews, then merge there. This command
   only reads status; it never marks ready, approves, bypasses checks or merges.
5. `publish`: requires the exact merged same-repository release PR targeting the
   default branch, successful actual required checks, and the latest `Required CI`
   check on its exact head. GitHub's branch protections and review policy govern
   merging; the CLI adds no human-review count, distinct-reviewer requirement or
   collaborator-permission lookup. It never uses `--admin` or bypasses checks. It verifies
   the public key fingerprint and exact ssh-agent fingerprint against the fixed
   approved signer, runs full checks, then requests an exact repository/tag/SHA
   confirmation. It creates a local signed annotated tag, runs post-tag release
   and signature checks, pushes only its staging object and dispatches
   `Create Platform Release Tag`. It waits for the exact remote object, polls
   exact-target verification and publication evidence to completion, then verifies
   the non-draft stable GitHub Release. Publication is a `workflow_run` on the default
   branch, so it is correlated by Base's `published-platform-release` identity
   artifact, not by a guessed target-SHA filter. That artifact expires after one
   day; missing/expired evidence fails closed. Protected approvals remain manual.

Signing uses only `~/.ssh/neurwerk_base_release_ed25519.pub` by default and ssh-agent.
An alternate public `.pub` path must have the same approved fingerprint. There is
no arbitrary signer override, private-key access, final-tag push, direct GitHub
Release creation, cluster access or client adoption command. Base publication may
trigger its separately configured draft adoption workflow; this CLI does not change
or bypass that configuration.

### Review Notes

```bash
uv run platform-release --base-repo /path/to/trusted/base finish-notes --pr 42
```

An interactive terminal is required. Selection and trust authorization use the same
open, same-repository, default-target PR rules as validation; `--tag vX.Y.Z` also
works. The fetched SHA must match the observed PR head. The editor uses a separate
`platform-notes-OWNER-REPO-PR-SHA` linked worktree on `release-notes/PR-SHA`, never
the detached validation snapshot. Retrying at the same head preserves uncommitted
edits. A moved remote head gets a new checkout, leaving old work for manual review.
A switched edit branch blocks reuse rather than resetting it. A single clean
evidence-only commit directly on the unchanged PR head can be reviewed and pushed
again without another commit, for example after a transport failure. Any other
local history requires manual review; the tool never infers ownership from a
commit subject and never resets or amends it.
Uncommitted changes outside the five evidence files block generation without cleanup.

Base's config, schema and inventory generator supply the release contract.
The helper preserves the existing policy, alpha revisions and recovery classification,
including `supported`, empty alpha revisions and `forward-fix` preparation defaults.
These are declarations, **not acceptance evidence**. Empty or missing summaries
stay optional, and absent migration files are not recreated. Non-text summary values
such as booleans and lists are rejected rather than converted into release prose.
The manifest is generated
by Base, not assembled independently by Tooling.

Public notes are intended to be `## vX.Y.Z` plus the selected existing changelog body,
normally a few short bug bullets. No AI or inferred summary is generated. Existing
long notes stay unchanged unless the operator chooses to edit them. Multiline input
ends with a line containing only `.`; blank input keeps the main notes. Special notes
are optional; blank, None, NULL or n/a adds nothing, not an empty heading.

Old generated scaffolds can be converted only after an exact conversion diff and
explicit default-No consent. Only exact generator placeholders and whole known
scaffold sections whose entire body is a placeholder (n/a, NULL or TODO) are removed.
Literal values mixed with authored instructions or under unknown headings remain;
a generic standalone value is not assumed to be generated. Authored
breaking changes, manual procedures, contradictory prose, unknown TODOs and fenced
or indented code remain. Internal Support/Recovery declarations remain; old Base's
required Breaking Changes section points to the actual changelog, not invented evidence.
Unresolved authored TODOs and contradictions still need manual review and validation.

The CLI probes the selected Base renderer using a synthetic temporary fixture, not
a version assumption. An older renderer produces a rollout warning: compact GitHub
publication requires merging the Base renderer change and refreshing the release
branch. This check also runs when retrying a pending evidence commit. Old-renderer
previews are labelled **Proposed notes (not actual publication output until Base updated)**,
not presented as actual publication output. A failed capability probe blocks preview/upload.
Manifest/client metadata and compatibility/adoption gates remain in place.

Only the five release evidence paths are eligible; the notes writer does not change
VERSION. Concurrent file edits or a moved/closed PR stop the save. Saving alone never
authorizes a commit or push. Local drafts are not a validation result.

**Optional upload:** one final release-notes preview precedes the default-No update
question. Git status, staged/unstaged diffs and recent history are still inspected
internally; `--verbose` exposes inspection details instead of dumping them repeatedly
in normal output. Review public text for private identifiers and unsupported claims.
A pre-existing staged index blocks upload
without including or unstaging that work. All five prepared evidence files must
already be tracked; untracked or unrelated implementation changes cannot be uploaded.

The tool discovers the trusted workstation executable
`.config/confidentiality-guard/public-pr-check` in the original Base checkout's
ancestry, not from the downloaded PR. An explicit global override is available:

```bash
uv run platform-release --base-repo /path/to/trusted/base \
  --public-check /path/to/trusted/public-pr-check finish-notes --pr 42
```

If the guard is missing or not executable, the tool keeps notes local and explains
how to configure it; no commit or push occurs. After Yes, configured `pre-commit`
hooks run on the five existing release files before staging. Formatting fixes show
changed filenames, offer a full diff and require renewed upload consent. A fixing
hook gets at most one retry; an unchanged failure is not retried. Unrelated changes
or staged work block without cleanup. Both `make check` and `make release-check`
must pass on the corrected files. Neither ordinary failure skips the other check.
The guard runs with `--scan-only` before committing,
on the staged evidence, and again after commit before push so commit messages are
also covered. The tool stages only the changed evidence paths, checks the staged
diff against the preview, uses normal commit hooks, and verifies the resulting tree
and single parent. It revalidates the original and editable repositories' origins,
open PR identity and exact remote branch head before commit and again before push.
The only remote write is `git push --atomic --no-follow-tags origin
COMMIT:refs/heads/release/vX.Y.Z`, pinned to the reviewed single-parent child,
without force, leases, admin overrides or history rewrites. A temporary push-scoped
`core.hooksPath` wrapper checks that receive-pack advertises exactly the authorized
old SHA for that one ref. It rejects deletion, rewind, advance and already-current
no-op races; receive-pack rejects subsequent ref changes when applying the update.
The existing executable pre-push hook receives its original arguments and exact stdin,
and its failure still blocks the push. Other hooks are forwarded through symlinks;
installed hooks and repository configuration are not edited.

On failure, local files, any staged changes and any commit stay available. A failed
commit hook prevents pushing; resolve its cause and inspect retained staged changes
before retrying. A clean single pending evidence commit on an unchanged PR head is
offered for upload again without a duplicate commit. If the remote moved, the tool
stops rather than rebasing or overwriting it. Check Release status before retrying a
push whose result is uncertain. Only a successful push produces **UPDATED release PR**.
No PR metadata edits, merges, tags or publication are part of this upload.

Run step 3 after upload: it still runs the full `make check` and `make release-check`
on the exact remote PR head. The pre-upload release check is not a substitute for
full validation. Do not rerun Prepare to upload notes. Merge remains a manual handoff
after step 3, through required CI, approvals and review resolution.

### Validate An Open PR

Check asks for permission to fetch and execute the selected **trusted, unmerged
same-repository code** at its observed SHA. Fork PRs and other base branches are
excluded. This is not a sandbox. Noninteractive check requires global
`--allow-local-preparation`, which also authorizes executing that selected code:

```bash
uv run platform-release --base-repo /path/to/trusted/base --allow-local-preparation \
  check --tag v1.2.3
```

`refs/pull/NUMBER/head` and the default branch are fetched from verified origin using
source-only refspecs and an empty refmap. No force, pruning, shared tags, branches or remote-tracking
refs are updated. Git objects and `FETCH_HEAD` are written. Normal pushes and rewritten
PR heads are supported by creating a new commit-specific worktree, not by moving an old
checkout or enforcing default-branch fast-forward rules on a PR. The fetched commit must
exactly match GitHub's observed SHA. The PR must still be open, same-repository, and at
that SHA after fetching, before validation, and after both checks. Any change requires
fresh selection; an old successful result is never reported as current-PR success.

Check asks Base's existing Git and generation helpers to recompute included changes
through the single latest shared ancestor of fetched main and the selected PR head.
It validates the predecessor at that boundary and compares actual config and generated
file contents, not CI color. Main commits absent from the PR are not listed as included.
An ambiguous history or missing predecessor blocks automatic correction.
Outdated files prompt **Update release files in a separate editable checkout?**,
default No. Only existing `release/config.yaml` and `release/manifest.yaml` are
regenerated; no Prepare rerun, VERSION change, scaffold replacement or inferred notes.
Authored summary, changelog, migration instructions and policy inputs are preserved.

Formatting runs in the existing notes-helper editable branch, never in the validation
snapshot. Check requires it to be clean at the selected PR head; prior notes edits or
a pending commit block without taking ownership. Use Review notes to finish those
sessions. Unchanged files require no correction/upload prompt. Corrections use the
same guarded, default-No upload as notes. Noninteractive Check cannot upload; it
fails with guidance if corrections are needed. After upload, Check reselects the
same PR, requires the uploaded commit, recomputes its files and runs full validation
in a fresh clean snapshot. A moved head never receives a stale success claim.

Check creates/reuses the same clean registered linked-worktree layout described above.
Dirty original checkouts remain untouched; dirty or occupied validation destinations
block rather than being cleaned. `VERSION` must match the selected release tag before
any make command. The header shows version, PR URL, full SHA and worktree, followed only
by that release's changelog section and evidence paths, not historical notes or a full
manifest dump. TODOs in the selected changelog, migration or release config stop
with **2. Review notes** guidance. Upload completed
notes and retry. Successful Required CI does not establish completed prose.

Once documentation passes this early screen, the full, unchanged `make check` and
pre-tag `make release-check` (without TAG) both run sequentially, even if the first
returns an ordinary failure. The early screen is not a substitute for either check.
The terminal shows Running and PASS/FAIL with elapsed time, and private temporary
log paths outside the repository (mode 0600). Full trusted validation output stays
in those logs. Failures show a bounded recent tail plus error/TODO lines, not the
entire suite output. Capture reads fixed-size byte chunks: long newline-free records
and carriage-return progress cannot grow the in-memory excerpts without bound. UTF-8
decoding spans chunks, and truncated records retain a bounded beginning and end.
The full log retains the original bytes. Use global `--verbose` before `check` or `publish` for live output
as well as logs. Review logs before sharing and remove them when no longer needed.
Other API and short command output remains captured. Base subprocesses clear the
CLI's `VIRTUAL_ENV` while preserving the SSH-agent and approved signer environment.
Validation may generate ignored caches. Ctrl+C prints `Validation cancelled.`, exits
130 and never reports success or starts the next check. Validation runs in a separate
process group; cancellation or the 30-minute timeout terminates its child tree with
bounded SIGTERM/SIGKILL cleanup, without signalling the CLI's parent process group.
This includes cancellation while draining output after the command leader exits;
children holding stdout are terminated and the pipe and log are closed.

The preparation wizard asks only for version, date and release summary, followed by
the preview and default-No confirmation, without retyping a long phrase.
Forward stable upgrades are the default. Base's legacy wire
inputs are still sent explicitly: `preparation_mode=successor`, the verified
`previous_tag`, `stable_upgrade=supported`, `upgrades_from_alpha_revisions=` and
`recovery=forward-fix`. There is no policy dropdown or declaration acknowledgment.
These fields and Base's validation gates have **not** been removed system-wide.

`forward-fix` is a general classification, not a promise that a repair or restore
is available. `configuration-revert` is not a universal recovery strategy: reverting
configuration cannot undo persistent-state changes. Complete the changelog's breaking
actions and the canonical migration/recovery evidence in the draft PR; change the
release configuration and regenerate its manifest there if concrete evidence needs
another classification or alpha sources. A supported default is not tested transition
evidence. Fresh installation still requires a verified empty or replacement target.
Bootstrap remains a separate manual Base procedure.

## Noninteractive Actions

`prepare --summary TEXT` defaults to the next verified published patch and today's
date; `--version` and `--release-date` override them. The unshipped policy,
predecessor and preparation-mode CLI flags have been removed, not retained as aliases.
For unattended invocation, explicitly authorize routine local preparation and the
exact remote action using the observed commit (these examples are not authorization):

```bash
uv run platform-release --base-repo /path/to/trusted/base --allow-local-preparation \
  prepare --summary 'Reviewed fixes' --version 1.2.3 \
  --confirm 'PREPARE OWNER/REPOSITORY v1.2.3 FULL_MAIN_COMMIT'
uv run platform-release --base-repo /path/to/trusted/base --allow-local-preparation \
  publish --tag v1.2.3 \
  --confirm 'PUBLISH OWNER/REPOSITORY v1.2.3 FULL_RELEASE_MERGE_COMMIT'
```

Without `--confirm`, preparation asks Yes/No (default No) after its preview;
publication still requests the exact phrase after preflight. Supplied `--confirm`
phrases remain exact repository/tag/full-commit checks for both commands.
Publication confirmation authorizes signing, staging and the automatic publication
chain together. A stale supplied confirmation fails closed. Interactive preflight
revalidates a changed tip and requests a new confirmation, bounded to three attempts.
If publication's new main tip is not the release PR's exact merge commit, it blocks
for provenance review instead. A fetch-time race stops and requires a fresh invocation;
no signing or remote mutation has occurred. A tip change after signing leaves a
partial local tag for inspection, never automatic replacement or restaging.

## Partial Operations

`status --tag vX.Y.Z` and `continue --tag vX.Y.Z` target that release, independently
of checkout `VERSION`. Without `--tag`, a single pending release PR takes priority;
multiple pending targets require explicit selection. The interactive wizard offers
pending PR targets, checkout VERSION and the latest verified published release.
Only same-repository release heads targeting the default branch enter this inventory;
forks and PRs targeting other base branches neither select a target nor block preparation.

`continue` reports local signed objects and commits, exact remote staging/final refs,
release PRs, workflow runs and GitHub Release state. If the final ref exists and matches
the locally verified signed tag, it waits/rechecks publication without redispatching.
If the local signed tag is absent, fetch it separately before continuing. Signed-only
and staged-only states remain inspection-only, with explicit manual recovery guidance.
`status` provides inspection without waiting, optionally as noninteractive JSON. `plan` is also
noninteractive and read-only. No state file, silent retries, ref deletion or
overwrites are used. Commands time out after 30 minutes; publication polling is
bounded to 30 attempts with two-second pauses per phase (plus command execution time)
and may stop while approvals are pending. Missing runs, pending runs, and delayed
artifact availability are polled rather than mistaken for failure. Exact-target
verification failures and artifact-correlated publication failures stop immediately.
Publication runs in status are explicitly labelled uncorrelated: only publication's
identity artifact establishes which release a workflow_run published.
Tag-creation runs are reported separately as `tag_creation_runs_uncorrelated`, with
run IDs and URLs. They dispatch on the default branch, not the release tag. Verify
their exact `tag`, `tag_object` and `target_commit` inputs against the inspected signed
identity before attributing a candidate run or authorizing a retry. Candidate success
or failure is never treated as proof about the selected release.
If a publication run fails before emitting its identity artifact, the current Base
workflow exposes no reliable target binding to this CLI. It reports those run IDs as
uncorrelated on timeout, not as a proven failure of the selected release. Expired
artifacts similarly require manual verification; no false success is inferred.

- Local tag only: signing or post-tag validation may have completed. Inspect and
  resolve the failed check before manually following the Base staging procedure.
- Staging ref exists: inspect the Create Platform Release Tag run and its exact
  inputs before any manual retry; the workflow may remove staging even on failure.
- Final ref exists: never recreate or move it. If already published, do not republish;
  use `continue --tag vX.Y.Z` only for separate correlated verification if needed.
  Otherwise inspect exact-tag verification and publication evidence, addressing
  environment approvals only when the workflow actually reports they are required.
- Conflicting objects: stop for custodian investigation; do not overwrite or delete.

`publish` deliberately refuses partial states. This avoids confusing a timed-out
dispatch with a safe retry. If default-branch work advances beyond the release PR
merge commit, publication is blocked for manual provenance review.

Start a partial-operation investigation with:

```bash
uv run platform-release --base-repo /path/to/trusted/base-release-worktree continue --tag v1.2.3
gh run view RUN_ID --repo OWNER/REPOSITORY
```

Use the reported exact object SHAs, PR URLs and run IDs, not the latest unrelated
run. If a custodian separately authorizes a manual tag-creation retry, all three
workflow inputs must match the inspected state: `tag=vX.Y.Z`,
`tag_object=<remote_staged_object>` and `target_commit=<verified signed tag commit>`.
An absent staging ref is not proof that the previous attempt never ran. Inspect
its outcome before any separately authorized restaging. Never create a final tag
or GitHub Release directly to get past a failed protected workflow.

## Validation

```bash
uv lock --check
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen pytest
uv build
```

Tests fake GitHub operations and signing. Local bare-Git regression tests use only
temporary filesystem remotes, with network protocols disabled. No credentials or cluster
are required.
The package enforces 80 percent coverage. Base owns release rules; this package
calls existing Base scripts and workflows.
Set `PLATFORM_RELEASE_BASE_FIXTURE=/path/to/trusted/base` when running pytest to
also exercise that checkout's real generator and migration parser without modifying
it. This optional integration test is skipped when no trusted local fixture is supplied.
