FROM python@sha256:2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301 AS python
FROM ghcr.io/ggml-org/llama.cpp@sha256:d309d9f3584e0a2429e13fbb2ad836bdf2f3201f6618ee63c538e310476fbec3

COPY --from=python /usr/local /usr/local

COPY app.py /opt/ocr-proxy/app.py

ENTRYPOINT ["/usr/local/bin/python3", "/opt/ocr-proxy/app.py"]
