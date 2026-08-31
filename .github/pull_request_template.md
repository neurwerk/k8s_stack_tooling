## Summary

Describe the operator-facing change and the affected project.

## Issues

Closes #<!-- implementation issue -->

Parent: #<!-- link only; do not use Closes/Fixes/Resolves -->

## Affected Contracts

List affected CLI, image, package, provider, recovery, platform, and consuming-repository contracts, or state `None`.

## Validation

- [ ] Root Kubernetes Tooling quality checks pass.
- [ ] Package Checker quality checks pass.
- [ ] Media Downloader Uploader quality checks pass.
- [ ] OpenBao Stack Setup quality checks pass.
- [ ] Not applicable projects and checks are explained below.

Record exact commands and results:

## Release Checklist

- [ ] Root image changes use only the root `pyproject.toml` and `uv.lock` version contract.
- [ ] Nested CLI versions remain independent and are not presented as the root image version.
- [ ] A release tag is not required, or the exact `vX.Y.Z` release impact is described.
- [ ] Documentation and platform consumers are updated when applicable.
- [ ] No credentials, provider tokens, recovery material, client data, or generated local files are included.

Image publication is tag-driven. Opening or merging this pull request must not publish an image.

## Release Classification

Apply exactly one: `release: none`, `release: notes`, `release: platform`, or `release: client`.
