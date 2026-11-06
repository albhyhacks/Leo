# Leo

**A fully local voice companion for the NVIDIA Jetson Orin Nano.**
You talk, Leo answers out loud. Speech recognition, the language model and the
voice all run on the board itself: no cloud API, no account, no key, and no
network once the images and weights are on disk.

> Status: a working end-to-end prototype, built and measured on a Jetson Orin
> Nano 8 GB. It is a research/hobby project, not a medical or safety product.
> See [Known limits](#known-limits) for what is and is not proven.

## Why local

A microphone that is always on in your home should not be streaming to someone
else's server. Leo is built so that the audio, the transcripts and the memory
never leave the device, and so that this can be checked: you can switch Wi-Fi
off and it keeps working.

## How it works

```
   browser on the Jetson (mic + speaker + UI)   https://localhost:8080
        │  ▲
        │  │  wss://localhost:8080/v1/realtime   (PCM16 24 kHz both ways)
        ▼  │
   ┌────────────────────────────────────────────────────────────────┐
   │  bridge — FastAPI, HTTPS                                       │
   │  Silero VAD → Parakeet (STT) → Ollama (LLM) → Kokoro (TTS)     │
   └───┬───────────────┬────────────────┬───────────────────────────┘
       │ :9002         │ :8000          │ :8880
       ▼               ▼                ▼
   Parakeet TDT     Ollama           Kokoro TTS
   0.6B v2 int8     llama3.2:1b      voice af_alloy
   sherpa-onnx CPU  GPU, pinned      24 kHz PCM16
```

| Piece | Service | Model | Port |
| --- | --- | --- | --- |
| Speech → text | `parakeet` (`./parakeet`) | NVIDIA Parakeet TDT 0.6B v2, int8, via sherpa-onnx (CPU) | 9002 |
| Text → text | `ollama` | `llama3.2:1b`, 1536 ctx, kept loaded | 8000 |
| Text → speech | `kokoro-tts` | Kokoro, voice `af_alloy` | 8880 |
| Orchestration | `bridge` (`./bridge`) | server VAD, sentence-level TTS streaming, memory | 8080 (HTTPS) |

All four containers use host networking. The browser is only a microphone, a
speaker and a view; it talks to its own origin and loads no external resources.

## Features

- **Hands-free voice chat** with barge-in handling and an echo gate while Leo
  speaks.
- **Continuous conversation:** the last 20 turns are stored in
  `data/history.jsonl` and reloaded after a restart.
- **Long-term memory:** `data/facts.md` holds durable facts about you.
  Extraction is rule-based (first-person patterns such as "my name is…"), not
  an LLM call, so it cannot invent facts, costs no tokens, and every fact is
  human-readable and deletable.
- **Personality** defined in `prompts/tau.system.md`; replies are capped at
  three short spoken sentences.
- **STT safeguards:** noise-phrase and repetition-loop filtering so a
  hallucinated transcript never reaches the LLM.
- **Per-turn latency log** in `logs/latency.csv`, plus optional raw-audio
  capture per turn for debugging the microphone path.
- **One focused interface** at `/`: Leo's live words as large type, a flat 3D
  sun whose rays follow the real pipeline state and your voice, memories as dots
  around it, and two drawers: **Conversation** (stored history) and **Systems**
  (Jetson vitals, service health, models, measured latency). No external
  resources; it runs fully offline.

## Requirements

- NVIDIA Jetson Orin Nano (8 GB) on JetPack 6 / L4T R36, with the NVIDIA
  container runtime. Developed on L4T R36.5.
- Docker with Compose v2, and your user in the `docker` group.
- About 20 GB of free disk for images and models.
- A USB or built-in microphone and speakers/headphones, and Firefox or Chrome.
- Internet **only once**, to pull images and download weights.

## Setup

```bash
git clone https://github.com/albhyhacks/Leo.git
cd Leo

# 1. one-time downloads (need internet)
./scripts/get-parakeet.sh                 # Parakeet STT weights (~230 MB)
docker compose pull ollama kokoro-tts     # model server images
docker compose up -d ollama               # then pull the LLM into ./ollama:
docker exec tau-ollama-1 ollama pull llama3.2:1b

# 2. HTTPS certificate (browsers only allow the mic on a secure origin)
./scripts/gen-cert.sh                     # add a LAN IP as an argument if needed

# 3. start everything (first run builds the bridge image, ~8 min)
./run-local.sh
```

`run-local.sh` runs `start.sh` (starts the four containers and waits for the
bridge health check) and opens Firefox at `https://localhost:8080`. Accept the
self-signed certificate warning once, then allow the microphone. Say one
sentence as a warm-up; the first turn after boot is the slowest.

Use `https://localhost:8080` rather than the board's Wi-Fi address if you want to
run with the network off — a page loaded from the Wi-Fi IP loses its connection
when Wi-Fi goes down.

## Running fully offline

1. Start Leo as above and confirm one reply works.
2. Turn Wi-Fi off.
3. Keep talking. Everything on the path is on the loopback interface.

The page and its scripts reference no external URLs, and the services read their
models from local disk. Network-off operation has been reasoned about and
spot-checked, not yet proven with a packet capture.

## Performance

Measured on the Jetson Orin Nano 8 GB, MAXN SUPER power mode, with the LLM fully
resident on the GPU. These come from synthetic spoken turns pushed through the
real WebSocket (not human speech).

| Stage | Typical |
| --- | --- |
| Speech end → transcript (Parakeet) | about 0.7 s (a 5 s clip decodes in about 0.6 s) |
| Transcript → first LLM token | about 1.0–1.3 s |
| First token → first audio (wait for first sentence + Kokoro) | about 2.7–3 s |
| Speech end → first audio | about 5 s |
| LLM generation | about 17–24 tokens/s |

Rough resource use: about 6–7 GB of 7.6 GB RAM in total with everything loaded.
Memory is the main constraint on this board; see below.

To reproduce:

```bash
docker cp tests/phase5_latency.py tau-bridge-1:/tmp/p5.py
docker exec tau-bridge-1 python /tmp/p5.py       # then: column -s, -t logs/latency.csv
```

The tests write to `data/`; back it up first.

## Known limits

- **Real-microphone accuracy is not yet established.** Parakeet transcribes
  clean synthetic speech correctly, but in earlier sessions Whisper produced
  wrong or looping transcripts from live-mic audio. The browser resampler was
  rewritten and a filter added, but this has not been verified across
  microphones and rooms. Mic gain matters: clipped input hurts recognition.
- **8 GB is tight.** If free RAM runs out, Ollama can fall back to running the
  model on the CPU, and replies slow from seconds to minutes. Check
  `curl localhost:8000/api/ps`: `size_vram` should be close to `size`.
  Keep to one UI tab, and close other heavy applications.
- **The 1B model is weak.** It rambles, forgets, and can say wrong things; it
  should never be trusted for facts, advice or anything safety-related.
- **Latency is not conversational yet.** About 5 s to first audio; the bridge
  waits for a full sentence before synthesis.
- **No true echo cancellation.** The mic is gated while Leo speaks; use
  headphones for reliable barge-in.
- **Flat-file memory.** About 40 facts and 20 turns, no embeddings or summaries.
- **The animation costs CPU** on a board that already runs hot. It lowers its
  frame rate when idle, and the Systems drawer has a calm mode.
- **One conversation at a time.** No wake word, no vision.

## Repository layout

```
.
├── docker-compose.yaml     services: ollama, parakeet, kokoro-tts, bridge (+ optional whisper profile)
├── run-local.sh            start the stack and open the UI on this machine
├── start.sh                start the stack and wait for health
├── scripts/                get-parakeet.sh, gen-cert.sh
├── bridge/                 FastAPI realtime bridge (src/realtime_api/) and the web UI (static/)
├── parakeet/               STT server (sherpa-onnx) and its Dockerfile
├── prompts/tau.system.md   Leo's personality prompt
├── tests/                  WebSocket smoke, memory and latency tests
├── DEMO.md                 two-minute demo script
├── PROGRESS.md, phases/    engineering log: what was tried, rejected, and why
├── docs/                   archived engineering notes
├── data/                   conversation history and facts (git-ignored, bind-mounted)
└── logs/                   latency CSV and service logs (git-ignored)
```

## Privacy

`data/` holds your conversation history and the facts Leo extracted. It is
git-ignored and stays on the device. `DELETE /memory` and `DELETE /memory/{id}`
forget everything or one fact. TLS keys are generated per machine and not
committed.

## Credits and license

The realtime bridge (`bridge/`) is derived from `realtime-api` in
[OriNachum/autonomous-intelligence](https://github.com/OriNachum/autonomous-intelligence)
(MIT). Speech models: NVIDIA Parakeet TDT 0.6B v2 through
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), and Kokoro TTS. The LLM is
Meta's Llama 3.2 1B via Ollama. Container images come from
[dusty-nv/jetson-containers](https://github.com/dusty-nv/jetson-containers).

Released under the MIT License; see [LICENSE](LICENSE).
