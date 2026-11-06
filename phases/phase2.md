# Phase 2 — Browser voice bridge

**Status:** **PASSED** — user spoke into the browser and heard Tau reply.

## What was built

`realtime-api/` copied to `tau-orin/bridge/`; only the copy was edited.

| Piece | Backend |
| --- | --- |
| LLM | Ollama `http://localhost:8000`, `llama3.2:1b` |
| STT | Speaches `http://localhost:8001`, `Systran/faster-whisper-tiny` |
| TTS | Kokoro `http://localhost:8880`, voice `af_alloy`, 24 kHz |
| Browser | `https://<jetson-ip>:8080/` (self-signed TLS) |

## Edits

- `config.py`: localhost backend defaults, `llama3.2:1b`,
  `Systran/faster-whisper-tiny`, `af_alloy`, `tts_speed=100`, SSL fields.
- `stt_client.py`: sends the required `model` form field.
- `tts_client.py`: Kokoro `/v1/audio/speech`, WAV decode with `wave`,
  mono PCM16 at the WAV header rate.
- `protocol.py`: `TTS_SAMPLE_RATE = 24000`.
- `main.py`: uvicorn gets `ssl_certfile` / `ssl_keyfile` when set.
- `static/index.html`: voice dropdown lists Kokoro voices.
- `docker-compose.yaml`: `bridge` service, `network_mode: host`, certs/logs/
  data/prompts bind mounts, `DEFAULT_AEC_MODE=none`,
  `DEFAULT_TURN_DETECTION=server_vad`.

## Deviations from the phase file

1. **Torch is not CPU-only on aarch64 PyPI.** `pip install torch` pulled
   `torch 2.15.0` plus CUDA 13.2 wheels (torch 484 MB, cuDNN 595 MB, cuBLAS,
   …). The bridge runs Silero VAD on CPU only, so the Dockerfile installs
   `torch==2.10.0+cpu` from `download.pytorch.org/whl/cpu` before `pip install .`.
2. **No `torch.hub` at runtime.** `SileroVAD` used
   `torch.hub.load("snakers4/silero-vad", ...)`, which fetches from GitHub on
   first start. It now uses `silero_vad.load_silero_vad()`, which reads the JIT
   model bundled in the pip wheel, so the container works offline.
   `torch.set_num_threads(1)` keeps the VAD from competing with the models.

## HTTPS

Self-signed cert in `tau-orin/certs/`:

```text
CN=tau.local
SAN: DNS:tau.local, IP:172.20.10.2
```

Re-generate if the Jetson IP changes (browser mic needs a secure origin).

## Kokoro WAV header bug (found during testing)

Kokoro's `response_format: "wav"` writes a data chunk size of `0xFFFFFFFF`
("unknown length"). Python's `wave.readframes(getnframes())` then tries to
allocate ~4 GB and raises `MemoryError`, so the first TTS attempt would have
failed. Two fixes:

- `_decode_wav()` now caps `readframes()` at the frames the payload can hold.
- The bridge requests `response_format: "pcm"` (raw 24 kHz PCM16) and only
  parses a header if the body actually starts with `RIFF`.

## Sentence streaming enabled

`tts_mode` now defaults to `sentence` (was `whole`), and the browser's TTS-mode
select defaults to sentence-by-sentence. The code path already existed; this
only changes the default. It lowers end-of-speech → first-audio latency because
sentence 1 is synthesised while the LLM writes sentence 2.

## Automated end-to-end test (no browser mic)

A WebSocket client inside the container fed 1.6 s of Kokoro speech
("Hello from Tau.") plus trailing silence. Observed:

```text
15:06:27  VAD speech_stopped
15:06:28  STT transcript: "Hello from Tau."        (~0.5 s)
15:06:29  LLM sentence #1
15:06:31  LLM sentence #4 (reply finished)
15:06:32  TTS 208 chars → 10.8 s audio in 1.09 s
```

With `tts_mode=sentence` on a re-run:

```text
0.96 s  STT: "Hello from Tau."
1.59 s  LLM sentence #1
3.18 s  FIRST AUDIO (measured from end of speech, incl. 0.6 s VAD hangover)
```

The 3.18 s is dominated by the 600 ms VAD silence window, ~0.5 s STT, and the
first-sentence LLM+TTS; the reply was 4 long sentences because Phase 3's Tau
prompt is not applied yet.

## Component checks

- `GET https://localhost:8080/health` → `{"status":"ok"}`
- Ollama 1B completion → 7.7 s cold (model load), warm ~1 s.
- Speaches tiny round-trip from Kokoro audio → 3.4 s cold, ~0.5 s warm.
- Bridge TTS call → 0.69 s for 3.16 s of audio.

## RAM (all services warm, desktop running)

| Container | RSS |
| --- | --- |
| Kokoro | 1.19 GB |
| Ollama (1B loaded) | 1.74 GB |
| Speaches (tiny loaded) | 291 MB |
| Bridge (VAD loaded) | 312 MB |

`free`: 6.2 GB used, ~1.1 GB available, 4.0 GB swap in use. Tight but running.

## Browser test (user, 172.20.10.3 → 172.20.10.2:8080)

The page initially "wouldn't load": tcpdump showed the browser sending a plain
`GET / HTTP/1.1` to the TLS port (the URL was `http://`, or the browser
auto-filled it). Opening `https://172.20.10.2:8080` and accepting the cert
worked.

Then three turns completed end to end:

| Turn | STT | VAD stop → first audio |
| --- | --- | --- |
| "Hi." | `Hi.` | ~5 s (cold LLM) |
| "What's up?" | `What's up?` | ~2 s |
| "What's happening?" | `What's happening?` | ~2 s |

The user confirmed they heard Tau's voice through the laptop/phone speakers.
Replies were far too long (the generic llama3.2 prompt produced up to 6
sentences / 45 s of audio) — exactly what Phase 3's Tau prompt fixes.

**Done-when check passed.**
