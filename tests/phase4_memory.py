"""Phase 4 memory check — runs inside the bridge container, no microphone.

    docker cp tests/phase4_memory.py tau-bridge-1:/tmp/p4.py
    docker exec tau-bridge-1 python /tmp/p4.py

Drives ``memory.extract_and_save()`` with a synthetic conversation, then checks
that facts are written, deduplicated on a second pass, and capped at
``memory_max_lines``. The real ``data/facts.md`` is backed up and restored, so
running this never destroys facts learned in a real conversation.

The same evidence appears in ``logs/realtime-api.log`` as ``[MEMORY]`` lines.
"""

import asyncio
import sys

from realtime_api import memory
from realtime_api.config import settings
from realtime_api.history_store import FACTS_FILE, load_facts

TURNS = [
    {"role": "user", "text": "I study computer science at Karunya University."},
    {"role": "assistant", "text": "That's wonderful! *nods* What are you working on there?"},
    {"role": "user", "text": "I'm building you, Tau, for a college project."},
    {"role": "assistant", "text": "I'm flattered! *happy wiggle*"},
    {"role": "user", "text": "Also, my favourite food is dosa."},
    {"role": "assistant", "text": "Dosa is delicious! *smiles*"},
]

SMALL_TALK = [
    {"role": "user", "text": "Hey Tau, how are you today?"},
    {"role": "assistant", "text": "I'm doing well, thanks! *smiles* How about you?"},
    {"role": "user", "text": "I'm pretty good, just tired."},
    {"role": "assistant", "text": "Get some rest soon!"},
]

# Loosely matched: the 1B model rewords things, so we check meaning not strings.
KEYWORDS = {
    "computer science": "studies computer science",
    "dosa": "favourite food",
    "project": "college project",
}

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    ok = ok and condition


def read_lines() -> list[str]:
    text = load_facts()
    return [line for line in text.splitlines() if line.strip()]


async def run() -> None:
    print(f"[test] facts file  : {FACTS_FILE}")
    print(f"[test] max lines   : {settings.memory_max_lines}")
    print("[test] extractor   : rule-based (no model call)")
    print("[test] --- extraction pass 1 (empty memory) ---")

    # force=True: this test drives the extractor directly, and auto-extraction
    # is off by default while the extractor's precision is being established.
    added = await memory.extract_and_save(TURNS, force=True)
    lines = read_lines()
    print("[test] facts now:\n" + ("\n".join(lines) if lines else "  (none)"))

    check("extraction wrote at least one fact", bool(added), f"{len(added)} added")
    check(
        "every stored line is a bullet",
        bool(lines) and all(line.startswith("- ") for line in lines),
        f"{len(lines)} lines",
    )

    hits = [label for needle, label in KEYWORDS.items() if needle in load_facts().lower()]
    check("remembered the user's facts", len(hits) >= 2, "matched: " + (", ".join(hits) or "none"))

    print("[test] --- extraction pass 2 (same conversation again) ---")
    before = len(read_lines())
    added_again = await memory.extract_and_save(TURNS, force=True)
    after = len(read_lines())
    check("second pass adds no duplicate", not added_again and after == before,
          f"{len(added_again)} added, {before} -> {after} lines")

    print("[test] --- small talk must not be remembered ---")
    before = len(read_lines())
    added_small = await memory.extract_and_save(SMALL_TALK, force=True)
    check("small talk stores nothing", not added_small and len(read_lines()) == before,
          f"{len(added_small)} added, {before} -> {len(read_lines())} lines")

    print("[test] --- cap check (51 synthetic facts) ---")
    memory.save_facts([f"- Synthetic fact number {i} used only for the cap test." for i in range(51)])
    capped = read_lines()
    check("facts file respects the cap", len(capped) <= settings.memory_max_lines,
          f"{len(capped)} <= {settings.memory_max_lines}")
    check("newest facts survive the cap", "Synthetic fact number 50" in " ".join(capped))


def main() -> int:
    backup = FACTS_FILE.read_bytes() if FACTS_FILE.exists() else None
    try:
        memory.clear_facts()
        asyncio.run(run())
    finally:
        if backup is None:
            FACTS_FILE.unlink(missing_ok=True)
        else:
            FACTS_FILE.write_bytes(backup)
        print("[test] restored original facts file"
              + (" (absent before the run)" if backup is None else ""))

    print("\n[test] RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
