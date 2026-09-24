"""OpenAI-compatible adapter for the pinned German Martin Kokoro artifacts."""

from __future__ import annotations

import importlib.util
import io
import os
import re
import sys
import threading
import wave
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from kokoro_onnx import Kokoro
from pydantic import BaseModel, ConfigDict, Field

SAMPLE_RATE = 24_000
MODEL_ROOT = Path(os.environ.get("KOKORO_MODEL_ROOT", "/models/active/tts-german"))
MODEL_PATH = MODEL_ROOT / "kokoro-martin.onnx"
VOICES_PATH = MODEL_ROOT / "voices-martin.npz"
NORMALIZER_PATH = MODEL_ROOT / "onnx-docker" / "tts_normalizer.py"
_tts: Kokoro | None = None
_normalize: Callable[[str], str] | None = None
_lock = threading.Lock()


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["tts-german"]
    input: str = Field(min_length=1, max_length=10_000)
    voice: Literal["default"] = "default"
    response_format: Literal["wav"] = "wav"
    language: Literal["de"] = "de"
    speed: float = Field(default=1.125, ge=0.5, le=2.0)


def load_normalizer() -> Callable[[str], str]:
    sys.path.insert(0, str(MODEL_ROOT))
    spec = importlib.util.spec_from_file_location("martin_tts_normalizer", NORMALIZER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the pinned German text normalizer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalize_tts_text


def load_tts() -> Kokoro:
    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, int(os.environ.get("KOKORO_ONNX_INTRA_OP_THREADS", "4")))
    options.inter_op_num_threads = max(1, int(os.environ.get("KOKORO_ONNX_INTER_OP_THREADS", "1")))
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    session = ort.InferenceSession(
        str(MODEL_PATH), sess_options=options, providers=["CPUExecutionProvider"]
    )
    return Kokoro.from_session(session, str(VOICES_PATH))


def wav_bytes(samples: np.ndarray) -> bytes:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype(np.int16)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


def synthesize(text: str, speed: float) -> bytes:
    if _tts is None or _normalize is None:
        raise RuntimeError("Kokoro is not ready")
    normalized = _normalize(text).strip()
    sentences = [
        part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", normalized) if part.strip()
    ]
    if not sentences:
        raise ValueError("Input contains no speakable text")
    audio: list[np.ndarray] = []
    with _lock:
        for index, sentence in enumerate(sentences):
            samples, sample_rate = _tts.create(
                text=sentence, voice="martin", speed=speed, lang="de"
            )
            if sample_rate != SAMPLE_RATE:
                raise RuntimeError(f"Unexpected sample rate: {sample_rate}")
            audio.append(np.asarray(samples, dtype=np.float32))
            if index < len(sentences) - 1:
                audio.append(np.zeros(SAMPLE_RATE // 4, dtype=np.float32))
    return wav_bytes(np.concatenate(audio))


@asynccontextmanager
async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
    global _normalize, _tts
    _normalize = load_normalizer()
    _tts = load_tts()
    _tts.create(text="Hallo.", voice="martin", speed=1.0, lang="de")
    yield
    _tts = None
    _normalize = None


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict[str, str]:
    if _tts is None:
        raise HTTPException(status_code=503, detail="Kokoro is not ready")
    return {"status": "ready"}


@app.get("/v1/audio/voices")
def voices() -> dict[str, list[str]]:
    return {"voices": ["default"]}


@app.post("/v1/audio/speech")
async def speech(request: SpeechRequest) -> Response:
    try:
        audio = await run_in_threadpool(synthesize, request.input, request.speed)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(audio, media_type="audio/wav")
