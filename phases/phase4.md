# Phase 4 — Simple long-term memory

**Status: PASSED (automated), 2026-11-06.** The write path is built, tested and
live; what remains is the browser check where a human says facts, history is
cleared, and Tau still remembers them.
**Plan source:** `/home/kids/Tau/Tau/05-phase4-memory.md`

## Goal

Facts about the user that outlive the 20-turn window in `data/history.jsonl`,
using the legacy project's "direct knowledge" idea. The vector database (Voyage
+ Pinecone in `TauLegacy/`) is out of scope: `data/facts.md` plus the LLM's own
context is enough at this scale. Hard rule from the plan: **memory work must
never delay a spoken reply**.

The read path was already built in Phase 3: `load_facts()` feeds
`{{direct_knowledge}}` in `prompts/tau.system.md`, re-read every turn, so a
saved fact is recalled on the very next reply with no restart. Phase 4 is only
the write path.

## How it works

Extraction is **deterministic rules**, not the model. User sentences are split
on punctuation and on "and I …", then matched against a table of first-person
patterns (name, age, studies, work, home, tastes, family, possessions, projects,
plans) and rewritten in the third person, keeping the user's own words:

```
I study computer science at Karunya University.
  -> - The Friend studies computer science at Karunya University.
My name is R.F. and I study computer science.
  -> - The Friend's name is R.F.
  -> - The Friend studies computer science.
I'm building you, Tau, for a college project.
  -> - The Friend is building Tau for a college project.
```

A scan takes microseconds, makes no model call, and cannot invent anything.

| Piece | File | What it does |
| --- | --- | --- |
| Rules + storage | `bridge/src/realtime_api/memory.py` | `facts_from_turns()`, `extract_and_save()`, `save_facts()`, `clear_facts()`; dedupe (case/punctuation-insensitive, plus containment for long facts), 40-line cap dropping oldest, atomic write via `.tmp` + `replace()` |
| Background trigger | `ws_handler.py` → `_schedule_memory_extraction()` | Runs after the reply finished playing, never awaited |
| Barge-in safety | `ws_handler.py` → `_cancel_memory_extraction()` | Cancels the task on `speech_started` |
| Guard | `session._memory_task` | Skips if the previous extraction is still running |
| Endpoints | `main.py` | `GET /memory`, `DELETE /memory`, `POST /memory/extract` (runs even when auto-extraction is off) |
| Config | `config.py` | `memory_enabled=True`, `memory_turns=6`, `memory_max_lines=40` |
| Test | `tests/phase4_memory.py` | Container-side; backs up and restores `facts.md` |

## Test result

```
[test] extractor   : rule-based (no model call)
[test] --- extraction pass 1 (empty memory) ---
[test] facts now:
- The Friend studies computer science at Karunya University.
- The Friend is building Tau for a college project.
- The Friend's favourite food is dosa.
[PASS] extraction wrote at least one fact — 3 added
[PASS] every stored line is a bullet — 3 lines
[PASS] remembered the user's facts — matched: studies computer science, favourite food, college project
[PASS] second pass adds no duplicate — 0 added, 3 -> 3 lines
[PASS] small talk stores nothing — 0 added, 3 -> 3 lines
[PASS] facts file respects the cap — 40 <= 40
[PASS] newest facts survive the cap
[test] RESULT: PASS
```

### Real turn, not just the endpoint

`tests/phase3_smoke.py` was run against the live bridge — a full WebSocket turn
with Kokoro-generated "microphone" audio — and the background task stored facts
from it while the reply streamed and played normally:

```
[test] DONE status=completed
[test] PASS transcript='Hello, Tao. My name is R.F. and I study computer science.' first_audio=True

[facts.md]
- The Friend's name is R.F.
- The Friend studies computer science.

[MEMORY] 2 candidate fact(s) from 4 turns
[MEMORY] saved 2 new fact(s), 2 total in /data/facts.md
```

`first_audio=True` in the same run is the evidence that memory did not delay the
spoken reply.

Run it again with:

```bash
cd /home/kids/autonomous-intelligence/tau-orin
docker cp tests/phase4_memory.py tau-bridge-1:/tmp/p4.py
docker exec tau-bridge-1 python /tmp/p4.py
```

## Why the LLM was dropped (do not retry these blind)

The plan had the local model summarise facts. Measured against `llama3.2:1b`,
it cannot do the job — it continues the dialogue and invents facts:

```
prompt: "copy the sentences where the Friend states a lasting fact"
reply : NONE
        The Friend has a pet cat named Mochi.
        Tau: That's great! What kind of cat is it?
        The Friend loves playing chess on weekends.
```

| Prompt style | Behaviour |
| --- | --- |
| Rule list ("record only NEW lasting facts…") | Invents all three facts, none from the conversation |
| Copy task + one-shot example | Accurate, but drops facts (2 of 3) |
| Copy task, 3-item example | 3/3 but replays small talk as facts and appends "Tau is flattered" |
| Copy task + a NONE example | Echoes the few-shot example verbatim |
| YES/NO gate ("strict classifier") | Answers NO on conversations that clearly contain facts |

Diagnosis: a 1B model can copy reasonably but cannot judge what counts as a
durable fact, and free generation leaks into role-play. Since `facts.md` is
injected into every prompt, a hallucinated fact corrupts Tau's persona on every
following turn — so precision wins over recall.

## Limits

- **Recall is bounded by the patterns.** "I've been learning guitar since
  March" or "My professor is called Meera" are not captured. Adding a pattern is
  a two-line change in `_RULES`.
- Facts are stored as third-person sentences quoting the user; they are not
  paraphrased, so they can read slightly mechanically ("The Friend's favourite
  food is dosa.").
- There is no conflict resolution: "I study CS" and "I study physics" both stay
  until the 40-line cap pushes the older one out (`DELETE /memory` clears all).
- Not yet measured with a real voice session in the browser — see done-when.
- Every `[MEMORY]` log line appears twice in `logs/realtime-api.log`. That is a
  logging configuration detail (two handlers writing to the same sink), not
  duplicated work: `facts.md` and the endpoints show each fact once. Harmless,
  but it makes the log confusing to read.

## Resume / verification commands

```bash
cd /home/kids/autonomous-intelligence/tau-orin
docker compose up -d --build bridge          # ~6 min: the Dockerfile reinstalls
                                             # torch on every source change
curl -sk https://localhost:8080/memory
curl -sk -X POST https://localhost:8080/memory/extract
curl -sk -X DELETE https://localhost:8080/memory
cat data/facts.md
```

## Recall check after clearing history (2026-11-06)

`history.jsonl` was backed up (to `data/history.pre-recall-test.jsonl`), emptied,
and the bridge restarted. Kokoro synthesised "What do you know about me?" and
sent it through the same WebSocket the browser uses. First run exposed a 1B
weakness: the model had the facts but still said "I don't really know much about
you yet". Two prompt changes fixed it without touching code:

1. `prompts/tau.system.md` now says that "The Friend" in the notes means the
   person being spoken to, and that these lines are already-known memories.
2. It adds: "When I ask what you know about me, answer with these facts in one
   or two sentences. You DO have these memories: never say you have no memory or
   database."

Re-run result: the reply named R.F., computer science, dosa and the robotics
project, with only `facts.md` as the source. Your original history was restored
afterwards.

## Done-when

- [x] facts appear in `data/facts.md` without delaying replies — extraction is
      rule-based and runs after playback; confirmed on a real WebSocket turn
      (`first_audio=True` in the same run)
- [x] `GET /memory` returns them, `DELETE /memory` clears them
- [x] duplicate facts are not stored twice; file capped at 40 lines
- [x] small talk is not stored
- [x] recalled after `history.jsonl` is cleared — automated WebSocket check
      passed on 2026-11-06 (see above)
- [ ] same recall check with a human voice in the browser (demo step)
