"""Private JSONC settings using the live-test runner load/save structure."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from docling_tester.transport import strict_json

DEFAULT_PATH = Path("settings.local.jsonc")
DEFAULTS: dict[str, Any] = {
    "direct_transport": "docker",
    "docker_context": "",
    "docker_container": "local-ai-vlm-documents-1",
    "direct_url": "",
    "direct_api_key": "",
    "gateway_url": "",
    "gateway_api_key": "",
    "gateway_model": "",
    "gateway_auth_header": "Authorization",
    "capture_url": "",
    "capture_api_key": "",
    "pipeline": "standard",
    "ocr": True,
    "ocr_preset": "rapidocr",
    "vlm_preset": "default",
    "reference_profile": "auto",
    "runtime_label": "unknown",
    "timeout": 300,
    "expectations": "",
    "output_directory": "results",
}
SECRETS = {"direct_api_key", "gateway_api_key", "capture_api_key"}
TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*.*?\*/', re.DOTALL)


def validate(values: Any) -> None:
    if not isinstance(values, dict) or values.keys() - DEFAULTS.keys():
        raise ValueError("Unknown settings")
    for key, value in values.items():
        if type(value) is not type(DEFAULTS[key]):
            raise ValueError("Invalid setting type")
    if values.get("direct_transport", "docker") not in {"docker", "http"}:
        raise ValueError("Invalid direct transport")
    if values.get("pipeline", "standard") not in {"standard", "native", "vlm"}:
        raise ValueError("Invalid pipeline")
    if values.get("gateway_auth_header", "Authorization") not in {
        "Authorization",
        "Authorization-Neurwerk",
    }:
        raise ValueError("Invalid authentication header")
    if values.get("timeout", 300) <= 0:
        raise ValueError("Timeout must be positive")


def load(path: Path) -> dict[str, Any]:
    text = path.read_text() if path.exists() else "{}"
    text = TOKENS.sub(
        lambda match: match[0] if match[0].startswith('"') else " " * len(match[0]), text
    )
    try:
        values = strict_json(text)
        validate(values)
    except ValueError:
        raise ValueError("Invalid JSONC settings") from None
    return DEFAULTS | values


def save(path: Path, values: dict[str, Any]) -> None:
    validate(values)
    descriptor, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write("// Private local settings. Contains API keys; do not commit.\n")
            json.dump(DEFAULTS | values, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
