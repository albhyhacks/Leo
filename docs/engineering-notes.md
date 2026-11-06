> **Archived engineering notes.** Written before the switch from Whisper to Parakeet and before the presence UI; STT numbers and service layout below are historical. See the top-level README for the current system.

# Leo — a fully local voice companion on a Jetson Orin Nano

Leo is a voice assistant you talk to in the browser. Speech goes in, an answer
comes back out loud. **Every model runs on the Jetson itself** — no cloud API,
no API key, no internet required once the images and weights are on the board.

The browser is only a microphone, a speaker and a transcript view; the work
happens in a small HTTPS bridge that talks to three local model services.

```
   browser (mic + speaker + transcript)
        │  ▲
        │  │  wss://<jetson-ip>:8080/v1/realtime     (PCM16 24 kHz both ways)
        ▼  │
   ┌──────────────────────────────────────────────────────────────┐
   │  bridge — FastAPI + uvicorn, HTTPS                           │
   │  VAD → Speaches (STT) → Ollama (LLM) → Kokoro (TTS)          │
   └───┬───────────────┬────────────────┬─────────────────────────┘
       │ :8001         │ :8000          │ :8880
       ▼               ▼                ▼
   Speaches        Ollama          Kokoro TTS        all four containers use
   faster-whisper  llama3.2:1b     af_alloy voice    network_mode: host and
   tiny, CUDA      (GPU, 1536 ctx) 24 kHz PCM16      runtime: nvidia
       │
       └── files on disk (bind mounts)
           data/history.jsonl   last 20 turns, survives restarts
           data/facts.md        long-term facts, human-editable
           prompts/tau.system.md  Leo's personality prompt
           logs/latency.csv     one row per spoken turn
```

## Start it

```bash
cd tau-orin
./start.sh
```

`start.sh` starts the four containers, waits for the bridge health check, and
prints the URL — usually `https://<jetson-ip>:8080`. Open it in Chrome and
accept the self-signed certificate warning; browsers only allow the microphone
on an HTTPS origin. The first turn after boot is slow (models load); say
something once as a warm-up before demoing.

`start.sh` also falls back to `sudo -A docker` if the login session predates
the `docker` group (`export SUDO_ASKPASS=/path/to/helper`).

`start.sh` prints one URL. The page opens on the **Leo** tab — the voice-first
dashboard where you talk to Leo. It shows a lion face that follows the real
pipeline state (connecting → listening → thinking → speaking), a live
conversation, and, below, the things you usually want to show in a viva:
Jetson vitals, the four local models, latency bars for the last turns, and
what Leo remembers. Voice, Text, Settings and Monitor are still there as tabs.
The page is a single offline HTML file plus `leo.css` / `leo.js`; it uses no
CDN and no framework, and only talks to its own origin.

Diagnostics poll lightweight, read-only endpoints: `/api/stats` (1 s),
`/api/health` and `/api/models` (5 s), `/api/latency` (6 s), `/memory` (8 s).
Missing sensors render as `n/a` instead of inventing a number, and the bridge
never blocks a reply to serve the dashboard.

**For a live demo, follow [`DEMO.md`](DEMO.md)** — a two-minute script with a
mid-demo bridge restart that proves memory is on disk.

## What is local

| Piece | Service | Image / model | Port |
| --- | --- | --- | --- |
| Speech → text | Speaches | `faster-whisper-tiny`, CUDA int8 | 8001 |
| Text → text | Ollama | `llama3.2:1b`, 1536 ctx, q8_0 KV | 8000 |
| Text → speech | Kokoro TTS | `af_alloy`, 24 kHz PCM16 | 8880 |
| Orchestration | bridge (this repo) | `tau-bridge:local` | 8080 (HTTPS) |

## Measured latency

Ten spoken turns through the real WebSocket path, no browser in the loop:
Kokoro synthesises a sentence, it is pushed as 24 kHz PCM16 in 100 ms chunks,
2 s of silence closes the VAD turn. Numbers are from `logs/latency.csv`
(medians, 2026-11-06, everything warm except turn 1):

| Stage | Median | Max |
| --- | --- | --- |
| Speech end → transcript (STT) | 572 ms | 919 ms |
| Transcript → first LLM token | 244 ms | 7527 ms¹ |
| First token → first audio chunk (rest of sentence + TTS) | 2392 ms | 3095 ms |
| **Speech end → first audio** (sum of the above) | **≈ 3.2 s** | ≈ 11.1 s¹ |
| Speech end → last audio chunk (whole reply generated) | 5394 ms | 10640 ms |

¹ Turn 1 only, with the model still cold: 7.5 s to the first token. Every warm
turn stayed under 1.5 s. That single outlier is why the maximum looks bad.

Resource use while the stack is fully warm: **6.6 GB of 7.6 GB RAM used**
(≈0.8–0.9 GB available, ~2.1 GB swap in use). Power from 30 s of `tegrastats`
around a spoken reply, MAXN SUPER: **idle ≈ 6.6–6.9 W**, **peak 19.6 W**
(the LLM + TTS burst), **≈ 14.8 W average over the four active seconds**.
`tegrastats` turned out to be readable without root on this image.

Reproduce:

```bash
docker cp tests/phase5_latency.py tau-bridge-1:/tmp/p5.py
docker exec tau-bridge-1 python /tmp/p5.py 10   # prints median/max per stage
column -s, -t logs/latency.csv
```

(The file currently holds 16 rows: the 10 measured turns above, 2 from a
post-rebuild sanity check and 4 from later browser sessions — all completed.)

One CSV row per spoken turn — `ts, session, speech_end_to_transcript_ms,
transcript_to_first_token_ms, first_token_to_first_audio_ms, total_ms,
llm_total_ms, tts_ms, llm_tokens, tokens_per_s, sentences, reply_chars,
status`. Missing stages are empty, never `0`; Phase 5 files are migrated in
place on the first new turn, keeping every old row. Rows are written as the
reply finishes, before the echo cooldown, so the total is the reply's real
generation time. `llm_tokens` / `tokens_per_s` come from Ollama's final usage
chunk (`stream_options.include_usage`), so the speed on the page is measured
rather than estimated. Typed turns are not voice-latency samples and are not
written to the file.

## Memory (Phase 4)

`data/facts.md` holds durable facts in plain markdown. Extraction is
**rule-based, not an LLM call**: `bridge/src/realtime_api/memory.py` matches 21
patterns in the user's own sentences ("my name is…", "I study…"), so it cannot
invent facts and costs no tokens. It runs in the background *after* the reply
has been spoken, is cancelled if the user starts talking, dedupes, and keeps at
most 40 lines. `data/history.jsonl` keeps the last 20 turns and is preloaded
into a new session, so a page reload or bridge restart continues the
conversation.

Recall was verified with `history.jsonl` emptied and the bridge restarted:
asking "What do you know about me?" returned the name, the computer-science
course, dosa and the robotics project from `facts.md` alone.

Why not the 1B model: it invented facts and continued the dialogue instead of
answering, across six prompts and two YES/NO gates — evidence and the rejected
variants are in `phases/phase4.md`.

Endpoints: `GET /memory` (facts with stable ids), `DELETE /memory/{id}` (forget
one fact), `DELETE /memory` (forget everything), `POST /memory/extract` (runs an
extraction immediately; `{"force": true}` works even when auto-memory is off).

## Dashboard (Phase 6)

The Leo tab is the demo surface, not a separate page. Everything on it is real
and read-only:

- **Hero** — inline SVG lion face. The state follows the audio path, not just
  `response.done`: green ears while the mic is open, amber eyes while the LLM
  thinks, a moving mouth only while audio is actually playing, then back to
  idle when the playback queue drains. Barge-in and a dropped WebSocket both
  return the face to a truthful state.
- **Conversation** — the same transcript as the Voice tab, mirrored live, with
  `*stage directions*` dimmed. The last few turns are replayed from
  `data/history.jsonl` so a reload does not start from a blank page.
- **Vitals** — RAM and swap (with meters), CPU, GPU load, hottest thermal zone,
  VDD_IN power from the INA3221, `nvpmodel` mode, host and bridge uptime, plus
  60-second sparklines. NV mode reads
  `/var/lib/nvpmodel/status` through a read-only bind mount.
- **Mind · Ears · Voice · Reflexes** — what each service is actually running.
  Ollama is shown as “asleep — loads on first turn” when `/api/ps` is empty,
  instead of pretending the model is resident.
- **How fast I reply** — medians and a coloured bar per recent turn (hearing,
  thinking, voice, speaking tail) from `logs/latency.csv`, including completion
  tokens and tokens/second. A `tau.metrics` event carries the same numbers to
  the page the moment a turn ends.
- **What I remember** — `data/facts.md` with a per-fact delete button (stable
  text-derived ids, so deleting one fact cannot shift or delete another) and a
  two-step “clear all”.

The old `/dashboard` URL still resolves: it redirects to `/#leo`.

## What was reused, what is new

| Source | What Leo takes from it |
| --- | --- |
| `realtime-api/` | The bridge runtime: OpenAI-Realtime-style WebSocket events, session + server VAD, PCM16 resampling, the browser client. Copied to `bridge/` and edited only there — the original is untouched. |
| `baby-tau/` | Service shapes proved on this board: `network_mode: host` + `runtime: nvidia` compose pattern, Speaches transcription request format, Kokoro TTS call, the `faster-whisper-tiny` choice. |
| `TauLegacy/` | The idea of persistent facts across sessions (it used a vector DB/Pinecone). Leo replaces that with a flat file so everything stays local and inspectable. |
| **new in `tau-orin/`** | STT→LLM→TTS pipeline with sentence-level TTS streaming, echo gate, rule-based memory, history/facts persistence, latency instrumentation, `start.sh`, HTTPS certs, and the container-free test scripts. |

## Layout

```
tau-orin/
├── docker-compose.yaml     four services, host networking
├── start.sh                bring everything up, print the URL
├── DEMO.md                 two-minute demo script
├── bridge/                 FastAPI realtime bridge (the only code we run)
│   └── src/realtime_api/
│       ├── ws_handler.py   event dispatch + the pipeline + instrumentation
│       ├── llm_client.py   streaming chat completions, sentence splitter
│       ├── stt_client.py   Speaches client
│       ├── tts_client.py   Kokoro client (serialised, concurrency = 1)
│       ├── memory.py       rule-based fact extraction (Phase 4)
│       ├── latency.py      per-turn CSV rows (Phase 5)
│       ├── history_store.py  history.jsonl + facts.md + system prompt
│       ├── dashboard.py    read-only telemetry for the Leo tab
│       └── static/         index.html, leo.css, leo.js — the browser page
├── tests/                  phase3_smoke.py, phase4_memory.py, phase5_latency.py
├── data/                   history.jsonl, facts.md, tau.system.md  (bind mount)
├── logs/                   latency.csv, service logs                 (bind mount)
├── certs/                  tau.crt / tau.key (self-signed, HTTPS)
└── phases/                 per-phase engineering record (see PROGRESS.md)
```

## Known limits

- **Replies can run long.** `llama3.2:1b` sometimes rambles for six sentences.
  A 3B model reads much better but OOMs with STT + TTS resident on 8 GB, so the
  1B model stays.
- **TTS is per sentence, not per token.** First audio arrives after the first
  complete sentence (plus a small character buffer) — that is the 2.4 s stage
  above, not a streaming vocoder.
- **Echo gate, not true AEC.** The mic is gated while Leo speaks (cooldown
  ≈ reply length) instead of cancelling the echo properly. Use headphones for
  the demo; talking over Leo interrupts it only after the gate opens.
- **Flat-file memory.** 40 facts, 12 turns of context, no embeddings, no
  summarisation. Good for a demo, not a year-long companion.
- **One conversation at a time.** TTS concurrency is 1 and the model runs one
  request slot.
- **No wake word, no vision, no camera.** It listens whenever the page is open.
- **Browser path is human-tested only.** The automated tests drive the same
  WebSocket with synthesised speech; microphone echo behaviour still needs a
  person in front of the Jetson.

## Where the history lives

`PROGRESS.md` is the index: machine facts, the phase table, and a running log.
Each phase has a record in `phases/` with what was tried, what was rejected and
why, and how to resume. Start there before changing anything.
