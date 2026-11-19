"""CUDA-only model execution. Document parsing and table cell matching are not served."""

from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path

import numpy as np
import torch
from rapidocr import EngineType, RapidOCR
from transformers import AutoImageProcessor, AutoModelForObjectDetection


class Models:
    def __init__(self, root: Path):
        if not torch.cuda.is_available():
            raise RuntimeError("Standard Docling requires a visible CUDA device")
        self.device = torch.device("cuda:0")
        torch.set_num_threads(4)
        torch.backends.cuda.matmul.allow_tf32 = False
        self.processor = AutoImageProcessor.from_pretrained(root, local_files_only=True)
        self.layout = (
            AutoModelForObjectDetection.from_pretrained(
                root, local_files_only=True, dtype=torch.float32
            )
            .to(self.device)
            .eval()
        )
        ocr = root / "rapidocr"
        self.ocr = RapidOCR(
            params={
                "Global.model_root_dir": "/tmp/rapidocr",
                "Det.engine_type": EngineType.TORCH,
                "Cls.engine_type": EngineType.TORCH,
                "Rec.engine_type": EngineType.TORCH,
                "EngineConfig.torch.use_cuda": True,
                "EngineConfig.torch.cuda_ep_cfg.device_id": 0,
                "Global.text_score": 0.5,
                "Global.log_level": "warning",
                "Det.model_path": str(ocr / "PP-OCRv6_det_small.pth"),
                "Cls.model_path": str(ocr / "ch_ppocr_mobile_v2.0_cls_mobile.pth"),
                "Rec.model_path": str(ocr / "PP-OCRv6_rec_small.pth"),
                "Rec.rec_keys_path": str(ocr / "ppocrv6_dict.txt"),
            }
        )
        stages = [
            self.ocr._load_det_model(),
            self.ocr._load_cls_model(),
            self.ocr._load_rec_model(),
        ]
        for model in [
            self.layout,
            *(stage.session.predictor for stage in stages),
        ]:
            if any(parameter.device.type != "cuda" for parameter in model.parameters()):
                raise RuntimeError("A standard Docling model is not on CUDA")
        self.info = {
            "device": str(self.device),
            "gpu": torch.cuda.get_device_name(self.device),
            "compute_capability": list(torch.cuda.get_device_capability(self.device)),
            "dtype": "float32",
            "versions": {
                name: importlib.metadata.version(name)
                for name in ("torch", "transformers", "rapidocr")
            },
            "files": {},
        }
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".safetensors", ".pth", ".json", ".txt"}:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                self.info["files"][path.relative_to(root).as_posix()] = digest.hexdigest()

    @torch.inference_mode()
    def infer(self, name: str, inputs: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        if name == "layout":
            pixels = torch.from_numpy(inputs["images"].copy()).to(self.device)
            output = self.layout(pixel_values=pixels)
            # Docling sends width,height; HF postprocessing expects height,width.
            sizes = inputs["orig_target_sizes"][:, ::-1].copy()
            results = self.processor.post_process_object_detection(
                output, target_sizes=torch.from_numpy(sizes).to(self.device), threshold=0.0
            )
            values = {
                key: np.stack([result[key].cpu().numpy() for result in results])
                for key in ("labels", "boxes", "scores")
            }
        elif name == "ocr":
            # Preserve Docling 2.150.1 RapidOcrModel's numpy.asarray(PIL image) input.
            result = self.ocr(inputs["image"][0].copy())
            values = {
                "boxes": np.asarray(
                    result.boxes if result.boxes is not None else [], dtype=np.float32
                ).reshape(-1, 4, 2),
                "txts": np.asarray(result.txts if result.txts is not None else [], dtype=object),
                "scores": np.asarray(
                    result.scores if result.scores is not None else [], dtype=np.float32
                ),
            }
        else:
            raise ValueError("Unknown model")
        torch.cuda.synchronize(self.device)
        return values
