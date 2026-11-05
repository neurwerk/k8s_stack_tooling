"""Authenticated, bounded streaming speaker activity for pinned Nemotron weights."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from struct import unpack_from

import numpy as np
import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from transformers import AutoModelForAudioFrameClassification, AutoProcessor

logger = logging.getLogger("uvicorn.error")
SAMPLE_RATE = 16000
MAX_PACKET_SAMPLES = 5120
MAX_PENDING_SAMPLES = 16000
MODEL_ROOT = Path(os.environ.get("DIARIZATION_MODEL_ROOT", "/models/active/diarization-general"))
DEVICE = os.environ.get("DIARIZATION_DEVICE", "cpu")
TTL = int(os.environ.get("DIARIZATION_SESSION_TTL_SECONDS", "86400"))
MAX_SESSIONS = int(os.environ.get("DIARIZATION_MAX_SESSIONS", "8"))
REPLAY_LIMIT = int(os.environ.get("DIARIZATION_REPLAY_EVENTS", "2048"))
MODE = os.environ.get("DIARIZATION_STREAMING_MODE", "ultra_low_latency")
KEY = os.environ.get("API_KEY", "")
THRESHOLD = float(os.environ.get("DIARIZATION_ACTIVITY_THRESHOLD", "0.5"))
if not KEY or DEVICE not in {"cpu", "cuda"} or TTL < 1 or MAX_SESSIONS < 1 or REPLAY_LIMIT < 1:
    raise RuntimeError("Diarization requires a key and valid device/session limits")
if MODE not in {"ultra_low_latency", "very_low_latency", "low_latency"} or not 0 < THRESHOLD < 1:
    raise RuntimeError("Invalid diarization streaming profile or threshold")


@dataclass
class Session:
    id: str
    owner: str
    touched: float
    attached: bool = False
    next_sample: int = 0
    finalized: int = 0
    seq: int = 0
    replay: deque = field(default_factory=lambda: deque(maxlen=REPLAY_LIMIT))
    cache: object | None = None
    buffer: bytearray = field(default_factory=bytearray)
    utterance_start: int = 0
    buffer_start: int = 0
    step: int = 0
    emitted: int = 0
    completed: bool = False


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
sessions: dict[str, Session] = {}
processor = None
model = None
model_lock = asyncio.Lock()


@app.on_event("startup")
async def load() -> None:
    global processor, model
    torch.set_num_threads(4)
    if DEVICE == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable (no CPU fallback)")
    processor = AutoProcessor.from_pretrained(MODEL_ROOT, local_files_only=True)
    processor.set_streaming_mode(MODE)
    model = (
        AutoModelForAudioFrameClassification.from_pretrained(
            MODEL_ROOT,
            local_files_only=True,
            dtype=torch.float32,
        )
        .eval()
        .to(DEVICE)
    )
    if model.device.type != DEVICE or model.dtype != torch.float32:
        raise RuntimeError("Required device/FP32 precision was not initialized")
    logger.info(
        "Nemotron diarization device=%s precision=float32 buffer_ms=%s",
        DEVICE,
        processor.streaming_latency_ms,
    )
    # Initialize CUDA and kernels before accepting microphone audio.
    warmup = Session("warmup", "internal", time.monotonic())
    await asyncio.to_thread(
        inference,
        warmup,
        b"\0" * (processor.num_samples_first_audio_chunk * 2),
        first=True,
        last=False,
    )


@app.get("/health")
async def health() -> dict:
    if model is None:
        raise RuntimeError("Model is not loaded")
    return {
        "status": "ok",
        "device": model.device.type,
        "precision": str(model.dtype),
        "input_buffer_ms": processor.streaming_latency_ms,
    }


def expire() -> None:
    now = time.monotonic()
    for sid, session in list(sessions.items()):
        if now - session.touched >= TTL and not session.attached:
            del sessions[sid]


def authorize(ws: WebSocket) -> str | None:
    header = ws.headers.get("authorization", "")
    if not hmac.compare_digest(header, "Bearer " + KEY):
        return None
    # A credential, not a session ID, establishes ownership.
    return "service-key"


async def send_error(ws: WebSocket, code: str, **details) -> None:
    await ws.send_json({"type": "error", "code": code, **details})


def inference(session: Session, audio: bytes, *, first: bool, last: bool):
    waveform = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0
    if last:
        # The extractor scores floor(utterance_samples / hop) frames. Complete
        # the final partial frame with silence so its real samples are scored.
        # Later chunks include STFT context, so align the utterance, not audio.
        padding = (
            -(session.next_sample - session.utterance_start)
            % processor.feature_extractor.hop_length
        )
        if padding:
            waveform = np.pad(waveform, (0, padding))
    inputs = processor(
        waveform,
        sampling_rate=SAMPLE_RATE,
        is_streaming=True,
        is_first_audio_chunk=first,
        is_last_audio_chunk=last,
    )
    inputs = inputs.to(model.device, dtype=model.dtype)
    if last and session.cache is None:
        # No look-ahead on a short initial flush: force streaming mode.
        inputs["num_lookback_frames"] = 0
    with torch.inference_mode():
        output = model(**inputs, speaker_cache=session.cache)
    logits = output.logits.detach().float().cpu().sigmoid().numpy()[0]
    return output.speaker_cache, logits


async def process(session: Session, ws: WebSocket, *, flush: bool = False) -> None:
    """Process complete model chunks; final chunk flushes without discarding cache."""
    while True:
        first = session.step == 0
        start = (
            0
            if first
            else processor.audio_chunk_start(session.step * processor.num_mel_frames_per_step)
        )
        required = (
            processor.num_samples_first_audio_chunk
            if first
            else processor.num_samples_per_audio_chunk
        )
        available = session.next_sample - session.utterance_start - start
        if available < required and not (flush and available > 0):
            break
        last = flush and available < required
        audio_start = session.utterance_start + start
        index = (audio_start - session.buffer_start) * 2
        length = available if last else required
        audio = bytes(session.buffer[index : index + length * 2])
        if len(audio) != length * 2:
            raise RuntimeError("Diarization audio buffer was incomplete")
        began = time.monotonic()
        async with model_lock:
            cache, probabilities = await asyncio.to_thread(
                inference, session, audio, first=first, last=last
            )
        inference_ms = round((time.monotonic() - began) * 1000)
        # Logits have 10 ms frames. Emit a separate interval for every active
        # speaker; no dominant-speaker collapse. Clamp final partial frame.
        origin = session.utterance_start + session.emitted
        batch = []
        next_seq = session.seq
        for frame, row in enumerate(probabilities):
            left = origin + frame * 160
            right = min(left + 160, session.next_sample)
            if right <= left:
                break
            for speaker, probability in enumerate(row):
                if probability > THRESHOLD:
                    next_seq += 1
                    event = {
                        "type": "activity",
                        "seq": next_seq,
                        "session_id": session.id,
                        "speaker_id": f"speaker_{speaker}",
                        "start_sample": left,
                        "end_sample": right,
                        "state": "final",
                    }
                    batch.append(event)
        # Commit the entire batch and cache before any network send. A lost
        # connection can then replay all overlaps without re-running the model.
        session.cache = cache
        session.seq = next_seq
        session.replay.extend(batch)
        session.emitted = min(
            session.next_sample - session.utterance_start,
            session.emitted + len(probabilities) * 160,
        )
        session.finalized = session.utterance_start + session.emitted
        session.step += 1
        # Drop past samples but retain the lookback needed by the next chunk.
        next_start = processor.audio_chunk_start(session.step * processor.num_mel_frames_per_step)
        drop_until = min(session.next_sample, session.utterance_start + next_start)
        drop = max(0, drop_until - session.buffer_start)
        del session.buffer[: drop * 2]
        session.buffer_start += drop
        if last:
            session.utterance_start = session.next_sample
            session.buffer.clear()
            session.buffer_start = session.next_sample
            session.step = 0
            session.emitted = 0
        for event in batch:
            await ws.send_json(event)
        await ws.send_json(
            {
                "type": "progress",
                "accepted_through_sample": session.next_sample,
                "finalized_through_sample": session.finalized,
                "input_buffer_ms": processor.streaming_latency_ms,
                "inference_ms": inference_ms,
            }
        )
        if last:
            break
    if flush:
        if session.finalized < session.next_sample:
            raise RuntimeError("Flush did not finalize all accepted audio")
        # A new utterance may use a fresh feature window; speaker cache survives.
        session.utterance_start = session.next_sample
        session.buffer.clear()
        session.buffer_start = session.next_sample
        session.step = 0
        session.emitted = 0


@app.websocket("/v1/diarization/stream")
async def stream(ws: WebSocket) -> None:
    owner = authorize(ws)
    if owner is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    session = None
    try:
        opening = await asyncio.wait_for(ws.receive_json(), timeout=10)
        expire()
        kind = opening.get("type") if isinstance(opening, dict) else None
        if (
            not isinstance(opening, dict)
            or opening.get("version") != 1
            or kind not in {"start", "resume"}
        ):
            await send_error(ws, "invalid_start")
            return
        if kind == "start":
            if (
                opening.get("encoding"),
                opening.get("sample_rate_hz"),
                opening.get("channels"),
            ) != ("pcm_s16le", 16000, 1) or opening.get("buffer_profile", MODE) != MODE:
                await send_error(ws, "unsupported_audio_format")
                return
            if len(sessions) >= MAX_SESSIONS:
                await send_error(ws, "session_limit")
                return
            session = Session(secrets.token_urlsafe(32), owner, time.monotonic(), attached=True)
            sessions[session.id] = session
            await ws.send_json(
                {
                    "type": "started",
                    "session_id": session.id,
                    "next_sample": 0,
                    "last_result_seq": 0,
                    "finalized_through_sample": 0,
                    "expires_in_s": TTL,
                }
            )
        else:
            sid = opening.get("session_id")
            candidate = sessions.get(sid) if isinstance(sid, str) else None
            if candidate is None or candidate.owner != owner:
                await send_error(ws, "session_expired")
                return
            if candidate.attached:
                await send_error(ws, "session_busy")
                return
            after = opening.get("after_result_seq")
            if type(after) is not int or after < 0 or after > candidate.seq:
                await send_error(ws, "invalid_replay_cursor")
                return
            earliest = candidate.replay[0]["seq"] if candidate.replay else candidate.seq + 1
            if after + 1 < earliest:
                await send_error(ws, "replay_gap", earliest_result_seq=earliest)
                return
            session = candidate
            session.attached = True
            session.touched = time.monotonic()
            await ws.send_json(
                {
                    "type": "resumed",
                    "session_id": session.id,
                    "next_sample": session.next_sample,
                    "last_result_seq": session.seq,
                    "finalized_through_sample": session.finalized,
                    "expires_in_s": TTL,
                }
            )
            for event in session.replay:
                if event["seq"] > after:
                    await ws.send_json(event)
            await ws.send_json(
                {
                    "type": "progress",
                    "accepted_through_sample": session.next_sample,
                    "finalized_through_sample": session.finalized,
                    "input_buffer_ms": processor.streaming_latency_ms,
                }
            )
        while True:
            packet = await asyncio.wait_for(ws.receive(), timeout=TTL)
            if packet["type"] == "websocket.disconnect":
                break
            session.touched = time.monotonic()
            if packet.get("bytes") is not None:
                data = packet["bytes"]
                if len(data) < 10 or len(data) > 8 + MAX_PACKET_SAMPLES * 2 or len(data) % 2:
                    await send_error(ws, "invalid_audio_packet")
                    continue
                offset = unpack_from("<Q", data)[0]
                if session.completed or offset != session.next_sample:
                    await send_error(ws, "offset_mismatch", expected_sample=session.next_sample)
                    continue
                if len(session.buffer) + len(data) - 8 > MAX_PENDING_SAMPLES * 2:
                    await send_error(ws, "buffer_limit")
                    continue
                session.buffer.extend(data[8:])
                session.next_sample += (len(data) - 8) // 2
                await ws.send_json({"type": "ack", "next_sample": session.next_sample})
                await process(session, ws)
                continue
            try:
                control = json.loads(packet["text"])
            except (ValueError, TypeError, KeyError):
                await send_error(ws, "invalid_control")
                continue
            if not isinstance(control, dict):
                await send_error(ws, "invalid_control")
                continue
            action = control.get("type")
            if action in {"flush", "finish"}:
                if session.completed or (
                    action == "flush"
                    and (
                        type(control.get("through_sample")) is not int
                        or control["through_sample"] != session.next_sample
                    )
                ):
                    await send_error(ws, "offset_mismatch", expected_sample=session.next_sample)
                    continue
                await process(session, ws, flush=True)
                if action == "finish":
                    session.completed = True
                    await ws.send_json(
                        {
                            "type": "completed",
                            "next_sample": session.next_sample,
                            "finalized_through_sample": session.finalized,
                        }
                    )
                else:
                    await ws.send_json(
                        {
                            "type": "flushed",
                            "request_id": control.get("request_id"),
                            "next_sample": session.next_sample,
                            "finalized_through_sample": session.finalized,
                        }
                    )
            elif action == "delete":
                sessions.pop(session.id, None)
                await ws.send_json({"type": "deleted", "session_id": session.id})
                break
            else:
                await send_error(ws, "invalid_control")
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        if session is not None:
            sessions.pop(session.id, None)
        await send_error(ws, "session_expired")
    except Exception:
        logger.exception("Diarization session failed (no audio or credentials logged)")
        if session is not None:
            sessions.pop(session.id, None)
        try:
            await send_error(ws, "session_expired")
        except Exception:
            pass
    finally:
        if session is not None:
            session.attached = False
            session.touched = time.monotonic()
        try:
            await ws.close()
        except RuntimeError:
            pass
