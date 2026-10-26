# syntax=docker/dockerfile:1
#
# k8s-stack-tooling - Kubernetes operational utilities and maintenance pages.
#
# Shared Python and uv inputs verified for the standalone tooling baseline.
FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

COPY --from=ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945 /uv /uvx /bin/

LABEL org.opencontainers.image.source="https://github.com/neurwerk/k8s_stack_tooling"
LABEL org.opencontainers.image.description="Kubernetes operational utilities and on-demand maintenance pages."
LABEL org.opencontainers.image.licenses="MIT"

# Copy the locked project definition and source.
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY.md /app/
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
