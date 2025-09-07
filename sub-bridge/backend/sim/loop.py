from __future__ import annotations
import asyncio
import json
import time
import math
from typing import Dict, Optional
import os
import random
from ..bus import BUS
from ..config import CONFIG, reload_from_env
from ..models import Ship, Kinematics, Hull, Acoustics, WeaponsSuite, Reactor, DamageState, MaintenanceTask, SHIP_CATALOG, TelemetryContact
from ..assets import load_ship_catalog, load_mission_by_id, apply_mission_to_world
from ..storage import init_engine, create_run, insert_snapshot, insert_event
from .ecs import World
from .physics import integrate_kinematics
from .sonar import passive_contacts, ActivePingState, active_ping, passive_projectiles, explosion_contacts, normalize_angle_deg
from .weapons import try_load_tube, try_flood_tube, try_set_doors, try_fire, step_torpedo, step_tubes, try_drop_depth_charges, step_depth_charge, try_launch_torpedo_quick
from .ai_tools import LocalAIStub
from .ai_orchestrator import AgentsOrchestrator
from .damage import step_damage, step_engineering
from .noise import NoiseEngine


class Simulation:
    def __init__(self) -> None:
        self.dt = 1.0 / CONFIG.tick_hz
        self.world = World()
        self.active_ping_state = ActivePingState(cooldown_s=12.0)
        self.engine = init_engine(CONFIG.sqlite_path)
        self.run_id = create_run(self.engine)
        self.ai = LocalAIStub()
        self._captain_consent = False
        self._periscope_raised = False
        self._radio_raised = False
        self._pump_fwd = False
        self._pump_aft = False
        # Debug toggles for visual detection (disabled by default)
        self._debug_player_visual_100 = False
        self._debug_enemy_visual_100 = False
        # Visual contact tracking for persistence and improved re-detection
        self._visual_contacts = {}  # {observer_id: {target_id: {"last_seen": timestamp, "detection_count": int, "last_confidence": float}}}
        # Lazily initialize asyncio.Event to avoid requiring an event loop during tests
        self._stop: Optional[asyncio.Event] = None
        self._last_snapshot = 0.0
        self._last_ai = 0.0
        self._transient_events = []  # cleared every tick
        self._last_ping_responses = []
        self._last_ping_at = None
        # Load external assets
        try:
            # Import models here to avoid circular import issues
            from .. import models
            load_ship_catalog()
            # Ensure we have at least the basic ships if catalog loading failed
            if not models.SHIP_CATALOG:
                print("WARNING: Ship catalog is empty, using fallback definitions")
                # Add minimal fallback ships
                models.SHIP_CATALOG.update({
                    "SSN": models.ShipDef(
                        name="Nuclear Attack Submarine",
                        ship_class="SSN",
                        capabilities=models.ShipCapabilities(
                            can_set_nav=True, has_active_sonar=True, has_torpedoes=True,
                            has_guns=False, has_depth_charges=False, countermeasures=["noisemaker", "decoy"]
                        ),
                        default_hull=models.Hull(max_depth=300.0, max_speed=30.0, quiet_speed=5.0),
                        default_weapons=models.WeaponsSuite(),
                        default_acoustics=models.Acoustics(),
                    ),
                    "Destroyer": models.ShipDef(
                        name="Destroyer (ASW)",
                        ship_class="Destroyer", 
                        capabilities=models.ShipCapabilities(
                            can_set_nav=True, has_active_sonar=True, has_torpedoes=True,
                            has_guns=True, has_depth_charges=True, countermeasures=[]
                        ),
                        default_hull=models.Hull(max_depth=0.0, max_speed=32.0, quiet_speed=8.0),
                        default_weapons=models.WeaponsSuite(tube_count=2, torpedoes_stored=10, depth_charges_stored=30),
                        default_acoustics=models.Acoustics(thermocline_on=False, source_level_by_speed={5: 125.0, 15: 140.0, 25: 150.0}),
                    )
                })
        except Exception as e:
            print(f"ERROR: Failed to load ship catalog: {e}")
            pass
        self._init_default_world()
        # Station task state
        self._active_tasks: Dict[str, list[MaintenanceTask]] = {s: [] for s in ["helm", "sonar", "weapons", "engineering"]}
        self._task_spawn_timers: Dict[str, float] = {s: CONFIG.first_task_delay_s for s in ["helm", "sonar", "weapons", "engineering"]}
        # Noise aggregation engine
        self._noise = NoiseEngine()
        # Mission briefing and ROE (only set defaults if not set by mission assets)
        if not hasattr(self, "mission_brief"):
            self.mission_brief = {
                "title": "Patrol Box KILO-7",
                "objective": "Shadow contact RED-01, maintain undetected posture, do not fire unless fired upon.",
                "roe": [
                    "Weapons free upon hostile engagement or direct order.",
                    "Avoid active sonar unless necessary for navigation or identification.",
                    "Maintain EMCON; minimize mast raises."
                ],
                "comms_schedule": [
                    {"at_s": 120.0, "msg": "FLASH: New tasking window opens at 18:00Z. Await further instructions."},
                    {"at_s": 300.0, "msg": "INFO: Intel suggests RED-01 may alter course east within 10 minutes."}
                ],
            }
        self._delivered_comms_idx = -1
        # EMCON and storms
        self._emcon_high_timer = 0.0
        self._storm_timer = 0.0
        # Debug control: suppress spawning new maintenance tasks
        self._suppress_maintenance_spawns = False
        # AI orchestrator (optional)
        if CONFIG.use_ai_orchestrator:
            self._ai_orch = AgentsOrchestrator(lambda: self.world, self.engine, self.run_id)
            try:
                self._ai_orch.set_fleet_engine(getattr(CONFIG, "ai_fleet_engine", "stub"), getattr(CONFIG, "ai_fleet_model", "stub"))
                self._ai_orch.set_ship_engine(getattr(CONFIG, "ai_ship_engine", "stub"), getattr(CONFIG, "ai_ship_model", "stub"))
                # Provide mission brief for Fleet Commander inputs
                try:
                    setattr(self._ai_orch, "_mission_brief", self.mission_brief)
                except Exception:
                    pass
            except Exception:
                pass
            # Timers (sim-time based)
            # Prime timers so first runs occur quickly after startup
            self._ai_fleet_timer = getattr(CONFIG, "ai_fleet_cadence_s", 45.0)
            self._ai_ship_timers: Dict[str, float] = {}
            self._ai_pending: set[asyncio.Task] = set()

    def _init_default_world(self) -> None:
        # Clear existing world and set to original game state
        self.world = World()
        # Try to load mission from assets unless a forced default reset is requested
        mission = None
        if not getattr(self, "_force_default_reset", False):
            # During tests, ignore external mission selection to ensure deterministic defaults
            if os.getenv("PYTEST_CURRENT_TEST"):
                mission = None
            else:
                mid = getattr(CONFIG, "mission_id", "")
                mission = load_mission_by_id(mid) if mid else None
        if mission:
            def _set_mission(brief: dict) -> None:
                self.mission_brief = brief
            apply_mission_to_world(mission, lambda: self.world, _set_mission)
        else:
            own = Ship(
                id="ownship",
                side="BLUE",
                kin=Kinematics(depth=100.0, heading=270.0, speed=8.0),
                hull=SHIP_CATALOG["SSN"].default_hull.model_copy(deep=True),
                acoustics=SHIP_CATALOG["SSN"].default_acoustics.model_copy(deep=True),
                weapons=SHIP_CATALOG["SSN"].default_weapons.model_copy(deep=True),
                reactor=Reactor(output_mw=60.0, max_mw=100.0),
                damage=DamageState(),
                ship_class="SSN",
                capabilities=SHIP_CATALOG["SSN"].capabilities.model_copy(deep=True),
            )
            red = Ship(
                id="red-01",
                side="RED",
                kin=Kinematics(x=3000.0, y=0.0, depth=120.0, heading=90.0, speed=8.0),
                hull=Hull(max_speed=28.0),
                acoustics=Acoustics(),
                weapons=WeaponsSuite(),
                reactor=Reactor(output_mw=50.0, max_mw=100.0),
                damage=DamageState(),
                ai_profile=None,
                ship_class="SSN",
                capabilities=SHIP_CATALOG["SSN"].capabilities.model_copy(deep=True),
            )
            self.world.add_ship(own)
            self.world.add_ship(red)
        # Always clear one-shot forced reset flag
        if getattr(self, "_force_default_reset", False):
            self._force_default_reset = False
        # Set ordered state from ownship (from mission or default spawn)
        try:
            own_ref = self.world.get_ship("ownship")
        except Exception:
            # Fallback: first BLUE ship or first ship
            ships = self.world.all_ships()
            own_ref = next((s for s in ships if getattr(s, "side", "") == "BLUE"), ships[0])
        self.ordered = {"heading": own_ref.kin.heading, "speed": own_ref.kin.speed, "depth": own_ref.kin.depth}
        # Reset toggles and ping state
        self._pump_fwd = False
        self._pump_aft = False
        self._periscope_raised = False
        self._radio_raised = False
        self._captain_consent = False
        self._last_ping_responses = []
        self._last_ping_at = None
        self.active_ping_state = ActivePingState(cooldown_s=12.0)
        # Reset maintenance/task state and timers on restart
        if hasattr(self, "_active_tasks"):
            self._active_tasks = {s: [] for s in ["helm", "sonar", "weapons", "engineering"]}
        if hasattr(self, "_task_spawn_timers"):
            from ..config import CONFIG as _C
            self._task_spawn_timers = {s: _C.first_task_delay_s for s in ["helm", "sonar", "weapons", "engineering"]}
        # Reset storm/emcon timers
        if hasattr(self, "_emcon_high_timer"):
            self._emcon_high_timer = 0.0
        if hasattr(self, "_storm_timer"):
            self._storm_timer = 0.0

    def stop(self) -> None:
        if self._stop is None:
            try:
                self._stop = asyncio.Event()
            except Exception:
                # As a last resort, set a simple attribute; run loop will honor if already stopping
                self._stop = asyncio.Event()
        self._stop.set()

    def set_captain_consent(self, consent: bool) -> None:
        self._captain_consent = consent

    def _spawn_task_for(self, station: str, now_s: float) -> None:
        # Richer task catalog per station
        catalog = {
            "helm": [
                ("rudder", "helm.rudder.lube", "Rudder Lubricate"),
                ("rudder", "helm.rudder.linkage", "Rudder Linkage Adjust"),
                ("ballast", "helm.depth.sensor", "Depth/Pressure Sensor Recal"),
                ("ballast", "helm.pressure.sensor", "Hull Pressure Transducer Test"),
                ("ballast", "helm.salinity.sensor", "Salinity Sensor Clean"),
                ("ballast", "helm.temp.sensor", "Thermocline Temp Probe Cal"),
                ("rudder", "helm.gyro.align", "Gyro Alignment Check"),
                ("rudder", "helm.gps.sync", "GPS Time/Almanac Sync"),
                ("rudder", "helm.heading.encoder", "Heading Encoder Verify"),
                ("rudder", "helm.hydraulics.filter", "Hydraulics Filter Replace"),
            ],
            "sonar": [
                ("sonar", "sonar.hydro.cal", "Hydrophone Calibration"),
                ("sonar", "sonar.hydro.servo", "Hydrophone Servo Grease"),
                ("sonar", "sonar.passive.dsp", "Passive DSP Self-Test"),
                ("sonar", "sonar.ping.tx", "Ping Transmit Chain Test"),
                ("sonar", "sonar.ping.rx", "Ping Response Chain Test"),
                ("sonar", "sonar.preamp", "Preamp Gain Trim"),
                ("sonar", "sonar.array.cable", "Array Cable Continuity"),
                ("sonar", "sonar.cooling.loop", "Cooling Loop Flush"),
                ("sonar", "sonar.beamformer", "Beamformer Rebalance"),
                ("sonar", "sonar.clock", "ADC Clock Discipline"),
            ],
            "weapons": [
                ("tubes", "weap.tube.seal", "Tube Seal Inspection"),
                ("tubes", "weap.tube.purge", "Tube Purge Cycle"),
                ("tubes", "weap.tube.door", "Door Actuator Lube"),
                ("tubes", "weap.tube.bore", "Bore Clean & Inspect"),
                ("tubes", "weap.fire.ctrl", "Fire Control Align"),
                ("tubes", "weap.wire.handler", "Wire Guide Service"),
                ("tubes", "weap.gyros.spinup", "Gyro Spinup Test"),
                ("tubes", "weap.seeker.bench", "Seeker Bench Check"),
                ("tubes", "weap.power.bus", "Weapons Bus Check"),
                ("tubes", "weap.cooling.pump", "Cooling Pump Service"),
            ],
            "engineering": [
                ("ballast", "eng.ballast.valve", "Ballast Valve Service"),
                ("ballast", "eng.pump.impeller", "Pump Impeller Inspect"),
                ("ballast", "eng.scrubber", "Air Scrubber Replace"),
                ("ballast", "eng.heat.xchg", "Heat Exchanger Clean"),
                ("ballast", "eng.reactor.coolant", "Coolant Chemistry Check"),
                ("ballast", "eng.generator", "Generator Bearing Lube"),
                ("ballast", "eng.battery.cell", "Battery Cell Test"),
                ("ballast", "eng.hvac.filter", "HVAC Filter Replace"),
                ("ballast", "eng.busbars", "Busbar Tightening"),
                ("ballast", "eng.pipe.leak", "Pipe Leak Inspection"),
            ],
        }
        choices = catalog.get(station, [("rudder", "misc.task", "Maintenance")])
        system, key, title = random.choice(choices)
        base_deadline = random.uniform(25.0, 45.0)
        tid = f"{station}-{int(now_s*1000)%100000}-{random.randint(100,999)}"
        self._active_tasks[station].append(MaintenanceTask(
            id=tid, station=station, system=system, key=key, title=title,
            stage="task", progress=0.0, base_deadline_s=base_deadline, time_remaining_s=base_deadline, created_at=now_s
        ))

    def _station_power_fraction(self, ship: Ship, station: str) -> float:
        p = ship.power
        return max(0.0, min(1.0, getattr(p, station if station != "engineering" else "engineering")))

    def _apply_stage_penalties(self, ship: Ship, station: str, stage: str) -> None:
        # Apply degradation effects per station and stage
        if station == "helm":
            # Map new stages: task (no penalty), failing (moderate), failed (worst)
            factor = {"task": 1.0, "failing": 0.7, "failed": 0.0}.get(stage, 1.0)
            ship.hull.turn_rate_max = 7.0 * factor
            if stage == "failed":
                ship.systems.rudder_ok = False
            # Additional helm-related effects keyed to degraded states
            if stage in ("failing", "failed"):
                ship.acoustics.thermocline_on = True
        elif station == "sonar":
            extra = {"task": 0.0, "failing": 3.0, "failed": 12.0}.get(stage, 0.0)
            ship.acoustics.bearing_noise_extra = extra
            if stage == "failed":
                ship.systems.sonar_ok = False
            if stage in ("failing", "failed"):
                ship.acoustics.passive_snr_penalty_db = 3.0 if stage == "failing" else 10.0
                ship.acoustics.active_range_noise_add_m = 50.0 if stage == "failing" else 250.0
                ship.acoustics.active_bearing_noise_extra = 0.5 if stage == "failing" else 3.0
        elif station == "weapons":
            mult = {"task": 1.0, "failing": 1.4, "failed": 2.5}.get(stage, 1.0)
            ship.weapons.time_penalty_multiplier = mult
            if stage == "failed":
                ship.systems.tubes_ok = False
        elif station == "engineering":
            # Engineering failed: ballast system effectively unavailable
            if stage == "failed":
                ship.systems.ballast_ok = False

    def _recompute_penalties_from_tasks(self, ship: Ship) -> None:
        """Aggregate active task stages per station and apply the worst effect.

        This prevents a completed task from resetting penalties while other degraded/failed
        tasks remain for the same station.
        """
        # Determine worst stage order for new model
        stage_rank = {"task": 0, "failing": 1, "failed": 2}
        # Default to normal for all stations
        worst_by_station = {s: "task" for s in ["helm", "sonar", "weapons", "engineering"]}
        for station, tasks in self._active_tasks.items():
            if not tasks:
                continue
            worst = "task"
            for t in tasks:
                if stage_rank[t.stage] > stage_rank[worst]:
                    worst = t.stage
            worst_by_station[station] = worst
        # Apply aggregated penalties
        for station, stage in worst_by_station.items():
            self._apply_stage_penalties(ship, station, stage)

    def _step_station_tasks(self, ship: Ship, dt: float) -> None:
        now_s = time.perf_counter()
        # Spawn logic per station (allow multiple concurrent tasks)
        for station in self._active_tasks.keys():
            self._task_spawn_timers[station] -= dt
            if self._task_spawn_timers[station] <= 0.0:
                if not self._suppress_maintenance_spawns:
                    self._spawn_task_for(station, now_s)
                base = random.uniform(60.0, 120.0)
                self._task_spawn_timers[station] = base / max(0.2, CONFIG.maint_spawn_scale)

        # Progress all active tasks based on power allocation for that station
        for station, tasks in self._active_tasks.items():
            if not tasks:
                continue
            power_frac = self._station_power_fraction(ship, station)
            for task in list(tasks):
                task.time_remaining_s = max(0.0, task.time_remaining_s - dt)
                if task.started:
                    task.progress = min(1.0, task.progress + (0.2 * power_frac) * dt)
                if task.progress >= 1.0:
                    ship.maintenance.levels[task.system] = min(1.0, ship.maintenance.levels.get(task.system, 1.0) + 0.1)
                    tasks.remove(task)
                    continue
                if task.time_remaining_s <= 0.0:
                    # New escalation: task -> failing -> failed
                    if task.stage == "task":
                        task.stage = "failing"
                        task.base_deadline_s *= 1.25
                        task.time_remaining_s = task.base_deadline_s
                        ship.maintenance.levels[task.system] = max(0.0, ship.maintenance.levels.get(task.system, 1.0) - 0.05)
                    elif task.stage == "failing":
                        task.stage = "failed"
                        # No further timeout; keep at zero
                        ship.maintenance.levels[task.system] = max(0.0, ship.maintenance.levels.get(task.system, 1.0) - 0.1)
        # After all updates, recompute aggregated penalties for accuracy
        self._recompute_penalties_from_tasks(ship)

    async def run(self) -> None:
        if self._stop is None:
            self._stop = asyncio.Event()
        last = time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            elapsed = now - last
            if elapsed < self.dt:
                await asyncio.sleep(self.dt - elapsed)
                continue
            last = now
            await self.tick(self.dt)

    async def tick(self, dt: float) -> None:
        own = self.world.get_ship("ownship")

        if CONFIG.use_ai_orchestrator:
            # Advance orchestrator timers
            self._ai_fleet_timer += dt
            # Default cadences (configurable)
            fleet_cadence = getattr(CONFIG, "ai_fleet_cadence_s", 45.0)
            fleet_alert_cadence = getattr(CONFIG, "ai_fleet_alert_cadence_s", 20.0)
            fleet_trigger_conf = getattr(CONFIG, "ai_fleet_trigger_conf_threshold", 0.7)
            ship_normal_cadence = getattr(CONFIG, "ai_ship_cadence_s", 20.0)
            ship_alert_cadence = getattr(CONFIG, "ai_ship_alert_cadence_s", 10.0)
            # Contact-confidence trigger: if any RED ship first crosses confidence >= threshold on any contact, trigger immediate fleet run and switch to alert cadence
            try:
                if not hasattr(self, "_fleet_conf_tripped"):
                    self._fleet_conf_tripped = False  # type: ignore[attr-defined]
                if not hasattr(self, "_fleet_last_alert_time"):
                    self._fleet_last_alert_time = 0.0  # type: ignore[attr-defined]
                trigger_now = False
                if isinstance(fleet_trigger_conf, (int, float)) and fleet_trigger_conf > 0.0:
                    for s in self.world.all_ships():
                        if s.side != "RED":
                            continue
                        # Build local contacts as orchestrator does; use passive_contacts for bearing-only confidence
                        contacts = passive_contacts(s, [x for x in self.world.all_ships() if x.side != s.side and x.id != s.id])
                        for c in contacts:
                            conf = float(getattr(c, "confidence", 0.0))
                            # Consider a first-time crossing per ship to avoid repeated triggers
                            key = f"_{s.id}_conf_crossed"
                            if conf >= float(fleet_trigger_conf) and not getattr(self, key, False):
                                setattr(self, key, True)
                                trigger_now = True
                                break
                        if trigger_now:
                            break
                # Fleet cadence selection: alert cadence while in tripped state
                chosen_fleet_cadence = fleet_alert_cadence if trigger_now or getattr(self, "_fleet_conf_tripped", False) else fleet_cadence
                if trigger_now:
                    self._fleet_conf_tripped = True  # type: ignore[attr-defined]
                # Schedule fleet run
                do_run_fleet = (self._ai_fleet_timer >= chosen_fleet_cadence) or trigger_now
            except Exception:
                do_run_fleet = (self._ai_fleet_timer >= fleet_cadence)
            if do_run_fleet:
                self._ai_fleet_timer = 0.0
                # Mirror current mission brief to orchestrator before the run
                try:
                    if hasattr(self, "_ai_orch") and getattr(self, "_ai_orch", None) is not None:
                        setattr(self._ai_orch, "_mission_brief", self.mission_brief)
                except Exception:
                    pass
                async def _fleet_job():
                    res = await self._ai_orch.run_fleet()
                    # Persist tool calls for trace
                    for tc in res.get("tool_calls_validated", []):
                        insert_event(self.engine, self.run_id, "ai.tool.fleet", json.dumps(tc))
                        # Update world-level FleetIntent for UI and ship guidance
                        if tc.get("tool") == "set_fleet_intent":
                            self._fleet_intent = tc.get("arguments", {})
                            # Also attach to orchestrator so ship summaries can see guidance slice
                            try:
                                setattr(self._ai_orch, "_last_fleet_intent", self._fleet_intent)
                            except Exception:
                                pass
                            # Record concise history entry for Fleet Commander context
                            try:
                                if not hasattr(self, "_fleet_intent_history"):
                                    self._fleet_intent_history = []  # type: ignore[attr-defined]
                                intent = dict(self._fleet_intent or {})
                                # Compute short hash of intent body
                                import hashlib as _hashlib, json as _json
                                ihash = ""
                                try:
                                    ihash = _hashlib.sha1(_json.dumps(intent, sort_keys=True).encode()).hexdigest()[:8]
                                except Exception:
                                    ihash = ""
                                # Brief per-ship destinations
                                obj = intent.get("objectives", {}) or {}
                                brief = {}
                                if isinstance(obj, dict):
                                    for sid, val in obj.items():
                                        if isinstance(val, dict) and "destination" in val:
                                            brief[sid] = {"destination": val.get("destination")}
                                hist_entry = {
                                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "hash": ihash,
                                    "summary": intent.get("summary", ""),
                                    "objectives_brief": brief,
                                    "engagement_rules": intent.get("engagement_rules", {}),
                                }
                                self._fleet_intent_history.append(hist_entry)  # type: ignore[attr-defined]
                                # Keep last 8
                                self._fleet_intent_history = self._fleet_intent_history[-8:]  # type: ignore[attr-defined]
                                # Mirror to orchestrator for Fleet Commander prompt
                                try:
                                    setattr(self._ai_orch, "_fleet_intent_history", list(self._fleet_intent_history))
                                except Exception:
                                    pass
                            except Exception:
                                pass
                    # Mirror recent runs into sim for Fleet UI; drop alert after one alert cadence window with no additional triggers
                    self._ai_recent_runs = getattr(self._ai_orch, "_recent_runs", [])
                    try:
                        if getattr(self, "_fleet_conf_tripped", False):
                            # If no trigger occurs for one alert cadence window, reset to normal cadence
                            self._fleet_last_alert_time = 0.0
                    except Exception:
                        pass
                t = asyncio.create_task(_fleet_job())
                self._ai_pending.add(t)
                t.add_done_callback(lambda _t: self._ai_pending.discard(_t))
            # Schedule per-ship runs (RED side only)
            for ship in self.world.all_ships():
                if ship.side != "RED":
                    continue
                sid = ship.id
                if sid not in self._ai_ship_timers:
                    # Trigger initial run quickly after startup for each ship
                    self._ai_ship_timers[sid] = ship_normal_cadence
                self._ai_ship_timers[sid] += dt
                # Detection-aware cadence (non-leaking heuristic):
                # - If ownship recently active pinged (cooldown active), enemies go alert
                # - If close to noisy ownship while EMCON alert is high
                dx = ship.kin.x - own.kin.x
                dy = ship.kin.y - own.kin.y
                dist_m = (dx * dx + dy * dy) ** 0.5
                recent_active_ping = self.active_ping_state.timer > 0.0
                close_and_noisy = (dist_m <= 7000.0) and (self._emcon_high_timer >= 10.0)
                is_alert = recent_active_ping or close_and_noisy
                cadence = ship_alert_cadence if is_alert else ship_normal_cadence
                if self._ai_ship_timers[sid] >= cadence:
                    self._ai_ship_timers[sid] = 0.0
                    async def _ship_job(_sid: str = sid):
                        res = await self._ai_orch.run_ship(_sid)
                        # Apply first validated tool call
                        for tc in res.get("tool_calls_validated", []):
                            tool = tc.get("tool")
                            args = tc.get("arguments", {})
                            if tool == "set_nav":
                                try:
                                    tgt = self.world.get_ship(_sid)
                                except Exception:
                                    continue
                                tgt.kin.heading = float(args.get("heading") or tgt.kin.heading) % 360.0
                                # Clamp against platform limits
                                spd = float(args.get("speed") or tgt.kin.speed)
                                dpt = float(args.get("depth") or tgt.kin.depth)
                                tgt.kin.speed = max(0.0, min(tgt.hull.max_speed, spd))
                                tgt.kin.depth = max(0.0, min(tgt.hull.max_depth, dpt))
                                insert_event(self.engine, self.run_id, "ai.tool.apply", json.dumps({"ship_id": _sid, **tc}))
                                # Record last orders per ship for agent continuity
                                try:
                                    if hasattr(self, "_ai_orch") and getattr(self, "_ai_orch", None) is not None:
                                        if not hasattr(self._ai_orch, "_orders_last_by_ship"):
                                            self._ai_orch._orders_last_by_ship = {}
                                        self._ai_orch._orders_last_by_ship[_sid] = {
                                            "heading": float(tgt.kin.heading),
                                            "speed": float(tgt.kin.speed),
                                            "depth": float(tgt.kin.depth),
                                        }
                                except Exception:
                                    pass
                            if tool == "fire_torpedo":
                                # Map standard fire to quick launch for AI ships
                                try:
                                    tgt = self.world.get_ship(_sid)
                                except Exception:
                                    break
                                if not getattr(getattr(tgt, "capabilities", None), "has_torpedoes", False):
                                    break
                                # Handle potential list values and None values
                                bearing_val = args.get("bearing")
                                if isinstance(bearing_val, list) and len(bearing_val) > 0:
                                    bearing_val = bearing_val[0]
                                bearing = float(bearing_val or tgt.kin.heading)
                                
                                run_depth_val = args.get("run_depth")
                                if isinstance(run_depth_val, list) and len(run_depth_val) > 0:
                                    run_depth_val = run_depth_val[0]
                                run_depth = float(run_depth_val or tgt.kin.depth)
                                
                                enable_range_val = args.get("enable_range")
                                if isinstance(enable_range_val, list) and len(enable_range_val) > 0:
                                    enable_range_val = enable_range_val[0]
                                enable_range = float(enable_range_val or 800.0) if enable_range_val is not None else None
                                doctrine = str(args.get("doctrine") or "passive_then_active")
                                res = try_launch_torpedo_quick(tgt, bearing, run_depth, enable_range, doctrine)
                                if res.get("ok"):
                                    torp = res.get("data")
                                    if torp:
                                        self.world.torpedoes.append(torp)
                                        insert_event(self.engine, self.run_id, "ai.tool.apply", json.dumps({"ship_id": _sid, **tc}))
                            # Other tools (server-applied)
                            if tool == "drop_depth_charges":
                                try:
                                    tgt = self.world.get_ship(_sid)
                                except Exception:
                                    break
                                if not getattr(getattr(tgt, "capabilities", None), "has_depth_charges", False):
                                    break
                                # Handle potential list values and None values
                                spread_val = args.get("spread_meters")
                                if isinstance(spread_val, list) and len(spread_val) > 0:
                                    spread_val = spread_val[0]
                                spread_m = int(float(spread_val or 20))
                                
                                min_d_val = args.get("minDepth")
                                if isinstance(min_d_val, list) and len(min_d_val) > 0:
                                    min_d_val = min_d_val[0]
                                min_d = int(float(min_d_val or 30))
                                
                                max_d_val = args.get("maxDepth")
                                if isinstance(max_d_val, list) and len(max_d_val) > 0:
                                    max_d_val = max_d_val[0]
                                max_d = int(float(max_d_val or 50))
                                
                                n_val = args.get("spreadSize")
                                if isinstance(n_val, list) and len(n_val) > 0:
                                    n_val = n_val[0]
                                n = int(float(n_val or 3))
                                res = try_drop_depth_charges(tgt, spread_m, min_d, max_d, n)
                                if res.get("ok"):
                                    for dc in res.get("data", []) or []:
                                        self.world.depth_charges.append(dc)
                                    insert_event(self.engine, self.run_id, "ai.tool.apply", json.dumps({"ship_id": _sid, **tc}))
                            if tool == "launch_torpedo_quick":
                                try:
                                    tgt = self.world.get_ship(_sid)
                                except Exception:
                                    break
                                if not getattr(getattr(tgt, "capabilities", None), "has_torpedoes", False):
                                    break
                                # Handle potential list values and None values
                                bearing_val = args.get("bearing")
                                if isinstance(bearing_val, list) and len(bearing_val) > 0:
                                    bearing_val = bearing_val[0]
                                bearing = float(bearing_val or tgt.kin.heading)
                                
                                run_depth_val = args.get("run_depth")
                                if isinstance(run_depth_val, list) and len(run_depth_val) > 0:
                                    run_depth_val = run_depth_val[0]
                                run_depth = float(run_depth_val or tgt.kin.depth)
                                
                                enable_range_val = args.get("enable_range")
                                if isinstance(enable_range_val, list) and len(enable_range_val) > 0:
                                    enable_range_val = enable_range_val[0]
                                enable_range = float(enable_range_val or 800.0) if enable_range_val is not None else None
                                doctrine = str(args.get("doctrine", "passive_then_active"))
                                res = try_launch_torpedo_quick(tgt, bearing, run_depth, enable_range, doctrine)
                                if res.get("ok"):
                                    torp = res.get("data")
                                    if torp:
                                        self.world.torpedoes.append(torp)
                                        insert_event(self.engine, self.run_id, "ai.tool.apply", json.dumps({"ship_id": _sid, **tc}))
                            if tool == "active_ping":
                                try:
                                    tgt = self.world.get_ship(_sid)
                                except Exception:
                                    break
                                if not getattr(getattr(tgt, "capabilities", None), "has_active_sonar", False):
                                    break
                                # Check cooldown
                                if tgt.active_sonar_cooldown > 0.0:
                                    break
                                # Perform active ping
                                res = active_ping(tgt, [s for s in self.world.all_ships() if s.id != tgt.id])
                                if res:
                                    # Set cooldown (12 seconds)
                                    tgt.active_sonar_cooldown = 12.0
                                    # Store ping responses for this ship (for AI use)
                                    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                                    ping_responses = [
                                        {"id": rid, "bearing": brg, "range_est": rng, "strength": st, "at": now_iso, "source": tgt.id}
                                        for (rid, rng, brg, st) in res
                                    ]
                                    # Store in ship-specific ping responses
                                    if not hasattr(self, "_ai_ping_responses"):
                                        self._ai_ping_responses = {}
                                    self._ai_ping_responses[tgt.id] = ping_responses
                                    
                                    # Create contact for player when enemy ship pings (counter-detection)
                                    own = self.world.get_ship("ownship")
                                    if own and own.id != tgt.id:
                                        dx = tgt.kin.x - own.kin.x
                                        dy = tgt.kin.y - own.kin.y
                                        rng = math.hypot(dx, dy)
                                        # Compass bearing: 0=N, 90=E, 180=S, 270=W
                                        brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
                                        # Add bearing noise for realism
                                        brg_noise = normalize_angle_deg(brg + random.gauss(0, 2.0))
                                        # Signal strength based on distance and enemy ship type
                                        strength = max(0.0, min(1.0, 1.0 / (1.0 + (rng / 3000.0))))
                                        
                                        # Create contact for enemy active ping
                                        enemy_ping_contact = TelemetryContact(
                                            id="ENEMY_ACTIVE_SONAR",
                                            bearing=brg_noise,
                                            strength=strength,
                                            classifiedAs="ENEMY_ACTIVE_SONAR",
                                            confidence=0.9,
                                            bearingKnown=True,
                                            rangeKnown=False
                                        )
                                        
                                        # Store in enemy ship contacts for this tick
                                        if not hasattr(self, "_enemy_ping_contacts"):
                                            self._enemy_ping_contacts = []
                                        self._enemy_ping_contacts.append({
                                            "contact": enemy_ping_contact,
                                            "timestamp": now_iso,
                                            "timestamp_epoch": time.time(),
                                            "source": tgt.id
                                        })
                                    
                                    # Add counter-detection event
                                    self._transient_events.append({"type": "counterDetected", "at": now_iso, "source": tgt.id})
                                    insert_event(self.engine, self.run_id, "ai.tool.apply", json.dumps({"ship_id": _sid, **tc}))
                            break
                        # Mirror recent runs into sim for Fleet UI
                        self._ai_recent_runs = getattr(self._ai_orch, "_recent_runs", [])
                    t = asyncio.create_task(_ship_job())
                    self._ai_pending.add(t)
                    t.add_done_callback(lambda _t: self._ai_pending.discard(_t))
        elif CONFIG.use_enemy_ai:
            self._last_ai += dt
            if self._last_ai >= CONFIG.ai_poll_s:
                self._last_ai = 0.0
                for ship in self.world.all_ships():
                    if ship.side == "RED":
                        tool = self.ai.propose_orders(ship)
                        insert_event(self.engine, self.run_id, "ai_tool", json.dumps(tool))
                        args = tool.get("arguments", {})
                        ship.kin.heading = args.get("heading", ship.kin.heading)
                        ship.kin.speed = args.get("speed", ship.kin.speed)
                        ship.kin.depth = args.get("depth", ship.kin.depth)

        ballast_boost = self._pump_fwd or self._pump_aft
        cav, heading, speed, depth = integrate_kinematics(
            own,
            self.ordered["heading"],
            self.ordered["speed"],
            self.ordered["depth"],
            dt,
            ballast_boost=ballast_boost,
        )
        # Cavitation impulse (helm)
        if cav:
            self._noise.add_impulse("helm", 82.0, 0.5)

        # Step weapon timers/cooldowns (tubes and depth charge cooldowns) for all ships
        for s in self.world.all_ships():
            step_tubes(s, dt)

        for ship in self.world.all_ships():
            if ship.id == "ownship":
                continue
            # Allow enemy movement when orchestrator is enabled regardless of ENEMY_STATIC
            if CONFIG.enemy_static and not CONFIG.use_ai_orchestrator:
                continue
            integrate_kinematics(ship, ship.kin.heading, ship.kin.speed, ship.kin.depth, dt)

        if self.world.torpedoes:
            for t in list(self.world.torpedoes):
                def _on_event(name: str, payload: dict) -> None:
                    insert_event(self.engine, self.run_id, name, json.dumps(payload))
                step_torpedo(t, self.world, dt, on_event=_on_event)
                if t["run_time"] > t["max_run_time"]:
                    self.world.torpedoes.remove(t)

        # Step depth charges
        if getattr(self.world, "depth_charges", None):
            for dc in list(self.world.depth_charges):
                def _on_dc_event(name: str, payload: dict) -> None:
                    insert_event(self.engine, self.run_id, name, json.dumps(payload))
                    if name == "depth_charge.detonated":
                        try:
                            tx = float(payload.get("x", 0.0)); ty = float(payload.get("y", 0.0))
                            dx = tx - own.kin.x; dy = ty - own.kin.y
                            brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
                            if not hasattr(self, "_sonar_explosions"):
                                self._sonar_explosions = []
                            self._sonar_explosions.append({"bearing": float(brg_true), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                            self._sonar_explosions = self._sonar_explosions[-12:]
                        except Exception:
                            pass
                step_depth_charge(dc, self.world, dt, on_event=_on_dc_event)
                if dc.get("exploded", False) or dc.get("depth", 0.0) > 1000.0:
                    self.world.depth_charges.remove(dc)

        pump_effect = 2.0 if (self._pump_fwd or self._pump_aft) else 0.0
        step_damage(own, dt, pump_effect=pump_effect)
        step_engineering(own, dt)

        # Station maintenance/repair tasks lifecycle
        self._step_station_tasks(own, dt)

        self.active_ping_state.tick(dt)
        
        # Tick AI ship active sonar cooldowns
        for ship in self.world.all_ships():
            if ship.side != "ownship" and ship.active_sonar_cooldown > 0.0:
                ship.active_sonar_cooldown = max(0.0, ship.active_sonar_cooldown - dt)
        
        # Acoustic noise budget and detectability
        noise_from_speed = min(100.0, (speed / max(1.0, own.hull.max_speed)) * 70.0)
        noise_cav = 30.0 if cav else 0.0
        noise_pumps = 10.0 if (self._pump_fwd or self._pump_aft) else 0.0
        noise_masts = (10.0 if self._periscope_raised else 0.0) + (10.0 if self._radio_raised else 0.0)
        noise_budget = max(0.0, min(100.0, noise_from_speed + noise_cav + noise_pumps + noise_masts))
        
        # Compute per-station noise dB for UI and dynamic source level
        noise_levels = self._noise.tick(own, self.world, dt, self)
        
        # Update submarine's dynamic source level based on comprehensive noise budget
        # Convert noise budget (0-100) to dB source level (110-140 dB range)
        # Base source level from speed, then add noise contributions
        base_src_lvl = 110.0 + (speed / max(1.0, own.hull.max_speed)) * 20.0  # 110-130 dB range
        noise_contributions = noise_cav + noise_pumps + noise_masts  # Additional noise from operations
        
        # Add per-station noise contributions to source level
        # Convert dB noise levels to source level contributions (scale factor 0.1)
        station_noise_contrib = 0.0
        for station, db_level in noise_levels.items():
            if station != "total" and db_level > 0:
                station_noise_contrib += db_level * 0.1  # Scale station noise to source level
        
        dynamic_src_lvl = base_src_lvl + (noise_contributions * 0.3) + station_noise_contrib
        
        # Update submarine's acoustics with dynamic source level
        # This makes the submarine more detectable to enemies based on its noise
        own.acoustics.source_level_by_speed = {
            int(speed): dynamic_src_lvl
        }
        
        # EMCON pressure: sustained high noise raises alert
        if noise_budget >= 60.0:
            self._emcon_high_timer = min(30.0, self._emcon_high_timer + dt)
        else:
            self._emcon_high_timer = max(0.0, self._emcon_high_timer - dt)
        emcon_alert = self._emcon_high_timer >= 10.0
        detectability = noise_budget / 100.0
        contacts = passive_contacts(own, [s for s in self.world.all_ships() if s.id != own.id])
        # Build simple alert map for RED ships for orchestrator visibility
        try:
            if hasattr(self, "_ai_orch") and getattr(self, "_ai_orch", None) is not None:
                alert_map = {}
                for ship in self.world.all_ships():
                    if ship.side != "RED":
                        continue
                    dx = ship.kin.x - own.kin.x
                    dy = ship.kin.y - own.kin.y
                    dist_m = (dx * dx + dy * dy) ** 0.5
                    recent_active_ping = self.active_ping_state.timer > 0.0
                    close_and_noisy = (dist_m <= 7000.0) and (self._emcon_high_timer >= 10.0)
                    alert_map[ship.id] = bool(recent_active_ping or close_and_noisy)
                setattr(self._ai_orch, "_ship_alert_map", alert_map)
        except Exception:
            pass

        # Build visual detection map for enemy ships to detect surface vessels
        try:
            if hasattr(self, "_ai_orch") and getattr(self, "_ai_orch", None) is not None:
                visual_detection_map = {}
                
                # Initialize enemy search timers if not exists
                if not hasattr(self, "_enemy_search_timers"):
                    self._enemy_search_timers = {}
                
                # For each enemy ship, check if they can visually detect other ships
                for observer in self.world.all_ships():
                    if observer.side != "RED":  # Only enemy ships can visually detect
                        continue
                    
                    # Initialize search timer for this observer
                    if observer.id not in self._enemy_search_timers:
                        self._enemy_search_timers[observer.id] = 0.0
                    
                    # Update search timer (re-roll detection every 5 seconds, same as periscope)
                    self._enemy_search_timers[observer.id] += dt
                    search_interval = 5.0  # 5 seconds between search attempts
                    
                    # Only perform detection check every 5 seconds to simulate realistic visual scanning
                    if self._enemy_search_timers[observer.id] >= search_interval:
                        self._enemy_search_timers[observer.id] = 0.0  # Reset timer
                        
                        observer_detections = {}
                        
                        # Check detection of all other ships
                        for target in self.world.all_ships():
                            if target.id == observer.id:
                                continue
                            
                            # Calculate distance and check if target is within visual range
                            dx = target.kin.x - observer.kin.x
                            dy = target.kin.y - observer.kin.y
                            dist_m = math.hypot(dx, dy)
                            
                            # Visual detection range: 15km (same as periscope)
                            if dist_m > 15000.0:
                                continue
                            
                            # Visual detection conditions:
                            # 1. Target must be at or near surface (≤5m depth, same as periscope detection)
                            # 2. Observer must be at surface or shallow depth (≤10m depth for surface ships)
                            
                            target_surface = target.kin.depth <= 5.0
                            observer_surface = observer.kin.depth <= 10.0
                            
                            if target_surface and observer_surface:
                                # Initialize visual contact tracking for this observer
                                if observer.id not in self._visual_contacts:
                                    self._visual_contacts[observer.id] = {}
                                
                                # Check if we have previous contact history for this target
                                contact_history = self._visual_contacts[observer.id].get(target.id, {})
                                last_seen = contact_history.get("last_seen", 0.0)
                                detection_count = contact_history.get("detection_count", 0)
                                last_confidence = contact_history.get("last_confidence", 0.0)
                                
                                # Calculate detection probability based on distance and conditions
                                if self._debug_enemy_visual_100:
                                    # Debug mode: 100% detection
                                    detection_prob = 1.0
                                    detection_roll = 0.0  # Always pass
                                else:
                                    # Normal mode: probabilistic detection
                                    # Base detection probability decreases with distance
                                    base_prob = max(0.0, 1.0 - (dist_m / 15000.0))
                                    
                                    # Surface vessels are easier to detect than submarines
                                    if target.ship_class == "Convoy":
                                        detection_prob = base_prob * 1.3  # 30% easier to detect
                                    elif target.ship_class == "Destroyer":
                                        detection_prob = base_prob * 1.1  # 10% easier to detect
                                    else:
                                        detection_prob = base_prob
                                    
                                    # Improve detection probability for previously spotted targets
                                    if detection_count > 0:
                                        # Increase probability by 20% per previous detection (capped at 50% bonus)
                                        memory_bonus = min(0.5, detection_count * 0.2)
                                        detection_prob = min(0.95, detection_prob + memory_bonus)
                                    
                                    # Cap at 95% maximum detection probability (never perfect, but very high for known targets)
                                    detection_prob = min(0.95, detection_prob)
                                    
                                    # Roll for detection
                                    detection_roll = random.random()
                                
                                # Check for detection
                                detected_this_cycle = detection_roll < detection_prob
                                
                                # If we detect the target OR we have recent contact history, include it
                                current_time = time.time()
                                time_since_last_seen = current_time - last_seen
                                
                                # Include contact if:
                                # 1. We detected it this cycle, OR
                                # 2. We saw it recently (within 30 seconds) and it's still in range
                                if detected_this_cycle or (detection_count > 0 and time_since_last_seen <= 30.0 and dist_m <= 15000.0):
                                    # Determine detection mode based on target depth
                                    if target.kin.depth <= 1.0:
                                        mode = "surface"  # Fully surfaced
                                    else:
                                        mode = "periscope"  # Periscope depth
                                    
                                    # Update contact history if we detected it this cycle
                                    if detected_this_cycle:
                                        self._visual_contacts[observer.id][target.id] = {
                                            "last_seen": current_time,
                                            "detection_count": detection_count + 1,
                                            "last_confidence": detection_prob
                                        }
                                    
                                    # Use current detection confidence or last known confidence
                                    current_confidence = detection_prob if detected_this_cycle else last_confidence
                                    
                                    observer_detections[target.id] = {
                                        "detected": detected_this_cycle,
                                        "mode": mode,
                                        "distance": dist_m,
                                        "confidence": current_confidence,
                                        "last_seen": last_seen if not detected_this_cycle else current_time,
                                        "detection_count": detection_count + (1 if detected_this_cycle else 0)
                                    }
                        
                        if observer_detections:
                            visual_detection_map[observer.id] = observer_detections
                
                setattr(self._ai_orch, "_visual_detection_map", visual_detection_map)
                
                # Clean up old visual contacts (older than 2 minutes)
                current_time = time.time()
                for observer_id in list(self._visual_contacts.keys()):
                    for target_id in list(self._visual_contacts[observer_id].keys()):
                        contact = self._visual_contacts[observer_id][target_id]
                        if current_time - contact.get("last_seen", 0.0) > 120.0:  # 2 minutes
                            del self._visual_contacts[observer_id][target_id]
                    # Remove empty observer entries
                    if not self._visual_contacts[observer_id]:
                        del self._visual_contacts[observer_id]
        except Exception:
            pass


        base = {
            "ownship": {
                "x": own.kin.x,
                "y": own.kin.y,
                "heading": heading,
                "orderedHeading": self.ordered["heading"],
                "orderedSpeed": self.ordered["speed"],
                "orderedDepth": self.ordered["depth"],
                "speed": speed,
                "depth": depth,
                "cavitation": cav,
                "damage": {
                    "hull": own.damage.hull,
                    "sensors": own.damage.sensors,
                    "propulsion": own.damage.propulsion,
                    "flooding_rate": own.damage.flooding_rate
                },
            },
            "acoustics": {
                "noiseBudget": noise_budget, 
                "detectability": detectability, 
                "emconRisk": ("high" if noise_budget >= 75 else "med" if noise_budget >= 40 else "low"), 
                "emconAlert": emcon_alert,
                "dynamicSourceLevel": dynamic_src_lvl,
                "baseSourceLevel": base_src_lvl,
                "noiseContributions": noise_contributions,
                "stationNoiseContrib": station_noise_contrib
            },
            "events": list(self._transient_events),
            "noise": {
                "helm_dB": noise_levels.get("helm", 0.0),
                "sonar_dB": noise_levels.get("sonar", 0.0),
                "weapons_dB": noise_levels.get("weapons", 0.0),
                "engineering_dB": noise_levels.get("engineering", 0.0),
                "total_dB": noise_levels.get("total", 0.0),
            },
        }

        # Broadcast class/capabilities in the general telemetry for downstream consumers/AI
        tel_all = {**base, "ships": [
            {
                "id": s.id,
                "side": s.side,
                "class": getattr(s, "ship_class", None),
                "capabilities": (getattr(s, "capabilities", None).dict() if getattr(s, "capabilities", None) else None),
                "x": s.kin.x, "y": s.kin.y, "depth": s.kin.depth,
                "heading": s.kin.heading, "speed": s.kin.speed,
            }
            for s in self.world.all_ships()
        ]}
        # Station status aggregation for captain dashboard
        def station_status(station: str, ok_flag: bool) -> str:
            if not ok_flag:
                return "Failed"
            tasks = self._active_tasks.get(station, [])
            if not tasks:
                return "OK"
            return "Degraded" if any(t.stage in ("failing",) for t in tasks) else "OK"

        station_statuses = {
            "helm": station_status("helm", own.systems.rudder_ok),
            "sonar": station_status("sonar", own.systems.sonar_ok),
            "weapons": station_status("weapons", own.systems.tubes_ok),
            "engineering": station_status("engineering", own.systems.ballast_ok),
        }

        # Periscope spotting: probabilistic detection with realistic search behavior and contact persistence
        periscope_contacts = []
        
        # Check if periscope is raised, at proper depth, and system is functional
        periscope_ok = getattr(own.systems, "periscope_ok", True)  # Default to True if systems not initialized
        if self._periscope_raised and own.kin.depth <= 20.0 and periscope_ok:
            # Initialize periscope search timer if not exists
            if not hasattr(self, "_periscope_search_timer"):
                self._periscope_search_timer = 0.0
            
            # Initialize player visual contact tracking if not exists
            if "ownship" not in self._visual_contacts:
                self._visual_contacts["ownship"] = {}
            
            # Update search timer (re-roll detection every 5 seconds)
            self._periscope_search_timer += dt
            search_interval = 5.0  # 5 seconds between search attempts
            
            # Only perform detection check every 5 seconds to simulate realistic periscope scanning
            if self._periscope_search_timer >= search_interval:
                self._periscope_search_timer = 0.0  # Reset timer
                
                for s in self.world.all_ships():
                    if s.id == own.id:
                        continue
                    if s.kin.depth <= 5.0:  # Target must be at or near surface
                        dx = s.kin.x - own.kin.x
                        dy = s.kin.y - own.kin.y
                        rng = (dx*dx + dy*dy) ** 0.5
                        if rng <= 15000.0:  # Within 15km range
                            # Check if we have previous contact history for this target
                            contact_history = self._visual_contacts["ownship"].get(s.id, {})
                            last_seen = contact_history.get("last_seen", 0.0)
                            detection_count = contact_history.get("detection_count", 0)
                            last_confidence = contact_history.get("last_confidence", 0.0)
                            
                            # Calculate detection probability based on range and conditions
                            if self._debug_player_visual_100:
                                # Debug mode: 100% detection
                                detection_prob = 1.0
                                detection_roll = 0.0  # Always pass
                            else:
                                # Normal mode: probabilistic detection
                                # Base probability decreases with distance
                                base_prob = max(0.0, 1.0 - (rng / 15000.0))
                                
                                # Apply maintenance penalty if periscope system is degraded
                                maintenance_factor = getattr(own.maintenance.levels, "periscope", 1.0) if hasattr(own, "maintenance") else 1.0
                                detection_prob = base_prob * maintenance_factor
                                
                                # Surface vessels are easier to detect than submarines
                                if s.ship_class == "Convoy":
                                    detection_prob *= 1.3  # 30% easier to detect
                                elif s.ship_class == "Destroyer":
                                    detection_prob *= 1.1  # 10% easier to detect
                                
                                # Improve detection probability for previously spotted targets
                                if detection_count > 0:
                                    # Increase probability by 20% per previous detection (capped at 50% bonus)
                                    memory_bonus = min(0.5, detection_count * 0.2)
                                    detection_prob = min(0.95, detection_prob + memory_bonus)
                                
                                # Cap at 95% maximum detection probability (never perfect, but very high for known targets)
                                detection_prob = min(0.95, detection_prob)
                                
                                # Roll for detection
                                detection_roll = random.random()
                            
                            # Check for detection
                            detected_this_cycle = detection_roll < detection_prob
                            
                            # If we detect the target OR we have recent contact history, include it
                            current_time = time.time()
                            time_since_last_seen = current_time - last_seen
                            
                            # Include contact if:
                            # 1. We detected it this cycle, OR
                            # 2. We saw it recently (within 2 minutes) and it's still in range
                            if detected_this_cycle or (detection_count > 0 and time_since_last_seen <= 120.0 and rng <= 15000.0):
                                # Update contact history if we detected it this cycle
                                if detected_this_cycle:
                                    self._visual_contacts["ownship"][s.id] = {
                                        "last_seen": current_time,
                                        "detection_count": detection_count + 1,
                                        "last_confidence": detection_prob
                                    }
                                
                                # Use current detection confidence or last known confidence
                                current_confidence = detection_prob if detected_this_cycle else last_confidence
                                
                                brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
                                
                                # Calculate time since last seen for UI display
                                time_since_last_seen = current_time - (last_seen if not detected_this_cycle else current_time)
                                
                                periscope_contacts.append({
                                    "id": s.id, 
                                    "bearing": brg_true, 
                                    "range_m": rng, 
                                    "speed_kn": s.kin.speed, 
                                    "type": (s.side + " vessel"),
                                    "confidence": current_confidence,
                                    "last_seen": last_seen if not detected_this_cycle else current_time,
                                    "detection_count": detection_count + (1 if detected_this_cycle else 0),
                                    "detected_this_cycle": detected_this_cycle,
                                    "time_since_last_seen": time_since_last_seen,
                                    "status": "visible" if detected_this_cycle else "last_seen"
                                })
        tel_captain = {**base, "periscopeRaised": self._periscope_raised, "radioRaised": self._radio_raised, "mission": {"title": self.mission_brief["title"], "objective": self.mission_brief["objective"], "roe": self.mission_brief["roe"]}, "comms": getattr(self, "_captain_comms", []), "stationStatus": station_statuses, "periscopeContacts": periscope_contacts}
        tel_helm = {**base, "cavitationSpeedWarn": speed > 25.0, "thermocline": own.acoustics.thermocline_on, "tasks": [t.__dict__ for t in self._active_tasks['helm']]}
        # Prepare recent active ping responses list (bearing, range_est, strength, time)
        # For now, only generate on demand when 'sonar.ping' happens; UI will render as DEMON dots
        if not hasattr(self, "_last_ping_responses"):
            self._last_ping_responses = []
        # Include passive projectiles (torpedoes only - depth charges are silent while sinking)
        proj_contacts = passive_projectiles(own, self.world.torpedoes, getattr(self.world, "depth_charges", []))
        # Initialize explosion overlays list if absent
        if not hasattr(self, "_sonar_explosions"):
            self._sonar_explosions = []
        # Create explosion contacts from recent explosions
        explosion_contacts_list = explosion_contacts(own, getattr(self, "_sonar_explosions", []))
        # Collect all AI ping responses
        # Include enemy ping contacts in the contacts list
        enemy_ping_contacts = []
        if hasattr(self, "_enemy_ping_contacts"):
            enemy_ping_contacts = [epc["contact"] for epc in self._enemy_ping_contacts]
        
        # Note: all_ai_ping_responses removed from pingResponses - enemy pings should appear as contacts, not ping responses
        all_contacts = contacts + proj_contacts + explosion_contacts_list + enemy_ping_contacts
        tel_sonar = {**base, "contacts": [c.dict() for c in all_contacts], "pingCooldown": max(0.0, self.active_ping_state.timer), "pingResponses": list(self._last_ping_responses), "lastPingAt": getattr(self, "_last_ping_at", None), "explosions": list(self._sonar_explosions), "tasks": [t.__dict__ for t in self._active_tasks['sonar']]}
        tel_weapons = {**base, "tubes": [t.dict() for t in own.weapons.tubes], "consentRequired": CONFIG.require_captain_consent, "captainConsent": self._captain_consent, "tasks": [t.__dict__ for t in self._active_tasks['weapons']]}
        tel_engineering = {**base, "reactor": own.reactor.dict(), "pumps": {"fwd": self._pump_fwd, "aft": self._pump_aft}, "damage": own.damage.dict(), "power": own.power.dict(), "systems": own.systems.dict(), "maintenance": own.maintenance.levels, "tasks": [t.__dict__ for t in self._active_tasks['engineering']]}

        def bearings_to(sx: float, sy: float) -> Dict[str, float]:
            # Compass bearing: 0=N, 90=E, 180=S, 270=W
            dx = sx - own.kin.x
            dy = sy - own.kin.y
            # atan2 returns angle from +X; to get compass bearing from +Y (north), swap args as atan2(dx, dy)
            brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
            # Validate south-of-ownship case to reduce confusion: if target.y < own.y and dx≈0, brg_true≈180
            brg_rel = (brg_true - own.kin.heading + 360.0) % 360.0
            return {"bearing_true": brg_true, "bearing_rel": brg_rel, "heading_to_face": brg_true}

        debug_payload = {
            "ownship": {
                "x": own.kin.x, "y": own.kin.y, "depth": own.kin.depth,
                "heading": own.kin.heading, "speed": own.kin.speed,
            },
            "maintenance": {"spawnsEnabled": (not self._suppress_maintenance_spawns)},
            "debugToggles": {
                "playerVisual100": self._debug_player_visual_100,
                "enemyVisual100": self._debug_enemy_visual_100
            },
            # AI orchestrator quick status for debug view
            "ai": {
                "enabled": bool(getattr(CONFIG, "use_ai_orchestrator", False)),
                "engines": {
                    "fleet": {"engine": getattr(CONFIG, "ai_fleet_engine", "stub"), "model": getattr(CONFIG, "ai_fleet_model", "stub")},
                    "ship": {"engine": getattr(CONFIG, "ai_ship_engine", "stub"), "model": getattr(CONFIG, "ai_ship_model", "stub")},
                },
                # Last provider call metadata (if any)
                "providerMeta": {
                    "fleet": (getattr(getattr(self, "_ai_orch", None), "_fleet_engine", None)._last_call_meta if getattr(getattr(self, "_ai_orch", None), "_fleet_engine", None) is not None and hasattr(getattr(self, "_ai_orch", None)._fleet_engine, "_last_call_meta") else None),
                    "ship": (getattr(getattr(self, "_ai_orch", None), "_ship_engine", None)._last_call_meta if getattr(getattr(self, "_ai_orch", None), "_ship_engine", None) is not None and hasattr(getattr(self, "_ai_orch", None)._ship_engine, "_last_call_meta") else None),
                },
            },
            "ships": [
                {
                    "id": s.id, "side": s.side,
                    "class": getattr(s, "ship_class", None),
                    "capabilities": (getattr(s, "capabilities", None).dict() if getattr(s, "capabilities", None) else None),
                    "weapons": (getattr(s, "weapons", None).dict() if getattr(s, "weapons", None) else None),
                    "x": s.kin.x, "y": s.kin.y, "depth": s.kin.depth,
                    "heading": s.kin.heading, "speed": s.kin.speed,
                    # Damage information
                    "damage": {
                        "hull": s.damage.hull,
                        "sensors": s.damage.sensors,
                        "propulsion": s.damage.propulsion,
                        "flooding_rate": s.damage.flooding_rate
                    },
                    # Passive detectability breakdown for debug
                    "slDb": getattr(s.acoustics, "last_snr_db", 0.0) + (20.0 * 0),
                    "snrDb": getattr(s.acoustics, "last_snr_db", 0.0),
                    "passiveDetect": getattr(s.acoustics, "last_detectability", 0.0),
                    **bearings_to(s.kin.x, s.kin.y),
                    "range_from_own": (( ( (s.kin.x - own.kin.x)**2 + (s.kin.y - own.kin.y)**2 ) ** 0.5 )),
                    # Include contacts this ship has detected (e.g., from player's active ping)
                    "contacts": [c.dict() for c in getattr(self, "_enemy_ship_contacts", {}).get(s.id, [])]
                }
                for s in self.world.all_ships() if s.id != own.id
            ],
            "torpedoes": list(self.world.torpedoes),
            "depth_charges": list(getattr(self.world, "depth_charges", [])),
        }
        # Include Fleet Commander contact history if orchestrator is present
        try:
            if hasattr(self, "_ai_orch") and getattr(self, "_ai_orch", None) is not None:
                debug_payload["contactHistory"] = list(getattr(self._ai_orch, "_fleet_contact_history", []))[-100:]
        except Exception:
            pass
        
        # Clean up old enemy ship contacts (older than 30 seconds)
        if hasattr(self, "_enemy_ship_contacts") and hasattr(self, "_enemy_ship_contacts_timestamps"):
            current_time = time.time()
            for ship_id in list(self._enemy_ship_contacts.keys()):
                if ship_id in self._enemy_ship_contacts_timestamps:
                    # Keep contacts and timestamps that are less than 30 seconds old
                    valid_indices = [
                        i for i, timestamp in enumerate(self._enemy_ship_contacts_timestamps[ship_id])
                        if (current_time - timestamp) < 30.0
                    ]
                    self._enemy_ship_contacts[ship_id] = [
                        self._enemy_ship_contacts[ship_id][i] for i in valid_indices
                    ]
                    self._enemy_ship_contacts_timestamps[ship_id] = [
                        self._enemy_ship_contacts_timestamps[ship_id][i] for i in valid_indices
                    ]
                    
                    # Remove empty entries
                    if not self._enemy_ship_contacts[ship_id]:
                        del self._enemy_ship_contacts[ship_id]
                        del self._enemy_ship_contacts_timestamps[ship_id]

        await BUS.publish("tick:all", {"topic": "telemetry", "data": tel_all})
        await BUS.publish("tick:captain", {"topic": "telemetry", "data": tel_captain})
        # Store for tests/inspection
        self._last_captain_tel = tel_captain
        await BUS.publish("tick:helm", {"topic": "telemetry", "data": tel_helm})
        await BUS.publish("tick:sonar", {"topic": "telemetry", "data": tel_sonar})
        await BUS.publish("tick:weapons", {"topic": "telemetry", "data": tel_weapons})
        await BUS.publish("tick:engineering", {"topic": "telemetry", "data": tel_engineering})
        await BUS.publish("tick:debug", {"topic": "telemetry", "data": debug_payload})
        # Fleet AI telemetry: intent and recent runs/tool calls
        from ..config import CONFIG as _CFG
        fleet_payload = {
            **base,
            "fleetIntent": getattr(self, "_fleet_intent", {}),
            "aiRuns": getattr(self, "_ai_recent_runs", [])[-50:],
            "engines": {
                "fleet": {"engine": getattr(_CFG, "ai_fleet_engine", "stub"), "model": getattr(_CFG, "ai_fleet_model", "stub")},
                "ship": {"engine": getattr(_CFG, "ai_ship_engine", "stub"), "model": getattr(_CFG, "ai_ship_model", "stub")},
            },
            "ships": [
                {
                    "id": s.id, "side": s.side, "class": getattr(s, "ship_class", None),
                    "x": s.kin.x, "y": s.kin.y, "depth": s.kin.depth,
                    "heading": s.kin.heading, "speed": s.kin.speed,
                }
                for s in self.world.all_ships()
            ],
        }
        await BUS.publish("tick:fleet", {"topic": "telemetry", "data": fleet_payload})

        # Clear transient events after publishing
        self._transient_events.clear()
        
        # Clear old enemy ping contacts (keep them for 5 seconds)
        if hasattr(self, "_enemy_ping_contacts"):
            current_time = time.time()
            self._enemy_ping_contacts = [
                epc for epc in self._enemy_ping_contacts 
                if current_time - epc.get("timestamp_epoch", 0) < 5.0
            ]

        self._last_snapshot += dt
        if self._last_snapshot >= CONFIG.snapshot_s:
            self._last_snapshot = 0.0
            insert_snapshot(self.engine, self.run_id, heading, speed, depth)
        # Handle timed comms after core tick; uses sim time
        self._handle_captain_comms(dt)

    def _handle_captain_comms(self, dt: float) -> None:
        own = self.world.get_ship("ownship")
        if not hasattr(self, "_sim_time_s"):
            self._sim_time_s = 0.0
        self._sim_time_s += dt
        # Require shallow enough depth and radio raised
        at_radio_depth = own.kin.depth <= 20.0 and self._radio_raised
        if not at_radio_depth:
            return
        # Deliver next scheduled message if time passed
        sched = self.mission_brief.get("comms_schedule", [])
        next_idx = self._delivered_comms_idx + 1
        if 0 <= next_idx < len(sched):
            if self._sim_time_s >= sched[next_idx]["at_s"]:
                # Append to captain comms list
                if not hasattr(self, "_captain_comms"):
                    self._captain_comms = []
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._captain_comms.append({"at": ts, "text": sched[next_idx]["msg"]})
                self._delivered_comms_idx = next_idx

    async def handle_command(self, topic: str, data: Dict) -> Optional[str]:
        own = self.world.get_ship("ownship")
        if topic == "helm.order":
            self.ordered["heading"] = float(data.get("heading", self.ordered["heading"])) % 360
            self.ordered["speed"] = float(data.get("speed", self.ordered["speed"]))
            self.ordered["depth"] = max(0.0, float(data.get("depth", self.ordered["depth"])))
            return None
        if topic == "sonar.ping":
            if self.active_ping_state.start():
                res = active_ping(own, [s for s in self.world.all_ships() if s.id != own.id])
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                # Store simplified responses for UI
                self._last_ping_at = now_iso
                self._last_ping_responses = [
                    {"id": rid, "bearing": brg, "range_est": rng, "strength": st, "at": now_iso}
                    for (rid, rng, brg, st) in res
                ]
                
                # Create counter-detection contacts for enemy ships
                for ship in self.world.all_ships():
                    if ship.side == "RED":  # Enemy ships
                        dx = own.kin.x - ship.kin.x
                        dy = own.kin.y - ship.kin.y
                        dist_m = math.hypot(dx, dy)
                        # Enemy ships can detect player's ping within active sonar range
                        if dist_m <= 15000.0:  # 15km active sonar range
                            # Calculate bearing from enemy ship to player
                            brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
                            # Add some noise to the bearing
                            brg_noise = normalize_angle_deg(brg + random.gauss(0, 2.0))
                            # Calculate signal strength based on distance
                            strength = max(0.0, min(1.0, 1.0 / (1.0 + (dist_m / 10000.0))))
                            
                            # Create a contact for this enemy ship to detect
                            enemy_contact = TelemetryContact(
                                id="ENEMY_ACTIVE_SONAR",
                                bearing=brg_noise,
                                strength=strength,
                                classifiedAs="ENEMY_ACTIVE_SONAR",
                                confidence=0.8,  # High confidence for active sonar detection
                                bearingKnown=True,
                                rangeKnown=False  # No range for passive detection
                            )
                            
                            # Store the contact for this enemy ship with timestamp
                            if not hasattr(self, "_enemy_ship_contacts"):
                                self._enemy_ship_contacts = {}
                            if not hasattr(self, "_enemy_ship_contacts_timestamps"):
                                self._enemy_ship_contacts_timestamps = {}
                            if ship.id not in self._enemy_ship_contacts:
                                self._enemy_ship_contacts[ship.id] = []
                                self._enemy_ship_contacts_timestamps[ship.id] = []
                            
                            self._enemy_ship_contacts[ship.id].append(enemy_contact)
                            self._enemy_ship_contacts_timestamps[ship.id].append(time.time())
                            
                            # Add to AI orchestrator's contact history for debug display
                            if hasattr(self, "_ai_orch") and self._ai_orch is not None:
                                if not hasattr(self._ai_orch, "_fleet_contact_history"):
                                    self._ai_orch._fleet_contact_history = []
                                
                                history_entry = {
                                    "time": now_iso,
                                    "reportedBy": ship.id,
                                    "reporter_pos": [ship.kin.x, ship.kin.y],
                                    "type": "active_sonar_detection",
                                    "id": "ownship",
                                    "bearing": brg_noise,
                                    "range_est": None,  # No range for passive detection
                                    "confidence": 0.8,
                                    "classifiedAs": "ENEMY_ACTIVE_SONAR"
                                }
                                self._ai_orch._fleet_contact_history.append(history_entry)
                                # Keep last 100 entries
                                self._ai_orch._fleet_contact_history = self._ai_orch._fleet_contact_history[-100:]
                
                # Active ping raises EMCON risk; emit counter-detected event for UI
                self._transient_events.append({"type": "counterDetected", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                return None
            return "Ping on cooldown"
        if topic == "weapons.tube.load":
            ok = try_load_tube(own, int(data.get("tube", 1)), str(data.get("weapon", "Mk48")))
            return None if ok else "Cannot load"
        if topic == "weapons.tube.flood":
            ok = try_flood_tube(own, int(data.get("tube", 1)))
            return None if ok else "Cannot flood"
        if topic == "weapons.tube.doors":
            ok = try_set_doors(own, int(data.get("tube", 1)), bool(data.get("open", True)))
            return None if ok else "Cannot set doors"
        if topic == "weapons.fire":
            if CONFIG.require_captain_consent and not self._captain_consent:
                return "Captain consent required"
            torp = try_fire(
                own,
                int(data.get("tube", 1)),
                float(data.get("bearing", own.kin.heading)),
                float(data.get("run_depth", own.kin.depth)),
                float(data.get("enable_range", own.weapons.tubes[0].weapon.enable_range_m if own.weapons.tubes and own.weapons.tubes[0].weapon else 800.0)),
                str(data.get("doctrine", "passive_then_active")),
            )
            if torp is None:
                return "Cannot fire"
            self.world.torpedoes.append(torp)
            insert_event(self.engine, self.run_id, "weapons.fire", json.dumps(data))
            return None
        if topic == "weapons.test_fire":
            # Test torpedo launch - bypasses all interlocks and tube preparation
            # Create a torpedo directly without going through tube state machine
            import time
            torp = {
                "id": f"torpedo_test_{int(time.time() * 1000)}",  # Unique ID for sonar tracking
                "x": own.kin.x,
                "y": own.kin.y,
                "depth": own.kin.depth,
                "heading": float(data.get("bearing", own.kin.heading)) % 360.0,
                "speed": 45.0,  # Default Mk48 speed
                "armed": False,
                "enable_range_m": float(data.get("enable_range", 800.0)),
                "seeker_range_m": 4000.0,
                "run_time": 0.0,
                "max_run_time": 600.0,
                "target_id": None,
                "name": "Mk48-TEST",
                "seeker_cone": 35.0,
                "side": own.side,
                "spoofed_timer": 0.0,
                "run_depth": float(data.get("run_depth", own.kin.depth)),
                "doctrine": str(data.get("doctrine", "passive_then_active")),
                "pn_nav_const": 3.0,
                "los_prev": None,
            }
            self.world.torpedoes.append(torp)
            insert_event(self.engine, self.run_id, "weapons.test_fire", json.dumps(data))
            return None
        if topic == "weapons.depth_charges.drop":
            # Debug/test: drop a spread of depth charges from a specified ship (e.g., a RED destroyer)
            ship_id = str(data.get("ship_id", "red-dd-01"))
            try:
                tgt = self.world.get_ship(ship_id)
            except Exception:
                return "Unknown ship"
            if not getattr(getattr(tgt, "capabilities", None), "has_depth_charges", False):
                return "Ship cannot drop depth charges"
            spread_m = float(data.get("spread_meters", 20.0))
            min_d = float(data.get("minDepth", 30.0))
            max_d = float(data.get("maxDepth", 50.0))
            n = int(data.get("spreadSize", 3))
            res = try_drop_depth_charges(tgt, spread_m, min_d, max_d, n, on_event=lambda n,p: insert_event(self.engine, self.run_id, n, json.dumps(p)))
            if not res.get("ok"):
                return res.get("error", "Drop failed")
            for dc in res.get("data", []) or []:
                self.world.depth_charges.append(dc)
            return None
        if topic == "engineering.reactor.set":
            mw = max(0.0, min(own.reactor.max_mw, float(data.get("mw", own.reactor.output_mw))))
            own.reactor.output_mw = mw
            return None
        if topic == "engineering.power.allocate":
            # Expect fractions for helm/weapons/sonar/engineering; must NOT exceed total budget (<= 1.0)
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
            return None
        if topic == "station.task.start":
            station = str(data.get("station", "")).lower()
            if station not in self._active_tasks:
                return "Unknown station"
            tasks = self._active_tasks[station]
            if not tasks:
                # Spawn an immediate task if none exists yet, then start it
                now_s = time.perf_counter()
                self._spawn_task_for(station, now_s)
                tasks = self._active_tasks[station]
            # If a specific task_id was provided, start only that one; stop others
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
                # Choose the most urgent task: worst stage first, then shortest remaining time
                stage_rank = {"task": 0, "failing": 1, "failed": 2}
                tasks.sort(key=lambda t: (-stage_rank.get(t.stage, 0), t.time_remaining_s))
                for i, t in enumerate(tasks):
                    t.started = (i == 0)
            # Return None to indicate accepted
            return None
        if topic == "engineering.pump.toggle":
            name = str(data.get("pump", "")).lower()
            state = bool(data.get("enabled", True))
            if name == "fwd":
                self._pump_fwd = state
            elif name == "aft":
                self._pump_aft = state
            return None
        if topic == "engineering.reactor.scram":
            own.reactor.scrammed = bool(data.get("scrammed", True))
            return None
        if topic == "captain.consent":
            self.set_captain_consent(bool(data.get("consent", False)))
            return None
        if topic == "captain.periscope.raise":
            self._periscope_raised = bool(data.get("raised", True))
            return None
        if topic == "captain.radio.raise":
            self._radio_raised = bool(data.get("raised", True))
            return None
        if topic == "debug.restart":
            # Reset to original game state
            # Reload .env on mission restart so config changes take effect without server restart
            try:
                reload_from_env()
            except Exception:
                pass
            # Force default world so tests expecting fixed coordinates pass
            self._force_default_reset = True
            # Recreate orchestrator if needed to pick up engine changes
            if getattr(CONFIG, "use_ai_orchestrator", False):
                self._ai_orch = AgentsOrchestrator(lambda: self.world, self.engine, self.run_id)
                try:
                    self._ai_orch.set_fleet_engine(getattr(CONFIG, "ai_fleet_engine", "stub"), getattr(CONFIG, "ai_fleet_model", "stub"))
                    self._ai_orch.set_ship_engine(getattr(CONFIG, "ai_ship_engine", "stub"), getattr(CONFIG, "ai_ship_model", "stub"))
                    # Provide mission brief for Fleet Commander inputs after restart as well
                    try:
                        setattr(self._ai_orch, "_mission_brief", self.mission_brief)
                    except Exception:
                        pass
                except Exception:
                    pass
                # Fleet/Ship engine health check for Fleet UI
                try:
                    hc = await self._ai_orch.health_check()
                    self._ai_recent_runs = (getattr(self, "_ai_recent_runs", []) or []) + [{
                        "agent": "system",
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "tool_calls": [{"tool": "health_check", "arguments": hc}],
                    }]
                except Exception:
                    pass
            self._init_default_world()
            return None
        if topic == "ai.tool":
            # Apply an AI tool call to a specified ship (default RED-01)
            ship_id = str(data.get("ship_id", "red-01"))
            try:
                tgt = self.world.get_ship(ship_id)
            except Exception:
                return "Unknown ship"
            tool = str(data.get("tool", "")).strip()
            args = data.get("arguments", {}) or {}
            # Respect platform capabilities
            caps = getattr(tgt, "capabilities", None)
            if tool == "set_nav":
                if caps and not caps.can_set_nav:
                    return "Tool not supported"
                tgt.kin.heading = float(args.get("heading") or tgt.kin.heading) % 360.0
                tgt.kin.speed = max(0.0, float(args.get("speed") or tgt.kin.speed))
                # Clamp against platform max depth so surface vessels cannot submerge
                tgt.kin.depth = max(0.0, min(tgt.hull.max_depth, float(args.get("depth") or tgt.kin.depth)))
                return None
            if tool == "fire_torpedo":
                if not caps or not caps.has_torpedoes:
                    return "Tool not supported"
                # For now, only ownship can launch torpedoes in the sim; ignore others
                return "Not implemented for non-ownship"
            if tool == "deploy_countermeasure":
                if not caps or not caps.countermeasures:
                    return "Tool not supported"
                # Placeholder: accept command without effect yet
                return None
            if tool == "drop_depth_charges":
                if not caps or not getattr(caps, "has_depth_charges", False):
                    return "Tool not supported"
                # Handle potential list values and None values
                spread_val = args.get("spread_meters")
                if isinstance(spread_val, list) and len(spread_val) > 0:
                    spread_val = spread_val[0]
                spread_m = float(spread_val or 20.0)
                
                min_d_val = args.get("minDepth")
                if isinstance(min_d_val, list) and len(min_d_val) > 0:
                    min_d_val = min_d_val[0]
                min_d = float(min_d_val or 30.0)
                
                max_d_val = args.get("maxDepth")
                if isinstance(max_d_val, list) and len(max_d_val) > 0:
                    max_d_val = max_d_val[0]
                max_d = float(max_d_val or 50.0)
                
                n_val = args.get("spreadSize")
                if isinstance(n_val, list) and len(n_val) > 0:
                    n_val = n_val[0]
                n = int(float(n_val or 3))
                res = try_drop_depth_charges(tgt, spread_m, min_d, max_d, n)
                if not res.get("ok"):
                    return res.get("error", "Drop failed")
                for dc in res.get("data", []) or []:
                    self.world.depth_charges.append(dc)
                return None
            return "Unknown tool"
        if topic == "debug.maintenance.spawns":
            # Toggle spawning of new maintenance tasks; existing tasks remain
            enabled = bool(data.get("enabled", True))
            self._suppress_maintenance_spawns = (not enabled)
            return None
        if topic == "debug.visual.player_100":
            # Toggle 100% visual detection for player periscope
            self._debug_player_visual_100 = bool(data.get("enabled", False))
            return f"Player visual detection 100%: {'ON' if self._debug_player_visual_100 else 'OFF'}"
        if topic == "debug.visual.enemy_100":
            # Toggle 100% visual detection for enemy ships
            self._debug_enemy_visual_100 = bool(data.get("enabled", False))
            return f"Enemy visual detection 100%: {'ON' if self._debug_enemy_visual_100 else 'OFF'}"
        if topic == "debug.mission.surface_vessel":
            # Reset to base world, then configure a single slow surface contact (convoy ship)
            self._init_default_world()
            own = self.world.get_ship("ownship")
            # Reconfigure the default RED contact as a surface vessel at ~6km, slow speed
            for ship in self.world.all_ships():
                if ship.id != own.id and ship.side == "RED":
                    ship.kin.x = 6000.0
                    ship.kin.y = 0.0
                    ship.kin.depth = 3.0  # surface contact
                    ship.kin.heading = 90.0
                    ship.kin.speed = 5.0
                    # Assign convoy class and capabilities; lower max speed per catalog
                    ship.ship_class = "Convoy"
                    if 'Convoy' in SHIP_CATALOG:
                        ship.capabilities = SHIP_CATALOG["Convoy"].capabilities
                        ship.hull.max_speed = min(ship.hull.max_speed, SHIP_CATALOG["Convoy"].default_hull.max_speed)
                    else:
                        ship.hull.max_speed = min(ship.hull.max_speed, 20.0)
                    break
            # Update mission brief to reflect this scenario
            self.mission_brief = {
                "title": "Surface Vessel Intercept (Training)",
                "objective": "Escort convoy ship red-01 safely across sector; training shot optional.",
                "roe": [
                    "Weapons release authorized for training shot.",
                    "Minimize active sonar to preserve EMCON.",
                ],
                # Provide a simple target waypoint for the fleet commander
                "target_wp": [100.0, 100.0],
                "comms_schedule": [
                    {"at_s": 90.0, "msg": "INFO: Surface contact maintaining 5 kn on easterly course."},
                ],
            }
            return None
        if topic == "debug.mission1":
            # Configure a slow-moving surface contact for torpedo testing
            # Keep ownship as-is; reposition/redesignate the RED ship
            for ship in self.world.all_ships():
                if ship.id != own.id and ship.side == "RED":
                    ship.kin.x = 6000.0
                    ship.kin.y = 0.0
                    ship.kin.depth = 3.0  # surface contact
                    ship.kin.heading = 90.0
                    ship.kin.speed = 5.0
                    break
            return None
        return None
