"""Command Dispatcher — extracted from loop.py handle_command().

Each handler method accesses simulation state through self._sim.
"""
from __future__ import annotations

import json
import math
import random
import time
from typing import TYPE_CHECKING, Dict, Optional

from ..models import TelemetryContact, SHIP_CATALOG

if TYPE_CHECKING:
    from .loop import Simulation


class CommandDispatcher:
    def __init__(self, sim: Simulation):
        self._sim = sim
        self._handlers = {
            "helm.order": self._helm_order,
            "sonar.ping": self._sonar_ping,
            "weapons.tube.load": self._weapons_tube_load,
            "weapons.tube.flood": self._weapons_tube_flood,
            "weapons.tube.doors": self._weapons_tube_doors,
            "weapons.fire": self._weapons_fire,
            "weapons.test_fire": self._weapons_test_fire,
            "weapons.countermeasure.deploy": self._weapons_countermeasure,
            "weapons.depth_charges.drop": self._weapons_depth_charges,
            "engineering.reactor.set": self._engineering_reactor_set,
            "engineering.power.allocate": self._engineering_power_allocate,
            "engineering.pump.assign": self._engineering_pump_assign,
            "engineering.pump.toggle": self._engineering_pump_toggle,
            "engineering.reactor.scram": self._engineering_scram,
            "station.task.start": self._station_task_start,
            "captain.consent": self._captain_consent,
            "captain.periscope.raise": self._captain_periscope,
            "captain.radio.raise": self._captain_radio,
            "captain.identify_contact": self._captain_identify,
            "plot.bearing.add": self._plot_bearing_add,
            "plot.bearing.remove": self._plot_bearing_remove,
            "plot.contact.add": self._plot_contact_add,
            "plot.contact.update": self._plot_contact_update,
            "plot.contact.remove": self._plot_contact_remove,
            "plot.note.append": self._plot_note_append,
            "plot.clear": self._plot_clear,
            "debug.restart": self._debug_restart,
            "debug.stop_mission": self._debug_stop_mission,
            "debug.maintenance.spawns": self._debug_maint_spawns,
            "debug.visual.player_100": self._debug_visual_player,
            "debug.visual.enemy_100": self._debug_visual_enemy,
            "debug.repair_all": self._debug_repair_all,
            "debug.mission.surface_vessel": self._debug_surface_vessel,
            "debug.mission1": self._debug_mission1,
            "ai.tool": self._ai_tool,
        }

    async def dispatch(self, topic: str, data: Dict) -> Optional[str]:
        handler = self._handlers.get(topic)
        if handler is None:
            return None  # unknown commands were previously ignored
        return await handler(data)

    # ------------------------------------------------------------------
    # Helm
    # ------------------------------------------------------------------

    async def _helm_order(self, data: Dict) -> Optional[str]:
        sim = self._sim
        sim.ordered["heading"] = float(data.get("heading", sim.ordered["heading"])) % 360
        sim.ordered["speed"] = float(data.get("speed", sim.ordered["speed"]))
        sim.ordered["depth"] = max(0.0, float(data.get("depth", sim.ordered["depth"])))
        sim._log_action("HELM", f"Set course to {sim.ordered['heading']:.0f}° at {sim.ordered['speed']:.1f} knots, depth {sim.ordered['depth']:.0f}m", data)
        return None

    # ------------------------------------------------------------------
    # Sonar
    # ------------------------------------------------------------------

    async def _sonar_ping(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        from .sonar import active_ping
        from ..models import TelemetryContact
        if sim.active_ping_state.start():
            res = active_ping(own, [s for s in sim.world.all_ships() if s.id != own.id])
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            sim._last_ping_at = now_iso
            # Active ping returns a "skin paint": precise range + bearing,
            # but NO identification — pings echo off a hull, they don't tell
            # you what kind of hull it is. Strip the actual ship id and emit
            # anonymous per-ping echo numbers so operators can distinguish
            # multiple returns in a single ping cycle without correlating
            # them to passive contacts or ship types.
            sim._last_ping_responses = [
                {"id": f"Echo-{i+1}", "bearing": brg, "range_est": rng, "strength": st, "at": now_iso}
                for i, (_rid, rng, brg, st) in enumerate(res)
            ]
            # Counter-detection contacts for enemy ships
            for ship in sim.world.all_ships():
                if ship.side == "RED":
                    dx = own.kin.x - ship.kin.x
                    dy = own.kin.y - ship.kin.y
                    dist_m = math.hypot(dx, dy)
                    if dist_m <= 15000.0:
                        from .sonar import normalize_angle_deg
                        brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
                        brg_noise = normalize_angle_deg(brg + random.gauss(0, 2.0))
                        strength = max(0.0, min(1.0, 1.0 / (1.0 + (dist_m / 10000.0))))
                        enemy_contact = TelemetryContact(
                            id="ENEMY_ACTIVE_SONAR", bearing=brg_noise, strength=strength,
                            classifiedAs="ENEMY_ACTIVE_SONAR", confidence=0.8,
                            bearingKnown=True, rangeKnown=False,
                        )
                        if not hasattr(sim, "_enemy_ship_contacts"):
                            sim._enemy_ship_contacts = {}
                        if not hasattr(sim, "_enemy_ship_contacts_timestamps"):
                            sim._enemy_ship_contacts_timestamps = {}
                        if ship.id not in sim._enemy_ship_contacts:
                            sim._enemy_ship_contacts[ship.id] = []
                            sim._enemy_ship_contacts_timestamps[ship.id] = []
                        sim._enemy_ship_contacts[ship.id].append(enemy_contact)
                        sim._enemy_ship_contacts_timestamps[ship.id].append(time.time())
                        if hasattr(sim, "_ai_orch") and sim._ai_orch is not None:
                            if not hasattr(sim._ai_orch, "_fleet_contact_history"):
                                sim._ai_orch._fleet_contact_history = []
                            sim._ai_orch._fleet_contact_history.append({
                                "time": now_iso, "reportedBy": ship.id,
                                "reporter_pos": [ship.kin.x, ship.kin.y],
                                "type": "active_sonar_detection", "id": "ownship",
                                "bearing": brg_noise, "range_est": None,
                                "confidence": 0.8, "classifiedAs": "ENEMY_ACTIVE_SONAR",
                            })
                            sim._ai_orch._fleet_contact_history = sim._ai_orch._fleet_contact_history[-100:]
            sim._transient_events.append({"type": "counterDetected", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            sim._log_action("SONAR", "Sent active ping", data)
            return None
        return "Ping on cooldown"

    # ------------------------------------------------------------------
    # Weapons
    # ------------------------------------------------------------------

    async def _weapons_tube_load(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        from .weapons import try_load_tube
        tube = int(data.get("tube", 1))
        weapon = str(data.get("weapon", "Mk48"))
        ok = try_load_tube(own, tube, weapon)
        if ok:
            sim._log_action("WEAPONS", f"Loaded {weapon} in Tube {tube}", data)
        return None if ok else "Cannot load"

    async def _weapons_tube_flood(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        from .weapons import try_flood_tube
        tube = int(data.get("tube", 1))
        ok = try_flood_tube(own, tube)
        if ok:
            sim._log_action("WEAPONS", f"Flooded Tube {tube}", data)
        return None if ok else "Cannot flood"

    async def _weapons_tube_doors(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        from .weapons import try_set_doors
        tube = int(data.get("tube", 1))
        is_open = bool(data.get("open", True))
        ok = try_set_doors(own, tube, is_open)
        if ok:
            action = "Opened" if is_open else "Closed"
            sim._log_action("WEAPONS", f"{action} Tube {tube} doors", data)
        return None if ok else "Cannot set doors"

    async def _weapons_fire(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        from .weapons import try_fire
        from ..config import CONFIG
        from ..storage import insert_event
        if CONFIG.require_captain_consent and not sim._captain_consent:
            return "Captain consent required"
        torp = try_fire(
            own, int(data.get("tube", 1)),
            float(data.get("bearing", own.kin.heading)),
            float(data.get("run_depth", own.kin.depth)),
            float(data.get("enable_range", own.weapons.tubes[0].weapon.enable_range_m if own.weapons.tubes and own.weapons.tubes[0].weapon else 800.0)),
            str(data.get("doctrine", "passive_then_active")),
        )
        if torp is None:
            return "Cannot fire"
        sim.world.torpedoes.append(torp)
        insert_event(sim.engine, sim.run_id, "weapons.fire", json.dumps(data))
        tube = int(data.get("tube", 1))
        bearing = float(data.get("bearing", own.kin.heading))
        sim._log_action("WEAPONS", f"Fired torpedo from Tube {tube} at bearing {bearing:.0f}°", data)
        # Surface a transient event so all-station audio can sound the launch.
        from datetime import datetime, timezone
        sim._transient_events.append({
            "type": "weapons.fire",
            "at": datetime.now(timezone.utc).isoformat(),
            "tube": tube,
            "bearing": bearing,
        })
        return None

    async def _weapons_test_fire(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        from ..storage import insert_event
        torp = {
            "id": f"torpedo_test_{int(time.time() * 1000)}",
            "x": own.kin.x, "y": own.kin.y, "depth": own.kin.depth,
            "heading": float(data.get("bearing", own.kin.heading)) % 360.0,
            "speed": 45.0, "armed": False,
            "enable_range_m": float(data.get("enable_range", 800.0)),
            "seeker_range_m": 4000.0, "run_time": 0.0, "max_run_time": 600.0,
            "target_id": None, "name": "Mk48-TEST", "seeker_cone": 35.0,
            "side": own.side, "spoofed_timer": 0.0,
            "run_depth": float(data.get("run_depth", own.kin.depth)),
            "doctrine": str(data.get("doctrine", "passive_then_active")),
            "pn_nav_const": 3.0, "los_prev": None,
        }
        sim.world.torpedoes.append(torp)
        insert_event(sim.engine, sim.run_id, "weapons.test_fire", json.dumps(data))
        bearing = float(data.get("bearing", own.kin.heading))
        sim._log_action("WEAPONS", f"Test fired torpedo at bearing {bearing:.0f}°", data)
        # Surface the same all-station launch audio event as a real fire so the
        # test button is a faithful audio check too.
        from datetime import datetime, timezone
        sim._transient_events.append({
            "type": "weapons.fire",
            "at": datetime.now(timezone.utc).isoformat(),
            "tube": int(data.get("tube", 1)),
            "bearing": bearing,
        })
        return None

    async def _weapons_countermeasure(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        from .weapons import try_deploy_countermeasure
        from ..storage import insert_event
        cm_type = str(data.get("type", "noisemaker"))
        def _on_cm_event(name: str, payload: dict) -> None:
            insert_event(sim.engine, sim.run_id, name, json.dumps(payload))
        result = try_deploy_countermeasure(own, cm_type, on_event=_on_cm_event)
        if result.get("ok"):
            sim.world.countermeasures.append(result["data"])
            sim._log_action("WEAPONS", f"Deployed {cm_type}", data)
            return None
        return result.get("error", "Deployment failed")

    async def _weapons_depth_charges(self, data: Dict) -> Optional[str]:
        sim = self._sim
        from .weapons import try_drop_depth_charges
        from ..storage import insert_event
        ship_id = str(data.get("ship_id", "red-dd-01"))
        try:
            tgt = sim.world.get_ship(ship_id)
        except Exception:
            return "Unknown ship"
        if not getattr(getattr(tgt, "capabilities", None), "has_depth_charges", False):
            return "Ship cannot drop depth charges"
        spread_m = float(data.get("spread_meters", 20.0))
        min_d = float(data.get("minDepth", 30.0))
        max_d = float(data.get("maxDepth", 50.0))
        n = int(data.get("spreadSize", 3))
        res = try_drop_depth_charges(tgt, spread_m, min_d, max_d, n,
                                      on_event=lambda nm, p: insert_event(sim.engine, sim.run_id, nm, json.dumps(p)))
        if not res.get("ok"):
            return res.get("error", "Drop failed")
        for dc in res.get("data", []) or []:
            sim.world.depth_charges.append(dc)
        sim._log_action("WEAPONS", f"Dropped {n} depth charges from {ship_id}", data)
        return None

    # ------------------------------------------------------------------
    # Engineering
    # ------------------------------------------------------------------

    async def _engineering_reactor_set(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        mw = max(0.0, min(own.reactor.max_mw, float(data.get("mw", own.reactor.output_mw))))
        own.reactor.output_mw = mw
        pct = int((mw / own.reactor.max_mw) * 100) if own.reactor.max_mw > 0 else 0
        sim._log_action("ENGINEERING", f"Set reactor output to {pct}%", data)
        return None

    async def _engineering_power_allocate(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        p = own.power
        helm = max(0.0, float(data.get("helm", p.helm)))
        weapons = max(0.0, float(data.get("weapons", p.weapons)))
        sonar = max(0.0, float(data.get("sonar", p.sonar)))
        engineering = max(0.0, float(data.get("engineering", p.engineering)))
        total = helm + weapons + sonar + engineering
        if total > 1.000001:
            return "Allocation exceeds budget"
        p.helm = helm
        p.weapons = weapons
        p.sonar = sonar
        p.engineering = engineering
        sim._log_action("ENGINEERING", f"Allocated power: Helm {int(helm*100)}%, Weapons {int(weapons*100)}%, Sonar {int(sonar*100)}%, Engineering {int(engineering*100)}%", data)
        return None

    async def _engineering_pump_assign(self, data: Dict) -> Optional[str]:
        sim = self._sim
        pump_num = int(data.get("pump", 0))
        compartment = data.get("compartment")
        if pump_num not in [1, 2]:
            return "Invalid pump number (must be 1 or 2)"
        if compartment is None:
            if pump_num in sim._pump_assignments:
                del sim._pump_assignments[pump_num]
                sim._log_action("ENGINEERING", f"Pump {pump_num} secured", data)
        else:
            compartment_idx = int(compartment)
            if not 0 <= compartment_idx <= 5:
                return "Invalid compartment index (must be 0-5)"
            other_pump = 2 if pump_num == 1 else 1
            if sim._pump_assignments.get(other_pump) == compartment_idx:
                return f"Pump {other_pump} already assigned to compartment {compartment_idx}"
            sim._pump_assignments[pump_num] = compartment_idx
            comp_names = ["FORE", "FORWARD", "CONTROL", "REACTOR", "ENGINE", "STERN"]
            sim._log_action("ENGINEERING", f"Pump {pump_num} assigned to {comp_names[compartment_idx]}", data)
        return None

    async def _engineering_pump_toggle(self, data: Dict) -> Optional[str]:
        sim = self._sim
        name = str(data.get("pump", "")).lower()
        state = bool(data.get("enabled", True))
        if name == "fwd" and state:
            sim._pump_assignments[1] = 0
        elif name == "fwd" and not state and 1 in sim._pump_assignments:
            del sim._pump_assignments[1]
        elif name == "aft" and state:
            sim._pump_assignments[2] = 5
        elif name == "aft" and not state and 2 in sim._pump_assignments:
            del sim._pump_assignments[2]
        return None

    async def _engineering_scram(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        own.reactor.scrammed = bool(data.get("scrammed", True))
        return None

    # ------------------------------------------------------------------
    # Station tasks
    # ------------------------------------------------------------------

    async def _station_task_start(self, data: Dict) -> Optional[str]:
        sim = self._sim
        station_name = str(data.get("station", "")).lower()
        if station_name not in sim._active_tasks:
            return "Unknown station"
        tasks = sim._active_tasks[station_name]
        if not tasks:
            now_s = time.perf_counter()
            sim._spawn_task_for(station_name, now_s)
            tasks = sim._active_tasks[station_name]
        task_id = str(data.get("task_id", "")).strip()
        if task_id:
            found = False
            for t in tasks:
                if t.id == task_id:
                    t.started = True
                    found = True
                else:
                    t.started = False
            if not found:
                return "Unknown task"
        else:
            stage_rank = {"task": 0, "failing": 1, "failed": 2}
            tasks.sort(key=lambda t: (-stage_rank.get(t.stage, 0), t.time_remaining_s))
            for i, t in enumerate(tasks):
                t.started = (i == 0)
        started_task = next((t for t in tasks if t.started), None)
        if started_task:
            sim._log_action(station_name.upper(), f"Started {started_task.title}", data)
        return None

    # ------------------------------------------------------------------
    # Captain
    # ------------------------------------------------------------------

    async def _captain_consent(self, data: Dict) -> Optional[str]:
        self._sim.set_captain_consent(bool(data.get("consent", False)))
        return None

    async def _captain_periscope(self, data: Dict) -> Optional[str]:
        self._sim._periscope_raised = bool(data.get("raised", True))
        return None

    async def _captain_radio(self, data: Dict) -> Optional[str]:
        self._sim._radio_raised = bool(data.get("raised", True))
        return None

    async def _captain_identify(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        designation = str(data.get("designation", ""))
        if not designation:
            return "No contact designation specified"
        if not sim._periscope_raised:
            return "Raise periscope to identify contacts"
        if own.kin.depth > 20.0:
            return "Too deep for visual identification"
        actual_id = sim._contact_registry.get_actual_id(designation)
        if not actual_id:
            return "Contact not found in registry"
        contact_visible = False
        for pc in sim._periscope_contacts:
            if pc.get("id") == designation and pc.get("status") == "visible":
                contact_visible = True
                break
        if not contact_visible:
            return "Contact not visible in periscope"
        try:
            actual_ship = sim.world.get_ship(actual_id)
            ship_class = getattr(actual_ship, "ship_class", None) or "Unknown Vessel"
            class_display = {
                "SSN": "Submarine", "Convoy": "Merchant Vessel",
                "Destroyer": "Destroyer", "Cruiser": "Cruiser", "Frigate": "Frigate",
            }.get(ship_class, ship_class)
            sim._contact_registry.identify_contact(designation, class_display)
            sim._log_action("CAPTAIN", f"Identified {designation} as {class_display}", data)
            return None
        except Exception as e:
            return f"Failed to identify contact: {e}"

    # ------------------------------------------------------------------
    # Plotting board (shared tactical map)
    # ------------------------------------------------------------------

    async def _plot_bearing_add(self, data: Dict) -> Optional[str]:
        sim = self._sim
        if not hasattr(sim, "_plot_board") or sim._plot_board is None:
            return "Plot board not available"
        own = sim.world.get_ship("ownship")
        anchor_x = float(data.get("anchor_x")) if data.get("anchor_x") is not None else (own.kin.x if own else 0.0)
        anchor_y = float(data.get("anchor_y")) if data.get("anchor_y") is not None else (own.kin.y if own else 0.0)
        try:
            bearing = float(data.get("bearing", 0.0))
        except Exception:
            return "Invalid bearing"
        sim._plot_board.add_bearing(
            anchor_x=anchor_x, anchor_y=anchor_y, bearing_deg=bearing,
            label=str(data.get("label", "") or ""),
            color=str(data.get("color", "#FACC15") or "#FACC15"),
        )
        return None

    async def _plot_bearing_remove(self, data: Dict) -> Optional[str]:
        sim = self._sim
        bid = str(data.get("id", "") or "")
        if not bid:
            return "No bearing id"
        sim._plot_board.remove_bearing(bid)
        return None

    async def _plot_contact_add(self, data: Dict) -> Optional[str]:
        sim = self._sim
        if not hasattr(sim, "_plot_board") or sim._plot_board is None:
            return "Plot board not available"
        try:
            x = float(data.get("x", 0.0))
            y = float(data.get("y", 0.0))
        except Exception:
            return "Invalid coordinates"
        sim._plot_board.add_contact(
            x=x, y=y,
            type_=str(data.get("type", "unknown") or "unknown"),
            heading_deg=float(data.get("heading_deg", 0.0) or 0.0),
            label=str(data.get("label", "") or ""),
        )
        return None

    async def _plot_contact_update(self, data: Dict) -> Optional[str]:
        sim = self._sim
        cid = str(data.get("id", "") or "")
        if not cid:
            return "No contact id"
        # Pass through whichever fields the client sent
        sim._plot_board.update_contact(
            cid,
            x=data.get("x"),
            y=data.get("y"),
            heading_deg=data.get("heading_deg"),
            type=data.get("type"),
            label=data.get("label"),
        )
        return None

    async def _plot_contact_remove(self, data: Dict) -> Optional[str]:
        sim = self._sim
        cid = str(data.get("id", "") or "")
        if not cid:
            return "No contact id"
        sim._plot_board.remove_contact(cid)
        return None

    async def _plot_note_append(self, data: Dict) -> Optional[str]:
        sim = self._sim
        text = str(data.get("text", "") or "").strip()
        if not text:
            return "Empty note"
        sim._plot_board.append_note(text)
        return None

    async def _plot_clear(self, data: Dict) -> Optional[str]:
        sim = self._sim
        sim._plot_board.clear()
        return None

    # ------------------------------------------------------------------
    # Debug
    # ------------------------------------------------------------------

    async def _debug_stop_mission(self, data: Dict) -> Optional[str]:
        """Stop the active mission: cancel all in-flight AI/LLM work, reset
        state, and return to idle. The server stays up for the next start."""
        await self._sim.stop_mission()
        return None

    async def _debug_restart(self, data: Dict) -> Optional[str]:
        sim = self._sim

        # Set loading flag to prevent tick() from seeing partial state
        sim._loading = True
        try:
            await sim._cancel_ai_tasks()
            from ..config import CONFIG, reload_from_env
            try:
                reload_from_env()
            except Exception:
                pass
            sim._force_default_reset = True
            if getattr(CONFIG, "use_ai_orchestrator", False):
                from ..sim.ai_orchestrator import AgentsOrchestrator
                sim._ai_orch = AgentsOrchestrator(lambda: sim.world, sim.engine, sim.run_id)
                try:
                    sim._ai_orch.set_fleet_engine(getattr(CONFIG, "ai_fleet_engine", "stub"), getattr(CONFIG, "ai_fleet_model", "stub"))
                    sim._ai_orch.set_ship_engine(getattr(CONFIG, "ai_ship_engine", "stub"), getattr(CONFIG, "ai_ship_model", "stub"))
                    if getattr(CONFIG, "ai_blue_fleet_enabled", True):
                        sim._ai_orch.set_blue_fleet_engine(
                            getattr(CONFIG, "ai_blue_fleet_engine", "stub"),
                            getattr(CONFIG, "ai_blue_fleet_model", "stub"),
                        )
                    try:
                        setattr(sim._ai_orch, "_mission_brief", sim.mission_brief)
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    import asyncio
                    hc = await sim._ai_orch.health_check()
                    sim._ai_recent_runs = (getattr(sim, "_ai_recent_runs", []) or []) + [{
                        "agent": "system",
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "tool_calls": [{"tool": "health_check", "arguments": hc}],
                    }]
                except Exception:
                    pass
            sim._init_default_world()
            return None
        finally:
            sim._loading = False

    async def _debug_maint_spawns(self, data: Dict) -> Optional[str]:
        enabled = bool(data.get("enabled", True))
        self._sim._suppress_maintenance_spawns = (not enabled)
        return None

    async def _debug_visual_player(self, data: Dict) -> Optional[str]:
        self._sim._debug_player_visual_100 = bool(data.get("enabled", False))
        return f"Player visual detection 100%: {'ON' if self._sim._debug_player_visual_100 else 'OFF'}"

    async def _debug_visual_enemy(self, data: Dict) -> Optional[str]:
        self._sim._debug_enemy_visual_100 = bool(data.get("enabled", False))
        return f"Enemy visual detection 100%: {'ON' if self._sim._debug_enemy_visual_100 else 'OFF'}"

    async def _debug_repair_all(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        if own:
            if own.damage:
                own.damage.hull = 0.0
                own.damage.flooding_rate = 0.0
            if hasattr(own, "maintenance") and hasattr(own.maintenance, "levels"):
                for system in own.maintenance.levels:
                    own.maintenance.levels[system] = 1.0
            if hasattr(own, "systems"):
                own.systems.rudder_ok = True
                own.systems.sonar_ok = True
                own.systems.tubes_ok = True
                own.systems.ballast_ok = True
                own.systems.planes_ok = True
                own.systems.radio_ok = True
                own.systems.periscope_ok = True
            for station_name in sim._active_tasks:
                sim._active_tasks[station_name].clear()
        return None

    async def _debug_surface_vessel(self, data: Dict) -> Optional[str]:
        sim = self._sim
        await sim._cancel_ai_tasks()
        sim._init_default_world()
        own = sim.world.get_ship("ownship")
        for ship in sim.world.all_ships():
            if ship.id != own.id and ship.side == "RED":
                ship.kin.x = 6000.0
                ship.kin.y = 0.0
                ship.kin.depth = 3.0
                ship.kin.heading = 90.0
                ship.kin.speed = 5.0
                ship.ship_class = "Convoy"
                if "Convoy" in SHIP_CATALOG:
                    ship.capabilities = SHIP_CATALOG["Convoy"].capabilities
                    ship.hull.max_speed = min(ship.hull.max_speed, SHIP_CATALOG["Convoy"].default_hull.max_speed)
                else:
                    ship.hull.max_speed = min(ship.hull.max_speed, 20.0)
                break
        sim.mission_brief = {
            "title": "Surface Vessel Intercept (Training)",
            "objective": "Escort convoy ship red-01 safely across sector; training shot optional.",
            "roe": ["Weapons release authorized for training shot.", "Minimize active sonar to preserve EMCON."],
            "target_wp": [100.0, 100.0],
            "comms_schedule": [{"at_s": 90.0, "msg": "INFO: Surface contact maintaining 5 kn on easterly course."}],
        }
        return None

    async def _debug_mission1(self, data: Dict) -> Optional[str]:
        sim = self._sim
        own = sim.world.get_ship("ownship")
        for ship in sim.world.all_ships():
            if ship.id != own.id and ship.side == "RED":
                ship.kin.x = 6000.0
                ship.kin.y = 0.0
                ship.kin.depth = 3.0
                ship.kin.heading = 90.0
                ship.kin.speed = 5.0
                break
        return None

    # ------------------------------------------------------------------
    # AI tool
    # ------------------------------------------------------------------

    async def _ai_tool(self, data: Dict) -> Optional[str]:
        sim = self._sim
        ship_id = str(data.get("ship_id", "red-01"))
        try:
            tgt = sim.world.get_ship(ship_id)
        except Exception:
            return "Unknown ship"
        tool = str(data.get("tool", "")).strip()
        args = data.get("arguments", {}) or {}
        caps = getattr(tgt, "capabilities", None)
        if tool == "set_nav":
            if caps and not caps.can_set_nav:
                return "Tool not supported"
            tgt.kin.heading = float(args.get("heading") or tgt.kin.heading) % 360.0
            tgt.kin.speed = max(0.0, float(args.get("speed") or tgt.kin.speed))
            tgt.kin.depth = max(0.0, min(tgt.hull.max_depth, float(args.get("depth") or tgt.kin.depth)))
            return None
        if tool == "fire_torpedo":
            if not caps or not caps.has_torpedoes:
                return "Tool not supported"
            return "Not implemented for non-ownship"
        if tool == "deploy_countermeasure":
            if not caps or not caps.countermeasures:
                return "Tool not supported"
            from .weapons import try_deploy_countermeasure
            from ..storage import insert_event
            cm_type = args.get("type", "noisemaker")
            if cm_type not in caps.countermeasures:
                return f"Countermeasure type '{cm_type}' not available"
            def _on_cm_event(name: str, payload: dict) -> None:
                insert_event(sim.engine, sim.run_id, name, json.dumps(payload))
            result = try_deploy_countermeasure(tgt, cm_type, on_event=_on_cm_event)
            if result.get("ok"):
                sim.world.countermeasures.append(result["data"])
                return None
            return result.get("error", "Deployment failed")
        if tool == "drop_depth_charges":
            if not caps or not getattr(caps, "has_depth_charges", False):
                return "Tool not supported"
            from .weapons import try_drop_depth_charges
            spread_val = args.get("spread_meters")
            if isinstance(spread_val, list):
                spread_val = spread_val[0] if spread_val else 20.0
            spread_m = float(spread_val if spread_val is not None else 20.0)
            min_d_val = args.get("minDepth")
            if isinstance(min_d_val, list):
                min_d_val = min_d_val[0] if min_d_val else 30.0
            min_d = float(min_d_val if min_d_val is not None else 30.0)
            max_d_val = args.get("maxDepth")
            if isinstance(max_d_val, list):
                max_d_val = max_d_val[0] if max_d_val else 50.0
            max_d = float(max_d_val if max_d_val is not None else 50.0)
            n_val = args.get("spreadSize")
            if isinstance(n_val, list):
                n_val = n_val[0] if n_val else 3
            n = int(float(n_val if n_val is not None else 3))
            res = try_drop_depth_charges(tgt, spread_m, min_d, max_d, n)
            if not res.get("ok"):
                return res.get("error", "Drop failed")
            for dc in res.get("data", []) or []:
                sim.world.depth_charges.append(dc)
            return None
        return "Unknown tool"
