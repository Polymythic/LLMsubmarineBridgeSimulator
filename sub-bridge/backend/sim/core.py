"""Pure physics step for the simulation.

`SimulationCore.step_physics` is the place where all "physical" tick work
lives: kinematics integration, weapon timers, projectile stepping, damage,
engineering, and ship destruction detection. It is callable from a plain
pytest — no asyncio, no BUS, no LLM orchestrator, no telemetry broadcast.

The method mutates the world it was constructed with (ships move, projectiles
advance, depth charges detonate). Side effects that need to escape the core
(events, cavitation, system-failure factors, sonar-relevant explosions) are
returned in `CoreStepResult` for the caller to dispatch to telemetry, BUS,
event storage, and so on.

What stays *out* of the core (and remains in `Simulation.tick`):

- Sensor/sonar updates and contact aggregation.
- AI orchestrator scheduling.
- Mission rules (waypoints, triggers, victory, intercepts).
- Captain comms + station tasks (timer-driven sim state, not physics).
- Telemetry broadcasts.
- Logging and database `insert_event` calls.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .damage import step_damage, step_engineering
from .ecs import World
from .physics import integrate_kinematics
from .weapons import (
    step_countermeasure,
    step_depth_charge,
    step_torpedo,
    step_tubes,
)


@dataclass
class CoreEvent:
    """An event produced by physics that the caller needs to react to."""

    kind: str
    payload: Dict[str, Any]


@dataclass
class CoreStepResult:
    events: List[CoreEvent] = field(default_factory=list)
    cavitation: bool = False
    system_failures: Dict[str, Any] = field(default_factory=dict)
    sonar_explosions: List[Dict[str, Any]] = field(default_factory=list)
    destroyed_ship_ids: List[str] = field(default_factory=list)


class SimulationCore:
    """Stateless-ish physics core. Mutates the bound `World`; returns events."""

    def __init__(self, world: World) -> None:
        self._world = world

    def step_physics(
        self,
        dt: float,
        ordered: Dict[str, float],
        pump_assignments: Optional[Dict[int, int]] = None,
        enemy_static: bool = False,
    ) -> CoreStepResult:
        """Advance one physics tick.

        - `ordered`: ownship ordered heading/speed/depth (the helm panel).
        - `pump_assignments`: dict from pump index to compartment index. Pumps
          assigned anywhere give a ballast boost to depth changes.
        - `enemy_static`: if True, enemy ships do not move. Used to gate
          enemy-AI-driven motion; the core itself doesn't care why.
        """
        result = CoreStepResult()
        world = self._world
        own = world.get_ship("ownship")
        if own is None:
            return result

        pump_assignments = pump_assignments or {}
        ballast_boost = len(pump_assignments) > 0

        # Ownship kinematics
        cav, _, _, _ = integrate_kinematics(
            own,
            ordered["heading"],
            ordered["speed"],
            ordered["depth"],
            dt,
            ballast_boost=ballast_boost,
        )
        result.cavitation = bool(cav)

        # Weapon-tube timers (all ships)
        for s in world.all_ships():
            step_tubes(s, dt)

        # Enemy kinematics
        if not enemy_static:
            for ship in world.all_ships():
                if ship.id == "ownship":
                    continue
                integrate_kinematics(
                    ship, ship.kin.heading, ship.kin.speed, ship.kin.depth, dt
                )

        # Torpedoes
        if world.torpedoes:
            for t in list(world.torpedoes):
                def _on_torp_event(name: str, payload: Dict[str, Any], _t=t) -> None:
                    result.events.append(CoreEvent(kind=name, payload=payload))
                    if name == "torpedo.detonated":
                        brg = self._bearing_from_ownship(own, payload)
                        if brg is not None:
                            result.sonar_explosions.append(
                                {"bearing": brg, "source": "torpedo"}
                            )

                step_torpedo(
                    t, world, dt, on_event=_on_torp_event, countermeasures=world.countermeasures
                )
                if t.get("run_time", 0.0) > t.get("max_run_time", 0.0):
                    world.torpedoes.remove(t)

        # Depth charges
        if getattr(world, "depth_charges", None):
            for dc in list(world.depth_charges):
                def _on_dc_event(name: str, payload: Dict[str, Any], _dc=dc) -> None:
                    result.events.append(CoreEvent(kind=name, payload=payload))
                    if name == "depth_charge.detonated":
                        brg = self._bearing_from_ownship(own, payload)
                        if brg is not None:
                            result.sonar_explosions.append({"bearing": brg})

                step_depth_charge(dc, world, dt, on_event=_on_dc_event)
                if dc.get("exploded", False) or dc.get("depth", 0.0) > 1000.0:
                    world.depth_charges.remove(dc)

        # Countermeasures
        if world.countermeasures:
            for cm in list(world.countermeasures):
                if not step_countermeasure(cm, dt):
                    world.countermeasures.remove(cm)

        # Ship destruction (hull damage >= 1.0). Caller is responsible for
        # deduping repeat detections via its own destroyed-set.
        for ship in world.all_ships():
            if ship.damage.hull >= 1.0:
                result.destroyed_ship_ids.append(ship.id)
                result.events.append(
                    CoreEvent(
                        kind="ship.destroyed",
                        payload={
                            "ship_id": ship.id,
                            "x": ship.kin.x,
                            "y": ship.kin.y,
                        },
                    )
                )

        # Damage / engineering on ownship
        result.system_failures = step_damage(own, dt, pump_assignments=pump_assignments) or {}
        step_engineering(own, dt)

        return result

    # ------------------------------------------------------------------ #

    @staticmethod
    def _bearing_from_ownship(own, payload: Dict[str, Any]) -> Optional[float]:
        try:
            tx = float(payload.get("x", 0.0))
            ty = float(payload.get("y", 0.0))
            dx = tx - own.kin.x
            dy = ty - own.kin.y
            return math.degrees(math.atan2(dx, dy)) % 360.0
        except Exception:
            return None
