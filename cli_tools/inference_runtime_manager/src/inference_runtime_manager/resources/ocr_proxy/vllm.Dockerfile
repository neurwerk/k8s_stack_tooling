FROM vllm/vllm-openai@sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a

COPY app.py /opt/ocr-proxy/app.py

ENTRYPOINT ["python3", "/opt/ocr-proxy/app.py"]
