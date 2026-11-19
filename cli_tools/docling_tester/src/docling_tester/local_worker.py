"""Disposable Docling conversion with a private loopback Docker bridge."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override

from docling_tester.transport import strict_json

REMOTE = """
import os, sys, urllib.request
try:
    request=urllib.request.Request(
        "http://127.0.0.1:8000/v1/chat/completions", data=sys.stdin.buffer.read(),
        headers={"Content-Type":"application/json",
                 "Authorization":"Bearer "+os.environ["VLM_DOCUMENTS_API_KEY"]})
    with urllib.request.urlopen(request, timeout=300) as response:
        data=response.read(16777217)
    if len(data)>16777216: raise ValueError()
    sys.stdout.buffer.write(data)
except Exception:
    sys.exit(1)
"""


def page_request(payload: dict[str, Any], config: dict[str, Any]) -> bytes:
    payload.update(model="vlm-documents", max_tokens=4096, stream=False)
    payload.pop("max_completion_tokens", None)
    result = subprocess.run(
        [
            "docker",
            "--context",
            config["context"],
            "exec",
            "-i",
            config["container"],
            "python3",
            "-c",
            REMOTE,
        ],
        input=json.dumps(payload).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=config["timeout"],
        check=False,
    )
    if result.returncode:
        raise ValueError("Docker inference failed")
    value = strict_json(result.stdout)
    choices = value.get("choices", []) if isinstance(value, dict) else []
    if (
        not isinstance(choices, list)
        or not choices
        or value.get("error")
        or any(
            not isinstance(choice, dict)
            or choice.get("finish_reason") != "stop"
            or not isinstance(choice.get("message"), dict)
            or not isinstance(choice["message"].get("content"), str)
            or not choice["message"]["content"].strip()
            for choice in choices
        )
    ):
        raise ValueError("Incomplete inference")
    return result.stdout


def convert(config: dict[str, Any]) -> dict[str, Any]:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
    from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
    from docling.pipeline.vlm_pipeline import VlmPipeline

    failures: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64 * 1024 * 1024:
                    raise ValueError("Request size")
                raw = page_request(strict_json(self.rfile.read(length)), config)
                code = 200
            except (ValueError, OSError, subprocess.SubprocessError):
                failures.append("inference_failed")
                code, raw = 502, b'{"error":{"code":"inference_failed"}}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as bridge:
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            options = VlmPipelineOptions(
                enable_remote_services=True,
                vlm_options=VlmConvertOptions.from_preset(
                    "granite_docling",
                    engine_options=ApiVlmEngineOptions(
                        engine_type=VlmEngineType.API,
                        url=f"http://127.0.0.1:{bridge.server_port}/v1/chat/completions",
                        concurrency=1,
                        timeout=config["timeout"],
                    ),
                ),
            )
            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_cls=VlmPipeline, pipeline_options=options
                    ),
                    InputFormat.IMAGE: ImageFormatOption(
                        pipeline_cls=VlmPipeline, pipeline_options=options
                    ),
                }
            )
            result = converter.convert(
                Path(config["path"]), raises_on_error=False, max_num_pages=200
            )
            return {
                "status": result.status.value,
                "remote_job_may_continue": bool(failures),
                "errors": ["conversion_errors"] if result.errors or failures else [],
                "document": {"json_content": result.document.export_to_dict()},
            }
        finally:
            bridge.shutdown()
            thread.join()


def main() -> None:
    config = strict_json(sys.stdin.buffer.read())
    output = os.dup(sys.stdout.fileno())
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), sys.stdout.fileno())
    try:
        value = convert(config)
    except Exception:
        value = {
            "status": "failure",
            "errors": ["local_conversion_failed"],
            "remote_job_may_continue": True,
        }
    with os.fdopen(output, "w") as stream:
        json.dump(value, stream)


if __name__ == "__main__":
    main()
