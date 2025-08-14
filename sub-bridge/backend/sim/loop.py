from __future__ import annotations
import asyncio
import json
import time
import math
from typing import Dict, Optional
import random
from ..bus import BUS
from ..config import CONFIG
from ..models import Ship, Kinematics, Hull, Acoustics, WeaponsSuite, Reactor, DamageState, MaintenanceTask, SHIP_CATALOG
from ..storage import init_engine, create_run, insert_snapshot, insert_event
from .ecs import World
from .physics import integrate_kinematics
from .sonar import passive_contacts, ActivePingState, active_ping
from .weapons import try_load_tube, try_flood_tube, try_set_doors, try_fire, step_torpedo, step_tubes
from .ai_tools import LocalAIStub
from .damage import step_damage, step_engineering


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
        self._stop = asyncio.Event()
        self._last_snapshot = 0.0
        self._last_ai = 0.0
        self._transient_events = []  # cleared every tick
        self._last_ping_responses = []
        self._last_ping_at = None
        self._init_default_world()
        # Station task state
        self._active_tasks: Dict[str, list[MaintenanceTask]] = {s: [] for s in ["helm", "sonar", "weapons", "engineering"]}
        self._task_spawn_timers: Dict[str, float] = {s: CONFIG.first_task_delay_s for s in ["helm", "sonar", "weapons", "engineering"]}
        # Mission briefing and ROE
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

    def _init_default_world(self) -> None:
        # Clear existing world and set to original game state
        self.world = World()
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
        self.ordered = {"heading": own.kin.heading, "speed": own.kin.speed, "depth": own.kin.depth}
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

        if CONFIG.use_enemy_ai:
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

        step_tubes(own, dt)

        for ship in self.world.all_ships():
            if ship.id == "ownship":
                continue
            if CONFIG.enemy_static:
                continue
            integrate_kinematics(ship, ship.kin.heading, ship.kin.speed, ship.kin.depth, dt)

        if self.world.torpedoes:
            for t in list(self.world.torpedoes):
                def _on_event(name: str, payload: dict) -> None:
                    insert_event(self.engine, self.run_id, name, json.dumps(payload))
                step_torpedo(t, self.world, dt, on_event=_on_event)
                if t["run_time"] > t["max_run_time"]:
                    self.world.torpedoes.remove(t)

        pump_effect = 2.0 if (self._pump_fwd or self._pump_aft) else 0.0
        step_damage(own, dt, pump_effect=pump_effect)
        step_engineering(own, dt)

        # Station maintenance/repair tasks lifecycle
        self._step_station_tasks(own, dt)

        self.active_ping_state.tick(dt)
        # Acoustic noise budget and detectability
        noise_from_speed = min(100.0, (speed / max(1.0, own.hull.max_speed)) * 70.0)
        noise_cav = 30.0 if cav else 0.0
        noise_pumps = 10.0 if (self._pump_fwd or self._pump_aft) else 0.0
        noise_masts = (10.0 if self._periscope_raised else 0.0) + (10.0 if self._radio_raised else 0.0)
        noise_budget = max(0.0, min(100.0, noise_from_speed + noise_cav + noise_pumps + noise_masts))
        # EMCON pressure: sustained high noise raises alert
        if noise_budget >= 60.0:
            self._emcon_high_timer = min(30.0, self._emcon_high_timer + dt)
        else:
            self._emcon_high_timer = max(0.0, self._emcon_high_timer - dt)
        emcon_alert = self._emcon_high_timer >= 10.0
        detectability = noise_budget / 100.0
        contacts = passive_contacts(own, [s for s in self.world.all_ships() if s.id != own.id])

        base = {
            "ownship": {
                "heading": heading,
                "orderedHeading": self.ordered["heading"],
                "orderedSpeed": self.ordered["speed"],
                "orderedDepth": self.ordered["depth"],
                "speed": speed,
                "depth": depth,
                "cavitation": cav,
            },
            "acoustics": {"noiseBudget": noise_budget, "detectability": detectability, "emconRisk": ("high" if noise_budget >= 75 else "med" if noise_budget >= 40 else "low"), "emconAlert": emcon_alert},
            "events": list(self._transient_events),
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

        # Periscope spotting: precise bearing/range/type/speed for shallow targets within 15km when scope up and at depth
        periscope_contacts = []
        if self._periscope_raised and own.kin.depth <= 20.0:
            for s in self.world.all_ships():
                if s.id == own.id:
                    continue
                if s.kin.depth <= 5.0:
                    dx = s.kin.x - own.kin.x
                    dy = s.kin.y - own.kin.y
                    rng = (dx*dx + dy*dy) ** 0.5
                    if rng <= 15000.0:
                        brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
                        periscope_contacts.append({"id": s.id, "bearing": brg_true, "range_m": rng, "speed_kn": s.kin.speed, "type": (s.side + " vessel")})
        tel_captain = {**base, "periscopeRaised": self._periscope_raised, "radioRaised": self._radio_raised, "mission": {"title": self.mission_brief["title"], "objective": self.mission_brief["objective"], "roe": self.mission_brief["roe"]}, "comms": getattr(self, "_captain_comms", []), "stationStatus": station_statuses, "periscopeContacts": periscope_contacts}
        tel_helm = {**base, "cavitationSpeedWarn": speed > 25.0, "thermocline": own.acoustics.thermocline_on, "tasks": [t.__dict__ for t in self._active_tasks['helm']]}
        # Prepare recent active ping responses list (bearing, range_est, strength, time)
        # For now, only generate on demand when 'sonar.ping' happens; UI will render as DEMON dots
        if not hasattr(self, "_last_ping_responses"):
            self._last_ping_responses = []
        tel_sonar = {**base, "contacts": [c.dict() for c in contacts], "pingCooldown": max(0.0, self.active_ping_state.timer), "pingResponses": list(self._last_ping_responses), "lastPingAt": getattr(self, "_last_ping_at", None), "tasks": [t.__dict__ for t in self._active_tasks['sonar']]}
        tel_weapons = {**base, "tubes": [t.dict() for t in own.weapons.tubes], "consentRequired": CONFIG.require_captain_consent, "captainConsent": self._captain_consent, "tasks": [t.__dict__ for t in self._active_tasks['weapons']]}
        tel_engineering = {**base, "reactor": own.reactor.dict(), "pumps": {"fwd": self._pump_fwd, "aft": self._pump_aft}, "damage": own.damage.dict(), "power": own.power.dict(), "systems": own.systems.dict(), "maintenance": own.maintenance.levels, "tasks": [t.__dict__ for t in self._active_tasks['engineering']]}

        def bearings_to(sx: float, sy: float) -> Dict[str, float]:
            # Compass bearing: 0=N, 90=E, 180=S, 270=W
            dx = sx - own.kin.x
            dy = sy - own.kin.y
            brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
            brg_rel = (brg_true - own.kin.heading + 360.0) % 360.0
            return {"bearing_true": brg_true, "bearing_rel": brg_rel, "heading_to_face": brg_true}

        debug_payload = {
            "ownship": {
                "x": own.kin.x, "y": own.kin.y, "depth": own.kin.depth,
                "heading": own.kin.heading, "speed": own.kin.speed,
            },
            "maintenance": {"spawnsEnabled": (not self._suppress_maintenance_spawns)},
            "ships": [
                {
                    "id": s.id, "side": s.side,
                    "class": getattr(s, "ship_class", None),
                    "capabilities": (getattr(s, "capabilities", None).dict() if getattr(s, "capabilities", None) else None),
                    "x": s.kin.x, "y": s.kin.y, "depth": s.kin.depth,
                    "heading": s.kin.heading, "speed": s.kin.speed,
                    # Passive detectability breakdown for debug
                    "slDb": getattr(s.acoustics, "last_snr_db", 0.0) + (20.0 * 0),
                    "snrDb": getattr(s.acoustics, "last_snr_db", 0.0),
                    "passiveDetect": getattr(s.acoustics, "last_detectability", 0.0),
                    **bearings_to(s.kin.x, s.kin.y),
                    "range_from_own": (( ( (s.kin.x - own.kin.x)**2 + (s.kin.y - own.kin.y)**2 ) ** 0.5 )),
                }
                for s in self.world.all_ships() if s.id != own.id
            ],
            "torpedoes": list(self.world.torpedoes),
        }

        await BUS.publish("tick:all", {"topic": "telemetry", "data": tel_all})
        await BUS.publish("tick:captain", {"topic": "telemetry", "data": tel_captain})
        # Store for tests/inspection
        self._last_captain_tel = tel_captain
        await BUS.publish("tick:helm", {"topic": "telemetry", "data": tel_helm})
        await BUS.publish("tick:sonar", {"topic": "telemetry", "data": tel_sonar})
        await BUS.publish("tick:weapons", {"topic": "telemetry", "data": tel_weapons})
        await BUS.publish("tick:engineering", {"topic": "telemetry", "data": tel_engineering})
        await BUS.publish("tick:debug", {"topic": "telemetry", "data": debug_payload})

        # Clear transient events after publishing
        self._transient_events.clear()

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
                tgt.kin.heading = float(args.get("heading", tgt.kin.heading)) % 360.0
                tgt.kin.speed = max(0.0, float(args.get("speed", tgt.kin.speed)))
                tgt.kin.depth = max(0.0, float(args.get("depth", tgt.kin.depth)))
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
            return "Unknown tool"
        if topic == "debug.maintenance.spawns":
            # Toggle spawning of new maintenance tasks; existing tasks remain
            enabled = bool(data.get("enabled", True))
            self._suppress_maintenance_spawns = (not enabled)
            return None
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
                "objective": "Approach undetected, classify, and conduct a training torpedo shot on a single surface contact.",
                "roe": [
                    "Weapons release authorized for training shot.",
                    "Minimize active sonar to preserve EMCON.",
                ],
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
