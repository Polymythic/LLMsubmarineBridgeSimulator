from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from .config import CONFIG
from .bus import BUS
from .sim.loop import Simulation
from .assets import MISSIONS_DIR, load_mission_by_id, get_all_mission_summaries


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
ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"

# Mount static files for JS, CSS, sounds, etc.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


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


@app.get("/plot")
async def plot() -> FileResponse:
    return FileResponse(station_file("plot"))


@app.get("/fleet")
async def fleet() -> FileResponse:
    return FileResponse(station_file("fleet"))


@app.get("/missions")
async def missions_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "missions.html")


@app.get("/logs")
async def logs() -> FileResponse:
    return FileResponse(station_file("logs"))


_station_clients: Dict[str, Set[WebSocket]] = {s: set() for s in ["captain", "helm", "sonar", "weapons", "engineering", "debug", "plot", "fleet", "logs"]}


@app.websocket("/ws/{station}")
async def ws_station(ws: WebSocket, station: str) -> None:
    await ws.accept()
    if station not in _station_clients:
        await ws.close()
        return
    _station_clients[station].add(ws)

    # Send initial status so client knows current state immediately
    try:
        initial_status = sim.get_current_status()
        await ws.send_text(json.dumps(initial_status))
    except Exception:
        pass

    topic_map = {
        "captain": "tick:captain",
        "helm": "tick:helm",
        "sonar": "tick:sonar",
        "weapons": "tick:weapons",
        "engineering": "tick:engineering",
        "debug": "tick:debug",
        "plot": "tick:plot",
        "fleet": "tick:fleet",
        "logs": "tick:logs",
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
            print(f"DEBUG WS RECV [{station}]: {raw[:200]}")  # Debug: log all incoming commands
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            topic = parsed.get("topic")
            data = parsed.get("data", {})
            print(f"DEBUG CMD: topic={topic}, data={data}")  # Debug: log parsed command
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


@app.get("/api/missions")
async def api_missions() -> JSONResponse:
    try:
        ids = [p.stem for p in MISSIONS_DIR.glob("*.json")]
        return JSONResponse({"missions": sorted(ids)})
    except Exception as e:
        return JSONResponse({"missions": [], "error": str(e)})


@app.get("/api/missions/all")
async def api_missions_all() -> JSONResponse:
    """Return all mission summaries for the selector UI."""
    try:
        summaries = get_all_mission_summaries()
        return JSONResponse(summaries)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/missions/{mission_id}")
async def api_mission_details(mission_id: str) -> JSONResponse:
    """Return full mission details."""
    try:
        mission = load_mission_by_id(mission_id)
        if not mission:
            return JSONResponse({"error": "Mission not found"}, status_code=404)
        return JSONResponse(mission.dict())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/missions/{mission_id}/start")
async def api_start_mission(mission_id: str) -> JSONResponse:
    """Load a mission and restart the simulation."""
    try:
        success = await sim.load_mission(mission_id)
        if success:
            return JSONResponse({"ok": True, "mission_id": mission_id})
        else:
            return JSONResponse({"ok": False, "error": "Mission not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
