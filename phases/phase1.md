# Phase 1 — Model services up

**Plan source:** `/home/kids/Tau/Tau/02-phase1-services.md`
**Status:** PASSED (2026-11-05)
**Goal:** Run Ollama, Speaches and Kokoro TTS together, each verified with `curl`.

## Pre-work findings (from reading `baby-tau/`)

- `baby-tau/docker-compose.yaml` uses `network_mode: host`, `runtime: nvidia`,
  `/dev/snd` and `/dev/bus/usb`. We only need `network_mode: host` + `runtime: nvidia`
  (the Jetson has no local mic/speaker; `/dev/snd` can be dropped if absent).
- The Ollama service sets `OLLAMA_MODELS=${OLLAMA_MODEL}` — in Ollama,
  `OLLAMA_MODELS` is the **model storage directory**, not the model name.
  This is a bug in the upstream compose. We'll use `OLLAMA_HOST=0.0.0.0:8000`
  and pull models explicitly with `docker exec`.
- The Speaches service overrides the command with `tail -f /dev/null`,
  which likely prevents the server from starting. Must inspect the image
  entrypoint/cmd and `ss -ltnp | grep 8001` before changing anything.
- `baby-tau/jetson/main.py` sends `model=Systran/faster-whisper-tiny` (its own choice).
  Phase 2 will use whatever model we verify here.
- TTS request shape (from `baby-tau/jetson/main.py`):
  `POST /v1/audio/speech` JSON `{input, voice, response_format:"wav"}`.

## Checklist

- [x] 1. Create `tau-orin/docker-compose.yaml` with only `ollama`, `speaches`,
      `kokoro-tts` (+ shared x-block), no `ports:`
- [x] 2. Inspect Speaches entrypoint/cmd; verify port 8001 listening; fix command
- [x] 3. `docker compose --env-file .env up -d`; follow logs
- [x] 4. Pull `llama3.2:3b` and `llama3.2:1b`; test LLM, STT, TTS
- [x] 5. `tegrastats` 10 s with all loaded; report RAM
- [x] Save outputs to `logs/phase1-tests.md`

## Results (see `logs/phase1-tests.md` for full output)

| Test | Result |
| --- | --- |
| LLM 1B short | 0.271 s, "Hello, how are you?" |
| LLM 1B throughput | 167 tokens / 5.00 s = **33.4 tok/s** |
| LLM 3B standalone | **18.4 tok/s** (OOM with STT+TTS loaded) |
| STT tiny warm | **0.45 s**, transcript "Hello, I am Tau." |
| TTS Kokoro | **0.536 s**, PCM16 mono **24000 Hz** |
| RAM all loaded | 6904/7607 MB, 642 MB available, 3.5 GB swap |

### Fixes made during Phase 1 (differences from the plan)

1. **Speaches command override removed.** The real entrypoint is
   `/opt/speaches/start-server.sh` and reads `$PORT`. The upstream
   `tail -f /dev/null` override never started the server.
2. **Speaches working directory set to `/opt/speaches`.** The app opens
   `model_aliases.json` relative to CWD; it only exists in `/opt/speaches`.
   Upstream baby-tau copied it to `/` instead.
3. **Ollama `OLLAMA_MODELS` bug avoided.** The upstream compose sets
   `OLLAMA_MODELS=<model name>`, but that variable is the model storage
   directory. We mount `./ollama:/data/models/ollama/models` and pull models
   explicitly.
4. **1B is the production model.** 3B OOMs when STT+TTS are loaded on 8 GB.
5. **`faster-whisper-tiny`** instead of small/base: keeps STT latency ~0.45 s
   and memory ~650 MB. Base was ~940 MB; TTL=0 reload cost 2.9 s/turn.
6. **Ollama memory tuning:** `OLLAMA_CONTEXT_LENGTH=1536`,
   `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_NUM_PARALLEL=1`,
   `OLLAMA_MAX_LOADED_MODELS=1`.
7. **Kokoro stays on GPU.** CPU Kokoro used 1.5 GB and took ~8 s/sentence.
   Speaches' Piper path works (`en_US-lessac-medium`, 22050 Hz) but is
   ~2.8 s/sentence; kept as a fallback.

## Done-when check
- [x] all three curl tests succeed together
- [x] tokens/s for 3B and 1B reported
- [x] Kokoro sample rate (24000 Hz) reported
- [x] STT model name (`Systran/faster-whisper-tiny`) reported
- [x] RAM headroom reported (~642 MB available; tight — desktop kept per user)
