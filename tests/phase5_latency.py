#!/usr/bin/env python3
"""Phase 5 — measure end-to-end latency over N spoken turns.

Runs *inside* the bridge container (host networking, so Kokoro on :8880 and
the bridge on :8080 are reachable) and speaks through the Realtime WebSocket
exactly like the browser does: Kokoro synthesises the text, the PCM16 is
pushed in 100 ms chunks, 2 s of silence closes the VAD turn.

The bridge writes one row per turn to ``/logs/latency.csv`` (host:
``tau-orin/logs/latency.csv``); this script waits until the turns are done and
prints median / max per stage for the turns it just made.

Usage (from tau-orin/):
    docker cp tests/phase5_latency.py tau-bridge-1:/tmp/p5.py
    docker exec tau-bridge-1 python /tmp/p5.py [turns]

Exit code 0 = every turn completed and the CSV holds its rows.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import json
import statistics
import ssl
import sys

import httpx
import websockets

TTS_URL = "http://localhost:8880/v1/audio/speech"
WS_URL = "wss://localhost:8080/v1/realtime"
CSV_PATH = "/logs/latency.csv"
VOICE = "af_alloy"
CHUNK = 4800              # 100 ms of 24 kHz PCM16
SILENCE_CHUNKS = 20       # 2 s of silence closes the VAD window
TURN_TIMEOUT_S = 120

# Short, natural utterances — a mix of small talk and one factual statement so
# memory extraction runs against realistic input.
UTTERANCES = [
    "Hey Tau, are you there?",
    "How are you doing today?",
    "What is the weather usually like in Bangalore?",
    "Tell me one interesting thing about the moon.",
    "My favourite food is dosa.",
    "Do you like music?",
    "What time is it right now?",
    "Can you suggest a name for a pet cat?",
    "I am working on a robotics project.",
    "Thanks Tau, that is all for now.",
]


async def synth(client: httpx.AsyncClient, text: str) -> bytes:
    resp = await client.post(
        TTS_URL, json={"input": text, "voice": VOICE, "response_format": "pcm"}
    )
    resp.raise_for_status()
    return resp.content


async def main(turns: int) -> int:
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    results: list[dict] = []
    session_id: str | None = None

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Pre-synthesise so TTS time is not part of the measured turn.
        clips = []
        for text in UTTERANCES[:turns]:
            pcm = await synth(client, text)
            duration = len(pcm) / 2 / 24000
            print(f"[p5] clip {len(pcm)}B ({duration:.2f}s): {text}", flush=True)
            clips.append(pcm)

        async with websockets.connect(
            WS_URL, ssl=ssl_ctx, max_size=None, ping_interval=None,
            subprotocols=["realtime"],
        ) as ws:
            done = asyncio.Event()
            turn: dict[str, object] = {}

            async def reader() -> None:
                nonlocal session_id
                while True:
                    event = json.loads(await ws.recv())
                    etype = event.get("type", "")
                    if etype == "session.created":
                        session_id = event.get("session", {}).get("id")
                        print(f"[p5] session {session_id}", flush=True)
                    elif etype == "response.audio.delta":
                        if "first_audio" not in turn:
                            turn["first_audio"] = asyncio.get_running_loop().time()
                        turn["audio_bytes"] = turn.get("audio_bytes", 0) + len(
                            base64.b64decode(event.get("delta", ""))
                        )
                    elif etype == "response.done":
                        turn["status"] = event.get("response", {}).get("status")
                        done.set()
                    elif etype == "error":
                        print(f"[p5] ERROR: {event.get('error')}", flush=True)

            reader_task = asyncio.create_task(reader())
            await asyncio.sleep(0.5)  # let session.created arrive

            loop = asyncio.get_running_loop()
            silence = b"\x00" * CHUNK

            for idx, pcm in enumerate(clips, start=1):
                done.clear()
                turn.clear()
                print(f"\n[p5] === turn {idx}/{len(clips)} ===", flush=True)
                for offset in range(0, len(pcm), CHUNK):
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm[offset:offset + CHUNK]).decode("ascii"),
                    }))
                    await asyncio.sleep(0.02)
                spoke_done = loop.time()
                for _ in range(SILENCE_CHUNKS):
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(silence).decode("ascii"),
                    }))
                    await asyncio.sleep(0.02)

                try:
                    await asyncio.wait_for(done.wait(), timeout=TURN_TIMEOUT_S)
                except asyncio.TimeoutError:
                    print(f"[p5] turn {idx} TIMEOUT", flush=True)
                    reader_task.cancel()
                    return 1

                first_audio = turn.get("first_audio")
                client_ms = (
                    (first_audio - spoke_done) * 1000
                    if isinstance(first_audio, float) else None
                )
                audio_s = turn.get("audio_bytes", 0) / 2 / 24000
                results.append({
                    "turn": idx,
                    "status": turn.get("status"),
                    "client_first_audio_ms": client_ms,
                    "reply_audio_s": audio_s,
                })
                print(
                    f"[p5] turn {idx} status={turn.get('status')} "
                    f"client_first_audio={client_ms:.0f}ms reply_audio={audio_s:.1f}s"
                    if client_ms is not None else
                    f"[p5] turn {idx} status={turn.get('status')} (no audio)",
                    flush=True,
                )

                # Let the echo cooldown / playback window expire before the
                # next utterance, otherwise the VAD gate swallows it.
                await asyncio.sleep(audio_s + 2.0)

            reader_task.cancel()

    # ---- report from the bridge's own CSV -------------------------------
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if session_id is None or row["session"] == session_id:
                rows.append(row)

    print(f"\n[p5] CSV rows for this session: {len(rows)}")
    stages = [
        ("speech_end_to_transcript_ms", "speech end → transcript (STT)"),
        ("transcript_to_first_token_ms", "transcript → first LLM token"),
        ("first_token_to_first_audio_ms", "first token → first audio chunk"),
        ("total_ms", "speech end → last audio chunk"),
    ]
    ok = len(rows) >= len(results)
    print(f"\n[p5] {'stage':42s} {'median':>8s} {'max':>8s} {'n':>3s}")
    for key, label in stages:
        values = [int(r[key]) for r in rows if r[key] not in ("", None)]
        if not values:
            print(f"[p5] {label:42s} {'—':>8s} {'—':>8s} {0:>3d}")
            ok = False
            continue
        print(f"[p5] {label:42s} {statistics.median(values):8.0f} "
              f"{max(values):8d} {len(values):3d}")

    failed = [r for r in results if r["status"] != "completed"]
    print(f"[p5] {'PASS' if ok and not failed else 'FAIL'} "
          f"turns={len(results)} rows={len(rows)} non_completed={len(failed)}")
    return 0 if ok and not failed else 1


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    raise SystemExit(asyncio.run(main(n)))
