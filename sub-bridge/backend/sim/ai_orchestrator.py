from __future__ import annotations

import asyncio
import time
import hashlib
import math
from typing import Any, Dict, List, Literal, Optional, TypedDict
import httpx

from ..config import CONFIG
import json
from .ai_engines import BaseEngine, StubEngine, OllamaAgentsEngine, OpenAIAgentsEngine
from ..models import Ship
from ..storage import insert_event
from .ai_tools import LocalAIStub
from .sonar import passive_contacts as _passive_contacts


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
                "sensors": {"passive_ok": getattr(ship.systems, 'sonar_ok', True), "has_active": getattr(getattr(ship, 'capabilities', None), 'has_active_sonar', False)},
            })
        # Aggregated enemy belief: merge passive + visual contacts from RED ships against BLUE ships
        enemy_belief: List[Dict[str, Any]] = []
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
                # Visual contacts: near-surface targets within ~15 km
                for blu in blue_ships:
                    try:
                        if blu.kin.depth <= 5.0:
                            dx = blu.kin.x - red.kin.x
                            dy = blu.kin.y - red.kin.y
                            rng = math.hypot(dx, dy)
                            if rng <= 15000.0:
                                # Calculate bearing from RED ship to BLUE ship
                                # Compass convention: 0°=North, 90°=East, 180°=South, 270°=West
                                # For bearing calculation: atan2(dx, dy) gives angle from Y-axis (North), which is correct
                                brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
                                merged[blu.id] = {
                                    "id": blu.id,
                                    "side": blu.side,  # Critical for friendly identification
                                    "bearing": float(brg_true),
                                    "range_est": float(rng),
                                    "confidence": 0.85,
                                    "class": str(getattr(blu, "ship_class", "Unknown")),
                                    "detectability": 1.0,
                                    "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                }
                    except Exception:
                        continue
            enemy_belief = list(merged.values())
        except Exception:
            enemy_belief = []
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
        
        # Add mission prompt as a hint for the AI engine
        prompt_hint = None
        if isinstance(mission_brief, dict) and mission_brief.get("ai_fleet_prompt"):
            prompt_hint = mission_brief.get("ai_fleet_prompt")
        # Include last FleetIntent (hash/body/summary) for stateless continuity
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
        result = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "own_fleet": own_fleet,
            "enemy_belief": enemy_belief,
            "mission": mission,
            "fleet_intent_last": {"hash": intent_hash, "body": last_intent, "summary": last_summary},
            "ship_last_runs": ship_last_runs,
            "orders_last_by_ship": orders_last_map,
        }
        
        # Add prompt hint if available
        if prompt_hint:
            result["_prompt_hint"] = prompt_hint
            
        return result

    def _build_ship_summary(self, ship: Ship) -> Dict[str, Any]:
        # Provide a narrow slice of fleet intent if available, e.g., guidance for this ship
        fleet_intent = {}
        try:
            # world-level fleet intent is maintained by sim loop into orchestrator recent runs mirror
            # We do not require this to exist; default to empty
            fleet_intent = getattr(self, "_last_fleet_intent", {})
        except Exception:
            fleet_intent = {}
        # Build local passive + visual contacts for this ship against non-friendly ships
        local_contacts: List[Dict[str, Any]] = []
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
            # Visual adds range when target is near-surface within ~15 km
            for oth in others:
                try:
                    if oth.kin.depth <= 5.0:
                        dx = oth.kin.x - ship.kin.x
                        dy = oth.kin.y - ship.kin.y
                        rng = math.hypot(dx, dy)
                        if rng <= 15000.0:
                            brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
                            by_id[oth.id] = {
                                **(by_id.get(oth.id, {})),
                                "id": oth.id,
                                "side": oth.side,  # Critical for friendly identification
                                "bearing": float(brg_true),
                                "range_est": float(rng),
                                "class": str(getattr(oth, "ship_class", "Unknown")),
                                "confidence": max(0.7, float(by_id.get(oth.id, {}).get("confidence", 0.0))),
                                "detectability": max(0.8, float(by_id.get(oth.id, {}).get("detectability", 0.0))),
                            }
                except Exception:
                    continue
            local_contacts = list(by_id.values())
        except Exception:
            local_contacts = []
        # Fetch last orders applied to this ship for continuity
        try:
            orders_last = (getattr(self, "_orders_last_by_ship", {}) or {}).get(ship.id, {})
        except Exception:
            orders_last = {}
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
            "orders_last": orders_last,
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
            # Capture full API call for debugging
            api_call_debug = {
                "system_prompt": "You are the Fleet Commander for an enemy flotilla. Plan high-level strategy for all of your ships to achieve mission objectives while minimizing detectability and respecting ROE. You will receive a structured summary of your fleet and an uncertain belief of enemy contacts. Never assume ground-truth positions. Output only a JSON FleetIntent. Include a concise 'summary' string explaining your rationale in 1 short sentence. Do not reveal unknown truths.",
                "user_prompt": "FLEET_SUMMARY_JSON:\n" + json.dumps(summary, separators=(",", ":")) + "\n\nCONSTRAINTS:\n- Do not reveal or rely on unknown enemy truth.\n- Prefer convoy protection unless ROE authorizes engagement.\n- If escorts are low on ammo, bias toward defensive spacing.\n\nProduce FleetIntent JSON.",
                "summary_size": len(str(summary)),
            }
            intent_raw = await asyncio.wait_for(self._fleet_decide(summary), timeout=max(1.0, CONFIG.ai_poll_s))
            # Normalize/augment intent to ensure objectives/guidance present
            intent = self._normalize_intent(summary, intent_raw)
            result["tool_calls"] = [{"tool": "set_fleet_intent", "arguments": intent}]
            # Add concise human summary
            fleet_thought = intent.get("summary") or self._summarize_fleet_intent(intent)
            # Validation/clamping would occur here (placeholder: accept as-is)
            result["tool_calls_validated"] = result["tool_calls"]
            # Capture model response
            api_call_debug["model_response"] = str(intent_raw)
            api_call_debug["model"] = self._fleet_model
            api_call_debug["duration_ms"] = int((time.perf_counter() - started) * 1000)
            # Emit trace event with full debug info
            insert_event(self._storage_engine, self._run_id, "ai.run.fleet", json.dumps(api_call_debug))
            # Record recent run for Fleet UI
            if not hasattr(self, "_recent_runs"):
                self._recent_runs = []  # type: ignore[attr-defined]
            self._recent_runs.append({
                "agent": "fleet",
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tool_calls": result["tool_calls_validated"],
                "summary": fleet_thought,
                "api_call_debug": api_call_debug,
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

    def _normalize_intent(self, fleet_summary: Dict[str, Any], intent: Dict[str, Any]) -> Dict[str, Any]:
        intent = dict(intent or {})
        objectives = intent.get("objectives")
        # Ensure dict objectives keyed by ship id
        if not isinstance(objectives, dict):
            objectives = {}
        # If empty or missing destinations, derive from mission target_wp for each RED ship
        mission = fleet_summary.get("mission", {}) or {}
        target = mission.get("target_wp")
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
        # Engagement rules: default from mission ROE
        er = intent.get("engagement_rules")
        if not isinstance(er, dict):
            er = {}
        if "weapons_free" not in er:
            er["weapons_free"] = bool((mission.get("roe") or {}).get("weapons_free", False))
        intent["engagement_rules"] = er
        intent["objectives"] = objectives
        # Optional advisory notes
        notes = intent.get("notes")
        if not isinstance(notes, list):
            notes = []
        hint = mission.get("ai_fleet_prompt")
        if hint and all("sub" not in (n.get("text","")) for n in notes if isinstance(n, dict)):
            if "sub" in str(hint).lower():
                # Add a simple advisory applicable to all
                for s in (fleet_summary.get("own_fleet", []) or []):
                    sid = s.get("id")
                    if sid:
                        notes.append({"ship_id": sid, "text": "Warning: possible enemy submarine in area."})
        intent["notes"] = notes
        return intent

    def _summarize_fleet_intent(self, intent: Dict[str, Any]) -> str:
        try:
            dests = []
            for sid, obj in (intent.get("objectives") or {}).items():
                if isinstance(obj, dict) and "destination" in obj:
                    d = obj["destination"]
                    if isinstance(d, (list, tuple)) and len(d) == 2:
                        dests.append(f"{sid}→[{float(d[0]):.0f},{float(d[1]):.0f}]")
            notes = "; ".join([n.get("text", "") for n in (intent.get("notes") or []) if isinstance(n, dict)])
            wf = intent.get("engagement_rules", {}).get("weapons_free")
            er = "WF" if wf else "HOLD"
            parts = []
            if dests:
                parts.append("Objectives: " + ", ".join(dests))
            parts.append(f"ROE:{er}")
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
            # Capture full API call for debugging
            api_call_debug = {
                "system_prompt": "You command a single ship. Make conservative, doctrine-aligned decisions based only on your local summary and the FleetIntent. Prefer following FleetIntent; if you must deviate due to local threats/opportunities, you may, but briefly explain by prefixing the 'summary' with 'deviate:'. Never expose or rely on unknown information. Output exactly one JSON object with keys: 'tool', 'arguments', 'summary'. Do not wrap with markdown or prose. Allowed tools: set_nav(heading: float 0-359.9, speed: float >=0, depth: float >=0); fire_torpedo(tube: int, bearing: float 0-359.9, run_depth: float, enable_range: float); deploy_countermeasure(type: 'noisemaker'|'decoy'). Only use tools supported by your capabilities.",
                "user_prompt": "SHIP_SUMMARY_JSON:\n" + json.dumps(summary, separators=(",", ":")) + "\n\nRules:\n- Strongly prefer the FleetIntent, but you may deviate if local conditions warrant; note 'deviate:' in summary.\n- Respect EMCON posture.\n- Obey ROE and captain consent requirement.\n- Use only allowed tools and only if supported by your capabilities (e.g., if has_torpedoes=false, never fire_torpedo).\n- Output one JSON with keys {tool, arguments, summary}. No markdown fences, no extra keys.\n",
                "summary_size": len(str(summary)),
            }
            tool = await asyncio.wait_for(self._ship_decide(ship, summary), timeout=max(1.0, CONFIG.ai_poll_s))
            result["tool_calls"] = [tool]
            # Validate tool; fallback to intent-driven set_nav if invalid
            tool_name = (tool or {}).get("tool") if isinstance(tool, dict) else None
            if tool_name not in ("set_nav", "fire_torpedo", "deploy_countermeasure"):
                # Try to derive navigation from FleetIntent destination
                nav = self._nav_from_intent(ship, summary)
                if nav is None:
                    nav = self._stub.propose_orders(ship)
                result["tool_calls_validated"] = [nav]
                # Mark error message to surface in UI trace
                result["error"] = "Unknown tool returned by engine; applied intent-driven set_nav"
            else:
                result["tool_calls_validated"] = [tool]
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
            insert_event(self._storage_engine, self._run_id, "ai.run.ship", json.dumps({
                "ship_id": ship_id,
                "summary_size": len(str(summary)),
                "model": self._ship_model,
                "api_call_debug": api_call_debug,
            }))
            if not hasattr(self, "_recent_runs"):
                self._recent_runs = []  # type: ignore[attr-defined]
            self._recent_runs.append({
                "agent": "ship",
                "ship_id": ship_id,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tool_calls": result["tool_calls_validated"],
                "summary": ship_thought,
                "api_call_debug": api_call_debug,
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
            # Choose modest convoy speed
            speed = min(ship.hull.max_speed, max(3.0, min(10.0, ship.kin.speed or 5.0)))
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


