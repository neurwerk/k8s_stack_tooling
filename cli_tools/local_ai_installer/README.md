# LocalAI Installer

Downloads verified models to external storage and provisions **stock LocalAI**
through an explicit Docker context. HTTP + API key; no Ceph, Kubernetes access,
TLS proxy, scheduler or backend patches. Use LocalAI's UI for runtime management.

## Setup

```bash
uv sync --dev
cp .env.example .env
# Set Docker context, private bind address and LOCALAI_API_KEY (openssl rand -hex 32).
# Set storage root and HF_HOME on external media for downloads/uploads.
uv run local-ai-installer
```

| Command suffix | Action |
| --- | --- |
| `downloads` | Model selection, HF login, download queue and inventory |
| `slots` | Show selected local model presets; works offline |
| `config` | Validate Compose locally |
| `images` | Download/verify Linux/AMD64 image and backend archives locally (Skopeo required) |
| `install` | Load cached images, install local backend archives and start LocalAI; no WAN |
| `upload --dry-run` | Verify local artifacts without server access |
| `upload [slot ...]` | Upload verified bundles, apply presets and restart LocalAI |
| `status` | Show Docker service status |

## Defaults

| Slot | Model | After upload |
| --- | --- | --- |
| `vlm-documents` | Granite-Docling 258M | Enabled |
| `stt-general` | Whisper large-v3 | Enabled |
| `tts-german` | Chatterbox Multilingual V2 | Enabled |
| `ner-german` | OpenMed German PII SuperClinical Large | Enabled |
| `llm-general` | Qwen3-4B Q4_K_M | Disabled |
| `vlm-general` | Qwen3-VL 8B Q4_K_M + projector | Disabled |

Presets/catalog/Compose live in `src/local_ai_installer/resources/`. Existing
external inventory paths are preserved. Installation seeds disabled definitions;
uploads replace selected presets during a maintenance restart. UI changes survive
normal restarts. Old bundles/configuration history are retained for rollback.
Run one installer per target; failed apply leaves LocalAI stopped for inspection.

Native API/UI: `http://<host>:8080`; bearer API key required. Standard chat/audio
paths plus `/api/pii/analyze` with `detectors: ["ner-german"]`. NER uses **UTF-8
byte offsets**; callers must chunk to the 512-token window. Stock Whisper uses
auto language detection; Chatterbox V2 returns completed audio. Native NER disable
semantics differ from chat: enforce access in the client routing layer.

Run `images` on the internet-connected workstation first; archives and temporary
files stay under external storage `docker-images/`. `install` verifies the full
bundle before target changes, loads runtime images through the Docker context,
and uses stock LocalAI `ocifile://` backend installation. All services use
`pull_policy: never`. Verified local tags avoid registry lookups after `docker load`.
The bundle also seeds Chatterbox's checksum-pinned PKUSEG cache (ZIP + extracted data).
Target deployment, cold auxiliary-cache readiness and actual VRAM use remain unverified.
