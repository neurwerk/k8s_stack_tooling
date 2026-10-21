# Development

## Local environment

Python projects require Python 3.12 or newer and use `uv`. Run commands from the project being changed: the repository root for the image package, or `cli_tools/<project>/` for a workstation CLI. Use the versions in the applicable `.tool-versions` through `mise exec -- <command>` when using mise. The root Node.js tool is needed by DocumentDB provisioning tests.

From the selected project directory:

```bash
uv lock --check
uv sync --frozen --dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen pytest
uv build
```

These commands follow [CLI Quality](../../.github/workflows/cli-quality.yaml). That workflow runs a matrix covering the root package and standalone Python CLIs. In `cli_tools/inference_runtime_manager/`, replace `pytest` with `uv run --frozen python -m unittest discover -s tests`; its CI also validates the packaged Compose configuration with Docker and the placeholder environment defined in the workflow. The LDAP Directory and OpenSearch Logs projects have no test step in that matrix. Package Checker CI also checks out Base; use the workflow as the reference for its cross-repository setup.

There is no Makefile. Run the relevant project's checks rather than a workspace-wide `make` target. The root package's tests live in [tests/](../../tests/); standalone projects keep their own tests alongside their source.

## Command and configuration changes

Register image commands in root `pyproject.toml` and workstation commands in their own project's `pyproject.toml`. Configuration parsing lives with each command rather than in a repository-wide configuration file. For example, the image's Keycloak utilities read `KC_` variables, while Package Checker uses its own `Settings` class and `PACKAGE_CHECKER_` environment variables or local `.env` file.

Check both the command implementation and the calling chart when changing workload inputs. Deployment configuration belongs in Base or the owning add-on, not this repository. Follow the [contribution guide](../../.github/CONTRIBUTING.md) and use [release adoption](../releases.md) when a change needs publication.
