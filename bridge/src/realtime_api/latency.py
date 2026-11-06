"""Per-turn latency rows in ``logs/latency.csv`` (Phase 5 + Phase 6).

One row per spoken turn, written after the reply has been generated so the
instrumentation can never delay audio.  Columns:

* ``ts`` / ``session`` — UTC timestamp and session id
* ``speech_end_to_transcript_ms`` — STT, from the VAD commit to the transcript
* ``transcript_to_first_token_ms`` — LLM time to first content delta
* ``first_token_to_first_audio_ms`` — first sentence + first TTS call
* ``total_ms`` — speech end to the last audio chunk handed to the WebSocket
* ``llm_total_ms`` — transcript to the last LLM token
* ``tts_ms`` — same start as ``first_token_to_first_audio_ms`` (first spoken audio)
* ``llm_tokens`` / ``tokens_per_s`` — completion tokens from Ollama's usage chunk
* ``sentences`` / ``reply_chars`` — size of the reply
* ``status`` — ``completed`` or ``cancelled``

Missing stages are written as an empty field, never as ``0``.  Files written by
Phase 5 (which has the old 9-column header) are migrated in place on the first
new turn, preserving every old row.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

HEADER = [
    "ts",
    "session",
    "speech_end_to_transcript_ms",
    "transcript_to_first_token_ms",
    "first_token_to_first_audio_ms",
    "total_ms",
    "llm_total_ms",
    "tts_ms",
    "llm_tokens",
    "tokens_per_s",
    "sentences",
    "reply_chars",
    "status",
]

_lock = threading.Lock()


def _ms(later: float | None, earlier: float | None) -> int | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier) * 1000)


def _cell(value) -> str:
    return "" if value is None else str(value)


def _migrate(path: Path) -> None:
    """Rewrite an old-header CSV with the current HEADER, keeping all rows."""
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return
    tmp = path.with_name(path.name + ".migrate.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in HEADER})
    tmp.replace(path)
    log.info("[LATENCY] migrated %s to the Phase 6 header (%d rows)", path, len(rows))


def _append(row: dict) -> None:
    path = Path(settings.latency_csv)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0
        if not is_new:
            try:
                with path.open(newline="", encoding="utf-8") as fh:
                    current_header = next(csv.reader(fh), [])
            except (OSError, csv.Error):
                current_header = []
            if current_header != HEADER:
                _migrate(path)
                is_new = False
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=HEADER, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(row)


def record_turn(
    session,
    *,
    first_audio_t: float | None,
    last_audio_t: float | None,
    sentences: int,
    reply_chars: int,
    status: str,
    llm_tokens: int | None = None,
) -> dict | None:
    """Append one row for the turn that just finished.

    Returns the same numbers as a dict so the bridge can also send them to the
    browser as a ``tau.metrics`` event.  ``None`` when logging is disabled.
    """
    if not settings.latency_enabled:
        return None

    marks = session.timing
    speech_end = marks.get("speech_end")
    if speech_end is None:
        # Typed turns never went through the microphone, so they are not a
        # speech-latency sample.  (Without this a text turn would either write
        # an empty row or, worse, reuse the marks of the previous voice turn.)
        return None
    transcript = marks.get("transcript")
    first_token = marks.get("first_token")
    llm_done = marks.get("llm_done")

    stt_ms = _ms(transcript, speech_end)
    llm_first_token_ms = _ms(first_token, transcript)
    llm_total_ms = _ms(llm_done, transcript)
    tts_ms = _ms(first_audio_t, first_token)
    total_ms = _ms(last_audio_t or first_audio_t, speech_end)

    tokens = llm_tokens if isinstance(llm_tokens, int) and llm_tokens > 0 else None
    tokens_per_s = None
    if tokens is not None and llm_total_ms and llm_total_ms > 0:
        tokens_per_s = round(tokens / (llm_total_ms / 1000.0), 1)

    metrics = {
        "stt_ms": stt_ms,
        "llm_first_token_ms": llm_first_token_ms,
        "llm_total_ms": llm_total_ms,
        "tts_ms": tts_ms,
        "total_ms": total_ms,
        "llm_tokens": tokens,
        "tokens_per_s": tokens_per_s,
        "sentences": sentences,
        "reply_chars": reply_chars,
        "status": status,
    }

    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": session.id,
        "speech_end_to_transcript_ms": _cell(stt_ms),
        "transcript_to_first_token_ms": _cell(llm_first_token_ms),
        "first_token_to_first_audio_ms": _cell(tts_ms),
        "total_ms": _cell(total_ms),
        "llm_total_ms": _cell(llm_total_ms),
        "tts_ms": _cell(tts_ms),
        "llm_tokens": _cell(tokens),
        "tokens_per_s": _cell(tokens_per_s),
        "sentences": sentences,
        "reply_chars": reply_chars,
        "status": status,
    }

    try:
        _append(row)
    except OSError as exc:  # never break a turn over logging
        log.error("[LATENCY] cannot append to %s: %s", settings.latency_csv, exc)
        return None

    metrics["ts"] = row["ts"]
    return metrics
