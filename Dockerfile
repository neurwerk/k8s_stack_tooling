# syntax=docker/dockerfile:1
#
# k8s-stack-tooling - Kubernetes initialization utilities for Keycloak and OpenSearch.
#
# Base image: python:3.12-slim
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/

LABEL org.opencontainers.image.source="https://github.com/neurwerk/k8s_stack_tooling"
LABEL org.opencontainers.image.description="Kubernetes initialization utilities for Keycloak and OpenSearch."
LABEL org.opencontainers.image.licenses="MIT"

# Copy the locked project definition and source.
COPY pyproject.toml uv.lock README.md LICENSE /app/
COPY src/ /app/src/

# Install exactly the locked runtime dependencies into the project environment.
WORKDIR /app
RUN uv sync --locked --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"

# Run as a non-root user.
RUN groupadd --system app && useradd --system --gid app appuser
USER appuser

# No entrypoint; Jobs select a command such as `upsert-realm`.
CMD []
