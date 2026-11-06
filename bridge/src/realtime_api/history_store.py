"""Persistent conversation history and Leo system-prompt assembly.

The history file is JSON Lines, one turn per line::

    {"ts": "2026-11-05T21:30:00+05:30", "role": "user", "text": "Hi Leo"}

Only the last ``CONTEXT_TURNS`` turns are replayed into a new session, and the
same bound is applied to every LLM request.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("TAU_DATA_DIR", "/data"))
HISTORY_FILE = Path(os.environ.get("TAU_HISTORY_FILE", DATA_DIR / "history.jsonl"))
FACTS_FILE = Path(os.environ.get("TAU_FACTS_FILE", DATA_DIR / "facts.md"))
PROMPTS_DIR = Path(os.environ.get("TAU_PROMPTS_DIR", "/prompts"))
SYSTEM_PROMPT_FILE = PROMPTS_DIR / "tau.system.md"

CONTEXT_TURNS = 20

_FALLBACK_PROMPT = (
    "You are Leo, a warm, curious and safe voice companion. "
    "Your body is an NVIDIA Jetson Orin Nano; you hear and speak through a "
    "web page on your friend's laptop or phone, and all your models run "
    "locally. You are not in the cloud. Reply in 1-3 short spoken sentences, "
    "with no markdown, lists or emoji. Put optional physical actions in "
    "*asterisks* after your speech.\n\n"
    "# Facts you know\n{{direct_knowledge}}\n"
)


def _now() -> datetime:
    return datetime.now().astimezone()


def append_turn(role: str, text: str, ts: datetime | None = None) -> dict:
    """Append one turn to the history file and return the stored record."""
    text = (text or "").strip()
    if not text:
        return {}
    entry = {
        "ts": (ts or _now()).isoformat(timespec="seconds"),
        "role": role,
        "text": text,
    }
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.error("[HISTORY] cannot write %s: %s", HISTORY_FILE, exc)
    return entry


def correct_last_user_turn(text: str) -> bool:
    """Replace the latest persisted user turn and remove its stale reply."""
    text = " ".join((text or "").split())
    if not text:
        return False
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.error("[HISTORY] cannot read %s for correction: %s", HISTORY_FILE, exc)
        return False

    entries: list[tuple[str, dict | None]] = []
    last_user_index = None
    for raw in lines:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            entries.append((raw, None))
            continue
        entries.append((raw, entry))
        if entry.get("role") == "user":
            last_user_index = len(entries) - 1
    if last_user_index is None:
        return False

    user_entry = entries[last_user_index][1]
    assert user_entry is not None
    user_entry["text"] = text
    corrected = [
        json.dumps(user_entry, ensure_ascii=False) if index == last_user_index else raw
        for index, (raw, _entry) in enumerate(entries[:last_user_index + 1])
    ]
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = HISTORY_FILE.with_name(HISTORY_FILE.name + ".tmp")
        temp_file.write_text("\n".join(corrected) + "\n", encoding="utf-8")
        temp_file.replace(HISTORY_FILE)
    except OSError as exc:
        log.error("[HISTORY] cannot save correction to %s: %s", HISTORY_FILE, exc)
        return False
    log.info("[HISTORY] corrected the latest user turn and removed its old reply")
    return True


def load_recent(limit: int = CONTEXT_TURNS) -> list[dict]:
    """Return the last *limit* valid turns, oldest first."""
    if not HISTORY_FILE.exists():
        return []

    turns: list[dict] = []
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("[HISTORY] skipping malformed line")
                    continue
                if entry.get("role") in ("user", "assistant") and entry.get("text"):
                    turns.append(entry)
    except OSError as exc:
        log.error("[HISTORY] cannot read %s: %s", HISTORY_FILE, exc)
        return []

    return turns[-limit:]


def _humanize_delta(delta_seconds: float) -> str:
    total = max(0, int(delta_seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} days")
    if hours:
        parts.append(f"{hours} hours")
    if minutes:
        parts.append(f"{minutes} minutes")
    if seconds or not parts:
        parts.append(f"{seconds} seconds")
    return ", ".join(parts)


def time_prefix(now: datetime | None = None) -> str:
    """Build the ``[timestamp][time since last message]`` prefix for the LLM.

    The prefix is deliberately kept out of the browser transcript and out of
    the stored history; it is only prepended to the user message that goes to
    the model on this turn.
    """
    now = now or _now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")

    recent = load_recent(1)
    if not recent:
        return f"[{stamp}]"

    try:
        last = datetime.fromisoformat(recent[-1]["ts"])
        if last.tzinfo is None:
            last = last.astimezone()
        gap = (now - last).total_seconds()
    except (KeyError, ValueError):
        return f"[{stamp}]"

    return f"[{stamp}][{_humanize_delta(gap)}]"


def load_facts() -> str:
    """Read ``facts.md`` fresh so Phase 4 updates are picked up each turn."""
    try:
        return FACTS_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        log.error("[FACTS] cannot read %s: %s", FACTS_FILE, exc)
        return ""


def build_system_prompt(override: str = "") -> str:
    """Return the browser override, or Tau's prompt with facts substituted.

    Re-read on every turn so edits to ``tau.system.md`` or ``facts.md`` apply
    to the next reply without restarting the container.
    """
    if override and override.strip():
        return override.strip()

    try:
        template = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("[PROMPT] %s missing, using built-in fallback", SYSTEM_PROMPT_FILE)
        template = _FALLBACK_PROMPT
    except OSError as exc:
        log.error("[PROMPT] cannot read %s: %s", SYSTEM_PROMPT_FILE, exc)
        template = _FALLBACK_PROMPT

    facts = load_facts()
    return template.replace("{{direct_knowledge}}", facts)
