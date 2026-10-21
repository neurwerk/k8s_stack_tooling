# Release adoption

The repository release tag matches the root project's version. Each workstation CLI has its own version in `cli_tools/<project>/pyproject.toml`; use the selected platform release's exact CLI version and Tooling source revision.

Run `stack-setup` from `cli_tools/openbao_stack_setup/` at that revision with `uv run --frozen`. It is a standalone workstation CLI, not part of the Tooling container image. A container image update does not update an operator's CLI checkout.

Repository tags matching `vX.Y.Z` also publish a Tooling image. When a release changes only standalone CLI code, existing image pins can remain unchanged; any image adoption requires separate verification and approval.

The [publication workflow](../.github/workflows/build-push.yaml) checks the tag against the root `pyproject.toml` and `uv.lock`, runs the CLI quality matrix, and publishes the `linux/amd64` image and its digest in a GitHub Release. Workstation CLI versions do not have to match the root package version. Follow shared [image release procedures](https://github.com/neurwerk/docs/blob/main/dev/operations/image-releases.md) for publication and pin verification, and [platform upgrades](https://github.com/neurwerk/docs/blob/main/dev/operations/upgrades.md) for client adoption.
