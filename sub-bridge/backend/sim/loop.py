from __future__ import annotations
import asyncio
import json
import time
from typing import Dict, Optional
from ..bus import BUS
from ..config import CONFIG
from ..models import Ship, Kinematics, Hull, Acoustics, WeaponsSuite, Reactor, DamageState
from ..storage import init_engine, create_run, insert_snapshot, insert_event
from .ecs import World
from .physics import integrate_kinematics
from .sonar import passive_contacts, ActivePingState, active_ping
from .weapons import try_load_tube, try_flood_tube, try_set_doors, try_fire, step_torpedo, step_tubes
from .ai_tools import LocalAIStub


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

    def stop(self) -> None:
        self._stop.set()

    def set_captain_consent(self, consent: bool) -> None:
        self._captain_consent = consent

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

        # Enemy AI proposals (optional)
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

        # Step weapons tube timers
        step_tubes(own, dt)

        # Move RED ships if not static testing
        for ship in self.world.all_ships():
            if ship.id == "ownship":
                continue
            if CONFIG.enemy_static:
                # Keep red static; no integration
                continue
            integrate_kinematics(ship, ship.kin.heading, ship.kin.speed, ship.kin.depth, dt)

        if self.world.torpedoes:
            for t in list(self.world.torpedoes):
                step_torpedo(t, self.world, dt)
                if t["run_time"] > t["max_run_time"]:
                    self.world.torpedoes.remove(t)

        self.active_ping_state.tick(dt)
        contacts = passive_contacts(own, [s for s in self.world.all_ships() if s.id != own.id])

        base = {
            "ownship": {
                "heading": heading,
                "orderedHeading": self.ordered["heading"],
                "speed": speed,
                "depth": depth,
                "cavitation": cav,
            },
            "events": [],
        }

        tel_all = dict(base)
        tel_captain = {**base, "periscopeRaised": self._periscope_raised, "radioRaised": self._radio_raised}
        tel_helm = {**base, "cavitationSpeedWarn": speed > 25.0}
        tel_sonar = {**base, "contacts": [c.dict() for c in contacts], "pingCooldown": max(0.0, self.active_ping_state.timer)}
        tel_weapons = {**base, "tubes": [t.dict() for t in own.weapons.tubes], "consentRequired": CONFIG.require_captain_consent, "captainConsent": self._captain_consent}
        tel_engineering = {**base, "reactor": own.reactor.dict(), "pumps": {"fwd": self._pump_fwd, "aft": self._pump_aft}}

        await BUS.publish("tick:all", {"topic": "telemetry", "data": tel_all})
        await BUS.publish("tick:captain", {"topic": "telemetry", "data": tel_captain})
        await BUS.publish("tick:helm", {"topic": "telemetry", "data": tel_helm})
        await BUS.publish("tick:sonar", {"topic": "telemetry", "data": tel_sonar})
        await BUS.publish("tick:weapons", {"topic": "telemetry", "data": tel_weapons})
        await BUS.publish("tick:engineering", {"topic": "telemetry", "data": tel_engineering})

        self._last_snapshot += dt
        if self._last_snapshot >= CONFIG.snapshot_s:
            self._last_snapshot = 0.0
            insert_snapshot(self.engine, self.run_id, heading, speed, depth)

    async def handle_command(self, topic: str, data: Dict) -> Optional[str]:
        own = self.world.get_ship("ownship")
        if topic == "helm.order":
            self.ordered["heading"] = float(data.get("heading", self.ordered["heading"])) % 360
            self.ordered["speed"] = float(data.get("speed", self.ordered["speed"]))
            self.ordered["depth"] = max(0.0, float(data.get("depth", self.ordered["depth"])))
            return None
        if topic == "sonar.ping":
            if self.active_ping_state.start():
                _ = active_ping(own, [s for s in self.world.all_ships() if s.id != own.id])
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
        if topic == "engineering.pump.toggle":
            name = str(data.get("pump", "")).lower()
            state = bool(data.get("enabled", True))
            if name == "fwd":
                self._pump_fwd = state
            elif name == "aft":
                self._pump_aft = state
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
        return None
