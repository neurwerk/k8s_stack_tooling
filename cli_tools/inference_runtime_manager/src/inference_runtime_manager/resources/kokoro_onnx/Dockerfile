FROM ghcr.io/astral-sh/uv:0.8.17@sha256:db99140470350437166de1fc646323ecb59e4d99d7857d0baf429a7b4a9523f3 AS uv
FROM python:3.11-slim-bookworm@sha256:4b4c524dc3dce996864e030c7bd9c6b0e517597189fee48f48e05b499442444b

LABEL org.opencontainers.image.title="Inference Runtime Manager Kokoro ONNX runtime"
LABEL org.opencontainers.image.source="https://github.com/neurwerk/k8s_stack_tooling"

RUN apt-get update \
 && apt-get install -y --no-install-recommends espeak-ng libsndfile1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 runtime

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app.py ./

USER runtime
EXPOSE 8881
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8881", "--loop", "uvloop", "--http", "httptools"]
