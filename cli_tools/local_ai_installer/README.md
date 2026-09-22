# LocalAI Installer

A workstation menu for downloading models, deploying an offline LocalAI server,
and testing its APIs through an SSH-backed Docker context.

## Start here

```bash
uv sync --dev
cp .env.example .env
uv run local-ai-installer
```

Configure `.env` with your Docker context, `LOCALAI_API_KEY`, and external model
storage paths. The menu reads these settings from the current directory.
Downloads need workstation internet access; the target server can stay offline.

### Connecting through a jump host

Configure a host alias in `~/.ssh/config`, for example:

```sshconfig
Host ai-server
    HostName 10.0.0.20
    User deploy
    ProxyJump deploy@bastion.example.com
```

Create the context once, then set `LOCAL_AI_INSTALLER_DOCKER_CONTEXT=ai-server`
in `.env`:

```bash
docker context create ai-server --docker host=ssh://ai-server
docker --context ai-server info
```

The remote SSH user needs access to Docker. The installer always uses the configured
context explicitly. API tests execute curl inside the running LocalAI container,
so the workstation does not need network access to port 8080.

## Use the menu

1. **Browse catalog / select downloads** — choose model variants. Compatibility
   labels describe reviewed backend support, not successful inference on your GPU.
2. **Download selected models** — download models and the offline runtime/backend bundle
   to external storage. Image downloads require Skopeo on the workstation.
3. On a new server, choose **Install/update LocalAI runtime** under Setup. This loads
   the offline bundle and starts LocalAI with disabled model definitions.
4. **Upload downloaded models** — copy verified files to the server.
5. **Assign uploaded models to aliases** — choose which model fills each role and
   whether it should be enabled.
6. **Review / apply assignments** — review the plan and restart LocalAI to apply it.
7. **Test API endpoints** — run manual smoke tests and inspect the output.

Setup also contains authentication, storage, bundle downloads, and server status.
Inventory contains the offline deployment table and explicit remote file checks.
Assignments are stored in `deployment.json` on external storage and tied to a target.
Run one installer per target. If applying fails, LocalAI may remain stopped for inspection.

## Manual API tests

Choose **Test API endpoints**, then an individual test or **Run all available tests**.
You can also open this submenu directly with `uv run local-ai-installer test`.
Model storage does not need to be attached for these tests.

| Test | API | Input / output |
| --- | --- | --- |
| Readiness | `GET /readyz` | HTTP status and response |
| List models | `GET /v1/models` | Advertised model IDs |
| Chat | `POST /v1/chat/completions` | Editable prompt → generated text |
| Document vision | `POST /v1/chat/completions` | Local page image → document output |
| General vision | `POST /v1/chat/completions` | Local image → description |
| Text-to-speech | `POST /v1/audio/speech` | Editable German text → local WAV file |
| Speech-to-text | `POST /v1/audio/transcriptions` | Local WAV or generated TTS sample → transcription |
| Speech activity (VAD) | `POST /v1/vad` | Local WAV or generated speech → speech segment boundaries |
| PII / NER | `POST /api/pii/analyze` | Synthetic German text → entities |

Tests show the target, sample curl command, HTTP status, elapsed time, and output.
The displayed command is executed **inside the remote container**; JSON or audio
is supplied on stdin. The actual API key is read from the container environment.
Image/audio inputs travel through Docker/SSH without shared directories or remote downloads.
Generated audio is saved to a unique file in the workstation's temporary directory.

General vision defaults to a generated red/blue PNG. Document vision uses a small
page reading **TEST 123**; select a local PNG/JPEG for a more realistic document. Speech-to-text accepts
a local WAV; leaving the path blank first generates speech with `tts-german`.
Inputs are limited to 16 MiB and each curl request has a five-minute timeout.
VAD accepts PCM16 WAV samples of at most 30 seconds; the menu downmixes and resamples
them to the API's 16 kHz mono float format and appends half a second of silence to
close the final speech segment. It detects speech boundaries, not sound classes
or spoken words.

**Run all** runs sequentially and asks for an optional local speech WAV, so STT/VAD can
be exercised even when TTS is unavailable. Without one, it uses the TTS result.
Live configuration checks skip disabled models, placeholder paths, missing model
files/caches, and missing vision projectors. Failures are printed immediately and
collected in the final summary. Inference may load models into VRAM.
A pass checks HTTP success and response structure; inspect text, entities, and audio
to judge quality. An empty NER entity list is a structurally valid response.

**Record model verification (TTS / chat)** performs the existing configuration-scoped
checks and records a receipt on external storage. Manual API smoke tests do not
update these receipts. Verification is invalidated by changes to target, assignment,
live configuration, artifacts, or backend pins.

**Live runtime / loaded models** displays the running server's actual resource
settings and loaded models. Enabled models load on demand. The default limit is
three active backends, with least-recently-used idle backends unloaded as needed;
this does not restrict the number of enabled models. Busy backends are not forcibly
evicted. Model switching may incur a cold-load delay.

## Runtime notes

The deployment uses stock LocalAI, pinned offline images/backends, HTTP and a bearer
API key. The default bind address is loopback; configure a private interface for LAN
clients if needed. LocalAI's native UI is available at `http://<host>:8080` when reachable.

The bundled aliases cover document vision, general vision, chat, German TTS,
speech-to-text, speech activity detection, and German NER. Presets, catalog and Compose resources live in
`src/local_ai_installer/resources/`. Unverified catalog models require a reviewed
backend recipe before activation. NER reports UTF-8 byte offsets and requires callers
to respect its 512-token input window. Runtime UI changes survive normal restarts;
applying assignments replaces the selected model definitions.

## Development

```bash
uv run ruff check .
uv run ty check
```
