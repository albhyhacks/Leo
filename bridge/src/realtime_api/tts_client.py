"""Async httpx client for Kokoro TTS (OpenAI-compatible /v1/audio/speech)."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
import wave

import httpx
import numpy as np

from .audio import resample_pcm16
from .config import settings
from .protocol import TTS_SAMPLE_RATE

log = logging.getLogger(__name__)

_req_counter = 0  # monotonic request ID for log correlation

# Regex to strip emoji (Supplementary Multilingual Plane + common emoji ranges)
_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F1E0-\U0001F1FF"   # flags
    "\U00002702-\U000027B0"   # dingbats
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000200D"              # zero-width joiner
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)

# Markdown-style formatting
_MARKDOWN_RE = re.compile(r"[*_~`#]")

# Max chars per TTS request. Kokoro handles long text, but keeping chunks
# moderate keeps first-audio latency low and matches the original pipeline.
_MAX_CLEAN_CHARS = 600


def _split_for_tts(text: str, max_chars: int = _MAX_CLEAN_CHARS) -> list[str]:
    """Split *text* into chunks of at most *max_chars* characters."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        idx = window.rfind(", ")
        if idx > 0:
            cut = idx + 2
        else:
            idx = window.rfind(" ")
            cut = idx + 1 if idx > 0 else max_chars
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks


_client: httpx.AsyncClient | None = None
_tts_semaphore: asyncio.Semaphore | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=60.0))
    return _client


def _reset_client() -> httpx.AsyncClient:
    """Close the existing client and create a fresh one (stale-connection recovery)."""
    global _client
    if _client is not None and not _client.is_closed:
        log.info("[TTS] resetting HTTP client (stale connection recovery)")
        asyncio.get_event_loop().create_task(_client.aclose())
    _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=60.0))
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _tts_semaphore
    if _tts_semaphore is None:
        _tts_semaphore = asyncio.Semaphore(settings.tts_concurrency)
        log.info("[TTS] concurrency gate: max %d parallel requests", settings.tts_concurrency)
    return _tts_semaphore


def _clean_for_tts(text: str) -> str:
    """Strip emoji, markdown, actions, dashes and quotes for TTS input."""
    # *actions* are shown in the transcript but must never be spoken.
    text = re.sub(r"\*[^*]*\*", " ", text, flags=re.DOTALL)
    text = _EMOJI_RE.sub(" ", text)
    text = _MARKDOWN_RE.sub("", text)
    # Em-dash / en-dash → comma (natural pause; raw dashes confuse TTS)
    text = text.replace("\u2014", ", ")
    text = text.replace("\u2013", ", ")
    # Curly single quotes / apostrophes → ASCII apostrophe (preserves contractions)
    text = text.replace("\u2018", "'")
    text = text.replace("\u2019", "'")
    # Strip double-quotes (TTS doesn't need to voice them)
    text = re.sub(r'["\u201c\u201d]', "", text)
    # Remove markdown list markers at line start:  - item  /  1. item
    text = re.sub(r"(?m)^\s*-\s+", " ", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", " ", text)
    # Collapse whitespace / newlines
    text = re.sub(r"\s+", " ", text).strip()
    return text


def trailing_pause_ms(original_text: str) -> int:
    """Return inter-sentence silence duration (ms) based on ending punctuation."""
    s = original_text.rstrip()
    if not s:
        return 200

    if re.search(r"!{3,}$", s):
        return 400
    if s.endswith("?!") or s.endswith("!?"):
        return 350
    if s.endswith("!!"):
        return 350
    if s.endswith("...") or s.endswith("\u2026"):
        return 400
    if s.endswith("."):
        return 350
    if s.endswith("?"):
        return 350
    if s.endswith("!"):
        return 300

    if _EMOJI_RE.search(s[-2:]):
        return 250

    return 200


def _decode_wav(data: bytes) -> tuple[bytes, int]:
    """Decode a WAV payload into mono PCM16 bytes + sample rate.

    Kokoro returns 24 kHz mono PCM16, but we read the header so a different
    rate still works. Stereo is downmixed to mono.
    """
    with wave.open(io.BytesIO(data), "rb") as w:
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        rate = w.getframerate()
        nframes = w.getnframes()
        # Kokoro streams WAV with an unknown-length data chunk (0xFFFFFFFF).
        # wave.readframes() would try to allocate that many frames, so cap the
        # request at the number of frames the payload can actually contain.
        max_frames = max(0, (len(data) - 44) // (channels * sampwidth))
        frames = w.readframes(min(nframes, max_frames))

    if sampwidth != 2:
        raise ValueError(f"expected 16-bit WAV, got {sampwidth * 8}-bit")

    if channels > 1:
        samples = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels)
        frames = samples.mean(axis=1).astype(np.int16).tobytes()

    return frames, rate


async def _synthesize_single(
    clean: str,
    url: str,
    voice: str,
    speed: int,
    cancel_event: asyncio.Event | None = None,
) -> bytes:
    """Synthesize one chunk via Kokoro, returning raw mono PCM16 at TTS_SAMPLE_RATE."""
    global _req_counter
    _req_counter += 1
    req_id = _req_counter
    tag = f"[TTS req={req_id}]"

    if cancel_event and cancel_event.is_set():
        return b""

    payload: dict = {
        "input": clean,
        "voice": voice,
        # Kokoro's WAV writer emits a 0xFFFFFFFF data-chunk size, which breaks
        # Python's wave reader. Raw PCM at 24 kHz avoids the broken header.
        "response_format": "pcm",
    }
    # Kokoro expects a multiplier; settings use percent (100 = normal).
    if speed != 100:
        payload["speed"] = speed / 100.0

    log.info("%s request: %d chars | voice=%s | %s", tag, len(clean), voice, clean[:120])

    sem = _get_semaphore()
    t_wait = time.monotonic()

    async with sem:
        sem_waited = time.monotonic() - t_wait
        if sem_waited > 0.01:
            log.info("%s semaphore acquired after %.3fs wait", tag, sem_waited)

        for attempt in range(2):
            try:
                client = _get_client()
                t0 = time.monotonic()
                resp = await client.post(url, json=payload)
                elapsed = time.monotonic() - t0

                if resp.status_code != 200:
                    log.error("%s HTTP %d after %.2fs | %s",
                              tag, resp.status_code, elapsed,
                              resp.text[:200])
                    return b""

                if not resp.content:
                    log.error("%s EMPTY response body after %.2fs | %s",
                              tag, elapsed, clean[:80])
                    if attempt == 0:
                        _reset_client()
                        continue
                    return b""

                if cancel_event and cancel_event.is_set():
                    return b""

                if resp.content[:4] == b"RIFF":
                    pcm, rate = _decode_wav(resp.content)
                    if rate != TTS_SAMPLE_RATE:
                        pcm = resample_pcm16(pcm, rate, TTS_SAMPLE_RATE)
                else:
                    # Raw PCM16 mono at Kokoro's native 24 kHz.
                    pcm, rate = resp.content, TTS_SAMPLE_RATE

                duration = len(pcm) / 2 / TTS_SAMPLE_RATE
                log.info("%s result: %d bytes (%.2fs audio, %d Hz) in %.2fs | %s",
                         tag, len(pcm), duration, rate, elapsed, clean[:120])
                return pcm

            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                log.error("%s %s (attempt %d)", tag, type(e).__name__, attempt + 1)
                if attempt == 0:
                    _reset_client()
                    continue
                return b""
            except Exception as e:
                log.error("%s error (%s, attempt %d): %s",
                          tag, type(e).__name__, attempt + 1, e)
                return b""

        return b""


async def synthesize(
    text: str,
    voice: str | None = None,
    speed: int | None = None,
    tts_url: str | None = None,
    cancel_event: asyncio.Event | None = None,
) -> bytes:
    """Synthesize text via Kokoro, returning complete mono PCM16 audio at 24 kHz.

    Signature is unchanged so ``ws_handler.py`` and ``barge_in.py`` keep working.
    """
    url = (tts_url or settings.tts_url).rstrip("/") + "/v1/audio/speech"
    kokoro_voice = voice or settings.default_voice
    spd = speed if speed is not None else settings.tts_speed

    clean = _clean_for_tts(text)
    if not clean:
        log.debug("[TTS] skipping empty text after cleanup (original: %s)", text[:40])
        return b""

    chunks = _split_for_tts(clean)
    if len(chunks) > 1:
        log.warning("[TTS] text too long (%d chars), split into %d chunks",
                    len(clean), len(chunks))

    pcm_parts: list[bytes] = []
    for i, chunk in enumerate(chunks):
        if cancel_event and cancel_event.is_set():
            break
        if len(chunks) > 1:
            log.info("[TTS] chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
        pcm = await _synthesize_single(chunk, url, kokoro_voice, spd, cancel_event)
        if pcm:
            pcm_parts.append(pcm)
    return b"".join(pcm_parts)


async def synthesize_stream(
    text: str,
    voice: str | None = None,
    speed: int | None = None,
    tts_url: str | None = None,
    cancel_event: asyncio.Event | None = None,
):
    """Compatibility wrapper — yields the complete result as one chunk."""
    data = await synthesize(
        text, voice=voice, speed=speed, tts_url=tts_url, cancel_event=cancel_event
    )
    if data:
        yield data
