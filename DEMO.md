# Leo — 2-minute demo script

The demo is a conversation. Everything is spoken to Leo in the browser; the
terminal is used twice (to show the saved facts and to restart the bridge live).
The page is one screen: Leo's words on the left, the sun on the right, and two
drawers (**Conversation** and **Systems**) for the detail.

## 0. Before the clock starts (~2 min)

```bash
cd ~/autonomous-intelligence/tau-orin   # or wherever you cloned Leo
./run-local.sh                          # starts the stack, opens https://localhost:8080
```

1. Accept the self-signed certificate warning once.
2. Click **Start microphone** and allow it.
3. Say **"Hi Leo, can you hear me?"** once as a warm-up. The first turn loads
   the models and is the slowest. Then start the clock.
4. Use headphones if you can; the echo gate is not true echo cancellation.

## The script

| Time | What you do | What to watch for |
| --- | --- | --- |
| 0:00 | Say **"Hey Leo, how are you doing today?"** | The status line changes (listening, thinking, speaking) and Leo's reply appears as large text as it is spoken. The sun's rays react to your voice and to his. |
| 0:20 | Open **Systems** | Real board data: CPU, GPU, temperature, power, RAM, service health, model names and measured latency. Close it again. |
| 0:35 | Say **"My name is R.F. and I study computer science."** | Leo acknowledges it. After the reply the memory count in the header goes up and new dots appear around the sun. |
| 0:50 | Ask **"Can you suggest a name for a pet cat?"** | Normal conversation. Memory work happens after the reply, so the answer is not slowed down. |
| 1:05 | Click a dot near the sun | The remembered fact appears, with a **Forget this memory** button. |
| 1:15 | In the terminal: `cat data/facts.md`, then `docker restart tau-bridge-1` (about 5 s) and reload the page | The facts are plain text on disk, and the conversation comes back after the restart. |
| 1:40 | Say **"What's my name?"** | Leo answers with the name you gave him. That proves the memory survived the restart. |
| 1:50 | Open **Conversation** | The stored history, in order, with timestamps. |

## Why each beat exists

- **Greeting and question:** shows the full speech-in, speech-out loop, not typed chat.
- **Systems drawer:** the Jetson is the body: watts, thermals, RAM and the real model names.
- **Two facts, no explicit "remember this":** the rule-based extractor picks up first-person statements in the background.
- **Live restart:** the state is on disk (`data/`), not in the process.
- **Dots and `facts.md`:** memory is inspectable and deletable, which is the privacy answer.

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| No microphone prompt | The page must be on `https://` and the certificate accepted. Run `./scripts/gen-cert.sh` if the certificate is missing. |
| Leo hears himself or repeats | Use headphones, or speak only after Leo has finished. |
| First reply takes about 5 s or more | Cold models. Do the warm-up turn before the clock starts. |
| Replies take a minute or more | Ollama fell back to the CPU. Check `curl localhost:8000/api/ps`: `size_vram` should be close to `size`. Free memory and reload the model. |
| Nothing after a restart | Wait for the `./start.sh` health line, reload the page, then speak once. |
| Long, rambling answer | A known limit of `llama3.2:1b`. Talk over it to interrupt. |
