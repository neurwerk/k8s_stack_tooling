"""Opt-in adapter for Docling 2.150.1; imports require the caller's Docling environment."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import ClassVar

import numpy as np
import requests
import torch
from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.object_detection_engine_options import (
    ApiKserveV2ObjectDetectionEngineOptions,
)
from docling.datamodel.pipeline_options import (
    KserveV2OcrOptions,
    LayoutObjectDetectionOptions,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.models.factories import get_table_structure_factory
from docling.models.inference_engines.common import KserveV2HttpClient
from docling.models.stages.table_structure.table_structure_model import TableStructureModel
from docling_ibm_models.tableformer.data_management.tf_predictor import TFPredictor
from pydantic import Field, SecretStr


class RemoteTableOptions(TableStructureOptions):
    kind: ClassVar[str] = "managed_remote_tableformer"
    url: str
    api_key: SecretStr = Field(repr=False)
    timeout: float = Field(default=180, gt=0)


class RemoteTableNetwork:
    def __init__(self, options: RemoteTableOptions):
        self.client = KserveV2HttpClient(
            base_url=options.url.rstrip("/"),
            model_name="tableformer",
            model_version=None,
            timeout=options.timeout,
            headers={"Authorization": "Bearer " + options.api_key.get_secret_value()},
            use_binary_data=True,
        )

    def eval(self):
        return self

    def predict(self, images, max_steps, beam_size):
        if (max_steps, beam_size) != (1024, 5):
            raise ValueError("Remote TableFormer requires the pinned Accurate decoding settings")
        result = self.client.infer(
            inputs={"images": images.detach().cpu().numpy().astype(np.float32)},
            output_names=["tag_seq", "classes", "coords"],
        )
        tags, classes, coords = (result[name] for name in ("tag_seq", "classes", "coords"))
        if (
            tags.ndim != 1
            or tags.dtype != np.int64
            or not 1 <= len(tags) <= 1025
            or not ((tags >= 0) & (tags <= 12)).all()
            or classes.ndim != 2
            or classes.shape[1] != 3
            or coords.ndim != 2
            or coords.shape[1] != 4
            or len(classes) != len(coords)
            or not np.isfinite(classes).all()
            or not np.isfinite(coords).all()
        ):
            raise RuntimeError("Invalid remote TableFormer prediction")
        return tags.tolist(), torch.from_numpy(classes.copy()), torch.from_numpy(coords.copy())

    def close(self):
        self.client.close()


class RemotePredictor(TFPredictor):
    def __init__(self, config, options):
        self.remote_options = options
        super().__init__(config, "cpu", 4)

    def _load_model(self):
        # Reuse upstream preprocessing, crop handling, cell matching and postprocessing,
        # replacing only neural-network execution. No local model is constructed.
        self._model_type = self._config["model"]["type"]
        if self._model_type != "TableModel04_rs":
            raise ValueError("Unsupported TableFormer architecture")
        self._remove_padding = False
        return RemoteTableNetwork(self.remote_options)


class RemoteTableModel(TableStructureModel):
    def __init__(
        self, enabled, artifacts_path, options, accelerator_options, enable_remote_services=False
    ):
        if enabled and not enable_remote_services:
            raise ValueError("Remote model services must be explicitly enabled")
        self.enabled = enabled
        self.options = options
        self.do_cell_matching = options.do_cell_matching
        self.mode = options.mode
        if enabled:
            if artifacts_path is None or self.mode != TableFormerMode.ACCURATE:
                raise ValueError("Pinned local TableFormer Accurate configuration is required")
            path = (
                Path(artifacts_path)
                / "docling-project--docling-models"
                / "model_artifacts/tableformer/accurate/tm_config.json"
            )
            self.tm_config = json.loads(path.read_text())
            self.tm_model_type = self.tm_config["model"]["type"]
            self.tf_predictor = RemotePredictor(self.tm_config, options)
            self.scale = 2.0

    @classmethod
    def get_options_type(cls):
        return RemoteTableOptions

    def close(self):
        if self.enabled:
            self.tf_predictor.get_model().close()


def pipeline_options(url: str, api_key: str, artifacts_path: Path) -> PdfPipelineOptions:
    """Build the baseline 4/4/4 CPU pipeline with all three inference stages remote.

    The caller owns conversion admission, status/error/coverage checks and closing
    model clients. This helper does not change the application's 200-page limit.
    """
    for name, expected in (("docling", "2.150.1"), ("docling-ibm-models", "4.0.4")):
        if importlib.metadata.version(name) != expected:
            raise ValueError(f"This adapter requires {name}=={expected}")
    if not api_key:
        raise ValueError("An inference API key is required")
    headers = {"Authorization": "Bearer " + api_key}
    response = requests.get(url.rstrip("/") + "/info", headers=headers, timeout=30)
    response.raise_for_status()
    info = response.json()
    if info.get("device") != "cuda:0" or info.get("dtype") != "float32":
        raise ValueError("Remote runtime is not CUDA FP32")
    for local_name, remote_name in (
        ("docling-project--docling-layout-heron/config.json", "config.json"),
        (
            "docling-project--docling-layout-heron/preprocessor_config.json",
            "preprocessor_config.json",
        ),
        (
            "docling-project--docling-models/model_artifacts/tableformer/accurate/tm_config.json",
            "tableformer/model_artifacts/tableformer/accurate/tm_config.json",
        ),
    ):
        digest = hashlib.sha256((artifacts_path / local_name).read_bytes()).hexdigest()
        if info.get("files", {}).get(remote_name) != digest:
            raise ValueError("Local preprocessing configuration differs from remote models")
    factory = get_table_structure_factory()
    if RemoteTableOptions not in factory.classes:
        factory.register(RemoteTableModel, "inference_runtime_manager", __name__)
    return PdfPipelineOptions(
        artifacts_path=artifacts_path,
        enable_remote_services=True,
        accelerator_options=AcceleratorOptions(device="cpu", num_threads=4),
        ocr_batch_size=4,
        layout_batch_size=4,
        table_batch_size=4,
        do_ocr=True,
        do_table_structure=True,
        ocr_options=KserveV2OcrOptions(
            url=url,
            model_name="ocr",
            transport="http",
            use_binary_data=True,
            headers=headers,
            lang=["ch"],
            scale=3.0,
            timeout=180,
        ),
        layout_options=LayoutObjectDetectionOptions(
            engine_options=ApiKserveV2ObjectDetectionEngineOptions(
                url=url,
                model_name="layout",
                transport="http",
                use_binary_data=True,
                headers=headers,
                timeout=180,
            )
        ),
        table_structure_options=RemoteTableOptions(url=url, api_key=SecretStr(api_key)),
    )
