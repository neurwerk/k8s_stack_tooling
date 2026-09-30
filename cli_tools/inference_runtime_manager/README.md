# Inference Runtime Manager

A workstation CLI for downloading verified model artifacts and managing explicit
inference containers through an SSH-backed Docker context. It does not contact
Kubernetes and never relies on the current Docker context implicitly.

## Start

```bash
uv sync --dev
uv run inference-runtime-manager
```

Use **Configuration** on first launch to select the target Docker context,
workstation Docker context and model staging storage. Copy `.env.example` to
`.env` only when environment defaults or service API keys are needed. Non-secret
interactive choices are stored under the workstation config directory. The old
`LOCAL_AI_INSTALLER_DOCKER_CONTEXT`,
`LOCAL_AI_INSTALLER_STORAGE_ROOT` and `LOCALAI_BIND_ADDRESS` names remain accepted
for migration.

Model staging storage may be a mounted external volume or a directory under
`~/.storage`, such as `~/.storage/inference-runtime-manager`. Select the directory
under **Configuration → Select model staging storage**; this also places `HF_HOME`
in its `huggingface` subdirectory. Other paths on the system volume are rejected
so an unmounted external drive cannot silently redirect downloads to the system disk.

For an SSH target:

```bash
docker context create ai-server --docker host=ssh://ai-server
docker --context ai-server info
```

## Workflow

1. Choose a stable service alias, shown with a short purpose description, and review its
   saved model, runtime, local artifacts and live target state.
2. Choose whether to change the alias's runtime, model and compatible variant.
3. Decide whether to download missing model files now or queue them for later.
4. Decide whether to prepare the required runtime image on the workstation.
5. Decide whether to stream the runtime to the offline target and activate the model.
6. Run the offered manual endpoint check and inspect output quality.

The guided workflow combines upload, assignment, enablement and readiness checks
behind the activation confirmation. **Advanced operations** retains the individual
steps and runtime-only image updates for diagnostics and unusual maintenance.

The catalog browser lists each model once, then opens a variant submenu for
models published in multiple formats or quantizations. Entries retained in an
older download queue but removed from the current catalog are shown explicitly;
the manager asks before dropping them and leaves the queue unchanged when the
operator declines or cancels.

The browser separates structured document parsing from private image OCR even
though both remain stored under the existing OCR artifact category. Granite-
Docling produces DocTags for `vlm-documents`; LightOnOCR, olmOCR and Nanonets
serve the mutually exclusive `vlm-images` attachment-inspection recipes.

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

Download selections and assignments live in small workstation state files and are
bound to one Docker context. Legacy `download.yaml` and `deployment.json` files are
imported from connected staging storage once. Existing assignments without a
runtime, or with the removed LocalAI runtime, are migrated to the reviewed runtime
for that alias. Large model artifacts and their installed inventory remain on the
selected staging storage.

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
| `vlm-images` | LightOnOCR; olmOCR Q6_K; Nanonets OCR2 | 8007 | LightOnOCR disabled |
| `image-generation-general` | catalog only | none | unavailable |

All inference services mount the preserved `local-ai_models` volume read-only.
The Docker project remains `local-ai` so installation can remove superseded
LocalAI containers as Compose orphans without copying model data.

The alias in the table is also the stable client-facing model name. Runtime
configuration maps it to the selected artifact or upstream model identifier, so
changing an assignment does not require a client configuration change.
For `vlm-images`, applying one recipe stops the other image-reader containers but
keeps their verified model files and runtime images available for later switching.

Each stable service alias has an independent optional key setting in `.env`.
An empty setting disables authentication for that service, and operators may use
the same value in several settings until separate rotation is needed. vLLM,
llama.cpp, Speaches and the package-owned Kokoro adapter enforce their configured
key directly. The pinned Chatterbox and KServe runtimes do not enforce their
reserved settings; keep those endpoints bound to loopback or a trusted private
interface.

The package-owned Kokoro and OCR prompt-adapter runtimes contain no model weights.
The manager pulls or builds locked `linux/amd64` images through
`INFERENCE_RUNTIME_MANAGER_BUILD_DOCKER_CONTEXT` (default `desktop-linux`), rejects
SSH/TCP build contexts, and streams selected images from that workstation Docker
store to the offline target. Runtime updates therefore do not depend on model
staging storage. The pinned Martin ONNX model, voice and German normalization files
remain a separately verified model artifact mounted at runtime. Existing Chatterbox
voice recordings remain preserved when Martin is selected.

All image-reader recipes expose the `vlm-images` OpenAI-compatible API.
LightOnOCR consumes the image directly. The package-owned olmOCR and Nanonets
images keep their inference backend on container loopback and inject a model-specific
OCR prompt before forwarding. olmOCR accepts one optional per-image text prompt in
place of its built-in default; Nanonets still rejects caller text. Remote image URLs,
multiple images and streaming are rejected at that adapter boundary. olmOCR YAML
front matter is validated and removed from completed default-prompt transcriptions;
custom prompts return the model's text as-is. Truncated completions retain
`finish_reason: length` for the caller's fail-closed handling.
The Nanonets checkpoint publishes no model license; its catalog entry keeps
commercial permission explicitly unconfirmed for operator review.

## Manual Checks

`uv run inference-runtime-manager test` tests every enabled service. Requests
run with `docker compose exec` inside the corresponding remote container and use
that container's loopback port. The workstation does not need direct access to
ports 8000-8007 and the bearer key is not printed.

The flow checks health first, then exercises chat/vision, document parsing, TTS,
STT, VAD or NER as applicable. TTS output can feed the speech checks, or the
operator can select a local WAV. A successful response proves endpoint structure,
not model quality or GPU fit.

For an individual **vlm-images** check, choose the standard `TEST 123` image or a
local PNG/JPEG (up to 16 MiB). The local-image check prints the complete OCR output
for review. When olmOCR is assigned, choose its built-in document prompt or provide
a prompt for this image as text or a UTF-8 file (up to 4096 characters). The default
still validates and removes olmOCR's YAML front matter; a custom prompt returns the
model's text without requiring that document-specific metadata. Other image-reader
recipes accept custom images but keep their own built-in prompts. After updating
the packaged olmOCR adapter, prepare and apply its runtime image before sending
custom prompts to an existing deployment.

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
it is model-only inference time. Use **Prepare or update runtime images** to deploy
an updated packaged Kokoro adapter, then accept the prompt to recreate affected
enabled services.

## GPU Notes

GPU services use `gpus: all` and `NVIDIA_DRIVER_CAPABILITIES=compute,utility`.
The NVIDIA container runtime cannot enforce hard per-service VRAM reservations
on a non-MIG Quadro RTX 5000. The manager warns when enabled-service estimates
exceed 14 GiB but never stops another service automatically.

Granite-Docling uses the immutable Transformers checkpoint and serves model name
`vlm-documents`. Stored weights remain BF16;
`VLLM_GRANITE_DTYPE=float32` controls computation on the Turing GPU.

Every image reader serves the `vlm-images` alias with one image and one request at
a time. LightOnOCR uses FP32 computation, a 4096-token context and a 2048-token
output cap because FP16 produces non-finite probabilities on the target Turing GPU.
olmOCR uses the Q6_K GGUF with its F16 vision projector and a 16384-token llama.cpp
context. Nanonets uses FP16 vLLM computation, an 8192-token context and the separate
`VLLM_NANONETS_GPU_MEMORY_UTILIZATION` reservation. The stable API retains the
2048-token output cap for all three recipes. olmOCR and Nanonets remain candidates
until their exact recipes are exercised on the target GPU; select only one image
reader at a time.

## Validation

Automated validation uses linting, formatting, type checking, Compose rendering
and package building. Runtime behavior and live inference are validated only by
the operator-confirmed manual endpoint checks.

```bash
uv run ruff check src
uv run ruff format --check src
uv run ty check
uv build
```
