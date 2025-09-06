from __future__ import annotations

import asyncio
import time
import hashlib
import math
import re
from typing import Any, Dict, List, Literal, Optional, TypedDict
from pathlib import Path
import httpx

from ..config import CONFIG
from openai import AsyncOpenAI
import json
from .ai_engines import BaseEngine, StubEngine, OllamaAgentsEngine, OpenAIAgentsEngine
from ..models import Ship
from ..storage import insert_event
from .ai_tools import LocalAIStub
from .sonar import passive_contacts as _passive_contacts


def _round_floats(obj: Any, ndigits: int = 1) -> Any:
    """Recursively round all float values within a structure to ndigits.

    - Leaves integers and non-numeric types unchanged
    - Preserves lists and dict structure
    """
    try:
        # Fast path for exact float
        if isinstance(obj, float):
            return round(obj, ndigits)
        # Avoid converting ints to floats
        if isinstance(obj, int):
            return obj
        if isinstance(obj, list):
            return [_round_floats(v, ndigits) for v in obj]
        if isinstance(obj, tuple):
            return tuple(_round_floats(v, ndigits) for v in obj)
        if isinstance(obj, dict):
            return {k: _round_floats(v, ndigits) for k, v in obj.items()}
        return obj
    except Exception:
        return obj

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
        # Optional JSONL log file path for /fleet API call history
        self._log_file_path: Optional[str] = None

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
                "sensors": {"passive_ok": getattr(ship.systems, 'sonar_ok', True), "has_active": getattr(getattr(ship, 'capabilities', None), 'has_active_sonar', False)},
                "capabilities": (getattr(ship, 'capabilities', None).dict() if getattr(ship, 'capabilities', None) else None),
                "constraints": {"maxSpeed": ship.hull.max_speed, "maxDepth": ship.hull.max_depth, "turnRate": ship.hull.turn_rate_max},
            })
        # Aggregated enemy belief: merge passive + visual contacts from RED ships against BLUE ships
        enemy_belief: List[Dict[str, Any]] = []
        # Rolling contact history for Fleet Commander (sensor reports only; no debug truth)
        if not hasattr(self, "_fleet_contact_history"):
            self._fleet_contact_history = []  # type: ignore[attr-defined]
        history_events: List[Dict[str, Any]] = []
        try:
            merged: Dict[str, Dict[str, Any]] = {}
            blue_ships = [s for s in world.all_ships() if s.side == "BLUE"]
            for red in world.all_ships():
                if red.side != "RED":
                    continue
                contacts = _passive_contacts(red, blue_ships)
                for c in contacts:
                    cid = getattr(c, "id", None)
                    if not cid:
                        continue
                    # Find the actual ship to get side information
                    target_ship = next((s for s in blue_ships if s.id == cid), None)
                    merged[cid] = {
                        "id": cid,
                        "side": target_ship.side if target_ship else "Unknown",  # Critical for friendly identification
                        "bearing": float(getattr(c, "bearing", 0.0)),
                        "confidence": float(getattr(c, "confidence", 0.0)),
                        "class": str(getattr(c, "classifiedAs", "Unknown")),
                        "detectability": float((getattr(c, "detectability", 0.0) or getattr(c, "strength", 0.0))),
                        "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    # Append passive contact event to history (bearing-only; no range position)
                    history_events.append({
                        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "reportedBy": red.id,
                        "reporter_pos": [red.kin.x, red.kin.y],
                        "type": "passive",
                        "id": cid,
                        "bearing": float(getattr(c, "bearing", 0.0)),
                        "range_est": None,
                        "confidence": float(getattr(c, "confidence", 0.0)),
                        "classifiedAs": str(getattr(c, "classifiedAs", "Unknown")),
                    })
                # Visual contacts: use probabilistic lock state provided by Simulation
                try:
                    vis_map = getattr(self, "_visual_detection_map", {}) or {}
                    from_obs = vis_map.get(red.id, {}) if isinstance(vis_map, dict) else {}
                    for blu in blue_ships:
                        st = from_obs.get(blu.id, {}) if isinstance(from_obs, dict) else {}
                        if not bool(st.get("detected", False)):
                            continue
                        dx = blu.kin.x - red.kin.x
                        dy = blu.kin.y - red.kin.y
                        rng = math.hypot(dx, dy)
                        if rng > 15000.0:
                            continue
                        brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
                        merged[blu.id] = {
                            "id": blu.id,
                            "side": blu.side,
                            "bearing": float(brg_true),
                            "range_est": float(rng),
                            "confidence": 0.9 if st.get("mode") == "surface" else 0.7,
                            "class": str(getattr(blu, "ship_class", "Unknown")),
                            "detectability": 1.0,
                            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "visual_mode": st.get("mode"),
                        }
                        # Append visual contact event with estimated position
                        heading_rad = math.radians(brg_true)
                        est_x = red.kin.x + math.sin(heading_rad) * rng
                        est_y = red.kin.y + math.cos(heading_rad) * rng
                        history_events.append({
                            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "reportedBy": red.id,
                            "reporter_pos": [red.kin.x, red.kin.y],
                            "type": "visual",
                            "id": blu.id,
                            "bearing": float(brg_true),
                            "range_est": float(rng),
                            "confidence": 0.9 if st.get("mode") == "surface" else 0.7,
                            "classifiedAs": str(getattr(blu, "ship_class", "Unknown")),
                            "est_pos": [float(est_x), float(est_y)],
                            "mode": st.get("mode"),
                        })
                except Exception:
                    pass
            enemy_belief = list(merged.values())
        except Exception:
            enemy_belief = []
        # Merge and cap history size
        try:
            if isinstance(getattr(self, "_fleet_contact_history"), list):
                self._fleet_contact_history.extend(history_events)  # type: ignore[attr-defined]
                # Keep last 100 entries
                self._fleet_contact_history = self._fleet_contact_history[-100:]  # type: ignore[attr-defined]
        except Exception:
            pass
        # Mission objective provided by Simulation (if attached by creator)
        mission_brief = getattr(self, "_mission_brief", None)
        if isinstance(mission_brief, dict):
            # Include a simple convoy list and an optional target waypoint for training missions
            convoy = [
                {"id": s.id, "class": getattr(s, "ship_class", None)}
                for s in world.all_ships() if s.side == "RED"
            ]
            target_wp = mission_brief.get("target_wp") if isinstance(mission_brief.get("target_wp", None), (list, tuple)) else None
            # Pass-through structured mission supplements when present (exclude any free-text AI prompts)
            mission = {
                "objective": mission_brief.get("objective"),
                # Prefer explicit side summary for Fleet Commander
                "mission_summary": mission_brief.get("red_mission_summary") or mission_brief.get("mission_summary"),
                "red_mission_summary": mission_brief.get("red_mission_summary"),
                "blue_mission_summary": mission_brief.get("blue_mission_summary"),
                "convoy": convoy,
                "target_wp": target_wp,
                "side_objectives": mission_brief.get("side_objectives"),
                "protected_assets": mission_brief.get("protected_assets"),
                "emcon": mission_brief.get("emcon"),
                "formations": mission_brief.get("formations"),
                "speed_limits": mission_brief.get("speed_limits"),
                "navigation_constraints": mission_brief.get("navigation_constraints"),
                "threat_hints": mission_brief.get("threat_hints"),
                "success_criteria": mission_brief.get("success_criteria"),
            }
        else:
            mission = {}
        # Include last FleetIntent (hash/body/summary) and recent history for stateless continuity
        try:
            last_intent = getattr(self, "_last_fleet_intent", {}) or {}
        except Exception:
            last_intent = {}
        try:
            runs = getattr(self, "_recent_runs", []) or []
            last_summary = next((r.get("summary", "") for r in reversed(runs) if r.get("agent") == "fleet" and r.get("summary")), "")
        except Exception:
            last_summary = ""
        try:
            intent_hash = hashlib.sha1(json.dumps(last_intent, sort_keys=True).encode()).hexdigest()[:8]
        except Exception:
            intent_hash = ""
        # Include per-ship last run summary/tool and last orders for Fleet Commander situational awareness
        ship_last_runs: List[Dict[str, Any]] = []
        try:
            runs = getattr(self, "_recent_runs", []) or []
            last_by_ship: Dict[str, Dict[str, Any]] = {}
            for r in runs:
                if r.get("agent") == "ship":
                    sid = r.get("ship_id")
                    if sid:
                        last_by_ship[sid] = {"ship_id": sid, "summary": r.get("summary"), "tool_calls": r.get("tool_calls", [])}
            ship_last_runs = list(last_by_ship.values())
        except Exception:
            ship_last_runs = []
        try:
            orders_last_map = getattr(self, "_orders_last_by_ship", {}) or {}
        except Exception:
            orders_last_map = {}
        # Retrieve recent history (if Simulation mirrored it onto orchestrator)
        try:
            history = list(getattr(self, "_fleet_intent_history", []))[-8:]
        except Exception:
            history = []
        result = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "own_fleet": own_fleet,
            "enemy_belief": enemy_belief,
            "mission": mission,
            "fleet_intent_last": {"hash": intent_hash, "body": last_intent, "summary": last_summary},
            "fleet_intent_history": history,
            "ship_last_runs": ship_last_runs,
            "orders_last_by_ship": orders_last_map,
        }
        # Add fleet-level contact history for planning context (sensor-only)
        try:
            result["contact_history"] = list(getattr(self, "_fleet_contact_history", []))[-100:]
        except Exception:
            pass
        # Truncate numeric precision to save prompt space
        return _round_floats(result, 1)

    def _build_ship_summary(self, ship: Ship) -> Dict[str, Any]:
        # Provide a narrow slice of fleet intent if available, e.g., guidance for this ship
        fleet_intent = {}
        try:
            # world-level fleet intent is maintained by sim loop into orchestrator recent runs mirror
            # We do not require this to exist; default to empty
            fleet_intent = getattr(self, "_last_fleet_intent", {})
        except Exception:
            fleet_intent = {}
        # Surface current fleet summary line to ship for alignment
        fleet_summary_line = ""
        try:
            fleet_summary_line = str((fleet_intent or {}).get("summary", ""))
        except Exception:
            fleet_summary_line = ""
        # Build local passive + visual contacts for this ship against non-friendly ships
        local_contacts: List[Dict[str, Any]] = []
        fleet_fused_contacts: List[Dict[str, Any]] = []
        try:
            world = self._world_getter()
            others = [s for s in world.all_ships() if s.id != ship.id and s.side != ship.side]
            contacts = _passive_contacts(ship, others)
            by_id: Dict[str, Dict[str, Any]] = {}
            for c in contacts:
                # Find the actual ship to get side information
                target_ship = next((s for s in others if s.id == getattr(c, "id", "")), None)
                by_id[getattr(c, "id", "")] = {
                    "id": getattr(c, "id", ""),
                    "side": target_ship.side if target_ship else "Unknown",  # Critical for friendly identification
                    "bearing": float(getattr(c, "bearing", 0.0)),
                    "class": str(getattr(c, "classifiedAs", "Unknown")),
                    "confidence": float(getattr(c, "confidence", 0.0)),
                    "detectability": float((getattr(c, "detectability", 0.0) or getattr(c, "strength", 0.0))),
                }
            # Visual adds range when detection is locked in Simulation's probabilistic model
            try:
                vis_map = getattr(self, "_visual_detection_map", {}) or {}
                from_obs = vis_map.get(ship.id, {}) if isinstance(vis_map, dict) else {}
                for oth in others:
                    st = from_obs.get(oth.id, {}) if isinstance(from_obs, dict) else {}
                    if not bool(st.get("detected", False)):
                        continue
                    dx = oth.kin.x - ship.kin.x
                    dy = oth.kin.y - ship.kin.y
                    rng = math.hypot(dx, dy)
                    if rng > 15000.0:
                        continue
                    brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
                    by_id[oth.id] = {
                        **(by_id.get(oth.id, {})),
                        "id": oth.id,
                        "side": oth.side,
                        "bearing": float(brg_true),
                        "range_est": float(rng),
                        "class": str(getattr(oth, "ship_class", "Unknown")),
                        "confidence": max(0.7 if st.get("mode") == "surface" else 0.5, float(by_id.get(oth.id, {}).get("confidence", 0.0))),
                        "detectability": max(0.8 if st.get("mode") == "surface" else 0.6, float(by_id.get(oth.id, {}).get("detectability", 0.0))),
                    }
            except Exception:
                pass
            local_contacts = list(by_id.values())
        except Exception:
            local_contacts = []
        # Derive bearings to any FleetIntent-estimated positions (from notes) and expose to LLM
        try:
            fi = fleet_intent if isinstance(fleet_intent, dict) else {}
            notes = fi.get("notes") if isinstance(fi, dict) else None
            if isinstance(notes, list):
                coord_re = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]")
                for n in notes:
                    if not isinstance(n, dict):
                        continue
                    text = str(n.get("text", ""))
                    m = coord_re.search(text)
                    if not m:
                        continue
                    try:
                        ex = float(m.group(1))
                        ey = float(m.group(2))
                        dx = ex - ship.kin.x
                        dy = ey - ship.kin.y
                        rng = math.hypot(dx, dy)
                        brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
                        fleet_fused_contacts.append({
                            "bearing": float(round(brg_true, 1)),
                            "range_est": float(round(rng, 1)),
                            "pos_est": [float(round(ex, 1)), float(round(ey, 1))],
                            "source": "fleet_note",
                            "text": text,
                        })
                    except Exception:
                        continue
        except Exception:
            fleet_fused_contacts = []
        # Maintain short contact history per ship for LLM memory (last 6 sightings)
        try:
            if not hasattr(self, "_contacts_history_by_ship"):
                self._contacts_history_by_ship = {}  # type: ignore[attr-defined]
            hist: List[Dict[str, Any]] = list(getattr(self._contacts_history_by_ship, ship.id, [])) if isinstance(getattr(self, "_contacts_history_by_ship"), dict) else []  # type: ignore[attr-defined]
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for c in local_contacts:
                entry = {
                    "time": now_iso,
                    "id": c.get("id"),
                    "bearing": c.get("bearing"),
                    "range_est": c.get("range_est"),
                    "class": c.get("class"),
                    "confidence": c.get("confidence"),
                }
                hist.append(entry)
            # keep last 6
            hist = hist[-6:]
            # store back
            if isinstance(getattr(self, "_contacts_history_by_ship"), dict):
                self._contacts_history_by_ship[ship.id] = hist  # type: ignore[attr-defined]
        except Exception:
            hist = []
        # Fetch last orders applied to this ship for continuity
        try:
            orders_last = (getattr(self, "_orders_last_by_ship", {}) or {}).get(ship.id, {})
        except Exception:
            orders_last = {}
        # Alert flag surfaced from Simulation (if present) or simple heuristic fallback
        alert_flag = False
        try:
            alert_map = getattr(self, "_ship_alert_map", {}) or {}
            alert_flag = bool(alert_map.get(ship.id, False))
        except Exception:
            alert_flag = False
        result = {
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
            "capabilities": {
                "can_set_nav": bool(getattr(ship.capabilities, "can_set_nav", True)),
                "has_active_sonar": bool(getattr(ship.capabilities, "has_active_sonar", False)),
                "has_torpedoes": bool(getattr(ship.capabilities, "has_torpedoes", False)),
                "has_guns": bool(getattr(ship.capabilities, "has_guns", False)),
                "has_depth_charges": bool(getattr(ship.capabilities, "has_depth_charges", False)),
            },
            "sensors": {"passive_ok": getattr(ship.systems, 'sonar_ok', True), "has_active": getattr(getattr(ship, 'capabilities', None), 'has_active_sonar', False)},
            # Local contacts should come from sonar; orchestrator does not have ground-truth enemy positions
            "contacts": local_contacts,
            "fleet_fused_contacts": fleet_fused_contacts,
            "contacts_history": hist,
            "orders_last": orders_last,
            "fleet_intent": {**fleet_intent, "summary": fleet_summary_line} if isinstance(fleet_intent, dict) else fleet_intent,
            "detected_state": {"alert": alert_flag},
        }
        # Truncate numeric precision to save prompt space
        return _round_floats(result, 1)

    # ---------- Engines ----------
    async def _fleet_decide(self, fleet_summary: Dict[str, Any]) -> Dict[str, Any]:
        # Pass summary as-is; engines are mission-agnostic and receive mission data only
        return await self._fleet_engine.propose_fleet_intent(fleet_summary)

    async def _ship_decide(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        # Pass summary as-is; engines are mission-agnostic
        return await self._ship_engine.propose_ship_tool(ship, ship_summary)

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
            # Capture full API call for debugging (mission-agnostic prompts)
            api_call_debug = {
                "system_prompt": (
                    "You are the RED Fleet Commander in a naval wargame.\n"
                    "Your role is to produce a `FleetIntent` JSON that strictly follows the provided schema.\n"
                    "Do not output anything except valid JSON conforming to schema.\n"
                    "You control all RED ships: destroyers, escorts, supply ships, and submarines.\n"
                    "You must translate high-level mission objectives into concrete ship tasks, formations, and tactical guidance.\n\n"
                    "### Duties\n"
                    "1. **Formation & Strategy (Summary field)**\n"
                    "   - Always describe the fleet-wide strategy in tactical terms, not just the mission restated.\n"
                    "   - Organize ships into task groups (e.g., Convoy A, Convoy B, Sub screen) and describe their roles.\n"
                    "   - Explicitly list key ship positions or offsets (e.g., “dd-01 escorts supply-01 1 km ahead”).\n"
                    "   - Capture EMCON posture and baseline speeds.\n"
                    "   - Repeat strategy across turns unless you are adapting — do not thrash.\n\n"
                    "2. **Ship Objectives**\n"
                    "   - Every RED ship must appear under `objectives`.\n"
                    "   - Include `destination` [x,y] and a one-sentence `goal`.\n"
                    "   - Add `speed_kn` only if a clear recommendation exists.\n\n"
                    "3. **EMCON**\n"
                    "   - Always set `active_ping_allowed` and `radio_discipline`.\n"
                    "   - If conditions for escalation exist (e.g., when to allow active sonar), place them in `notes`.\n\n"
                    "4. **Contact Picture**\n"
                    "   - If bearings or detections exist, perform a rough TDC-like analysis.\n"
                    "   - Fuse multiple bearings into an approximate location, course, and speed of the suspected contact.\n"
                    "   - Include this as a note, e.g., “Bearings converge: possible sub at [x,y], heading ~200, ~12 knots.”\n\n"
                    "5. **Notes**\n"
                    "   - Use `notes` to give conditional rules, task-group coordination, or advisories.\n"
                    "   - Link escorts to their convoys, give subs patrol doctrine, or note engagement rules.\n"
                    "   - Keep concise and actionable.\n\n"
                    "6. **Constraints**\n"
                    "   - Do not invent enemy truth beyond provided beliefs.\n"
                    "   - Do not omit RED ships.\n"
                    "   - Do not output extra fields outside the schema.\n\n"
                    "### Schema (reminder)\n"
                    "{\n"
                    " \"objectives\": { ship_id: { \"destination\": [x,y], \"goal\": \"string\", \"speed_kn\": optional number }},\n"
                    " \"emcon\": { \"active_ping_allowed\": bool, \"radio_discipline\": \"string\" },\n"
                    " \"summary\": \"string\",\n"
                    " \"notes\": [ { \"ship_id\": optional, \"text\": \"string\" } ]\n"
                    "}\n"
                ),
                "user_prompt": (
                    "SCHEMA (JSON Schema):\n"
                    "{"
                    "\"type\":\"object\",\n"
                    "\"required\":[\"objectives\",\"summary\"],\n"
                    "\"properties\":{\n"
                    "  \"objectives\":{\"type\":\"object\",\"additionalProperties\":{\n"
                    "    \"type\":\"object\",\n"
                    "    \"required\":[\"destination\",\"goal\"],\n"
                    "    \"properties\":{\n"
                    "      \"destination\":{\"type\":\"array\",\"items\":{\"type\":\"number\"},\"minItems\":2,\"maxItems\":2},\n"
                    "      \"speed_kn\":{\"type\":\"number\"},\n"
                    "      \"goal\":{\"type\":\"string\"}\n"
                    "    },\n"
                    "    \"additionalProperties\":false\n"
                    "  }},\n"
                    "  \"emcon\":{\"type\":\"object\",\"properties\":{\n"
                    "    \"active_ping_allowed\":{\"type\":\"boolean\"},\n"
                    "    \"radio_discipline\":{\"type\":\"string\"}\n"
                    "  },\"additionalProperties\":false},\n"
                    "  \"summary\":{\"type\":\"string\"},\n"
                    "  \"notes\":{\"type\":\"array\",\"items\":{\n"
                    "    \"type\":\"object\",\n"
                    "    \"properties\":{\"ship_id\":{\"type\":[\"string\",\"null\"]},\"text\":{\"type\":\"string\"}},\n"
                    "    \"required\":[\"text\"],\"additionalProperties\":false\n"
                    "  }}\n"
                    "},\n"
                    "\"additionalProperties\":false\n"
                    "}\n\n"
                    "DATA (use only this):\n"
                    "FLEET_SUMMARY_JSON:\n" + json.dumps(summary, separators=(',', ':')) + "\n\n"
                    "CONSTRAINTS:\n"
                    "- Include EVERY RED ship id under 'objectives'.\n"
                    "- Each ship MUST include a one-sentence 'goal'.\n"
                    "- Include 'speed_kn' only if clearly recommended.\n"
                    "- If bearings exist, attempt to produce a fused contact estimate (location, course, speed).\n"
                    "- Encode strategy in the 'summary' so that strategy is persistent across turns.\n"
                    "- Use 'notes' for conditional rules, escort logic, patrol instructions, and advisories.\n"
                    "- Do not infer unknown enemy truth beyond provided beliefs.\n"
                    "- Output ONLY the JSON object conforming to the schema\n"
                ),
                "summary_size": len(str(summary)),
            }
            # Ensure engines receive EXACTLY these prompts by passing a prompt hint
            summary_for_engine = dict(summary)
            summary_for_engine["_prompt_hint"] = {
                "system_prompt": api_call_debug["system_prompt"],
                "user_prompt": api_call_debug["user_prompt"],
            }
            intent_raw = await asyncio.wait_for(self._fleet_decide(summary_for_engine), timeout=max(1.0, getattr(CONFIG, "ai_http_timeout_s", 15.0)))
            # Normalize/augment intent to ensure objectives/guidance present
            intent = self._normalize_intent(summary, intent_raw)
            # Start with intent as primary tool call
            tool_calls = [{"tool": "set_fleet_intent", "arguments": intent}]
            result["tool_calls"] = tool_calls
            # Add concise human summary
            fleet_thought = intent.get("summary") or self._summarize_fleet_intent(intent)
            # Validation/clamping would occur here (placeholder: accept as-is)
            result["tool_calls_validated"] = result["tool_calls"]
            # Capture model response
            api_call_debug["model_response"] = str(intent_raw)
            api_call_debug["model"] = self._fleet_model
            api_call_debug["duration_ms"] = int((time.perf_counter() - started) * 1000)
            # Include provider call metadata if available
            try:
                engine_meta = getattr(self._fleet_engine, "_last_call_meta", None)
                if engine_meta:
                    api_call_debug["provider_meta"] = engine_meta
            except Exception:
                pass
            # Emit trace event with full debug info
            insert_event(self._storage_engine, self._run_id, "ai.run.fleet", json.dumps(api_call_debug))
            # Record recent run for Fleet UI
            if not hasattr(self, "_recent_runs"):
                self._recent_runs = []  # type: ignore[attr-defined]
            run_entry = {
                "agent": "fleet",
                "provider": self._fleet_engine_kind,
                "model": self._fleet_model,
                "ok": True,
                "source": "llm",
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tool_calls": result["tool_calls_validated"],
                "summary": fleet_thought,
                "api_call_debug": api_call_debug,
            }
            self._recent_runs.append(run_entry)  # type: ignore[attr-defined]
            # Append to log file if configured
            try:
                self._append_run_log(run_entry)
            except Exception:
                pass
        except Exception as e:
            result["error"] = str(e)
            # Surface errors to Fleet UI recent runs
            try:
                if not hasattr(self, "_recent_runs"):
                    self._recent_runs = []  # type: ignore[attr-defined]
                run_entry = {  # type: ignore[var-annotated]
                    "agent": "fleet",
                    "provider": self._fleet_engine_kind,
                    "model": self._fleet_model,
                    "ok": False,
                    "source": "llm",
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "error": result["error"],
                    "api_call_debug": {
                        "model": self._fleet_model,
                        "provider_meta": getattr(self._fleet_engine, "_last_call_meta", None),
                    },
                }
                self._recent_runs.append(run_entry)  # type: ignore[attr-defined]
                try:
                    self._append_run_log(run_entry)
                except Exception:
                    pass
            except Exception:
                pass
        finally:
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    def _normalize_intent(self, fleet_summary: Dict[str, Any], intent: Dict[str, Any]) -> Dict[str, Any]:
        intent = dict(intent or {})
        objectives = intent.get("objectives")
        # Ensure dict objectives keyed by ship id
        if not isinstance(objectives, dict):
            objectives = {}
        # If empty or missing destinations, derive from mission target_wp for each RED ship
        mission = fleet_summary.get("mission", {}) or {}
        target = mission.get("target_wp")
        speed_limits = mission.get("speed_limits") or {}
        if target and isinstance(target, (list, tuple)) and len(target) == 2:
            # Check if objectives are missing or incomplete
            needs_defaults = not objectives
            if objectives:
                # Check if any ships are missing destination objectives
                for s in (fleet_summary.get("own_fleet", []) or []):
                    sid = s.get("id")
                    if sid and sid not in objectives:
                        needs_defaults = True
                        break
                    elif sid and sid in objectives:
                        ship_obj = objectives[sid]
                        if not isinstance(ship_obj, dict) or "destination" not in ship_obj:
                            needs_defaults = True
                            break
            if needs_defaults:
                for s in (fleet_summary.get("own_fleet", []) or []):
                    sid = s.get("id")
                    if not sid:
                        continue
                    if sid not in objectives:
                        objectives[sid] = {}
                    if not isinstance(objectives[sid], dict):
                        objectives[sid] = {}
                    if "destination" not in objectives[sid]:
                        objectives[sid]["destination"] = [float(target[0]), float(target[1])]
        # Enforce richer per-ship fields: speed_kn and goal
        try:
            for s in (fleet_summary.get("own_fleet", []) or []):
                sid = s.get("id")
                if not sid:
                    continue
                if sid not in objectives or not isinstance(objectives[sid], dict):
                    objectives[sid] = {}
                ship_obj = objectives[sid]
                # speed default: mission speed_limits (if applicable) else conservative fraction of max speed
                spd = ship_obj.get("speed_kn")
                if spd is None:
                    # Try mission speed limits by group if present; fall back to 0.6 * maxSpeed
                    max_speed = float(((s.get("constraints") or {}).get("maxSpeed") or 18.0))
                    # If mission defines convoy/group limits, prefer max of that window
                    # Here we use any available limit that looks applicable; otherwise default
                    convoy_limits = None
                    try:
                        # naive pick: first numeric max_kn in speed_limits
                        for _k, v in (speed_limits or {}).items():
                            if isinstance(v, dict) and isinstance(v.get("max_kn"), (int, float)):
                                convoy_limits = v
                                break
                    except Exception:
                        convoy_limits = None
                    if convoy_limits and isinstance(convoy_limits.get("max_kn"), (int, float)):
                        spd = float(convoy_limits.get("max_kn"))
                    else:
                        spd = max(4.0, min(max_speed, 0.6 * max_speed))
                    ship_obj["speed_kn"] = float(spd)
                # goal default: concise one-liner
                if not isinstance(ship_obj.get("goal"), str) or not ship_obj.get("goal").strip():
                    dest = ship_obj.get("destination")
                    if isinstance(dest, (list, tuple)) and len(dest) == 2:
                        ship_obj["goal"] = f"Proceed to [{float(dest[0]):.0f},{float(dest[1]):.0f}] at {float(ship_obj.get('speed_kn', 0.0)):.0f} kn"
                    else:
                        ship_obj["goal"] = f"Hold current course and speed"
        except Exception:
            pass
        # Remove legacy engagement_rules if present
        if "engagement_rules" in intent:
            try:
                intent.pop("engagement_rules", None)
            except Exception:
                pass
        intent["objectives"] = objectives
        # Normalize EMCON
        emcon = intent.get("emcon")
        if not isinstance(emcon, dict):
            emcon = {}
        if "active_ping_allowed" not in emcon:
            try:
                emcon["active_ping_allowed"] = bool((((mission.get("emcon") or {}).get("RED") or {}).get("active_ping_allowed", False)))
            except Exception:
                emcon["active_ping_allowed"] = False
        if "radio_discipline" not in emcon:
            try:
                emcon["radio_discipline"] = str((((mission.get("emcon") or {}).get("RED") or {}).get("radio_discipline", "restricted")))
            except Exception:
                emcon["radio_discipline"] = "restricted"
        intent["emcon"] = emcon
        # Normalize notes list (ensure present; add a default advisory if empty)
        notes = intent.get("notes")
        if not isinstance(notes, list):
            notes = []
        if not notes:
            try:
                notes = [{"ship_id": None, "text": "Adhere EMCON and maintain formation; speeds may be adjusted tactically by ships."}]
            except Exception:
                notes = []
        intent["notes"] = notes
        # Ensure top-level summary exists
        if not isinstance(intent.get("summary"), str) or not intent.get("summary"):
            intent["summary"] = self._summarize_fleet_intent(intent)
        return intent

    def _summarize_fleet_intent(self, intent: Dict[str, Any]) -> str:
        try:
            dests = []
            for sid, obj in (intent.get("objectives") or {}).items():
                if isinstance(obj, dict) and "destination" in obj:
                    d = obj["destination"]
                    if isinstance(d, (list, tuple)) and len(d) == 2:
                        spd = obj.get("speed_kn")
                        if isinstance(spd, (int, float)):
                            dests.append(f"{sid}→[{float(d[0]):.0f},{float(d[1]):.0f}]@{float(spd):.0f}kn")
                        else:
                            dests.append(f"{sid}→[{float(d[0]):.0f},{float(d[1]):.0f}]")
            notes = "; ".join([n.get("text", "") for n in (intent.get("notes") or []) if isinstance(n, dict)])
            parts = []
            if dests:
                parts.append("Objectives: " + ", ".join(dests))
            if notes:
                parts.append(notes)
            return " | ".join(parts)
        except Exception:
            return ""

    def _summarize_ship_tool(self, ship_id: str, tool: Dict[str, Any] | None) -> str:
        try:
            if not tool or not isinstance(tool, dict):
                return ""
            name = tool.get("tool")
            args = tool.get("arguments", {}) or {}
            if name == "set_nav":
                h = float(args.get("heading", 0.0))
                s = float(args.get("speed", 0.0))
                d = float(args.get("depth", 0.0))
                return f"{ship_id}: set_nav hdg {h:.0f} spd {s:.1f} dpt {d:.0f}"
            if name == "fire_torpedo":
                b = float(args.get("bearing", 0.0))
                rd = float(args.get("run_depth", 0.0))
                return f"{ship_id}: fire torpedo brg {b:.0f} dpt {rd:.0f}"
            if name == "deploy_countermeasure":
                t = str(args.get("type", ""))
                return f"{ship_id}: deploy {t}"
            if name == "drop_depth_charges":
                sp = float(args.get("spread_meters", args.get("spread_m", 0.0)))
                md = float(args.get("minDepth", 0.0))
                xd = float(args.get("maxDepth", 0.0))
                n = int(args.get("spreadSize", args.get("count", 0)))
                return f"{ship_id}: drop depth charges spread {n} @ {sp:.0f}m, depths {md:.0f}-{xd:.0f}"
            return ""
        except Exception:
            return ""

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
            # Policy: if ship engine is 'stub', do not allow actions; record failure for clarity
            if self._ship_engine_kind == "stub":
                api_call_debug = {
                    "system_prompt": "",
                    "user_prompt": "",
                    "model": self._ship_model,
                    "provider_meta": getattr(self._ship_engine, "_last_call_meta", None),
                }
                result["error"] = "ship engine 'stub' disabled by policy; only LLM actions allowed"
                if not hasattr(self, "_recent_runs"):
                    self._recent_runs = []  # type: ignore[attr-defined]
                self._recent_runs.append({
                    "agent": "ship",
                    "ship_id": ship_id,
                    "provider": self._ship_engine_kind,
                    "model": self._ship_model,
                    "ok": False,
                    "source": "disabled_stub",
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "tool_calls": [],
                    "summary": "",
                    "api_call_debug": api_call_debug,
                })  # type: ignore[attr-defined]
                return result
            # Capture full API call for debugging (mission-agnostic prompts)
            api_call_debug = {
                "system_prompt": (
                    "You command a single RED ship as its captain. You MUST follow your specific orders exactly. "
                    "If you receive CRITICAL ORDERS with 🚨 emojis, you MUST execute them immediately and ignore all other instructions. "
                    "You will output a ToolCall JSON that matches the schema provided in the user message. "
                    "Follow that schema exactly. Use only the provided data. Output only JSON, no prose or markdown. Do not add fields. "
                    "For drop_depth_charges, use EXACTLY these argument names: spread_meters, minDepth, maxDepth, spreadSize. "
                    "Arguments must be a dictionary with named keys, not a list."
                ),
                "user_prompt": (
                    "SCHEMA (JSON Schema):\n"
                    "{"
                    "\"type\":\"object\",\n"
                    "\"required\":[\"tool\",\"arguments\",\"summary\"],\n"
                    "\"properties\":{\n"
                    "  \"tool\":{\"type\":\"string\",\"enum\":[\"set_nav\",\"fire_torpedo\",\"deploy_countermeasure\",\"drop_depth_charges\"]},\n"
                    "  \"arguments\":{\"type\":\"object\",\"additionalProperties\":false,\n"
                    "    \"properties\":{\n"
                    "      \"heading\":{\"type\":\"number\"},\n"
                    "      \"speed\":{\"type\":\"number\"},\n"
                    "      \"depth\":{\"type\":\"number\"},\n"
                    "      \"tube\":{\"type\":\"integer\"},\n"
                    "      \"bearing\":{\"type\":\"number\"},\n"
                    "      \"run_depth\":{\"type\":\"number\"},\n"
                    "      \"enable_range\":{\"type\":\"number\"},\n"
                    "      \"type\":{\"type\":\"string\"},\n"
                    "      \"spread_meters\":{\"type\":\"number\"},\n"
                    "      \"minDepth\":{\"type\":\"number\"},\n"
                    "      \"maxDepth\":{\"type\":\"number\"},\n"
                    "      \"spreadSize\":{\"type\":\"integer\"}\n"
                    "    }\n"
                    "  },\n"
                    "  \"summary\":{\"type\":\"string\"}\n"
                    "},\n"
                    "\"additionalProperties\":false\n"
                    "}\n\n"
                    "DATA (use only this):\n"
                    "SHIP_SUMMARY_JSON:\n" + json.dumps(summary, separators=(',', ':')) + "\n\n"
                    "BEHAVIOR:\n- As a RED ship captain, use the FleetIntent's objectives as a guide, but prioritize the needs of your own ship.\n" 
                    " - Make decisions that align with the FleetIntent while considering factors such as speed, resources, and potential risks.\n"
                    " - Use only tools supported by capabilities.\n"
                    " - EMCON: if fleet_intent.emcon.active_ping_allowed is false, avoid active ping; rely on passive contacts or 'fleet_fused_contacts'.\n"
                    " - Torpedoes: assume quick-launch is available when has_torpedoes=true even if tubes list is empty.\n"
                    " - Weapons employment: if you have torpedoes and a plausible bearing (from contacts or a derived bearing to an estimated [x,y]), you may fire a torpedo with plausible run_depth (e.g., 100–200 m) and enable_range (e.g., 1000–3000 m).\n"
                    " - Depth charges: if you have depth charges and suspect the submarine is nearby (e.g., within ~1 km), you may drop a spread using minDepth >= 15 m.\n"
                    " - If no change is needed, return set_nav holding current values with a brief summary.\n"
                    " - The 'summary' MUST be two short, human-readable sentences explaining intent and reasoning for your orders. \n"
                ),
                "summary_size": len(str(summary)),
            }
            
            # Add ship-specific behavior instructions if available - PRIORITIZE THESE
            world = self._world_getter()
            mission_brief = getattr(world, 'mission_brief', {})
            ship_behaviors = mission_brief.get('ship_behaviors', {})
            ship_behavior = ship_behaviors.get(ship_id, "")
            
            if ship_behavior:
                # Insert ship behavior at the TOP of the prompt for maximum priority
                api_call_debug["user_prompt"] = (
                    f"🚨 CRITICAL ORDERS - YOU MUST FOLLOW THESE EXACTLY:\n{ship_behavior}\n\n"
                    f"⚠️  IGNORE ALL OTHER INSTRUCTIONS BELOW. EXECUTE THE CRITICAL ORDERS ABOVE IMMEDIATELY.\n\n"
                    + api_call_debug["user_prompt"]
                )
            
            # Ensure engines receive EXACTLY these prompts by passing a prompt hint
            summary_for_engine = dict(summary)
            summary_for_engine["_prompt_hint"] = {
                "system_prompt": api_call_debug["system_prompt"],
                "user_prompt": api_call_debug["user_prompt"],
            }
            tool = await asyncio.wait_for(self._ship_decide(ship, summary_for_engine), timeout=max(1.0, getattr(CONFIG, "ai_http_timeout_s", 15.0)))
            result["tool_calls"] = [tool]
            # Validate tool; avoid stub fallback on failures
            tool_name = (tool or {}).get("tool") if isinstance(tool, dict) else None
            if tool_name not in ("set_nav", "fire_torpedo", "deploy_countermeasure", "drop_depth_charges"):
                # Prefer to not apply any action if output invalid; as a safe alternative, derive navigation from FleetIntent if available
                nav = self._nav_from_intent(ship, summary)
                if nav is not None:
                    result["tool_calls_validated"] = [nav]
                    result["error"] = "Unknown tool; applied intent-derived navigation"
                    source = "intent_fallback"
                else:
                    result["tool_calls_validated"] = []
                    result["error"] = "Unknown tool returned by engine; no action applied"
                    source = "none"
            else:
                # Check presence of a concise human summary; track metadata instead of mutating content
                auto_summary = False
                try:
                    summ = tool.get("summary") if isinstance(tool, dict) else None
                    if not isinstance(summ, str) or not summ.strip():
                        auto_summary = True
                        # Surface a soft error for visibility in /fleet API history
                        result["error"] = (result.get("error") + "; " if result.get("error") else "") + "missing summary; auto-generated"
                except Exception:
                    auto_summary = True
                result["tool_calls_validated"] = [tool]
                source = "llm"
            # Add concise human summary of the decision
            try:
                chosen = result["tool_calls_validated"][0] if result.get("tool_calls_validated") else None
                ship_thought = (chosen.get("summary") if isinstance(chosen, dict) else None) or self._summarize_ship_tool(ship_id, chosen)
            except Exception:
                ship_thought = None
            # Capture model response
            api_call_debug["model_response"] = str(tool)
            api_call_debug["model"] = self._ship_model
            api_call_debug["duration_ms"] = int((time.perf_counter() - started) * 1000)
            # Include provider call metadata if available
            try:
                engine_meta = getattr(self._ship_engine, "_last_call_meta", None)
                if engine_meta:
                    api_call_debug["provider_meta"] = engine_meta
            except Exception:
                pass
            insert_event(self._storage_engine, self._run_id, "ai.run.ship", json.dumps({
                "ship_id": ship_id,
                "summary_size": len(str(summary)),
                "model": self._ship_model,
                "api_call_debug": api_call_debug,
            }))
            if not hasattr(self, "_recent_runs"):
                self._recent_runs = []  # type: ignore[attr-defined]
            run_entry = {
                "agent": "ship",
                "ship_id": ship_id,
                "provider": self._ship_engine_kind,
                "model": self._ship_model,
                "ok": not bool(result.get("error")),
                "source": source,
                "autoSummary": bool('missing summary' in (result.get("error") or "")),
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tool_calls": result["tool_calls_validated"],
                "summary": ship_thought,
                "api_call_debug": api_call_debug,
            }
            self._recent_runs.append(run_entry)  # type: ignore[attr-defined]
            try:
                self._append_run_log(run_entry)
            except Exception:
                pass
        except Exception as e:
            result["error"] = str(e)
            # Surface errors to Fleet UI recent runs
            try:
                if not hasattr(self, "_recent_runs"):
                    self._recent_runs = []  # type: ignore[attr-defined]
                run_entry = {  # type: ignore[var-annotated]
                    "agent": "ship",
                    "ship_id": ship_id,
                    "provider": self._ship_engine_kind,
                    "model": self._ship_model,
                    "ok": False,
                    "source": "llm",
                    "autoSummary": False,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "error": result["error"],
                    "api_call_debug": {
                        "model": self._ship_model,
                        "provider_meta": getattr(self._ship_engine, "_last_call_meta", None),
                    },
                }
                self._recent_runs.append(run_entry)  # type: ignore[attr-defined]
                try:
                    self._append_run_log(run_entry)
                except Exception:
                    pass
            except Exception:
                pass
        finally:
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    # ---------- Logging ----------
    def _append_run_log(self, entry: Dict[str, Any]) -> None:
        """Append a single run entry (as JSON) to the configured log file, if set."""
        try:
            path = getattr(self, "_log_file_path", None)
            if not path:
                return
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def _nav_from_intent(self, ship: Ship, ship_summary: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        import math as _math
        try:
            fi = ship_summary.get("fleet_intent", {}) or {}
            objectives = fi.get("objectives", {}) or {}
            obj = objectives.get(ship.id)
            if not isinstance(obj, dict):
                return None
            dest = obj.get("destination")
            if not (isinstance(dest, (list, tuple)) and len(dest) == 2):
                return None
            # Compute bearing to destination (compass)
            sx, sy = ship.kin.x, ship.kin.y
            dx = float(dest[0]) - sx
            dy = float(dest[1]) - sy
            brg_true = (_math.degrees(_math.atan2(dx, dy)) % 360.0)
            # Choose speed: prefer fleet 'speed_kn' if provided, otherwise sensible default; clamp to platform
            speed_kn = None
            try:
                speed_kn = float(obj.get("speed_kn")) if obj.get("speed_kn") is not None else None
            except Exception:
                speed_kn = None
            if speed_kn is None:
                is_alert = bool(((ship_summary.get("detected_state") or {}).get("alert", False)))
                speed_kn = ship.hull.max_speed if is_alert else min(ship.hull.max_speed, 18.0)
            speed = max(0.0, min(float(speed_kn), float(ship.hull.max_speed)))
            # Surface vessels: stay at or near surface
            depth = 0.0
            return {"tool": "set_nav", "arguments": {"heading": brg_true, "speed": speed, "depth": depth}}
        except Exception:
            return None

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
            try:
                if not CONFIG.openai_api_key:
                    raise ValueError("missing OPENAI_API_KEY")
                client = AsyncOpenAI(api_key=CONFIG.openai_api_key, base_url=CONFIG.openai_base_url)
                # Quick metadata call to verify connectivity
                models = await client.models.list()
                ok = len(getattr(models, "data", []) or []) >= 0
                result["fleet"] = {"ok": bool(ok), "detail": "connected"}
            except Exception as e:
                result["fleet"] = {"ok": False, "detail": str(e)}
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
            try:
                if not CONFIG.openai_api_key:
                    raise ValueError("missing OPENAI_API_KEY")
                client = AsyncOpenAI(api_key=CONFIG.openai_api_key, base_url=CONFIG.openai_base_url)
                models = await client.models.list()
                ok = len(getattr(models, "data", []) or []) >= 0
                result["ship"] = {"ok": bool(ok), "detail": "connected"}
            except Exception as e:
                result["ship"] = {"ok": False, "detail": str(e)}
        else:
            result["ship"] = {"ok": True, "detail": "stub"}
        return result


