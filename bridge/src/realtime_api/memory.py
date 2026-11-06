"""Phase 4 — long-term facts that outlive the 20-turn history window.

``history_store.load_facts()`` already reads ``data/facts.md`` and substitutes it
into the system prompt on every turn, so anything written here is recalled on the
next reply with no restart. This module adds only the *write* path.

Why rules and not the model
---------------------------
The plan called for the local LLM to summarise durable facts. Measured on this
machine, ``llama3.2:1b`` cannot do it. Asked to copy the Friend's own sentences
it continued the dialogue and invented facts instead::

    NONE
    The Friend has a pet cat named Mochi.
    Tau: That's great! What kind of cat is it?

and asked to answer a YES/NO gate it said NO for obvious facts. Six prompt
variants were tried (see ``phases/phase4.md``); none was both faithful and
stable. A hallucinated fact is worse than a missing one, because ``facts.md`` is
injected into every system prompt.

So extraction is deterministic: the user's first-person sentences are matched by
regex and rewritten in the third person, keeping their own words. Nothing
invented can reach the prompt, there is no extra model call (no RAM pressure and
no way to delay a reply), and a scan costs microseconds. The trade-off is
recall: a fact phrased outside the patterns below is simply not stored.
"""

from __future__ import annotations

import hashlib
import logging
import re

from .config import settings
from .history_store import FACTS_FILE, load_recent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# facts.md read/write helpers
# ---------------------------------------------------------------------------
def _read_lines() -> list[str]:
    """Return the fact lines currently in ``facts.md`` (empty list if absent)."""
    try:
        text = FACTS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.error("[MEMORY] cannot read %s: %s", FACTS_FILE, exc)
        return []
    return [line.strip() for line in text.splitlines() if line.strip().startswith("- ")]


def _write_lines(lines: list[str]) -> None:
    """Write the fact list atomically, so a crash cannot truncate memory."""
    FACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FACTS_FILE.with_name(FACTS_FILE.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(FACTS_FILE)


def _norm(line: str) -> str:
    """Normalise a fact for duplicate detection (case/punctuation insensitive)."""
    text = line.strip().lstrip("- ").strip().lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


def _already_known(candidate: str, known: list[str]) -> bool:
    """True if *candidate* duplicates (or is contained in) an existing fact."""
    norm = _norm(candidate)
    if not norm:
        return True
    for existing in known:
        if norm == existing:
            return True
        # Substring containment catches restatements such as "studies CS at
        # Karunya" vs "studies computer science at Karunya University", but
        # only when both sides are long enough to be specific.
        if len(norm) >= 15 and len(existing) >= 15 and (norm in existing or existing in norm):
            return True
    return False


def save_facts(candidates: list[str]) -> list[str]:
    """Append new facts to ``facts.md`` and return the ones actually added.

    Duplicates are dropped (case/punctuation insensitive) and the file is
    capped at ``settings.memory_max_lines``, oldest lines dropped first.
    """
    if not candidates:
        return []

    existing = _read_lines()
    known = [_norm(line) for line in existing]
    added: list[str] = []

    for raw in candidates:
        line = " ".join(raw.split())
        if not line.startswith("- ") or len(line) <= 3:
            continue
        if _already_known(line, known):
            log.debug("[MEMORY] skipping known fact: %s", line)
            continue
        existing.append(line)
        known.append(_norm(line))
        added.append(line)

    if not added:
        return []

    cap = max(1, settings.memory_max_lines)
    if len(existing) > cap:
        log.info("[MEMORY] trimming %d oldest fact(s) (cap=%d)", len(existing) - cap, cap)
        existing = existing[-cap:]

    try:
        _write_lines(existing)
    except OSError as exc:
        log.error("[MEMORY] cannot write %s: %s", FACTS_FILE, exc)
        return []

    for line in added:
        log.info("[MEMORY] + %s", line)
    log.info("[MEMORY] saved %d new fact(s), %d total in %s",
             len(added), len(existing), FACTS_FILE)
    return added


def clear_facts() -> int:
    """Delete the facts file. Returns how many facts were removed."""
    removed = len(_read_lines())
    try:
        FACTS_FILE.unlink(missing_ok=True)
    except OSError as exc:
        log.error("[MEMORY] cannot delete %s: %s", FACTS_FILE, exc)
        return 0
    log.info("[MEMORY] cleared %d fact(s) (%s)", removed, FACTS_FILE)
    return removed


# ---------------------------------------------------------------------------
# Stable IDs for the dashboard's per-fact delete button
# ---------------------------------------------------------------------------
def fact_id(line: str) -> str:
    """Stable short id derived from the fact text (survives reordering)."""
    text = " ".join(line.strip().lstrip("- ").split())
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def list_facts() -> list[dict]:
    """Return facts as ``{"id", "text"}`` with ids stable across edits.

    Duplicate lines get a ``-2``, ``-3`` … suffix on their id (the first keeps
    the plain hash), so deleting one duplicate cannot delete both.
    """
    items: list[dict] = []
    counts: dict[str, int] = {}
    for line in _read_lines():
        base = fact_id(line)
        counts[base] = counts.get(base, 0) + 1
        item_id = base if counts[base] == 1 else f"{base}-{counts[base]}"
        items.append({"id": item_id, "text": line.lstrip("- ").strip()})
    return items


def delete_fact(item_id: str) -> bool:
    """Delete one fact by stable id; rewrite the file atomically.

    Returns ``True`` when a fact was removed, ``False`` when the id is unknown.
    Line numbers would shift after every delete, which is why the dashboard
    uses text-derived ids instead.
    """
    lines = _read_lines()
    counts: dict[str, int] = {}
    for index, line in enumerate(lines):
        base = fact_id(line)
        counts[base] = counts.get(base, 0) + 1
        current = base if counts[base] == 1 else f"{base}-{counts[base]}"
        if current != item_id:
            continue
        del lines[index]
        try:
            _write_lines(lines)
        except OSError as exc:
            log.error("[MEMORY] cannot write %s: %s", FACTS_FILE, exc)
            return False
        log.info("[MEMORY] deleted fact %s: %s", item_id, line)
        return True
    return False


# ---------------------------------------------------------------------------
# Extraction: first-person sentence -> third-person fact
# ---------------------------------------------------------------------------
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# "My name is R.F. and I study computer science" is two statements, and a rule
# that grabs everything after "my name is" would swallow both. Split compound
# clauses before matching so each rule sees one fact.
_COMPOUND_RE = re.compile(r"\s+and\s+(?=i(?:'m|\b))", re.IGNORECASE)

# Fillers people put in front of a sentence when speaking.
_LEAD_RE = re.compile(
    r"^(?:also|and|so|well|um+|uh+|er+|yeah|yep|okay|ok|oh|ah|actually|plus|"
    r"by the way|anyway)\b[,\s]*",
    re.IGNORECASE,
)

# A tail starting like this is a plan/negation, not a durable fact
# ("I have to go", "I like nothing").
_REJECT_TAIL_RE = re.compile(r"^(?:to|no|not|never|any|it|that|this)\b", re.IGNORECASE)

_FAMILY = (
    r"(?:sister|brother|mother|father|mom|mum|dad|wife|husband|son|daughter|"
    r"roommate|best friend|girlfriend|boyfriend)"
)

# Verb -> third-person form for "I <verb> X".
_LIKE_VERBS = {"like": "likes", "love": "loves", "enjoy": "enjoys", "prefer": "prefers"}


def _rule(pattern: str, build) -> tuple[re.Pattern[str], object]:
    return re.compile(pattern, re.IGNORECASE), build


# Order matters: the more specific pattern must come first.
_RULES = [
    # Name
    _rule(r"^my name(?:'s| is) (?:called )?(.+)$", lambda m: f"The Friend's name is {m.group(1)}."),
    _rule(r"^(?:i am|i'm) called (.+)$", lambda m: f"The Friend's name is {m.group(1)}."),
    _rule(r"^call me (.+)$", lambda m: f"The Friend's name is {m.group(1)}."),
    # Age
    _rule(r"^(?:i am|i'm) (\d{1,3})(?: years old| yo)?$",
          lambda m: f"The Friend is {m.group(1)} years old."),
    # Studies
    _rule(r"^i study (?:at )?(.+)$", lambda m: f"The Friend studies {m.group(1)}."),
    _rule(r"^(?:i'm|i am) studying (.+)$", lambda m: f"The Friend is studying {m.group(1)}."),
    _rule(r"^i(?:'m| am)? (?:a|an) (?:student|undergrad|undergraduate) at (.+)$",
          lambda m: f"The Friend is a student at {m.group(1)}."),
    # Work
    _rule(r"^i work (?:at|for|in) (.+)$", lambda m: f"The Friend works at {m.group(1)}."),
    _rule(r"^i(?:'m| am) working (?:at|for|on) (.+)$",
          lambda m: f"The Friend is working on {m.group(1)}."),
    # Home / origin
    _rule(r"^i live in (.+)$", lambda m: f"The Friend lives in {m.group(1)}."),
    _rule(r"^(?:i'm|i am) from (.+)$", lambda m: f"The Friend is from {m.group(1)}."),
    # Tastes
    _rule(r"^i (like|love|enjoy|prefer) (.+)$",
          lambda m: f"The Friend {_LIKE_VERBS[m.group(1).lower()]} {m.group(2)}."),
    _rule(r"^my (favou?rite .+?) is (.+)$",
          lambda m: f"The Friend's {m.group(1).lower()} is {m.group(2)}."),
    # Possessions and family
    _rule(rf"^my ({_FAMILY}) is (.+)$",
          lambda m: f"The Friend's {m.group(1).lower()} is {m.group(2)}."),
    _rule(rf"^my ({_FAMILY}) (?:works|worked) (?:at|for|as) (.+)$",
          lambda m: f"The Friend's {m.group(1).lower()} works {m.group(2)}."),
    _rule(r"^i have (.+)$", lambda m: f"The Friend has {m.group(1)}."),
    # Projects and plans
    _rule(r"^(?:i'm|i am) building (.+)$", lambda m: f"The Friend is building {m.group(1)}."),
    _rule(r"^(?:i'm|i am) (?:making|creating) (.+)$", lambda m: f"The Friend is making {m.group(1)}."),
    _rule(r"^i want to (.+)$", lambda m: f"The Friend wants to {m.group(1)}."),
    _rule(r"^(?:i'm|i am) planning to (.+)$", lambda m: f"The Friend is planning to {m.group(1)}."),
    _rule(r"^i need to (.+)$", lambda m: f"The Friend needs to {m.group(1)}."),
]


def _third_person(text: str) -> str:
    """Clean up a rewritten fact: address Tau, tidy spacing, one full stop."""
    # "I'm building you, Tau, for a project" -> drop the vocative, then "you"
    # becomes the companion's name.
    text = re.sub(r",\s*Tau\s*,", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\byour\b", "Tau's", text, flags=re.IGNORECASE)
    text = re.sub(r"\byou\b", "Tau", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTau's (?:Tau's|Tau)\b", "Tau's", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(" .,;:!-")
    return text + "."


def _fact_from_sentence(sentence: str) -> str | None:
    """Return a ``- The Friend ...`` fact for one sentence, or None."""
    text = sentence.strip().strip("\"'").strip()
    text = _LEAD_RE.sub("", text).strip()
    if not text or text.endswith("?"):
        return None
    text = text.rstrip(" .!;,").strip()
    if not text:
        return None

    for pattern, build in _RULES:
        match = pattern.match(text)
        if not match:
            continue
        tail = (match.groups()[-1] or "").strip().rstrip(" .!;,")
        if not tail or len(tail) > 120 or _REJECT_TAIL_RE.match(tail):
            return None
        fact = _third_person(build(match))
        if len(fact) <= len("The Friend. "):
            return None
        return "- " + fact
    return None


def facts_from_turns(turns: list[dict]) -> list[str]:
    """Scan the user's turns for first-person facts and rewrite each one."""
    facts: list[str] = []
    seen: set[str] = set()
    for turn in turns:
        if turn.get("role") != "user":
            continue
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            for clause in _COMPOUND_RE.split(sentence):
                fact = _fact_from_sentence(clause)
                if not fact:
                    continue
                key = _norm(fact)
                if key in seen:
                    continue
                seen.add(key)
                facts.append(fact)
    return facts


async def extract_and_save(turns: list[dict] | None = None, force: bool = False) -> list[str]:
    """Store new durable facts found in *turns* (default: recent history).

    Never raises: a memory failure must not surface to the user. Returns the
    facts that were added. *force* runs even when ``memory_enabled`` is false
    (used by the manual ``POST /memory/extract`` endpoint).

    Async only to match the caller's ``create_task``; the scan itself is
    synchronous and takes microseconds, so it can never compete with a reply.
    """
    if not settings.memory_enabled and not force:
        log.debug("[MEMORY] auto-extraction disabled (MEMORY_ENABLED=false)")
        return []

    if turns is None:
        turns = load_recent(settings.memory_turns)
    turns = [t for t in turns if (t.get("text") or "").strip()]
    if not any(t.get("role") == "user" for t in turns):
        return []

    try:
        candidates = facts_from_turns(turns)
        if not candidates:
            log.info("[MEMORY] no first-person facts in the last %d turns", len(turns))
            return []
        log.info("[MEMORY] %d candidate fact(s) from %d turns", len(candidates), len(turns))
        return save_facts(candidates)
    except Exception as exc:  # noqa: BLE001 - memory must never break a turn
        log.error("[MEMORY] extraction failed: %s", exc, exc_info=True)
        return []
