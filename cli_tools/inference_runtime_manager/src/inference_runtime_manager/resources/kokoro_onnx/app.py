"""OpenAI-compatible adapter for the pinned German Martin Kokoro artifacts."""

from __future__ import annotations

import hmac
import importlib.util
import io
import logging
import os
import re
import sys
import threading
import time
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import anyio
import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from kokoro_onnx import Kokoro
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Receive, Scope, Send

SAMPLE_RATE = 24_000
MODEL_ROOT = Path(os.environ.get("KOKORO_MODEL_ROOT", "/models/active/tts-german"))
MODEL_PATH = MODEL_ROOT / "kokoro-martin.onnx"
VOICES_PATH = MODEL_ROOT / "voices-martin.npz"
NORMALIZER_PATH = MODEL_ROOT / "onnx-docker" / "tts_normalizer.py"
_tts: Kokoro | None = None
_normalize: Callable[[str], str] | None = None
_lock = threading.Lock()
# StreamingResponse may finalize its iterator from a different ASGI task.
# A semaphore allows that task to release the request slot safely.
_request_lock = anyio.Semaphore(1)
logger = logging.getLogger("uvicorn.error")


class NoSpeakableText(ValueError):
    """Normalization or phonemization left no material for the speech model."""


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["tts-german"]
    input: str = Field(min_length=1, max_length=10_000)
    voice: Literal["default"] = "default"
    response_format: Literal["wav", "pcm"] = "wav"
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


def pcm_bytes(samples: np.ndarray) -> bytes:
    """OpenAI raw PCM: signed 16-bit little endian, mono, 24 kHz."""
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim != 1:
        raise ValueError("Expected mono audio")
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def wav_bytes(samples: np.ndarray) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm_bytes(samples))
    return output.getvalue()


def split_sentences(text: str) -> list[str]:
    if _tts is None or _normalize is None:
        raise RuntimeError("Kokoro is not ready")
    normalized = _normalize(text).strip()
    sentences = [
        part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", normalized) if part.strip()
    ]
    if not sentences:
        raise NoSpeakableText("Input contains no speakable text")
    return sentences


def prepare_speech(text: str) -> list[str]:
    """Check phonemes before starting a response; never synthesize an empty batch."""
    if _tts is None:
        raise RuntimeError("Kokoro is not ready")
    with _lock:
        phonemes = [
            value
            for sentence in split_sentences(text)
            if (value := _tts.tokenizer.phonemize(sentence, "de").strip())
        ]
    if not phonemes:
        raise NoSpeakableText("Input contains no speakable text")
    return phonemes


def sentence_audio(phonemes: str, speed: float) -> np.ndarray:
    if _tts is None:
        raise RuntimeError("Kokoro is not ready")
    # The worker owns this lock: cancelling its caller cannot stop ONNX or
    # release exclusivity while create() is still running.
    with _lock:
        samples, sample_rate = _tts.create(
            text=phonemes, voice="martin", speed=speed, lang="de", is_phonemes=True
        )
    if sample_rate != SAMPLE_RATE:
        raise RuntimeError(f"Unexpected sample rate: {sample_rate}")
    return np.asarray(samples, dtype=np.float32)


def synthesize(text: str, speed: float) -> bytes:
    sentences = prepare_speech(text)
    audio: list[np.ndarray] = []
    for index, sentence in enumerate(sentences):
        audio.append(sentence_audio(sentence, speed))
        if index < len(sentences) - 1:
            audio.append(np.zeros(SAMPLE_RATE // 4, dtype=np.float32))
    return wav_bytes(np.concatenate(audio))


async def stream_pcm(
    sentences: list[str], speed: float, *, started: float | None = None, preparation_ms: float = 0
) -> AsyncGenerator[bytes, None]:
    queued = time.monotonic()
    started = queued if started is None else started
    queue_ms = None
    first_audio_ms = None
    synthesis_ms = 0.0
    audio_bytes = 0
    outcome = "failed"
    try:
        async with _request_lock:
            queue_ms = round((time.monotonic() - queued) * 1000)
            for index, sentence in enumerate(sentences):
                await anyio.lowlevel.checkpoint()
                # Do not abandon a running native call on disconnect. The request
                # lock stays held until it finishes, then cancellation discards it.
                synthesis_started = time.monotonic()
                samples = await anyio.to_thread.run_sync(sentence_audio, sentence, speed)
                synthesis_ms += (time.monotonic() - synthesis_started) * 1000
                await anyio.lowlevel.checkpoint()
                audio = pcm_bytes(samples)
                if first_audio_ms is None:
                    first_audio_ms = round((time.monotonic() - started) * 1000)
                audio_bytes += len(audio)
                yield audio
                if index < len(sentences) - 1:
                    silence = b"\0" * (SAMPLE_RATE // 4 * 2)
                    audio_bytes += len(silence)
                    yield silence
            outcome = "complete"
    except (GeneratorExit, anyio.get_cancelled_exc_class()):
        outcome = "interrupted"
        raise
    finally:
        logger.info(
            "Kokoro PCM outcome=%s preparation_ms=%s queue_ms=%s synthesis_ms=%s "
            "first_audio_ms=%s elapsed_ms=%s audio_bytes=%s",
            outcome,
            round(preparation_ms),
            queue_ms,
            round(synthesis_ms),
            first_audio_ms,
            round((time.monotonic() - started) * 1000),
            audio_bytes,
        )


class PCMResponse(StreamingResponse):
    def __init__(
        self,
        sentences: list[str],
        speed: float,
        *,
        started: float | None = None,
        preparation_ms: float = 0,
    ) -> None:
        self.stream = stream_pcm(sentences, speed, started=started, preparation_ms=preparation_ms)
        super().__init__(self.stream, media_type="audio/pcm")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A disconnect during send() leaves the generator suspended at a
            # yield. Close it explicitly rather than waiting for GC to unlock.
            with anyio.CancelScope(shield=True):
                await self.stream.aclose()


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


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """Require the optional service key on inference routes."""
    key = os.environ.get("API_KEY", "")
    supplied = request.headers.get("Authorization")
    if (
        request.url.path.startswith("/v1/")
        and key
        and (supplied is None or not hmac.compare_digest(supplied, "Bearer " + key))
    ):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


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
    started = time.monotonic()
    try:
        if request.response_format == "pcm":
            sentences = await run_in_threadpool(prepare_speech, request.input)
            return PCMResponse(
                sentences,
                request.speed,
                started=started,
                preparation_ms=(time.monotonic() - started) * 1000,
            )
        async with _request_lock:
            audio = await run_in_threadpool(synthesize, request.input, request.speed)
    except NoSpeakableText:
        raise HTTPException(
            status_code=422,
            detail={"code": "no_speakable_text", "message": "Input contains no speakable text"},
        ) from None
    except (RuntimeError, ValueError):
        logger.error("Kokoro synthesis failed; payload omitted")
        raise HTTPException(status_code=500, detail="Speech synthesis failed") from None
    return Response(
        audio,
        media_type="audio/wav",
        headers={"X-Generation-Time-Ms": f"{(time.monotonic() - started) * 1000:.1f}"},
    )
