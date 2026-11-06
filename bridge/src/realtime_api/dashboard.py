"""Read-only data for the Leo dashboard.

Everything here is best-effort: a missing CSV, an unreadable ``/proc`` file or
an offline model service must never break the page.  Values that cannot be
measured are ``None``; the UI renders them as "n/a" instead of inventing a
number.

Endpoints (wired in ``main.py``):

* ``/api/stats``    — RAM, swap, CPU, GPU, temperature, power, nvpmodel
* ``/api/health``   — up/down + response time for the three model services
* ``/api/models``   — what each service is actually running
* ``/api/latency``  — parsed rows + medians from ``logs/latency.csv``
* ``/api/history``  — recent turns from ``data/history.jsonl``
* ``/api/status``   — one aggregate snapshot (kept for the old /dashboard page)
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import settings
from .history_store import FACTS_FILE, HISTORY_FILE, load_facts, load_recent

log = logging.getLogger(__name__)

# The dashboard polls three local health endpoints. httpx logs each request at
# INFO, which would flood the bridge log; keep warnings and errors only.
logging.getLogger("httpx").setLevel(logging.WARNING)

STARTED_AT = time.time()

# (key, CSV column, human label) — used for the medians shown on the page.
STAGES = [
    ("stt", "speech_end_to_transcript_ms", "Speech end → transcript"),
    ("llm", "transcript_to_first_token_ms", "Transcript → first token"),
    ("tts", "first_token_to_first_audio_ms", "First token → first audio"),
    ("total", "total_ms", "Speech end → last audio"),
]

# Health/models change slowly and are relatively expensive to probe. The 1 s
# stats endpoint must stay cheap, so cache these for a few seconds.
_CACHE_TTL = 4.0
_health_cache: dict = {"t": 0.0, "value": None}
_models_cache: dict = {"t": 0.0, "value": None}


# ---------------------------------------------------------------------------
# Host telemetry
# ---------------------------------------------------------------------------
def _read_meminfo() -> dict[str, int]:
    mem: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                mem[key] = int(parts[0])
    except (OSError, ValueError):
        pass
    return mem


def _cpu_times() -> tuple[int, int] | None:
    """Return (idle, total) jiffies from the aggregate line of /proc/stat."""
    try:
        first = Path("/proc/stat").read_text().splitlines()[0]
        fields = [int(x) for x in first.split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    if len(fields) < 4:
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return idle, sum(fields)


_cpu_pct: float | None = None


def _cpu_sampler() -> None:
    """Sample CPU usage every second in the background (no request blocking)."""
    global _cpu_pct
    prev = _cpu_times()
    while True:
        time.sleep(1.0)
        cur = _cpu_times()
        if cur and prev:
            d_idle = cur[0] - prev[0]
            d_total = cur[1] - prev[1]
            if d_total > 0:
                _cpu_pct = round(100.0 * (1.0 - d_idle / d_total), 1)
        prev = cur


threading.Thread(target=_cpu_sampler, name="leo-cpu", daemon=True).start()


def _gpu_pct() -> float | None:
    """Jetson GPU load is reported in tenths of a percent."""
    for path in (
        "/sys/devices/platform/gpu.0/load",
        "/sys/devices/gpu.0/load",
    ):
        try:
            return round(int(Path(path).read_text().strip()) / 10.0, 1)
        except (OSError, ValueError, TypeError):
            # Some sysfs files are empty (0 bytes) and pathlib raises
            # TypeError when decoding them, not ValueError.
            continue
    return None


def _temperatures() -> dict[str, float]:
    temps: dict[str, float] = {}
    try:
        zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*"))
    except OSError:
        return temps
    for zone in zones:
        try:
            name = (zone / "type").read_text().strip()
            raw = int((zone / "temp").read_text().strip())
        except (OSError, ValueError, TypeError):
            # cv0/cv1/cv2 are empty on this board; skip them silently.
            continue
        if name:
            temps[name] = round(raw / 1000.0, 1)
    return temps


def _power_w() -> float | None:
    """VDD_IN from the on-board INA3221 (mV × mA → W)."""
    try:
        hwmons = list(Path("/sys/bus/i2c/drivers/ina3221/1-0040/hwmon").glob("hwmon*"))
    except OSError:
        return None
    for hwmon in hwmons:
        try:
            millivolts = int((hwmon / "in1_input").read_text().strip())
            milliamps = int((hwmon / "curr1_input").read_text().strip())
        except (OSError, ValueError, TypeError):
            continue
        if millivolts > 0:
            return round(millivolts * milliamps / 1_000_000.0, 2)
    return None


# Mode names for this board (Orin Nano 8GB). The raw number is always shown;
# the name is only a convenience label.
_NVPMODEL_NAMES = {0: "15W", 1: "25W", 2: "MAXN_SUPER"}


def _nvpmodel() -> dict:
    """Read the current nvpmodel mode from the host bind mount, if present."""
    for path in (
        Path("/host/nvpmodel-status"),
        Path("/var/lib/nvpmodel/status"),
    ):
        try:
            text = path.read_text().strip()
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            continue
        match = re.search(r"pmode:\s*(\d+)", text)
        if match:
            mode = int(match.group(1))
            return {"mode": mode, "name": _NVPMODEL_NAMES.get(mode), "raw": text[:80]}
    return {"mode": None, "name": None, "raw": None}


def _system() -> dict:
    mem = _read_meminfo()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    used = max(total - available, 0)

    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(swap_total - swap_free, 0)

    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0

    try:
        host_uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        host_uptime = 0.0

    temps = _temperatures()
    return {
        "hostname": os.uname().nodename,
        "host_uptime_s": round(host_uptime),
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "mem_total_mb": round(total / 1024),
        "mem_available_mb": round(available / 1024),
        "mem_used_mb": round(used / 1024),
        "mem_used_pct": round(100.0 * used / total, 1) if total else None,
        "swap_total_mb": round(swap_total / 1024),
        "swap_used_mb": round(swap_used / 1024),
        "swap_used_pct": round(100.0 * swap_used / swap_total, 1) if swap_total else None,
        "cpu_pct": _cpu_pct,
        "gpu_pct": _gpu_pct(),
        "temp_c": max(temps.values()) if temps else None,
        "temps": temps,
        "power_w": _power_w(),
        "nvpmodel": _nvpmodel(),
    }


def stats() -> dict:
    """Cheap snapshot used by the 1 s poller."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bridge": {
            "uptime_s": round(time.time() - STARTED_AT),
            "model": settings.openai_model,
            "stt_model": settings.stt_model,
            "voice": settings.default_voice,
            "port": settings.port,
        },
        "system": _system(),
    }


# ---------------------------------------------------------------------------
# Service health / models
# ---------------------------------------------------------------------------
async def _probe(client: httpx.AsyncClient, name: str, url: str, detail: str) -> dict:
    started = time.perf_counter()
    ok = False
    error = None
    try:
        response = await client.get(url)
        ok = response.status_code < 400
        if not ok:
            error = f"HTTP {response.status_code}"
    except httpx.TimeoutException:
        error = "timeout"
    except Exception as exc:  # noqa: BLE001 - any failure means "down"
        error = str(exc) or exc.__class__.__name__
    return {
        "name": name,
        "ok": ok,
        "state": "ok" if ok else ("down" if error != "timeout" else "warming"),
        "detail": detail,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "error": (error or "")[:120] or None,
    }


async def _probe_health() -> dict:
    base_llm = settings.openai_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=2.0) as client:
        ollama, speaches, kokoro = await asyncio.gather(
            _probe(client, "Ollama", f"{base_llm}/api/tags",
                   f"{settings.openai_model} · :8000"),
            _probe(client, "STT", f"{settings.stt_url.rstrip('/')}/health",
                   f"{settings.stt_model} · :9002"),
            _probe(client, "Kokoro", f"{settings.tts_url.rstrip('/')}/health",
                   f"voice {settings.default_voice} · :8880"),
        )

    bridge = {
        "name": "Bridge",
        "ok": True,
        "state": "ok",
        "detail": f"HTTPS :{settings.port}",
        "latency_ms": 0,
        "error": None,
    }
    services = [bridge, ollama, speaches, kokoro]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "services": services,
        "ok": all(s["ok"] for s in services),
    }


async def health() -> dict:
    now = time.monotonic()
    cached = _health_cache.get("value")
    if cached is not None and now - float(_health_cache.get("t") or 0.0) < _CACHE_TTL:
        return cached
    value = await _probe_health()
    _health_cache.update(t=now, value=value)
    return value


async def _probe_models() -> dict:
    base_llm = settings.openai_base_url.rstrip("/")
    ps: dict = {"models": []}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{base_llm}/api/ps")
            if response.status_code < 400:
                ps = response.json()
    except Exception:  # noqa: BLE001 - offline is a valid state
        ps = {"models": []}

    loaded = ps.get("models") or []
    loaded_names = {str(m.get("name") or m.get("model") or "") for m in loaded}
    configured = settings.openai_model
    is_loaded = any(
        name == configured or name.split(":")[0] == configured.split(":")[0]
        for name in loaded_names
    )
    size_bytes = 0
    for m in loaded:
        name = str(m.get("name") or m.get("model") or "")
        if name == configured or name.split(":")[0] == configured.split(":")[0]:
            size_bytes = int(m.get("size") or 0)
            is_loaded = True
            break

    health_data = await health()
    by_name = {s["name"]: s for s in health_data.get("services", [])}
    ollama_ok = by_name.get("Ollama", {}).get("ok", False)
    stt_ok = by_name.get("STT", {}).get("ok", False)
    tts_ok = by_name.get("Kokoro", {}).get("ok", False)

    def llm_detail() -> str:
        if not ollama_ok:
            return "offline"
        if is_loaded:
            return f"loaded · {round(size_bytes / 1024 / 1024)} MB in memory" if size_bytes else "loaded"
        return "asleep — loads on first turn"

    models = [
        {
            "id": "llm",
            "role": "Mind",
            "name": configured,
            "detail": llm_detail(),
            "loaded": bool(is_loaded and ollama_ok),
            "ok": ollama_ok,
            "memory_mb": round(size_bytes / 1024 / 1024) if size_bytes else None,
        },
        {
            "id": "stt",
            "role": "Ears",
            "name": settings.stt_model.split("/")[-1],
            "detail": "Parakeet TDT · int8 · CPU" if stt_ok else "offline",
            "loaded": stt_ok,
            "ok": stt_ok,
            "memory_mb": None,
        },
        {
            "id": "tts",
            "role": "Voice",
            "name": f"Kokoro {settings.default_voice}",
            "detail": "24 kHz PCM · :8880" if tts_ok else "offline",
            "loaded": tts_ok,
            "ok": tts_ok,
            "memory_mb": None,
        },
        {
            "id": "vad",
            "role": "Reflexes",
            "name": "Silero VAD",
            "detail": "server VAD · CPU",
            "loaded": True,
            "ok": True,
            "memory_mb": None,
        },
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": models,
    }


async def models() -> dict:
    now = time.monotonic()
    cached = _models_cache.get("value")
    if cached is not None and now - float(_models_cache.get("t") or 0.0) < _CACHE_TTL:
        return cached
    value = await _probe_models()
    _models_cache.update(t=now, value=value)
    return value


# ---------------------------------------------------------------------------
# Latency CSV
# ---------------------------------------------------------------------------
def _as_int(value) -> int | None:
    value = (value or "").strip() if isinstance(value, str) else value
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    value = (value or "").strip() if isinstance(value, str) else value
    if value is None or value == "":
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _read_latency_rows() -> list[dict]:
    path = Path(settings.latency_csv)
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return []
    # Drop rows written before the header existed / malformed tails.
    return [r for r in rows if r.get("ts")]


def _latency_row(row: dict) -> dict:
    return {
        "ts": row.get("ts", ""),
        "session": row.get("session", ""),
        "stt_ms": _as_int(row.get("speech_end_to_transcript_ms")),
        "llm_first_token_ms": _as_int(row.get("transcript_to_first_token_ms")),
        "llm_total_ms": _as_int(row.get("llm_total_ms")),
        "tts_ms": _as_int(row.get("first_token_to_first_audio_ms")),
        "total_ms": _as_int(row.get("total_ms")),
        "llm_tokens": _as_int(row.get("llm_tokens")),
        "tokens_per_s": _as_float(row.get("tokens_per_s")),
        "sentences": _as_int(row.get("sentences")),
        "status": row.get("status", ""),
    }


def _median(values: list) -> float | None:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 1) if clean else None


def latency(n: int = 20) -> dict:
    path = Path(settings.latency_csv)
    rows = [_latency_row(r) for r in _read_latency_rows()]
    rows = [r for r in rows if r["total_ms"] is not None or r["stt_ms"] is not None]

    stages = {}
    for key, column, label in STAGES:
        if key == "stt":
            values = [r["stt_ms"] for r in rows if r["stt_ms"] is not None]
        elif key == "llm":
            values = [r["llm_first_token_ms"] for r in rows if r["llm_first_token_ms"] is not None]
        elif key == "tts":
            values = [r["tts_ms"] for r in rows if r["tts_ms"] is not None]
        else:
            values = [r["total_ms"] for r in rows if r["total_ms"] is not None]
        stages[key] = {
            "label": label,
            "median_ms": round(statistics.median(values)) if values else None,
            "max_ms": max(values) if values else None,
            "samples": len(values),
        }

    recent = rows[-max(1, min(n, 200)):]
    return {
        "file": str(path),
        "turns": len(rows),
        "completed": sum(1 for r in rows if r["status"] == "completed"),
        "cancelled": sum(1 for r in rows if r["status"] == "cancelled"),
        "stages": stages,
        "medians": {
            "total_ms": _median([r["total_ms"] for r in rows]),
            "stt_ms": _median([r["stt_ms"] for r in rows]),
            "llm_total_ms": _median([r["llm_total_ms"] for r in rows]),
            "tokens_per_s": _median([r["tokens_per_s"] for r in rows]),
        },
        "recent": recent,
    }


def history(n: int = 50) -> dict:
    turns = load_recent(max(1, min(n, 500)))
    return {
        "turns": [
            {
                "ts": t.get("ts", ""),
                "role": t.get("role", ""),
                "text": t.get("text", ""),
            }
            for t in turns
        ]
    }


def presence() -> dict:
    """Return time, real conversation gap/day count, and a known user name."""
    now = datetime.now().astimezone()
    hour = now.hour
    time_of_day = (
        "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 17
        else "evening" if 17 <= hour < 21 else "night"
    )
    result: dict = {"time_of_day": time_of_day, "local_time": now.isoformat(timespec="seconds")}
    first_turn = last_turn = None
    try:
        with HISTORY_FILE.open(encoding="utf-8") as history_file:
            for line in history_file:
                try:
                    turn = json.loads(line)
                    if turn.get("role") not in ("user", "assistant"):
                        continue
                    timestamp = datetime.fromisoformat(turn["ts"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                first_turn = first_turn or timestamp
                last_turn = timestamp
    except OSError:
        pass
    if first_turn is not None and last_turn is not None:
        result["day_count"] = max(1, (now.date() - first_turn.astimezone().date()).days + 1)
        result["last_turn_at"] = last_turn.isoformat(timespec="seconds")
        result["gap_seconds"] = max(0, int((now - last_turn.astimezone()).total_seconds()))

    from . import memory as memory_module

    for fact in memory_module.list_facts():
        match = re.match(r"^The Friend['’]s name is (.+?)[.!]?$", fact["text"], re.IGNORECASE)
        if match:
            result["user_name"] = match.group(1).strip()
            break
    return result


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
def memory_snapshot() -> dict:
    from . import memory as memory_module

    items = memory_module.list_facts()
    return {
        "count": len(items),
        "items": items,
        "facts": load_facts(),
        "file": str(FACTS_FILE),
    }


# ---------------------------------------------------------------------------
# Aggregate snapshot (kept for /api/status and the old standalone page)
# ---------------------------------------------------------------------------
async def snapshot() -> dict:
    health_data, models_data = await asyncio.gather(health(), models())
    lat = latency(20)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bridge": stats()["bridge"],
        "system": _system(),
        "services": health_data["services"],
        "models": models_data["models"],
        "memory": memory_snapshot(),
        "latency": lat,
        "history": history(8)["turns"],
    }
