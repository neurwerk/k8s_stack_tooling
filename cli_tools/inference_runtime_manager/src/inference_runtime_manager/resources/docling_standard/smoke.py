"""Synthetic in-container check of all three authenticated model endpoints."""

import json
import os
import struct
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from docling_ibm_models.tableformer.data_management.tf_predictor import TFPredictor
from PIL import Image, ImageDraw, ImageFont
from protocol import DTYPES
from transformers import AutoImageProcessor


def infer(name, inputs):
    tensors, chunks = [], []
    for key, value in inputs.items():
        dtype = (
            "BYTES"
            if value.dtype == object
            else next(name for name, dtype in DTYPES.items() if value.dtype == dtype)
        )
        if dtype == "BYTES":
            raw = b"".join(
                struct.pack("<I", len(str(x).encode())) + str(x).encode() for x in value.flat
            )
        else:
            raw = value.tobytes()
        tensors.append(
            dict(
                name=key,
                datatype=dtype,
                shape=list(value.shape),
                parameters={"binary_data_size": len(raw)},
            )
        )
        chunks.append(raw)
    header = json.dumps({"inputs": tensors}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:8011/v2/models/{name}/infer",
        data=header + b"".join(chunks),
        headers={
            "Authorization": "Bearer " + os.environ["DOCLING_STANDARD_API_KEY"],
            "Inference-Header-Content-Length": str(len(header)),
            "Content-Type": "application/octet-stream",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        data = response.read()
        offset = int(response.headers["Inference-Header-Content-Length"])
    metadata = json.loads(data[:offset])
    outputs = {}
    for item in metadata["outputs"]:
        length = item["parameters"]["binary_data_size"]
        raw = data[offset : offset + length]
        offset += length
        if item["datatype"] == "BYTES":
            texts = []
            while raw:
                size = struct.unpack("<I", raw[:4])[0]
                texts.append(raw[4 : 4 + size].decode())
                raw = raw[4 + size :]
            value = np.array(texts, dtype=object)
        else:
            value = np.frombuffer(raw, dtype=DTYPES[item["datatype"]])
            if not np.isfinite(value).all():
                raise RuntimeError("Nonfinite inference output")
        outputs[item["name"]] = value.reshape(item["shape"])
    if offset != len(data):
        raise RuntimeError("Unexpected output bytes")
    return outputs


def main():
    root = Path("/models/active/docling-standard")
    image = Image.new("RGB", (800, 400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    draw.text((50, 40), "CHECK 64", fill="black", font=font)
    for x in (40, 400, 760):
        draw.line((x, 160, x, 360), fill="black", width=3)
    for y in (160, 260, 360):
        draw.line((40, y, 760, y), fill="black", width=3)
    for x, y, text in (
        (70, 180, "Name"),
        (430, 180, "Count"),
        (70, 280, "Sample"),
        (430, 280, "64"),
    ):
        draw.text((x, y), text, fill="black", font=font)
    started = time.monotonic()
    ocr = infer(
        "ocr", {"image": np.asarray(image)[None], "lang_type": np.array([["ch"]], dtype=object)}
    )
    if "CHECK64" not in "".join(ocr["txts"]).replace(" ", "").upper():
        raise RuntimeError("Synthetic OCR text was not recognized")
    processor = AutoImageProcessor.from_pretrained(root, local_files_only=True)
    layout = infer(
        "layout",
        {
            "images": processor(images=[image] * 4, return_tensors="np")["pixel_values"],
            "orig_target_sizes": np.array([[800, 400]] * 4, dtype=np.int64),
        },
    )
    if len(layout["labels"]) != 4 or not (layout["scores"] > 0.3).any():
        raise RuntimeError("Synthetic layout batch did not produce detections")
    config = json.loads(
        (root / "tableformer/model_artifacts/tableformer/accurate/tm_config.json").read_text()
    )
    tensor = TFPredictor._prepare_image(
        SimpleNamespace(_config=config, _device="cpu"),
        np.asarray(image.crop((40, 160, 760, 360))),
    )
    table = infer("tableformer", {"images": tensor.numpy()})
    if not len(table["coords"]) or 3 not in table["tag_seq"] or 5 not in table["tag_seq"]:
        raise RuntimeError("Synthetic table did not produce a completed cell structure")
    print(
        json.dumps(
            {
                "smoke": "passed",
                "layout_pages": 4,
                "ocr_text": "CHECK 64",
                "table_cells": len(table["coords"]),
                "seconds": time.monotonic() - started,
                "accuracy": "synthetic contract check only",
            }
        )
    )


if __name__ == "__main__":
    main()
