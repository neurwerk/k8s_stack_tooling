FROM kserve/huggingfaceserver@sha256:c84aefd18daf9055aa649fba519751c22818834bf0783ebf5e5032613848914f

LABEL org.opencontainers.image.title="Inference Runtime Manager GLiNER runtime"
LABEL org.opencontainers.image.source="https://github.com/neurwerk/k8s_stack_tooling"

USER root
COPY requirements.txt /opt/ner-gliner/requirements.txt
RUN python3 -m pip install --no-cache-dir --no-deps --require-hashes -r /opt/ner-gliner/requirements.txt
COPY app.py /opt/ner-gliner/app.py
WORKDIR /opt/ner-gliner
USER 1000
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
