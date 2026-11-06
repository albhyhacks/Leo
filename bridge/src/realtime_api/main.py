"""FastAPI app + uvicorn entrypoint."""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import dashboard as dashboard_data
from . import memory
from .config import settings
from .ws_handler import handle_realtime_ws

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
# In Docker the package is installed, so fall back to /app/static
if not STATIC_DIR.exists():
    STATIC_DIR = Path("/app/static")

LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s  %(message)s"
LOG_DATEFMT = "%H:%M:%S"

logging.basicConfig(level=logging.INFO, format=LOG_FMT, datefmt=LOG_DATEFMT)

# File handler — persisted to bind-mounted /logs for host access
_log_dir = Path("/logs")
if _log_dir.exists():
    _fh = logging.FileHandler(_log_dir / "realtime-api.log")
    _fh.setFormatter(logging.Formatter(LOG_FMT, datefmt=LOG_DATEFMT))
    logging.getLogger().addHandler(_fh)

log = logging.getLogger(__name__)

app = FastAPI(title="OpenAI Realtime API Bridge", version="0.1.0")


@app.get("/")
async def index():
    # no-store while the Leo UI is being iterated on; the page is small anyway
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/classic")
async def classic_index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/stage")
async def stage_index():
    return FileResponse(STATIC_DIR / "stage.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/dashboard")
async def dashboard_page():
    """Compatibility: the old standalone page now lives inside the Leo tab."""
    return RedirectResponse(url="/#leo", status_code=307)


@app.get("/api/status")
async def dashboard_status():
    return await dashboard_data.snapshot()


@app.get("/api/stats")
async def dashboard_stats():
    """Cheap 1 s poll: RAM, swap, CPU, GPU, temperature, power, nvpmodel."""
    return dashboard_data.stats()


@app.get("/api/health")
async def dashboard_health():
    return await dashboard_data.health()


@app.get("/api/models")
async def dashboard_models():
    return await dashboard_data.models()


@app.get("/api/latency")
async def dashboard_latency(n: int = Query(default=20, ge=1, le=200)):
    return dashboard_data.latency(n)


@app.get("/api/history")
async def dashboard_history(n: int = Query(default=50, ge=1, le=500)):
    return dashboard_data.history(n)


@app.get("/api/presence")
async def dashboard_presence():
    return dashboard_data.presence()


@app.get("/memory")
async def get_memory():
    """What Leo remembers about you (Phase 4), with stable ids for deletion."""
    return dashboard_data.memory_snapshot()


@app.delete("/memory/{fact_id}")
async def delete_single_fact(fact_id: str):
    """Forget one fact by its stable text-derived id."""
    if not memory.delete_fact(fact_id):
        raise HTTPException(status_code=404, detail="fact not found")
    return {"status": "deleted", "id": fact_id, "count": len(memory.list_facts())}


@app.delete("/memory")
async def delete_memory():
    """Forget everything — the reset half of the privacy story."""
    return {"status": "cleared", "removed": memory.clear_facts()}


@app.post("/memory/extract")
async def extract_memory():
    """Force an extraction from recent history (useful for demos and tests)."""
    added = await memory.extract_and_save(force=True)
    return {"added": added, "count": len(added)}


@app.websocket("/v1/realtime")
async def realtime_ws(
    ws: WebSocket,
    model: str = Query(default=None),
    presence: bool = Query(default=False),
):
    await handle_realtime_ws(ws, model=model, presence=presence)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    log.info("Starting Realtime API on %s:%d", settings.host, settings.port)
    ssl_kwargs = {}
    if settings.ssl_certfile and settings.ssl_keyfile:
        ssl_kwargs["ssl_certfile"] = settings.ssl_certfile
        ssl_kwargs["ssl_keyfile"] = settings.ssl_keyfile
        log.info("HTTPS enabled (cert=%s)", settings.ssl_certfile)
    uvicorn.run(
        "realtime_api.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
