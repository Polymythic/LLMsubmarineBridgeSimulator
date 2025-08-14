from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import CONFIG
from .bus import BUS
from .sim.loop import Simulation


app = FastAPI(title="Submarine Bridge Simulator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


sim = Simulation()
_sim_task = None  # type: ignore[assignment]


@app.on_event("startup")
async def _startup() -> None:
    global _sim_task
    _sim_task = asyncio.create_task(sim.run())


@app.on_event("shutdown")
async def _shutdown() -> None:
    sim.stop()
    if _sim_task is not None:
        await _sim_task


STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


def station_file(station: str) -> Path:
    name = f"{station}.html" if station != "home" else "index.html"
    return STATIC_DIR / name


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(station_file("home"))


@app.get("/captain")
async def captain() -> FileResponse:
    return FileResponse(station_file("captain"))


@app.get("/helm")
async def helm() -> FileResponse:
    return FileResponse(station_file("helm"))


@app.get("/sonar")
async def sonar() -> FileResponse:
    return FileResponse(station_file("sonar"))


@app.get("/weapons")
async def weapons() -> FileResponse:
    return FileResponse(station_file("weapons"))


@app.get("/engineering")
async def engineering() -> FileResponse:
    return FileResponse(station_file("engineering"))


@app.get("/debug")
async def debug() -> FileResponse:
    return FileResponse(station_file("debug"))


@app.get("/fleet")
async def fleet() -> FileResponse:
    return FileResponse(station_file("fleet"))


_station_clients: Dict[str, Set[WebSocket]] = {s: set() for s in ["captain", "helm", "sonar", "weapons", "engineering", "debug", "fleet"]}


@app.websocket("/ws/{station}")
async def ws_station(ws: WebSocket, station: str) -> None:
    await ws.accept()
    if station not in _station_clients:
        await ws.close()
        return
    _station_clients[station].add(ws)

    topic_map = {
        "captain": "tick:captain",
        "helm": "tick:helm",
        "sonar": "tick:sonar",
        "weapons": "tick:weapons",
        "engineering": "tick:engineering",
        "debug": "tick:debug",
        "fleet": "tick:fleet",
    }
    forward_topic = topic_map.get(station, "tick:all")

    async def forward_task():
        async for msg in BUS.subscribe(forward_topic):
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                break

    fwd = asyncio.create_task(forward_task())

    try:
        while True:
            raw = await ws.receive_text()
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            topic = parsed.get("topic")
            data = parsed.get("data", {})
            err = await sim.handle_command(topic, data)
            if err:
                await ws.send_text(json.dumps({"topic": "error", "error": err}))
    except WebSocketDisconnect:
        pass
    finally:
        fwd.cancel()
        _station_clients[station].discard(ws)


@app.get("/api/ai/health")
async def api_ai_health() -> JSONResponse:
    if not getattr(CONFIG, "use_ai_orchestrator", False):
        return JSONResponse({"ok": False, "detail": "orchestrator disabled"})
    orch = getattr(sim, "_ai_orch", None)
    if orch is None:
        return JSONResponse({"ok": False, "detail": "orchestrator not initialized"})
    try:
        res = await orch.health_check()
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)})
