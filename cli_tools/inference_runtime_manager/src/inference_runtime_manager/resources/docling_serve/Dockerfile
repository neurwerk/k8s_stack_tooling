FROM ghcr.io/docling-project/docling-serve-cu128:v1.38.2@sha256:a9b2e7ce5074d1639f07805a4ada6a7dcb777f7d096e2ed1af529333912692e8

LABEL org.opencontainers.image.title="Inference Runtime Manager Docling Serve runtime"
LABEL org.opencontainers.image.source="https://github.com/neurwerk/k8s_stack_tooling"

USER 0
RUN dnf install -y --setopt=install_weak_deps=False \
        libreoffice-writer-1:7.1.8.1-17.el9 \
        libreoffice-calc-1:7.1.8.1-17.el9 \
        libreoffice-impress-1:7.1.8.1-17.el9 \
    && dnf clean all \
    && rm -rf /var/cache/dnf

COPY requirements.txt /opt/docling-overlay/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/docling-overlay/requirements.txt \
    && python -m pip check

USER 1001
ENV DOCLING_ARTIFACTS_PATH=/opt/app-root/src/.cache/docling/models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

CMD ["docling-serve", "run"]
