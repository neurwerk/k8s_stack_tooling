# Inference Runtime Manager

A workstation CLI for downloading verified model artifacts and managing explicit
inference containers through an SSH-backed Docker context. It does not contact
Kubernetes and never relies on the current Docker context implicitly.

## Start

```bash
uv sync --dev
cp .env.example .env
uv run inference-runtime-manager
```

Set the Docker context, external storage root and API key in `.env`. The old
`LOCAL_AI_INSTALLER_DOCKER_CONTEXT`, `LOCAL_AI_INSTALLER_STORAGE_ROOT`,
`LOCALAI_API_KEY` and `LOCALAI_BIND_ADDRESS` names remain accepted for migration.

For an SSH target:

```bash
docker context create ai-server --docker host=ssh://ai-server
docker --context ai-server info
```

## Workflow

1. Browse the catalog and select model variants.
2. Download models and prepare immutable Linux/AMD64 runtime images on external storage.
3. Install the offline runtime images on the target.
4. Upload verified model files.
5. Assign an uploaded model to a stable service alias.
6. Enable or disable services and apply the assignments.
7. Run **Test all enabled endpoints** and inspect output quality manually.

## Default TTS Voice

When the active `tts-german` assignment is Chatterbox, choose **Record and
provision default TTS voice** from the **Setup** section. The
manager displays a German reading script, lets the operator select a microphone,
records for 80 seconds, validates and plays the local recording, then asks before
contacting the configured Docker target. Bluetooth headset microphones commonly
switch to a low-bandwidth hands-free profile and can sound distorted; prefer the
Mac's built-in microphone or a USB/wired microphone for voice cloning. Recording
requires an existing `ffmpeg` executable; the manager never installs workstation
packages. On macOS, install it separately with `brew install ffmpeg` if it is not
already available.

The manager uploads the WAV with SHA-256 verification into the persistent runtime
configuration volume only after verifying that the assigned Chatterbox artifact is
active and that Chatterbox is the sole running TTS recipe. It then recreates only
`tts-german`. For comparison, the manager plays the original recording, Chatterbox
reading the same full German script, and a short one-line test with the temporary
candidate voice. The previous `default` voice is replaced atomically only after the
operator accepts the comparison. Local recordings and generated samples remain in
a temporary directory and are deleted when the workflow exits.

The same flow is available as the following command, which rejects recipes without
the `voice_cloning` capability:

```bash
uv run inference-runtime-manager voice
```

The voice recording is private biometric material. It is never placed in Git or
external model storage. Anyone who can call the unauthenticated Chatterbox endpoint
can synthesize with the configured default voice, so keep that endpoint on the
documented trusted network boundary.

Assignments live in external-storage `deployment.json` and are bound to one
Docker context. Existing assignments without a runtime, or with the removed
LocalAI runtime, are migrated to the reviewed runtime for that alias.

Applying one assignment atomically points `/models/active/<alias>` at its
immutable uploaded artifact, stops alternative recipes for the alias, and
recreates the selected service. Disabling stops every recipe service for that
alias. There is no gateway, request-driven activation, backend scheduler or
automatic eviction.

After choosing a model, the menu offers to apply that assignment immediately.
The **Live deployment status (fast)** and **Review / apply assignments** screens
show saved choices alongside running services, health, and the active model
links read from running containers.
Applying again skips models whose selected service is already healthy, uses the
expected runtime image and has a matching active model link. Changed services
report local verification, remote upload, activation and startup times. A stopped
service is identified before a manual test with the menu action needed to start it.

Routine status and tests do not start temporary setup containers or re-hash
model weights. Upload verifies the model before publishing it, and the separate
**Verify remote model files** action performs full local and remote SHA-256
verification when requested. The displayed active link identifies model files;
the endpoint test checks that the service actually responds.

## Services

| Alias | Runtime | Host port | Default |
| --- | --- | ---: | --- |
| `vlm-documents` | vLLM | 8000 | enabled |
| `llm-general` | llama.cpp | 8001 | disabled |
| `vlm-general` | llama.cpp | 8002 | disabled |
| `stt-general` | Speaches / Faster-Whisper | 8003 | enabled |
| `tts-german` | Kokoro ONNX German Martin; Chatterbox fallback | 8004 | Kokoro enabled |
| `vad-general` | Speaches / packaged Silero VAD | 8005 | enabled |
| `ner-german` | KServe Hugging Face token classification | 8006 | disabled |
| `image-generation-general` | catalog only | none | unavailable |

All inference services mount the preserved `local-ai_models` volume read-only.
The Docker project remains `local-ai` so installation can remove superseded
LocalAI containers as Compose orphans without copying model data.

The alias in the table is also the stable client-facing model name. Runtime
configuration maps it to the selected artifact or upstream model identifier, so
changing an assignment does not require a client configuration change.

vLLM, llama.cpp and Speaches enforce the configured bearer key. Kokoro,
Chatterbox and KServe do not provide equivalent authentication; keep their ports
bound to loopback or a trusted private interface until a separate authenticated
client boundary is adopted.

The package-owned Kokoro runtime contains no model weights. `images` builds its
locked `linux/amd64` image through
`INFERENCE_RUNTIME_MANAGER_BUILD_DOCKER_CONTEXT` (default `desktop-linux`), rejects
SSH/TCP build contexts, and writes the verified Docker archive directly to external
storage. The pinned Martin ONNX model, voice and German normalization files remain
a separately verified model artifact mounted at runtime. Existing Chatterbox voice
recordings remain preserved when Martin is selected.

## Manual Checks

`uv run inference-runtime-manager test` tests every enabled service. Requests
run with `docker compose exec` inside the corresponding remote container and use
that container's loopback port. The workstation does not need direct access to
ports 8000-8006 and the bearer key is not printed.

The flow checks health first, then exercises chat/vision, document parsing, TTS,
STT, VAD or NER as applicable. TTS output can feed the speech checks, or the
operator can select a local WAV. A successful response proves endpoint structure,
not model quality or GPU fit.

Each manual check prints the assigned catalog model name, model ID, variant,
runtime, Docker context and service. For running services it reads the active
model link and checks whether it matches the assignment; this is distinct from
a response-reported model (which may only be the stable endpoint alias). The
test-all summary includes the assigned model for each result. A mismatch means
the response's underlying model cannot be verified from the saved assignment
alone.

The standard and custom-text TTS checks save the generated WAV and then play it
locally, including during test-all. Playback uses `afplay` on macOS or `ffplay`
elsewhere. If no player is available or playback fails, the saved WAV remains
available and successful generation still passes its endpoint check. The output
reports Docker roundtrip and service HTTP time in milliseconds, model generation
time when the runtime provides it, WAV duration, byte size and audio format,
plus save and playback time. Generation-only timing requires an updated Kokoro
image; older images and other TTS runtimes show request timing without claiming
it is model-only inference time. Use **Download runtime image bundle** and
**Install/update runtime images**, then **Review / apply assignments**, to
deploy an updated packaged Kokoro adapter.

## GPU Notes

GPU services use `gpus: all` and `NVIDIA_DRIVER_CAPABILITIES=compute,utility`.
The NVIDIA container runtime cannot enforce hard per-service VRAM reservations
on a non-MIG Quadro RTX 5000. The manager warns when enabled-service estimates
exceed 14 GiB but never stops another service automatically.

Granite-Docling uses the immutable Transformers checkpoint and serves model name
`vlm-documents`. Stored weights remain BF16;
`VLLM_GRANITE_DTYPE=float32` controls computation on the Turing GPU.

## Validation

The project intentionally has no automated test suite. CI and local review use
linting, formatting, type checking, Compose rendering and package building. Live
inference validation remains the operator-confirmed menu action.

```bash
uv run ruff check src
uv run ruff format --check src
uv run ty check
uv build
```
