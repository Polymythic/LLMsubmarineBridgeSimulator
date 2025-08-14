from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Literal, Optional, TypedDict
import httpx

from ..config import CONFIG
import json
from .ai_engines import BaseEngine, StubEngine, OllamaAgentsEngine, OpenAIAgentsEngine
from ..models import Ship
from ..storage import insert_event
from .ai_tools import LocalAIStub


class RunResult(TypedDict, total=False):
    run_id: str
    parent_run_id: Optional[str]
    agent_type: Literal["fleet", "ship"]
    engine: Literal["stub", "ollama", "openai"]
    model: str
    duration_ms: int
    error: Optional[str]
    tool_calls: List[Dict[str, Any]]
    tool_calls_validated: List[Dict[str, Any]]
    applied: bool


class AgentsOrchestrator:
    """Minimal agent orchestrator interface.

    - Keeps engines pluggable (stub/ollama/openai)
    - Builds summaries within information boundaries
    - Returns RunResult structures for tracing (storage integration optional)
    - Does NOT mutate sim state directly; caller chooses when/how to apply
    """

    def __init__(self, world_getter, storage_engine, run_id: str) -> None:
        # world_getter: callable returning the authoritative world object with ships
        self._world_getter = world_getter
        self._storage_engine = storage_engine
        self._run_id = run_id
        # Default engines
        self._fleet_engine_kind: Literal["stub", "ollama", "openai"] = "stub"
        self._ship_engine_kind: Literal["stub", "ollama", "openai"] = "stub"
        self._fleet_model = "stub"
        self._ship_model = "stub"
        self._stub = LocalAIStub()
        self._fleet_engine: BaseEngine = StubEngine()
        self._ship_engine: BaseEngine = StubEngine()

    # ---------- Public configuration ----------
    def set_fleet_engine(self, kind: Literal["stub", "ollama", "openai"], model: str) -> None:
        self._fleet_engine_kind = kind
        self._fleet_model = model
        if kind == "stub":
            self._fleet_engine = StubEngine()
        elif kind == "ollama":
            self._fleet_engine = OllamaAgentsEngine(model=model, host=CONFIG.ollama_host)
        elif kind == "openai":
            self._fleet_engine = OpenAIAgentsEngine(model=model)
        else:
            self._fleet_engine = StubEngine()

    def set_ship_engine(self, kind: Literal["stub", "ollama", "openai"], model: str) -> None:
        self._ship_engine_kind = kind
        self._ship_model = model
        if kind == "stub":
            self._ship_engine = StubEngine()
        elif kind == "ollama":
            self._ship_engine = OllamaAgentsEngine(model=model, host=CONFIG.ollama_host)
        elif kind == "openai":
            self._ship_engine = OpenAIAgentsEngine(model=model)
        else:
            self._ship_engine = StubEngine()

    # ---------- Summaries (information boundaries) ----------
    def _build_fleet_summary(self) -> Dict[str, Any]:
        world = self._world_getter()
        own_fleet = []
        for ship in world.all_ships():
            if ship.side != "RED":
                continue
            own_fleet.append({
                "id": ship.id,
                "class": getattr(ship, "ship_class", None),
                "pos": [ship.kin.x, ship.kin.y],
                "depth": ship.kin.depth,
                "heading": ship.kin.heading,
                "speed": ship.kin.speed,
                # Minimal placeholders; extend as models carry more
                "health": {"hull": ship.damage.hull},
                "weapons": {"tubes_ready": sum(1 for t in ship.weapons.tubes if t.state == "DoorsOpen"), "ammo": {"torpedo": ship.weapons.torpedoes_stored}},
                "detectability": {"noise": getattr(ship.acoustics, "last_detectability", 0.0)},
            })
        # Aggregated enemy belief: TODO hook from sonar; placeholder empty
        enemy_belief: List[Dict[str, Any]] = []
        # Mission objective provided by Simulation (if attached by creator)
        mission_brief = getattr(self, "_mission_brief", None)
        if isinstance(mission_brief, dict):
            mission_roe = {"weapons_free": any(
                isinstance(r, str) and ("Weapons release authorized" in r)
                for r in mission_brief.get("roe", [])
            )}
            # Include a simple convoy list and an optional target waypoint for training missions
            convoy = [
                {"id": s.id, "class": getattr(s, "ship_class", None)}
                for s in world.all_ships() if s.side == "RED"
            ]
            target_wp = mission_brief.get("target_wp") if isinstance(mission_brief.get("target_wp", None), (list, tuple)) else None
            mission = {
                "objective": mission_brief.get("objective"),
                "roe": mission_roe,
                "convoy": convoy,
                "target_wp": target_wp,
                # Optional prompts for AI engines to use when constructing system messages
                "ai_fleet_prompt": mission_brief.get("ai_fleet_prompt"),
            }
        else:
            mission = {"roe": {"weapons_free": False}}
        return {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "own_fleet": own_fleet,
            "enemy_belief": enemy_belief,
            "mission": mission,
        }

    def _build_ship_summary(self, ship: Ship) -> Dict[str, Any]:
        # Provide a narrow slice of fleet intent if available, e.g., guidance for this ship
        fleet_intent = {}
        try:
            # world-level fleet intent is maintained by sim loop into orchestrator recent runs mirror
            # We do not require this to exist; default to empty
            fleet_intent = getattr(self, "_last_fleet_intent", {})
        except Exception:
            fleet_intent = {}
        return {
            "self": {
                "id": ship.id,
                "class": getattr(ship, "ship_class", None),
                "pos": [ship.kin.x, ship.kin.y],
                "depth": ship.kin.depth,
                "heading": ship.kin.heading,
                "speed": ship.kin.speed,
            },
            "constraints": {
                "maxSpeed": ship.hull.max_speed,
                "maxDepth": ship.hull.max_depth,
                "turnRate": ship.hull.turn_rate_max,
            },
            "weapons": {
                "tubes": [{"idx": t.idx, "state": t.state} for t in ship.weapons.tubes],
                "has_countermeasures": bool(getattr(ship.capabilities, "countermeasures", [])),
            },
            # Local contacts should come from sonar; orchestrator does not have ground-truth enemy positions
            "contacts": [],
            "orders_last": {},
            "fleet_intent": fleet_intent,
            "detected_state": {"alert": False},
        }

    # ---------- Engines (stub only for now) ----------
    async def _fleet_decide(self, fleet_summary: Dict[str, Any]) -> Dict[str, Any]:
        # Allow engines to incorporate a mission-specific prompt if present
        prompt_hint = None
        try:
            prompt_hint = (fleet_summary.get("mission", {}) or {}).get("ai_fleet_prompt")
        except Exception:
            prompt_hint = None
        return await self._fleet_engine.propose_fleet_intent(fleet_summary if prompt_hint is None else {**fleet_summary, "_prompt_hint": prompt_hint})

    async def _ship_decide(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        # If mission provided a per-ship prompt, include it as a hint
        hint = None
        try:
            mission_brief = getattr(self, "_mission_brief", {}) or {}
            ai_prompts = mission_brief.get("ai_ship_prompts", {}) or {}
            hint = ai_prompts.get(ship.id)
        except Exception:
            hint = None
        enriched = dict(ship_summary)
        if hint:
            enriched["_prompt_hint"] = hint
        return await self._ship_engine.propose_ship_tool(ship, enriched)

    # ---------- Public runs ----------
    async def run_fleet(self, parent_run_id: Optional[str] = None) -> RunResult:
        started = time.perf_counter()
        result: RunResult = {
            "run_id": f"fleet_{int(started*1000)}",
            "parent_run_id": parent_run_id,
            "agent_type": "fleet",
            "engine": self._fleet_engine_kind,
            "model": self._fleet_model,
            "tool_calls": [],
            "tool_calls_validated": [],
            "applied": False,
        }
        try:
            summary = self._build_fleet_summary()
            intent = await asyncio.wait_for(self._fleet_decide(summary), timeout=max(1.0, CONFIG.ai_poll_s))
            result["tool_calls"] = [{"tool": "set_fleet_intent", "arguments": intent}]
            # Validation/clamping would occur here (placeholder: accept as-is)
            result["tool_calls_validated"] = result["tool_calls"]
            # Emit trace event
            insert_event(self._storage_engine, self._run_id, "ai.run.fleet", json.dumps({
                "summary_size": len(str(summary)),
                "model": self._fleet_model,
            }))
            # Record recent run for Fleet UI
            if not hasattr(self, "_recent_runs"):
                self._recent_runs = []  # type: ignore[attr-defined]
            self._recent_runs.append({
                "agent": "fleet",
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tool_calls": result["tool_calls_validated"],
            })  # type: ignore[attr-defined]
        except Exception as e:
            result["error"] = str(e)
            # Surface errors to Fleet UI recent runs
            try:
                if not hasattr(self, "_recent_runs"):
                    self._recent_runs = []  # type: ignore[attr-defined]
                self._recent_runs.append({  # type: ignore[attr-defined]
                    "agent": "fleet",
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "error": result["error"],
                })
            except Exception:
                pass
        finally:
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    async def run_ship(self, ship_id: str, parent_run_id: Optional[str] = None) -> RunResult:
        started = time.perf_counter()
        result: RunResult = {
            "run_id": f"ship_{ship_id}_{int(started*1000)}",
            "parent_run_id": parent_run_id,
            "agent_type": "ship",
            "engine": self._ship_engine_kind,
            "model": self._ship_model,
            "tool_calls": [],
            "tool_calls_validated": [],
            "applied": False,
        }
        try:
            world = self._world_getter()
            ship = world.get_ship(ship_id)
            summary = self._build_ship_summary(ship)
            tool = await asyncio.wait_for(self._ship_decide(ship, summary), timeout=max(1.0, CONFIG.ai_poll_s))
            result["tool_calls"] = [tool]
            # Validate tool; fallback to a conservative set_nav if invalid
            tool_name = (tool or {}).get("tool") if isinstance(tool, dict) else None
            if tool_name not in ("set_nav", "fire_torpedo", "deploy_countermeasure"):
                # Fallback: conservative navigation respecting constraints
                fallback = self._stub.propose_orders(ship)
                result["tool_calls_validated"] = [fallback]
                # Mark error message to surface in UI trace
                result["error"] = "Unknown tool returned by engine; applied fallback set_nav"
            else:
                result["tool_calls_validated"] = [tool]
            insert_event(self._storage_engine, self._run_id, "ai.run.ship", json.dumps({
                "ship_id": ship_id,
                "summary_size": len(str(summary)),
                "model": self._ship_model,
            }))
            if not hasattr(self, "_recent_runs"):
                self._recent_runs = []  # type: ignore[attr-defined]
            self._recent_runs.append({
                "agent": "ship",
                "ship_id": ship_id,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tool_calls": result["tool_calls_validated"],
            })  # type: ignore[attr-defined]
        except Exception as e:
            result["error"] = str(e)
            # Surface errors to Fleet UI recent runs
            try:
                if not hasattr(self, "_recent_runs"):
                    self._recent_runs = []  # type: ignore[attr-defined]
                self._recent_runs.append({  # type: ignore[attr-defined]
                    "agent": "ship",
                    "ship_id": ship_id,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "error": result["error"],
                })
            except Exception:
                pass
        finally:
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    async def health_check(self) -> Dict[str, Any]:
        """Lightweight connectivity check for configured engines."""
        result: Dict[str, Any] = {"fleet": {"ok": True}, "ship": {"ok": True}}
        # Fleet engine
        if self._fleet_engine_kind == "ollama":
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{CONFIG.ollama_host}/api/tags")
                    resp.raise_for_status()
                result["fleet"] = {"ok": True}
            except Exception as e:
                result["fleet"] = {"ok": False, "detail": str(e)}
        elif self._fleet_engine_kind == "openai":
            ok = bool(CONFIG.openai_api_key)
            result["fleet"] = {"ok": ok, "detail": ("missing OPENAI_API_KEY" if not ok else "")}
        else:
            result["fleet"] = {"ok": True, "detail": "stub"}
        # Ship engine
        if self._ship_engine_kind == "ollama":
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{CONFIG.ollama_host}/api/tags")
                    resp.raise_for_status()
                result["ship"] = {"ok": True}
            except Exception as e:
                result["ship"] = {"ok": False, "detail": str(e)}
        elif self._ship_engine_kind == "openai":
            ok = bool(CONFIG.openai_api_key)
            result["ship"] = {"ok": ok, "detail": ("missing OPENAI_API_KEY" if not ok else "")}
        else:
            result["ship"] = {"ok": True, "detail": "stub"}
        return result


