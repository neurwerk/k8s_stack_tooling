"""Bounded KServe V2 tensor transport; no pickle or document payloads."""

from __future__ import annotations

import json
import math
import struct

import numpy as np

MAX_BODY = 48 * 1024 * 1024
MAX_HEADER = 64 * 1024
DTYPES = {"FP32": np.dtype("<f4"), "INT64": np.dtype("<i8"), "UINT8": np.dtype("u1")}
SCHEMAS = {
    "layout": (
        [("images", "FP32", [-1, 3, 640, 640]), ("orig_target_sizes", "INT64", [-1, 2])],
        [
            ("labels", "INT64", [-1, -1]),
            ("boxes", "FP32", [-1, -1, 4]),
            ("scores", "FP32", [-1, -1]),
        ],
    ),
    "ocr": (
        [("image", "UINT8", [1, -1, -1, 3]), ("lang_type", "BYTES", [1, 1])],
        [("boxes", "FP32", [-1, 4, 2]), ("txts", "BYTES", [-1]), ("scores", "FP32", [-1])],
    ),
}


def metadata(name: str) -> dict:
    inputs, outputs = SCHEMAS[name]
    return {
        "name": name,
        "versions": ["1"],
        "platform": "pytorch_cuda",
        "inputs": [dict(name=n, datatype=d, shape=s) for n, d, s in inputs],
        "outputs": [dict(name=n, datatype=d, shape=s) for n, d, s in outputs],
    }


def decode(body: bytes, header_length: int, name: str) -> dict[str, np.ndarray]:
    if not 0 < header_length <= min(len(body), MAX_HEADER) or len(body) > MAX_BODY:
        raise ValueError("Invalid request size")
    header = json.loads(body[:header_length])
    tensors = header.get("inputs")
    schema = SCHEMAS[name][0]
    if not isinstance(tensors, list) or len(tensors) != len(schema):
        raise ValueError("Invalid input count")
    expected = {key: (dtype, shape) for key, dtype, shape in schema}
    result = {}
    offset = header_length
    for tensor in tensors:
        expected_name = tensor.get("name")
        if expected_name not in expected or expected_name in result:
            raise ValueError("Unknown or duplicate input tensor")
        dtype, expected_shape = expected[expected_name]
        shape = tensor.get("shape")
        if (
            tensor.get("name") != expected_name
            or tensor.get("datatype") != dtype
            or not isinstance(shape, list)
            or len(shape) != len(expected_shape)
            or any(type(d) is not int or d <= 0 or d > 8192 for d in shape)
            or any(e != -1 and d != e for d, e in zip(shape, expected_shape, strict=True))
        ):
            raise ValueError("Invalid input tensor")
        count = math.prod(shape)
        size = tensor.get("parameters", {}).get("binary_data_size")
        if type(size) is not int or size < 0 or offset + size > len(body) or "data" in tensor:
            raise ValueError("Invalid binary tensor length")
        raw = body[offset : offset + size]
        offset += size
        if dtype == "BYTES":
            if size < 4:
                raise ValueError("Invalid language tensor")
            length = struct.unpack("<I", raw[:4])[0]
            if count != 1 or length != size - 4 or raw[4:] != b"ch":
                raise ValueError("This bundle serves the baseline ch recognizer only")
            value = np.array([["ch"]], dtype=object)
        else:
            if size != count * DTYPES[dtype].itemsize:
                raise ValueError("Invalid binary tensor length")
            value = np.frombuffer(raw, dtype=DTYPES[dtype]).reshape(shape)
            if dtype == "FP32" and not np.isfinite(value).all():
                raise ValueError("Nonfinite input")
        result[expected_name] = value
    if offset != len(body):
        raise ValueError("Trailing input bytes")
    if name == "layout":
        batch = len(result["images"])
        if not 1 <= batch <= 4 or result["orig_target_sizes"].shape != (batch, 2):
            raise ValueError("Invalid layout batch")
        if not ((result["orig_target_sizes"] > 0) & (result["orig_target_sizes"] <= 32768)).all():
            raise ValueError("Invalid original image size")
    return result


def encode(name: str, values: dict[str, np.ndarray]) -> tuple[bytes, int]:
    outputs, chunks = [], []
    for key, dtype, shape in SCHEMAS[name][1]:
        value = np.asarray(values[key])
        if value.ndim != len(shape) or any(
            expected != -1 and actual != expected
            for actual, expected in zip(value.shape, shape, strict=True)
        ):
            raise ValueError("Invalid model output shape")
        if dtype == "BYTES":
            encoded = [str(item).encode("utf-8") for item in value.flat]
            raw = b"".join(struct.pack("<I", len(item)) + item for item in encoded)
        else:
            if not np.isfinite(value).all():
                raise ValueError("Nonfinite model output")
            raw = value.astype(DTYPES[dtype], copy=False).tobytes()
        outputs.append(
            dict(
                name=key,
                datatype=dtype,
                shape=list(value.shape),
                parameters={"binary_data_size": len(raw)},
            )
        )
        chunks.append(raw)
    if name == "layout" and (
        values["labels"].shape != values["scores"].shape
        or values["boxes"].shape[:2] != values["labels"].shape
    ):
        raise ValueError("Mismatched layout outputs")
    if name == "ocr" and len({len(values[k]) for k in ("boxes", "txts", "scores")}) != 1:
        raise ValueError("Mismatched OCR outputs")
    header = json.dumps({"model_name": name, "model_version": "1", "outputs": outputs}).encode()
    body = header + b"".join(chunks)
    if len(body) > MAX_BODY:
        raise ValueError("Model output exceeds limit")
    return body, len(header)
