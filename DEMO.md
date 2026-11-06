# Leo — 2-minute demo script

The demo is a conversation, not a slide deck. Everything below is spoken out
loud to Leo in the browser; the terminal is used once (to restart the bridge
live). The page opens on the **Leo** tab, which is the demo surface: face,
conversation, vitals, models, latency and memory all in one place.

## 0. Before the clock starts (setup, ~2 min)

```bash
cd ~/autonomous-intelligence/tau-orin
./start.sh          # prints "Voice: https://<jetson-ip>:8080"
```

1. Open that URL in Chrome, accept the self-signed certificate warning.
2. Allow the microphone when asked.
3. Say **“Hi Leo, can you hear me?”** once as a warm-up (the first turn loads
   the models and is always the slowest). Then start the clock.
4. Dismiss the “Hi, I’m Leo” banner once (“Got it”); it stays dismissed.

Keep a terminal open, already in `tau-orin/`, for step 5.

## The script

| Time | What you do | What to watch for |
| --- | --- | --- |
| 0:00 | Say **“Hey Leo, how are you doing today?”** | Leo answers out loud; the face goes amber (thinking) then moves its mouth while the audio plays. The spoken words appear in **Conversation**. |
| 0:15 | Point at **Vitals** and **Mind · Ears · Voice · Reflexes** | Real board data: RAM/swap, GPU, temperature, VDD_IN watts, `nvpmodel`, 60 s sparklines, and the model each service is running. Ollama shows “asleep — loads on first turn” when idle. |
| 0:30 | Say **“My name is R.F. and I study computer science.”** | Leo acknowledges it — no need to ask it to remember. After the reply, **What I remember** grows by two lines. |
| 0:45 | Ask a normal question: **“Can you suggest a name for a pet cat?”** | The conversation continues normally; memory work happens after the reply, so the spoken answer is not slowed down. |
| 1:00 | In the terminal: `cat data/facts.md` | The facts are already saved as plain text — no database, no embedding. The page shows the same lines, each with a delete button. |
| 1:15 | In the terminal: `docker restart tau-bridge-1` (~5 s), then reload the page | Leo comes back with the same conversation (`data/history.jsonl`) and the same facts (`data/facts.md`); the page replays the last turns. |
| 1:35 | Say **“What's my name?”** | Leo answers **“R.F.”** — proof that memory survived the restart. |
| 1:45 | Point at **How fast I reply** | A coloured bar per recent turn (hearing / thinking / voice / speaking tail), medians above it, and completion tokens per second. The same rows are in `logs/latency.csv`. |

## Why each beat exists

- **Greet + question** — shows the full speech-in → speech-out loop, not a
  typed chat, and the face proves the page knows what the pipeline is doing.
- **Vitals / models on screen** — the Jetson is visibly the body: watts,
  thermals, RAM and the actual model names, not a cloud status page.
- **Two facts, no explicit “remember this”** — the Phase 4 rule engine picks
  up first-person statements from the user's own words in the background.
- **Live restart** — kills the “it's just a prompt” objection: the state is on
  disk (`data/`), not in the process.
- **`facts.md` on screen** — memory is inspectable, editable and deletable;
  the per-fact delete is the privacy answer.
- **Latency bars + CSV** — every claim about speed is measured, and the page
  shows tokens/second from Ollama's own usage chunk.

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| No microphone prompt | The page must be on `https://` (not `http://`) and the cert accepted. Re-run `./start.sh` and check its cert hint. |
| Leo hears itself / repeats | Use headphones, or speak only after Leo has finished. The echo gate (not true AEC) suppresses the mic during playback. |
| First reply takes ~5 s | Cold models. Do the warm-up turn before the clock starts. |
| Nothing after a restart | Wait for the `./start.sh` health line, reload the page, then speak once. |
| Face stuck on “Speaking…” | Audio output was blocked (tab in the background, autoplay policy). Click the mic once to unlock audio; the face settles on the next turn. |
| Long, rambling answer | Known limit of `llama3.2:1b` (see README). Interrupt by talking over it, or click the mic while Leo speaks. |
