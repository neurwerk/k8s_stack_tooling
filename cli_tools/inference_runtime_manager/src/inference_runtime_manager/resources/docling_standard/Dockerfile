FROM ghcr.io/astral-sh/uv:0.8.17@sha256:db99140470350437166de1fc646323ecb59e4d99d7857d0baf429a7b4a9523f3 AS uv
FROM python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4
RUN useradd --create-home --uid 10001 runtime
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=4 uv sync --locked --no-dev --no-install-project
COPY app.py models.py protocol.py smoke.py ./
USER runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
EXPOSE 8011
ENTRYPOINT ["python", "app.py"]
