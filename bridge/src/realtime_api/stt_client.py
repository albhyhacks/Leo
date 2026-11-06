"""Async httpx client for Parakeet STT."""

from __future__ import annotations

import logging

import httpx

from .audio import client_pcm16_to_wav_16k
from .config import settings

log = logging.getLogger(__name__)

# Whisper-family models invent these on silence / room noise / breaths.
_HALLUCINATIONS = {
    "thank you", "thank you very much", "thanks", "thanks for watching",
    "thank you for watching", "you", "bye", "bye bye", "hello dear",
    "hmm", "um", "uh",
    "subtitles by the amara.org community", "please subscribe",
}


def _is_hallucination(text: str) -> bool:
    import re
    words = re.sub(r"[^a-z' ]", "", text.lower()).split()
    # repetition loop: "I didn't, I didn't, I didn't, ..."
    for n in (1, 2, 3, 4):
        if len(words) >= 3 * n and all(
            words[i:i + n] == words[:n] for i in range(0, len(words) - n + 1, n)
        ):
            return True
    norm = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    return len(norm) < settings.stt_min_chars or norm in _HALLUCINATIONS


async def transcribe(pcm16_24khz: bytes, stt_url: str | None = None) -> str:
    """Send PCM16 24kHz audio to Parakeet STT and return the transcription text.

    Converts 24kHz PCM16 to 16kHz WAV before sending.
    """
    url = (stt_url or settings.stt_url).rstrip("/") + "/v1/audio/transcriptions"
    wav_data = client_pcm16_to_wav_16k(pcm16_24khz)

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            url,
            data={"model": settings.stt_model, "language": "en"},
            files={"file": ("audio.wav", wav_data, "audio/wav")},
        )

    if resp.status_code != 200:
        log.error("STT error %d: %s", resp.status_code, resp.text)
        return ""

    text = resp.json().get("text", "").strip()
    if _is_hallucination(text):
        log.info("STT dropped as likely noise/hallucination: %r", text)
        return ""
    log.info("STT transcript: %s", text)
    return text
