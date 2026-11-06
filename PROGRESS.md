# Tau on Jetson Orin Nano — Progress Log

## Leo presence UI handoff — 2026-11-06

This is a new UI task layered over the working Tau voice stack described below.
The previous completed stack is preserved; the current home page remains the
old interface while the `/stage` prototype is reviewed.

### Phase 0 — instrumentation prepared; Yeti absence identified

- Added per-turn raw PCM capture to `logs/audio/turn-N.wav` (24 kHz, mono,
  PCM16) at the VAD turn boundary. Each file logs duration, peak, RMS, and
  clipped sample count in `/logs/realtime-api.log`.
- Added early TTS chunk flush at a comma after at least 40 characters. Reply
  sentence cap still counts terminal punctuation.
- Python compilation and `git diff --check` pass. The bridge was quick-updated
  with `docker cp` and restarted (no image rebuild).
- Verified `/health` returns 200; `/stage`, `/classic`, and `/static/stage.js`
  return 200; all four containers are up. `/api/stats` immediately after
  restart reported 480 MB available, 5.5 GB swap used, 39.2% CPU, 86% GPU,
  53.3 C, and 7.46 W. This is one instantaneous reading, not an idle baseline.
- No mic turn has been recorded yet. Transcript accuracy, saved WAV quality,
  STT re-transcription, and first-audio latency are not verified. Do not claim
  the Yeti profile or recognition accuracy is fixed without this evidence.
- Read-only device checks found no Blue Yeti in either `lsusb` or `pactl list
  cards`. The active default source is the Plantronics Blackwire microphone;
  no Yeti profile can be named while the Yeti is absent. This identifies the
  current real-mic blocker, but does not establish Yeti transcription quality.
- Routed new playback to the verified Jetson built-in analog sink
  (`alsa_output.platform-sound.analog-stereo`) as requested; confirmed the
  microphone default remains Plantronics until the Yeti is connected.
- Next: speak five sentences through the Yeti in `/classic` (or current root),
  then inspect each WAV's level/clipping and replay those exact WAVs through
  Parakeet. If channel/profile is wrong, inspect `pactl list cards` and report
  the matching `pactl set-card-profile <card> <profile>` command; do not guess
  the card or profile name.

### Phase 1 — prototype ready for visual review

- Added isolated `/stage` using `stage.html`, `stage.css`, and `stage.js`.
  It has a warm sun core, state selector, signal sliders, synthesized earcon,
  and calm mode. Rendering uses Canvas 2D at at most 30 fps,
  caps DPR at 1.5, and pauses while hidden. `/classic` keeps the existing page
  reachable; `/` is unchanged.
- Laptop/phone screenshots and a user look approval are pending.
- Next: review `/stage` on desktop and phone; look approval is not yet recorded.

### Phase 2 — live browser wiring implemented; human trial pending

- `/stage` now opens `/v1/realtime` with the confirmed `realtime` subprotocol,
  reconnects with bounded backoff, and maps the existing speech-started,
  speech-stopped, transcript, response, and audio-delta events to its visible
  state and captions.
- Added user-initiated mic capture with the existing 24 kHz PCM16 wire format,
  stateful windowed-sinc resampling, and the existing echo-cancellation,
  noise-suppression, and auto-gain constraints. The mic is muted from speech
  end through Leo playback because the bridge runs with `aec=none`.
- TTS chunks are scheduled as PCM16 at 24 kHz; their measured RMS drives the
  visual level. Captions show the server's completed transcript and streamed
  assistant transcript. A timeout shows “didn't catch that” if no transcript
  arrives. This is not token-confidence dimming. The correction path is in
  Phase 4 below.
- `node --check bridge/static/stage.js` and `git diff --check` pass; `/health`,
  `/stage`, and the served script return 200. No browser/mic turn was run, so
  event order, device playback, transcripts, reconnect behavior in Firefox,
  and actual CPU use remain to be verified with a user trial.
- Phase 0 still has no clips. Phase 1 look approval and mobile/laptop screenshots
  are still pending; Phase 2's spoken-sentence acceptance check is pending.
- Next: open `https://localhost:8080/stage`, tap the mic, and speak one sentence.
  This is the required human check for both Phase 0 capture and Phase 2 live
  event/audio behavior. Phase 1 look approval and Phase 3 add/delete visual
  acceptance remain pending.

### Phase 3 — memory constellation implemented; add/delete trial pending

- Replaced generated sky points with saved facts loaded from the verified
  `GET /memory` shape (`count`, `items[{id,text}]`). Positions use a stable hash
  of each existing text-derived ID. New IDs animate in after polling; the page
  fetches every 5 s only while visible.
- Hover/tap selects a fact; its Forget button calls the existing
  `DELETE /memory/{id}` route and refreshes. Matching reply/fact words cause a
  temporary star brightening and light thread; this is explicitly labeled as a
  **word overlap heuristic**, not semantic recall certainty.
- Verified the live endpoint currently contains one real fact (Arib's name),
  and `/health`, `/stage`, `/classic`, and `/static/stage.js` return 200.
  JavaScript syntax and `git diff --check` pass. Did not alter user memory to
  create a disposable test fact; add/delete visibility is pending a user turn.
- No browser screenshot or actual tap/hover check has been done. Phase 0 real
  mic capture, Phase 1 visual approval, and Phase 2 spoken-event verification
  remain pending; do not infer their acceptance from these route checks.

### Phase 4 — personality work implemented in part; voice-session check pending

- Added `GET /api/presence` with actual local time-of-day, last-turn gap, day
  count since the first history timestamp, and a name only when a matching
  saved fact exists. The live payload reports Arib, a 6-second gap, and day 2.
- `/stage` opts into one local Kokoro greeting per WebSocket session; greetings
  and replies use the existing Realtime event format. A measured gap of at
  least four hours uses “Welcome back”.
- Set the bridge timezone to `Asia/Kolkata` to match the Jetson host. Verified
  `/api/presence` now returns `night` at 22:13 IST, not afternoon in UTC.
- Added mood inputs from time-of-day, turn gap/rate, measured mic RMS, question
  rate, and a small explicit sentiment word list. Missing axes keep preview
  controls available. Added ten-minute visual sleep and a short WebAudio
  mic-on earcon; the capture path is muted while the earcon plays.
- Added explicit “No, I said …” correction handling. It replaces the latest
  persisted user turn, removes that turn's stale assistant reply, and answers
  again. Temporary-file history rewrite checks and in-container phrase checks
  passed.
- Not implemented: pre-rendered Kokoro “mm/hm” fillers and a night-idle
  goodnight ritual. The board reported 416 MB available, 43% CPU, 53.2 C, and
  7.5 W at one instantaneous sample. Greeting playback, mood response, and a
  10-minute session have not been verified with a person.

### Phase 5 — Systems and Conversation drawers implemented; browser check pending

- Systems reads the existing `/api/stats`, `/api/health`, `/api/models`, and
  `/api/latency?n=20`; Conversation reads `/api/history?n=50`. It renders only
  values the API supplies. Polling runs every 3 seconds only while a drawer is
  open and the tab is visible.
- Live API payloads were read before wiring. All four containers remain up;
  `/health`, `/stage`, `/classic`, and `/api/presence` returned OK after the
  bridge update.
- Rebuilt `tau-bridge:local` and then `tau-parakeet:local` against the new base,
  and recreated the four normal services. Health, presence, memory, `/stage`,
  and `/classic` all pass post-rebuild checks. The rebuild first hit a broken
  pipe downloading Torch; the retry succeeded.
- Recreated only the bridge with the host timezone setting; all four containers
  remain up and `/health` still returns OK.
- The immediate post-start `/api/stats` sample showed 1.7 GB available, 60.5%
  CPU, 98.5% GPU, 54.2 C, and 9.13 W. This is during model startup, not an
  idle UI measurement; top/thermal measurements and the 15%-of-one-core UI
  budget still need a settled browser session.
- A later `top` snapshot showed 45.6% system idle and Firefox at 123.5% CPU;
  stage was not opened for that sample, so this is not attributable to the new
  UI. Available RAM was 1.35 GB after services settled. No pre-change `top`
  baseline was captured, so before/after attribution is unavailable.
- All services and API health are now OK. No smoke/latency scripts were run
  as a final end-to-end suite. The Phase 3 smoke script was attempted after
  Phase 0 cause identification: its synthetic transcript and completed bridge
  response are in logs, but the tool did not capture the client's final
  PASS/FAIL line. `history.jsonl` and `facts.md` were restored byte-for-byte
  immediately after the test. Both files changed again later around 22:29;
  those newer contents were left untouched because their source is unknown.
  The response started at 22:15:46 and the first sentence arrived at 22:16:55;
  TTS took another 1.16 s. This run did not meet the under-3-second target.
  The cause is not established (cold model or system load are possibilities).
  Phase 5's 10-turn test was deferred with 686 MB available and 35.6% CPU after
  the smoke turn; those scripts require backup/restore and this board was busy.
- Drawer rendering, offline Wi-Fi behavior, CPU budget, and screenshots need a
  browser check. Phase 6 promotion is not done; look approval is pending and
  `/` remains the existing page.

### Phase 6 — not promoted

- `/stage` remains isolated and `/classic` remains available. Do not switch `/`
  until the visual review is approved and the human mic trial is complete.
- Laptop/phone screenshots, offline Wi-Fi trial, sustained-session heat/CPU
  measurement, smoke/latency test reruns, and the final `/` promotion remain.

**Project:** Fully local voice companion. Browser (laptop/phone) is mic+speaker; Jetson runs all models.
**Repo:** `/home/kids/autonomous-intelligence` (branch/worktree: main repo, no changes outside `tau-orin/`)
**Plan:** `/home/kids/Tau/Tau/00-README.md` + `01`…`06` phase files.
**Goal:** Open `https://<jetson-ip>:8080`, talk to Tau; personality + persistent conversation + fact memory + measured latency.

---

## Resume context (read this first if starting a new agent session)

- **Per-phase detail lives in `tau-orin/phases/phase0.md` … `phase5.md`.** This file is the index: machine facts, phase table, running log. Read the phase file for the phase you are resuming.
- Work only in `/home/kids/autonomous-intelligence/tau-orin/`.
- Do **not** modify `baby-tau/`, `realtime-api/`, `TauLegacy/`.
- No cloud AI APIs. No `git push`. No `Co-Authored-By` lines.
- **Docker works without sudo since 2026-11-06** (`docker ps` as `kids`).
  The old `/tmp/opencode/askpass.sh` is gone after the reboot, so root-only
  tools (`tegrastats`, `nvpmodel`, `jetson_clocks`) are unavailable until the
  user supplies the password again. `start.sh` still has a `sudo -A` fallback.
- User chose **no reboot**; desktop (`graphical.target`) stays running.
  Watch RAM: 7.4 GiB total shared CPU/GPU. Fall back to `llama3.2:1b` or
  `Systran/faster-whisper-base` if free RAM is tight.
- Machine IP at Phase 0: `172.20.10.2`; on 2026-11-06 it changed to
  `172.22.91.151` (Wi-Fi). Re-check with `hostname -I` before giving URLs;
  regenerate the cert if the SAN no longer matches (`start.sh` warns).

### Machine facts verified in Phase 0

| Item | Value |
| --- | --- |
| L4T | R36.5.0 (`nvidia-l4t-core 36.5.0-20260115194252`) |
| OS | Ubuntu 22.04.5 LTS, aarch64 |
| CUDA | 12.6 (V12.6.68) |
| RAM | 7.4 GiB total, ~4.4 GiB available at start, 11 GiB swap |
| Disk | 227 G total, 170 G available on `/` |
| Power | was 15W (mode 0) → set `MAXN_SUPER` (mode 2) + `jetson_clocks` |
| Docker | 29.4.1, Compose v5.1.3, runtimes include `nvidia` |
| Default target | `graphical.target` (user chose not to reboot) |

### Image tags

Upstream `dustynv/*` has **no `r36.5` tags**, so we use the project's
`r36.4.0` tags (CUDA 12.6, same L4T 36.x ABI). This is a deliberate deviation
from the phase file's "tags unchanged" instruction, because the local L4T is
R36.5.0; the closest published tags still target L4T 36.4.0.

---

## Phase status

| Phase | File | Status | Check |
| --- | --- | --- | --- |
| 0. Preflight + downloads | `phases/phase0.md` | **PASSED** | nvidia runtime, MAXN_SUPER, `.env`, 3 pulls |
| 1. Model services | `phases/phase1.md` | **PASSED** | 3 curl tests |
| 2. Browser bridge | `phases/phase2.md` | **PASSED** | spoke + heard reply |
| 3. Tau core | `phases/phase3.md` | **PASSED (automated)** — browser test pending | survives restart |
| 4. Memory | `phases/phase4.md` | PASSED (automated) | browser recall check pending |
| 5. Demo + measure | `phases/phase5.md` | **PASSED (automated)** — browser demo pending | 10 turns in `latency.csv`, README + start.sh + DEMO.md |

---

## Running log

### 2026-11-05 — Phase 0

**Done**
- Read all 6 plan files.
- System checks: L4T R36.5.0, Ubuntu 22.04.5, CUDA 12.6, 7.4 GiB RAM, 170 G free.
- `nvpmodel -m 2` → `MAXN_SUPER`; `jetson_clocks` run.
- Docker `nvidia` runtime confirmed.
- Added `kids` to `docker` group (needs re-login to take effect).
- Created `tau-orin/{logs,data,certs,prompts}`, `.env`, `.gitignore`.
- Started background pulls: `logs/pull-ollama.log`, `logs/pull-speaches.log`,
  `logs/pull-kokoro.log` (PIDs in matching `.pid` files).

**Problems / fixes**
- `docker` CLI initially gave permission denied; user not in `docker` group.
  Fixed with `usermod -aG docker kids` + `sudo -A` for this session.
- Phase file expected L4T R36.4.x; machine is R36.5.0. No r36.5 image tags
  exist upstream → kept r36.4.0 tags. Must validate at runtime in Phase 1.

**Next action**
- Wait for the three pulls, then Phase 1: create `tau-orin/docker-compose.yaml`
  with only `ollama`, `speaches`, `kokoro-tts`; verify Speaches entrypoint
  before changing its command; start services and run the 3 curl tests.

### 2026-11-05 — Phase 0 complete

- All three images pulled. Docker GPU test passed: `nvidia-smi` sees the Orin,
  driver 540.5.0, CUDA 12.6.
- Disk: 129 G free. RAM: 4.2 GiB available.
- **Phase 0 done-when check passed.**

### 2026-11-05 — Phase 1 complete

- Created `tau-orin/docker-compose.yaml` with Ollama, Speaches, Kokoro.
- Fixed upstream issues: Speaches command override, `model_aliases.json`
  working directory, Ollama `OLLAMA_MODELS` misuse.
- Production models: `llama3.2:1b`, `Systran/faster-whisper-tiny`, Kokoro GPU.
- Measured: 1B 33.4 tok/s (warm, 167 tokens/5.0 s); 3B 18.4 tok/s standalone
  (OOM with all services); STT 0.45 s warm; TTS 0.536 s at 24000 Hz.
- RAM: 6904/7607 MB used, ~642 MB available, 3.5 GB swap. Desktop kept per
  user request; bridge must stay lightweight.
- Full evidence in `logs/phase1-tests.md` and `phases/phase1.md`.
- **Phase 1 done-when check passed.**

**Next action**
- Phase 2: copy `realtime-api/` to `tau-orin/bridge/`, swap TTS/STT/LLM
  backends, generate HTTPS cert, build the bridge, and test from the browser.

### 2026-11-05 — Phase 2 code complete, bridge build in progress

- Copied `realtime-api/` to `tau-orin/bridge/`; edited only the copy.
- Local backends wired: Ollama `:8000`, Speaches `:8001`, Kokoro `:8880`.
- HTTPS cert for `CN=tau.local` with `SAN DNS:tau.local, IP:172.20.10.2`.
- Bridge added to `docker-compose.yaml` (host network, cert/log/data/prompt mounts).
- **Build fix (deviation from phase file):** the first `pip install` pulled
  `torch 2.15.0` **with CUDA 13.2** deps (torch 484 MB + cuDNN 595 MB + …,
  multi-GB) because PyPI's aarch64 torch wheels are CUDA builds. The phase file
  says "torch installs CPU wheels for Silero VAD"; that is false here.
  The bridge only runs VAD on CPU, so the Dockerfile now installs
  `torch==2.10.0+cpu` from `download.pytorch.org/whl/cpu` first.
- Also replaced `torch.hub.load("snakers4/silero-vad", ...)` (runtime GitHub
  fetch) with `silero_vad.load_silero_vad()` from the pip wheel (offline model,
  `torch.set_num_threads(1)` to avoid stealing Jetson cores).
- Kokoro and Speaches stopped during the build to free RAM; restart after.

### 2026-11-05 — Phase 2 complete

- Bridge built with CPU-only torch (146 MB instead of multi-GB CUDA wheels).
- Fixed Kokoro WAV bug: its `wav` output declares a `0xFFFFFFFF` data size,
  so Python `wave` tried to allocate ~4 GB. The bridge now requests raw PCM.
- Enabled the existing sentence-by-sentence TTS mode (default was whole-response).
- Verified each component and the whole WebSocket pipeline by feeding TTS audio
  back in: VAD → STT → LLM → TTS → audio deltas.
- **User browser test passed:** spoke three times, heard Tau's voice.
  End-of-speech → first audio ~2 s warm (~5 s on the first cold turn).
- Replies were too long/generic — Phase 3 will fix this with the Tau prompt.

### 2026-11-05 — Phase 3 complete (automated verification)

**Built** (detail in `phases/phase3.md`)
- `bridge/src/realtime_api/history_store.py`: `append_turn()`, `load_recent()`,
  `time_prefix()`, `load_facts()`, `build_system_prompt()` (re-read every turn).
- `session.py`: a new WebSocket session preloads the last 20 turns.
- `ws_handler.py`: persists user + assistant turns, prefixes the LLM user
  message with `[timestamp][gap]` (never the browser transcript), and strips
  `*actions*` before TTS via `tts_client._clean_for_tts()`.
- `prompts/tau.system.md` already had the right personality/body/speech rules.
- Image rebuilt and restarted; the running container contains `history_store.py`.

**Bug found and fixed**
- The 1B model echoed the timestamp prefix (`[2026-11-05 16:28:33] Nice to meet
  you…`), which was then spoken by TTS and written into history, polluting
  every later turn. Added `_strip_time_echo()` to `ws_handler.py` (both TTS
  modes). Pre-fix history kept at `data/history.pre-fix.jsonl` as evidence.

**Verification** — new re-runnable test, `tests/phase3_smoke.py`
(result: `PASS`, transcript in, `response.done` completed, no timestamp echo):
```bash
cd tau-orin
export SUDO_ASKPASS=/tmp/opencode/askpass.sh
sudo -A docker cp tests/phase3_smoke.py tau-bridge-1:/tmp/p3.py
sudo -A docker exec tau-bridge-1 python /tmp/p3.py
```
- `data/history.jsonl` gained a user + assistant turn.
- A fresh session logged `preloaded 2 history turns` → continuity works.
- Test-side gotcha: `websockets >= 14` needs `subprotocols=["realtime"]`.

**Open before the demo**
- Replies run long (6 sentences / ~45 s) — the 1B model ignores the 1–3
  sentence rule. Cap it in code (stop after 2–3 sentences or set `max_tokens`).
- Manual browser restart test still to do (user speaks, reloads, asks a name).
- Container logs are UTC, host is IST — settle the convention before
  `latency.csv` in Phase 5.

### 2026-11-06 — Phase 4 PASSED: memory extraction is rule-based

`llama3.2:1b` was tested on six prompt styles and two YES/NO gates: it invents
facts ("The Friend has a pet cat named Mochi") and continues the dialogue
instead of copying, so extraction was rewritten as deterministic first-person
rules that quote the user's own sentences into `data/facts.md`. No model call,
no RAM pressure, no hallucinated persona. `tests/phase4_memory.py` passes all
seven checks (extraction, faithful facts, dedupe, small talk, cap). Full
evidence and the rejected prompt variants: `phases/phase4.md`.

Note for future edits: the Dockerfile installs the package into site-packages,
so any source change needs a full image rebuild (~6 min, torch re-downloads).
`docker cp` into the container is a fast way to test a change before building.

**Next action**
- Phase 4 browser check: speak three facts, delete `data/history.jsonl`,
  restart the bridge, ask "What do you know about me?".
- Phase 3 manual browser restart check is still pending (needs a human voice).
- Phase 5 (`phases/phase5.md`) is next: `latency.csv`, `README.md`, `start.sh`.

### 2026-11-06 — Phase 5 PASSED: measured, documented, demo-ready

**Built** (detail in `phases/phase5.md`)
- `latency.py` + per-turn marks on `Session`: one row per spoken turn in
  `logs/latency.csv` (speech end → transcript → first token → first audio →
  last chunk, plus sentences/chars/status). Written before the echo cooldown,
  so `total_ms` is generation time, not playback. `first_token_cb` added to
  `llm_client.stream_chat_completion` — that is a real first-token hook, not
  the first *sentence*.
- `tests/phase5_latency.py`: 10 synthetic spoken turns through the same
  WebSocket the browser uses; prints median/max per stage from the CSV.
- `README.md` (architecture, reuse table, measured numbers, limits),
  `start.sh`, `DEMO.md` (2-minute demo incl. a live bridge restart).

**Measured** — `PASS turns=10 rows=10 non_completed=0`

| Stage | Median | Max |
| --- | --- | --- |
| speech end → transcript | 572 ms | 919 ms |
| transcript → first LLM token | 244 ms | 7527 ms (turn 1, cold) |
| first token → first audio | 2392 ms | 3095 ms |
| speech end → first audio | ≈ 3.2 s | ≈ 11.1 s (cold) |
| speech end → last audio chunk | 5394 ms | 10640 ms |

RAM during the run: 6462/7607 MB used, 897 MB available. **Power not
measured** — `tegrastats`/`nvpmodel` need root and the askpass file is gone.

**Bug found and fixed** — the first measurement run timed out on turn 1: after
Speaches' 300 s TTL unload, the reload's huggingface.co revision check stalled
*while holding the model lock*, so STT hung for 120 s. Added
`HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` to the speaches service (weights
are already in `./hf-cache`); recreated the container, STT then answered in
1.7 s and all 10 turns passed.

### 2026-11-06 — resumed after server restart: recall fix + power measured

**Verified after restart**
- All four containers up; LLM, TTS, bridge health and `/memory` all OK.
- Wi-Fi IP changed `172.20.10.2` → **`172.22.91.151`**, so the self-signed cert
  no longer matched. Regenerated `certs/tau.crt` with both IPs + `tau.local`,
  restarted the bridge. `curl --cacert certs/tau.crt
  https://172.22.91.151:8080/health` → **200**.
- Speaches `GET /v1/models` returns 500 because `HF_HUB_OFFLINE=1` and the
  cached Whisper model has no `README.md`; transcription is unaffected.
  Cosmetic, not fixed.

**Memory recall (closes Phase 4's automated check)**
- Backed up history, emptied it, restarted the bridge, sent "What do you know
  about me?" as Kokoro PCM through the real WebSocket.
- First attempt: facts were in the prompt but the 1B hedged ("I don't really
  know much about you yet"). Fixed in `prompts/tau.system.md`: "The Friend"
  means the person speaking, these are already-known memories, and never say
  you have no memory/database.
- Re-run recalled name, computer science, dosa and robotics **from facts.md
  alone**. Original 28-turn history restored afterwards.

**Power (Phase 5)**
- `tegrastats` runs unprivileged on this image. 30 s around a synthetic reply:
  idle **6.6–6.9 W**, peak **19.6 W**, **14.8 W** average over the four active
  seconds; RAM 6785/7607 MB. Samples: `logs/power-test.log`.
- `README.md` and `phases/phase5.md` updated with real watts.

**Next action**
- Human demo run: open **https://172.22.91.151:8080**, accept the cert, speak,
  restart the bridge, ask "What's my name?" — follow `DEMO.md`. This also
  closes the Phase 3/4 browser checks.
- Nothing is committed: `tau-orin/` is still untracked in the repo.
