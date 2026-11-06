#!/usr/bin/env python3
"""Phase 3 smoke test — proves history persistence without a browser mic.

Runs *inside* the bridge container (host networking, so localhost:8880/8080
are reachable). It synthesises a sentence with Kokoro, feeds it into the
Realtime WebSocket as 24 kHz PCM16, and waits for the reply. Afterwards check
that ``data/history.jsonl`` gained a user + assistant turn.

Usage (from tau-orin/):
    export SUDO_ASKPASS=/tmp/opencode/askpass.sh
    sudo -A docker cp tests/phase3_smoke.py tau-bridge-1:/tmp/p3.py
    sudo -A docker exec tau-bridge-1 python /tmp/p3.py
    tail -2 data/history.jsonl

Exit code 0 = full turn completed (transcript in, response.done out).
"""

from __future__ import annotations

import asyncio
import base64
import json
import ssl

import httpx
import websockets

TTS_URL = "http://localhost:8880/v1/audio/speech"
WS_URL = "wss://localhost:8080/v1/realtime"
VOICE = "af_alloy"
SPEAK_TEXT = "Hello Tau. My name is Aarav and I study computer science."
CHUNK = 4800          # 100 ms of 24 kHz PCM16
TIMEOUT_S = 120


async def synth(text: str) -> bytes:
    """Return raw 24 kHz mono PCM16 for *text* (Kokoro, no WAV header)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            TTS_URL,
            json={"input": text, "voice": VOICE, "response_format": "pcm"},
        )
    resp.raise_for_status()
    return resp.content


async def main() -> int:
    pcm = await synth(SPEAK_TEXT)
    duration = len(pcm) / 2 / 24000
    print(f"[test] TTS {len(pcm)} bytes ({duration:.2f}s @24kHz)", flush=True)
    if len(pcm) < 24000:  # < 0.5 s of audio: VAD will never latch
        print("[test] FAIL: TTS audio too short", flush=True)
        return 1

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    seen: dict[str, object] = {"transcript": None, "done": None}

    # The bridge accepts with subprotocol="realtime"; websockets >= 14 refuses
    # the handshake unless the client asks for it explicitly.
    async with websockets.connect(
        WS_URL, ssl=ctx, max_size=None, ping_interval=None,
        subprotocols=["realtime"],
    ) as ws:

        async def reader() -> None:
            while True:
                event = json.loads(await ws.recv())
                etype = event.get("type", "")
                if etype == "conversation.item.input_audio_transcription.completed":
                    seen["transcript"] = event.get("transcript")
                    print(f"[test] TRANSCRIPT: {event.get('transcript')!r}", flush=True)
                elif etype == "response.audio_transcript.delta":
                    print(f"[test] delta: {event.get('delta', '')[:70]}", flush=True)
                elif etype == "response.audio.delta":
                    seen.setdefault("first_audio", True)
                elif etype == "response.done":
                    seen["done"] = event.get("response", {}).get("status")
                    print(f"[test] DONE status={seen['done']}", flush=True)
                    return
                elif etype == "error":
                    print(f"[test] ERROR event: {event.get('error')}", flush=True)

        reader_task = asyncio.create_task(reader())
        await asyncio.sleep(0.5)  # let session.created arrive

        # Speak: real-time-ish pacing so the VAD sees continuous speech.
        for offset in range(0, len(pcm), CHUNK):
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm[offset:offset + CHUNK]).decode("ascii"),
            }))
            await asyncio.sleep(0.02)

        # 2 s of silence closes the VAD's 600 ms end-of-speech window.
        silence = b"\x00" * CHUNK
        for _ in range(20):
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(silence).decode("ascii"),
            }))
            await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(reader_task, timeout=TIMEOUT_S)
        except asyncio.TimeoutError:
            print("[test] TIMEOUT waiting for response.done", flush=True)
            reader_task.cancel()
            return 1

    ok = bool(seen["transcript"]) and seen["done"] == "completed"
    print(f"[test] {'PASS' if ok else 'FAIL'} transcript={seen['transcript']!r} "
          f"status={seen['done']} first_audio={bool(seen.get('first_audio'))}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
