# Phase 5 — Demo, measure, document

**Status:** **PASSED (automated)** — the browser demo itself still needs a human
**Plan source:** `/home/kids/Tau/Tau/06-phase5-demo.md`

## Goal

Make Tau demo-ready and produce the numbers the college report needs.

## Deliverables — all done

1. **Latency instrumentation** — `bridge/src/realtime_api/latency.py`, driven
   from `ws_handler.py`. One CSV row per spoken turn in `logs/latency.csv`
   (host bind mount `/logs`):

   ```
   ts,session,speech_end_to_transcript_ms,transcript_to_first_token_ms,
   first_token_to_first_audio_ms,total_ms,sentences,reply_chars,status
   ```

   Marks (monotonic, first occurrence wins, per turn) live on `Session`:
   `start_turn()` stamps `speech_end` when the pipeline task starts (within a
   few ms of the VAD commit), `mark("transcript")` after STT,
   `first_token_cb` in `llm_client.stream_chat_completion` fires on the first
   content delta, and `first_audio_t`/`last_audio_t` are the existing send
   timestamps. The row is written **before** the echo cooldown, so `total_ms`
   is real generation time, not playback. Missing stages are empty, never `0`.
   `settings.latency_enabled` / `latency_csv` control it.

2. **Measured results** — `tests/phase5_latency.py` (10 turns through the real
   WebSocket, Kokoro-synthesised speech, no browser) → `PASS turns=10 rows=10
   non_completed=0`:

   | Stage | Median | Max |
   | --- | --- | --- |
   | speech end → transcript (STT) | 572 ms | 919 ms |
   | transcript → first LLM token | 244 ms | 7527 ms (turn 1, cold) |
   | first token → first audio chunk | 2392 ms | 3095 ms |
   | speech end → **first audio** | ≈ 3.2 s | ≈ 11.1 s (cold) |
   | speech end → last audio chunk | 5394 ms | 10640 ms |

   Warm turns never exceeded 1.5 s to first token. RAM during the run:
   **6462 / 7607 MB used**, 897 MB available, 2.1 GB swap.

   **Power measured** (2026-11-06): 30 s of `tegrastats --interval 1000`
   around a synthetic spoken reply, MAXN SUPER. `tegrastats` runs without root
   on this image, so no password was needed. Idle **6.6–6.9 W**, peak
   **19.6 W** during the LLM+TTS burst, average **14.8 W** over the four
   active seconds. RAM during the reply: 6785 / 7607 MB (~0.8 GB available).
   Samples are in `logs/power-test.log`.

3. **`tau-orin/README.md`** — architecture diagram, service/port table, start
   steps, the measured table above, reuse table (`realtime-api/` runtime,
   `baby-tau/` service shapes, `TauLegacy/` memory idea), layout, and known
   limits (long replies, per-sentence TTS, echo gate not AEC, flat-file memory,
   one conversation at a time, no wake word, browser path human-tested only).

4. **`tau-orin/start.sh`** — `docker compose up -d`, waits for
   `https://localhost:8080/health`, prints `https://<hostname -I first IP>:8080`,
   warns if `certs/tau.crt` has no SAN entry for that IP. Keeps the
   `sudo -A docker` fallback for sessions predating the docker group.
   Syntax-checked (`bash -n`); run this phase without sudo (docker works as
   `kids` now).

5. **`tau-orin/DEMO.md`** — 2-minute script: greet → two facts → normal
   question → `cat data/facts.md` → `docker restart tau-bridge-1` + page reload
   → "What's my name?" → fresh turn → point at `logs/latency.csv`.

## Bug found while measuring: Speaches hung on a model reload

Turn 1 of the first measurement run timed out: the bridge logged
`[PIPELINE] STT starting` and then nothing for 120 s. Speaches had unloaded
Whisper after its 300 s TTL and, on reload, called huggingface.co for the
`revision/main` check **while holding the model lock** — that HTTPS call stalled
(host `curl https://huggingface.co` returned 200, so it is an in-process stall,
not a dead network) and every later transcription queued behind it.

Fix: `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` on the speaches service. The
weights are already in `./hf-cache`, so the revision check is pointless and
STT can never block on the network again. Applied with
`docker compose up -d speaches` (recreate, no rebuild). After that a direct
`curl` transcription took 1.7 s (model load) and the 10-turn run passed.

## Notes for whoever resumes

- The bridge image was rebuilt after the instrumentation, so a plain
  `docker compose up -d` keeps the code (it is no longer only `docker cp`ed).
- Measurement writes into the same `data/facts.md` the demo uses; the 10 test
  turns left facts behind. `DELETE /memory` clears them.
- Two `[MEMORY]` log lines per event are still duplicated (two handlers on one
  sink) — cosmetic, documented in `phases/phase4.md`.
- Container logs are UTC, host is IST. `latency.csv` timestamps carry `+00:00`,
  so the offset is explicit.

## Done-when

- [x] `logs/latency.csv` holds ≥10 real turns with per-stage timings
- [x] README exists with real measured numbers (no placeholders)
- [x] `start.sh` exists, syntax-checked, prints the URL and cert warning
- [x] demo script written (`DEMO.md`), including the live restart beat
- [x] power measured from 30 s of `tegrastats` during a reply (idle 6.6 W,
      peak 19.6 W)
- [ ] demo run end to end in the browser — needs a human voice on the Jetson
