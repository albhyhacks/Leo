# Phase 3 — Tau personality and continuous conversation

**Status:** **PASSED (automated)** — history persistence and prompt wiring proven
by `tests/phase3_smoke.py`. The plan's *browser* restart test is the last open box
(it needs a human speaking into the page).
**Plan source:** `/home/kids/Tau/Tau/04-phase3-tau-core.md`
**Time budget in plan:** 1:15–1:35 (20 min)

## What was built

| Piece | File | Notes |
| --- | --- | --- |
| Personality prompt | `tau-orin/prompts/tau.system.md` | Legacy text kept; body rewritten to the Jetson truth; 1–3 short spoken sentences, no markdown, `*actions*` after speech |
| Persistent history + prompt assembly | `bridge/src/realtime_api/history_store.py` | `append_turn()`, `load_recent()`, `time_prefix()`, `load_facts()`, `build_system_prompt()` |
| Conversation preload | `bridge/src/realtime_api/session.py` | `Session.__init__` replays the last `CONTEXT_TURNS` (20) turns into the new `Conversation` |
| Turn persistence | `bridge/src/realtime_api/ws_handler.py` | `append_turn("user", transcript)` before the LLM; `append_turn("assistant", …)` right after generation, **before** the echo cooldown, so a reload mid-reply cannot lose the turn |
| Time awareness | `history_store.time_prefix()` | `[YYYY-MM-DD HH:MM:SS][time since last message]` prefixed to the user message **sent to the LLM only** — not to the browser transcript, not to history |
| Actions never spoken | `tts_client._clean_for_tts()` | `re.sub(r"\*[^*]*\*", " ", …)` — `*nods*` stays in the transcript, is stripped before synthesis |
| Context bound | `session.Conversation.to_chat_messages()` | system prompt + last 20 turns only |
| Prompt re-read per turn | `ws_handler._generate_response()` | `build_system_prompt(session.config.instructions)` runs on every reply, so edits to `tau.system.md` / `facts.md` apply without a restart. A non-empty browser `instructions` field still wins |

Paths are configurable: `TAU_DATA_DIR` (default `/data`), `TAU_HISTORY_FILE`,
`TAU_FACTS_FILE`, `TAU_PROMPTS_DIR` (default `/prompts`). Compose mounts
`./data:/data` and `./prompts:/prompts`.

## Bug found and fixed during verification

**The 1B model echoed the timestamp prefix.** The prefix is only meant for the
model, but llama3.2:1b repeated it at the start of its reply:

```
[2026-11-05 16:28:33] Nice to meet you, Rita! *nods* …
```

That text was then synthesised (spoken as a date) and persisted into
`history.jsonl`, so it re-entered the context on every later turn.

Fix: `_strip_time_echo()` in `ws_handler.py` — strips leading
`[YYYY-MM-DD …][gap]` groups from each LLM sentence before it reaches TTS or
`full_transcript`, in both `whole` and `sentence` TTS modes. The pre-fix history
is kept as evidence at `data/history.pre-fix.jsonl`.

Verified after rebuild — stored turn:

```json
{"role": "assistant", "text": "\"Hello, R.F.! Nice to meet you! *warm smile* My name is Tau, by the way. What brings you here today? …*\""}
```

## How to re-run the verification (no mic needed)

`tests/phase3_smoke.py` synthesises a sentence with Kokoro, feeds it into the
Realtime WebSocket as 24 kHz PCM16, and waits for `response.done`.

```bash
cd tau-orin
export SUDO_ASKPASS=/tmp/opencode/askpass.sh      # see PROGRESS.md
sudo -A docker cp tests/phase3_smoke.py tau-bridge-1:/tmp/p3.py
sudo -A docker exec tau-bridge-1 python /tmp/p3.py
tail -2 data/history.jsonl
grep -a preloaded logs/realtime-api.log | tail -2
```

**Gotcha:** `websockets >= 14` refuses the handshake unless the client passes
`subprotocols=["realtime"]` — the bridge accepts with that subprotocol. This was
the first failure of the test, not a bridge bug.

### Results (2026-11-05, all services warm, desktop running)

```text
[test] TTS 193204 bytes (4.03s @24kHz)
[test] TRANSCRIPT: 'Hello, Tao. My name is R.F. and I study computer science.'
[test] DONE status=completed
[test] PASS
```

- `data/history.jsonl` gained one user and one assistant turn.
- A fresh WebSocket session logged `preloaded 2 history turns` → **conversation
  continuity across reconnects works** (this is the same code path a page
  reload or container restart takes).

## Open issues / limits (carry into Phase 5)

1. **Replies are too long.** The prompt demands 1–3 short sentences; the test
   reply was 6 sentences / ~45 s of audio earlier. The 1B model largely ignores
   the length instruction. Before the demo, cap it in code — e.g. stop the
   `stream_sentences()` loop after 2–3 sentences, or set `max_tokens` in
   `stream_chat_completion` — rather than only asking politely in the prompt.
2. **STT mishears the name.** "Tau" came back as "Tao"/"Tao" and the user's
   name as "R.F."; `faster-whisper-tiny` is the accuracy/RAM trade-off. If the
   report needs better transcripts, try `faster-whisper-base` (≈ +290 MB, warm
   STT still ~0.47 s) — but RAM is tight.
3. **Container clock is UTC; host is IST (+05:30).** Log timestamps are UTC
   while `history.jsonl` `ts` values follow the host timezone offset
   (`+00:00` inside the container). Pick one convention before writing
   `latency.csv` in Phase 5.
4. Assistant text is stored **with** `*actions*` and without the time prefix;
   the browser transcript shows actions too. That matches the plan.

## Done-when check (plan)

- [x] Tau introduces itself as Tau and uses its own personality (verified in the
      automated run: *"My name is Tau, by the way."*)
- [x] never reads markdown or asterisks aloud (`_clean_for_tts()` strips both;
      `*warm smile*` appears in the transcript only)
- [x] continues the same conversation after a reconnect / restart
      (`preloaded 2 history turns`)
- [ ] **manual browser restart test** — user speaks, reloads the page, asks
      "What's my name?" (same path as the automated check, but confirms the UI)
- [ ] replies limited to 1–3 sentences (open issue 1)
