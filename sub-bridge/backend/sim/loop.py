from __future__ import annotations
import asyncio
import json
import time
import math
from typing import Dict, Optional
import random
from ..bus import BUS
from ..config import CONFIG
from ..models import Ship, Kinematics, Hull, Acoustics, WeaponsSuite, Reactor, DamageState, MaintenanceTask
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
        self._active_tasks: Dict[str, Optional[MaintenanceTask]] = {s: None for s in ["helm", "sonar", "weapons", "engineering"]}
        self._task_spawn_timers: Dict[str, float] = {s: 0.0 for s in ["helm", "sonar", "weapons", "engineering"]}
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

    def _init_default_world(self) -> None:
        # Clear existing world and set to original game state
        self.world = World()
        own = Ship(
            id="ownship",
            side="BLUE",
            kin=Kinematics(depth=100.0, heading=270.0, speed=8.0),
            hull=Hull(),
            acoustics=Acoustics(),
            weapons=WeaponsSuite(),
            reactor=Reactor(output_mw=60.0, max_mw=100.0),
            damage=DamageState(),
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

    def stop(self) -> None:
        self._stop.set()

    def set_captain_consent(self, consent: bool) -> None:
        self._captain_consent = consent

    def _spawn_task_for(self, station: str, now_s: float) -> None:
        titles = {
            "helm": ("rudder", "Rudder Lubricate"),
            "sonar": ("sonar", "Array Recalibration"),
            "weapons": ("tubes", "Tube Seal Inspection"),
            "engineering": ("ballast", "Ballast Valve Service"),
        }
        system, title = titles.get(station, ("rudder", "Maintenance"))
        base_deadline = random.uniform(25.0, 45.0)
        tid = f"{station}-{int(now_s*1000)%100000}-{random.randint(100,999)}"
        self._active_tasks[station] = MaintenanceTask(
            id=tid, station=station, system=system, title=title,
            stage="normal", progress=0.0, base_deadline_s=base_deadline, time_remaining_s=base_deadline, created_at=now_s
        )

    def _station_power_fraction(self, ship: Ship, station: str) -> float:
        p = ship.power
        return max(0.0, min(1.0, getattr(p, station if station != "engineering" else "engineering")))

    def _apply_stage_penalties(self, ship: Ship, station: str, stage: str) -> None:
        # Apply degradation effects per station and stage
        if station == "helm":
            factor = {"normal": 1.0, "degraded": 0.8, "damaged": 0.5, "failed": 0.0}[stage]
            ship.hull.turn_rate_max = max(1.0, 7.0 * factor)
        elif station == "sonar":
            extra = {"normal": 0.0, "degraded": 2.0, "damaged": 5.0, "failed": 10.0}[stage]
            ship.acoustics.bearing_noise_extra = extra
        elif station == "weapons":
            mult = {"normal": 1.0, "degraded": 1.2, "damaged": 1.5, "failed": 2.0}[stage]
            ship.weapons.time_penalty_multiplier = mult
        elif station == "engineering":
            # Reduce effective pumps and depth rate
            # For now, we limit ballast_ok threshold via maintenance below and rely on physics depth rate gate
            pass

    def _step_station_tasks(self, ship: Ship, dt: float) -> None:
        now_s = time.perf_counter()
        # Spawn logic per station
        for station in self._active_tasks.keys():
            if self._active_tasks[station] is None:
                self._task_spawn_timers[station] -= dt
                if self._task_spawn_timers[station] <= 0.0:
                    # Random spawn; interval depends on maintenance state of linked system
                    self._spawn_task_for(station, now_s)
                    # Next spawn after 60-120s
                    self._task_spawn_timers[station] = random.uniform(60.0, 120.0)

        # Progress active tasks based on power allocation for that station
        for station, task in self._active_tasks.items():
            if task is None:
                continue
            power_frac = self._station_power_fraction(ship, station)
            # Only one task per station; progress toward completion
            task.time_remaining_s = max(0.0, task.time_remaining_s - dt)
            # Progress only if explicitly started by station crew (clicked Repair)
            if task.started:
                task.progress = min(1.0, task.progress + (0.2 * power_frac) * dt)  # completes in ~5s at full power
            # If completed
            if task.progress >= 1.0:
                # Apply recovery: maintenance bump and clear penalties
                ship.maintenance.levels[task.system] = min(1.0, ship.maintenance.levels.get(task.system, 1.0) + 0.1)
                self._apply_stage_penalties(ship, station, "normal")
                self._active_tasks[station] = None
                continue
            # If deadline passed without completion → escalate stage
            if task.time_remaining_s <= 0.0:
                if task.stage == "normal":
                    task.stage = "degraded"
                    task.base_deadline_s *= 1.25
                elif task.stage == "degraded":
                    task.stage = "damaged"
                    task.base_deadline_s *= 1.5
                elif task.stage == "damaged":
                    task.stage = "failed"
                # Reset countdown for next stage unless failed
                if task.stage != "failed":
                    task.time_remaining_s = task.base_deadline_s
                # Apply penalties immediately
                self._apply_stage_penalties(ship, station, task.stage)
                # Also degrade maintenance slowly downwards
                ship.maintenance.levels[task.system] = max(0.0, ship.maintenance.levels.get(task.system, 1.0) - (0.05 if task.stage == "degraded" else 0.1))

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
            "acoustics": {"noiseBudget": noise_budget, "detectability": detectability, "emconRisk": ("high" if noise_budget >= 75 else "med" if noise_budget >= 40 else "low")},
            "events": list(self._transient_events),
        }

        tel_all = dict(base)
        tel_captain = {**base, "periscopeRaised": self._periscope_raised, "radioRaised": self._radio_raised, "mission": {"title": self.mission_brief["title"], "objective": self.mission_brief["objective"], "roe": self.mission_brief["roe"]}, "comms": getattr(self, "_captain_comms", [])}
        tel_helm = {**base, "cavitationSpeedWarn": speed > 25.0, "thermocline": own.acoustics.thermocline_on, "task": (None if self._active_tasks['helm'] is None else self._active_tasks['helm'].__dict__)}
        # Prepare recent active ping responses list (bearing, range_est, strength, time)
        # For now, only generate on demand when 'sonar.ping' happens; UI will render as DEMON dots
        if not hasattr(self, "_last_ping_responses"):
            self._last_ping_responses = []
        tel_sonar = {**base, "contacts": [c.dict() for c in contacts], "pingCooldown": max(0.0, self.active_ping_state.timer), "pingResponses": list(self._last_ping_responses), "lastPingAt": getattr(self, "_last_ping_at", None), "task": (None if self._active_tasks['sonar'] is None else self._active_tasks['sonar'].__dict__)}
        tel_weapons = {**base, "tubes": [t.dict() for t in own.weapons.tubes], "consentRequired": CONFIG.require_captain_consent, "captainConsent": self._captain_consent, "task": (None if self._active_tasks['weapons'] is None else self._active_tasks['weapons'].__dict__)}
        tel_engineering = {**base, "reactor": own.reactor.dict(), "pumps": {"fwd": self._pump_fwd, "aft": self._pump_aft}, "damage": own.damage.dict(), "power": own.power.dict(), "systems": own.systems.dict(), "maintenance": own.maintenance.levels, "task": (None if self._active_tasks['engineering'] is None else self._active_tasks['engineering'].__dict__)}

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
            "ships": [
                {
                    "id": s.id, "side": s.side,
                    "x": s.kin.x, "y": s.kin.y, "depth": s.kin.depth,
                    "heading": s.kin.heading, "speed": s.kin.speed,
                    **bearings_to(s.kin.x, s.kin.y),
                    "range_from_own": (( ( (s.kin.x - own.kin.x)**2 + (s.kin.y - own.kin.y)**2 ) ** 0.5 )),
                }
                for s in self.world.all_ships() if s.id != own.id
            ],
            "torpedoes": list(self.world.torpedoes),
        }

        await BUS.publish("tick:all", {"topic": "telemetry", "data": tel_all})
        await BUS.publish("tick:captain", {"topic": "telemetry", "data": tel_captain})
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
            torp = try_fire(own, int(data.get("tube", 1)), float(data.get("bearing", own.kin.heading)), float(data.get("run_depth", own.kin.depth)))
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
            # Only one task per station; start if exists and not in progress
            t = self._active_tasks[station]
            if t is None:
                return "No task to start"
            # Mark as started (player clicked Repair)
            t.started = True
            # Return None to indicate accepted
            return None
        if topic == "station.task.defer":
            station = str(data.get("station", "")).lower()
            if station not in self._active_tasks:
                return "Unknown station"
            # Defer: small severity bump and respawn later
            t = self._active_tasks[station]
            if t is None:
                return None
            t.stage = "degraded"
            self._active_tasks[station] = None
            self._task_spawn_timers[station] = 15.0
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
        return None
