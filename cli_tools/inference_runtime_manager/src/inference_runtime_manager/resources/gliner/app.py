"""Offline, bounded GLiNER span inference; never log or echo submitted text."""

import asyncio
import json
import math
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import torch
from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from gliner import GLiNER
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.concurrency import run_in_threadpool


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GLINER_", extra="ignore")
    model_name: str = Field(pattern=r"^ner-(english|german|multilingual)$")
    model_dir: Path
    dtype: Literal["float32", "float16"] = "float32"
    threshold: float = Field(default=0.5, gt=0, lt=1)
    labels: list[str] = Field(min_length=1, max_length=25)
    api_key: SecretStr | None = None


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8192)


settings = Settings()
model = None
busy = asyncio.Lock()


@asynccontextmanager
async def lifespan(app):
    global model
    if not torch.cuda.is_available():
        raise RuntimeError("GLiNER requires a visible CUDA device")
    if len(settings.labels) != len(set(settings.labels)) or any(
        not label.strip() or len(label) > 64 for label in settings.labels
    ):
        raise RuntimeError("Invalid GLiNER entity labels")
    torch.set_num_threads(2)
    source = settings.model_dir.resolve()
    # Resolve the legacy checkpoint's backbone identifier without changing its
    # immutable source files or requiring a populated remote Hugging Face cache.
    with tempfile.TemporaryDirectory(prefix="gliner-runtime-") as directory:
        local = Path(directory)
        config = json.loads((source / "gliner_config.json").read_text())
        config["model_name"] = str(source / "backbone")
        (local / "gliner_config.json").write_text(json.dumps(config))
        (local / "pytorch_model.bin").symlink_to(source / "pytorch_model.bin")
        model = GLiNER.from_pretrained(
            str(local),
            local_files_only=True,
            map_location="cuda",
            dtype=getattr(torch, settings.dtype),
            strict=True,
        ).eval()
        if any(parameter.device.type != "cuda" for parameter in model.parameters()):
            raise RuntimeError("GLiNER parameters must remain on CUDA")
        yield
        model = None
        torch.cuda.empty_cache()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


def authenticate(authorization):
    key = settings.api_key.get_secret_value() if settings.api_key else ""
    if key and not secrets.compare_digest(authorization or "", "Bearer " + key):
        raise HTTPException(status_code=401, detail="Authentication required")


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    return JSONResponse(status_code=422, content={"detail": "Invalid extraction request"})


@app.get("/health")
def health():
    if model is None:
        raise HTTPException(status_code=503, detail="Model is not ready")
    return {"model": settings.model_name, "device": "cuda", "dtype": settings.dtype}


def extract_spans(text):
    processor = model.data_processor
    words = [word for word, _, _ in processor.words_splitter(text)]
    # Use the same prompt words and tokenizer as GLiNER, before any truncation.
    prompt = []
    for label in settings.labels:
        prompt.extend([processor.ent_token, label])
    prompt.append(processor.sep_token)
    tokens = processor.transformer_tokenizer(prompt + words, is_split_into_words=True)
    if len(words) > model.config.max_len or len(tokens["input_ids"]) > 512:
        raise HTTPException(status_code=413, detail="Input exceeds model limits; chunk text first")
    with torch.inference_mode():
        entities = model.predict_entities(text, settings.labels, threshold=settings.threshold)
    result = []
    for entity in entities:
        start, end, score = entity["start"], entity["end"], float(entity["score"])
        if not (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(text)
            and math.isfinite(score)
            and 0 <= score <= 1
            and entity["label"] in settings.labels
            and entity["text"] == text[start:end]
        ):
            raise HTTPException(status_code=502, detail="Invalid model prediction")
        result.append({"start": start, "end": end, "label": entity["label"], "score": score})
    return {"model": settings.model_name, "entities": result}


@app.post("/extract")
async def extract(payload: ExtractionRequest, authorization: str | None = Header(default=None)):
    authenticate(authorization)
    if busy.locked():
        raise HTTPException(status_code=429, detail="Model is busy; retry later")
    async with busy:
        return await run_in_threadpool(extract_spans, payload.text)
