# Release adoption

The repository release tag matches the root project's version. Each workstation CLI has its own version in `cli_tools/<project>/pyproject.toml`; use the selected platform release's exact CLI version and Tooling source revision.

Run `stack-setup` from `cli_tools/openbao_stack_setup/` at that revision with `uv run --frozen`. It is a standalone workstation CLI, not part of the Tooling container image. A container image update does not update an operator's CLI checkout.

Repository tags matching `vX.Y.Z` also publish a Tooling image. When a release changes only standalone CLI code, existing image pins can remain unchanged; any image adoption requires separate verification and approval.
