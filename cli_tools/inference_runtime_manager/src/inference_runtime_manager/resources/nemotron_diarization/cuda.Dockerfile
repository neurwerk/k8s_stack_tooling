FROM ghcr.io/astral-sh/uv:0.8.17@sha256:db99140470350437166de1fc646323ecb59e4d99d7857d0baf429a7b4a9523f3 AS uv
FROM python:3.11-slim-bookworm@sha256:4b4c524dc3dce996864e030c7bd9c6b0e517597189fee48f48e05b499442444b
RUN useradd --create-home --uid 10001 runtime
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY cuda/pyproject.toml cuda/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=4 uv sync --locked --no-dev --no-install-project
COPY app.py ./
COPY LICENSE.openmdw ./LICENSE.openmdw
USER runtime
EXPOSE 8010
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 DIARIZATION_DEVICE=cuda
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1", "--ws-max-size", "10248", "--ws-max-queue", "64"]
