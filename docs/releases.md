# Release adoption

Shared Tooling release tags use `vX.Y.Z`, matching the root project's version. Standalone setup tags use `stack-setup-vX.Y.Z`, matching `cli_tools/openbao_stack_setup/pyproject.toml`. Each workstation CLI has its own version; use the selected platform release's exact CLI version and Tooling source revision.

Keep the SDK tag fixed at its reviewed commit. Publish dependent setup at its own reviewed revision after its lock binds the real published SDK artifact/hash. Do not move the SDK tag to include later setup work or assume its bundled standalone subtree supplies a newer setup interface. Select the setup tag separately; SDK publication alone does not make that setup release available.

Run `stack-setup` from `cli_tools/openbao_stack_setup/` at that revision with `uv run --frozen`. It is a standalone workstation CLI, not part of the Tooling container image. A container image update does not update an operator's CLI checkout.

The standalone CLI and add-on tooling packages depend on an exact shared Tooling distribution. Require its published wheel/source artifact and verified hash before freezing dependent locks. Use real artifacts, not placeholders or sibling-checkout substitutions. Setup also requires the exact selected Base/add-on sources and staged selection; see [source prerequisites](https://github.com/neurwerk/docs/blob/main/dev/operations/openbao.md#sources-and-artifacts). Candidate validation is not installable-release evidence.

Repository tags matching `vX.Y.Z` also publish a Tooling image. When a release changes only standalone CLI code, existing image pins can remain unchanged; any image adoption requires separate verification and approval.

The [shared publication workflow](../.github/workflows/build-push.yaml) and [standalone setup workflow](../.github/workflows/stack-setup-release.yaml) define tag/version checks, quality gates, and release assets. Verify the actual published distributions, checksums, image architectures, and digests before adoption. Workstation CLI versions do not have to match the root package version. Follow shared [image release procedures](https://github.com/neurwerk/docs/blob/main/dev/operations/image-releases.md) for publication and pin verification, and [platform upgrades](https://github.com/neurwerk/docs/blob/main/dev/operations/upgrades.md) for client adoption. The operator runs release commands; workflow definitions alone do not establish artifact availability.
