"""Ship control surface ("hands").

`ShipControls` is the single chokepoint for mutating a ship as a result of an
action (AI tool call or human command). Methods are gated by
`ship.capabilities`; calls that aren't supported on the platform return a
failed `ControlResult` rather than raising.

World-level side effects that are *intrinsic to the action* (a fired torpedo
appears in the world, dropped depth charges appear in the world, etc.) live
here. Side effects that are *the simulation reacting* to the action (e.g.
counter-detection contacts created when an enemy pings) stay in the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from ...models import Ship
from ..ecs import World
from ..sonar import active_ping as _do_active_ping
from ..weapons import (
    try_deploy_countermeasure,
    try_drop_depth_charges,
    try_launch_torpedo_quick,
)


@dataclass(frozen=True)
class ControlResult:
    """Outcome of a `ShipControls` method call.

    `ok=False` results carry an `error` describing why; callers may log it or
    surface it to the operator. `data` carries action-specific payloads
    (the spawned torpedo, list of dropped charges, ping responses, etc).
    """

    ok: bool
    error: Optional[str] = None
    data: Any = None

    @classmethod
    def success(cls, data: Any = None) -> "ControlResult":
        return cls(ok=True, error=None, data=data)

    @classmethod
    def fail(cls, error: str) -> "ControlResult":
        return cls(ok=False, error=error, data=None)


class ShipControls:
    """Action surface for one ship.

    A single instance binds a `Ship` and the `World` it lives in. Methods are
    capability-gated against `ship.capabilities` (an empty/missing
    capabilities object is treated as 'standard nav-only platform').
    """

    def __init__(self, ship: Ship, world: World) -> None:
        self._ship = ship
        self._world = world

    @property
    def ship(self) -> Ship:
        return self._ship

    # -------------------------------------------------------------------- #
    # Navigation
    # -------------------------------------------------------------------- #

    def set_nav(
        self,
        heading: Optional[float] = None,
        speed: Optional[float] = None,
        depth: Optional[float] = None,
    ) -> ControlResult:
        """Set ordered heading / speed / depth, clamped to hull limits.

        `None` for any parameter leaves that axis unchanged. Heading is
        normalized modulo 360. Speed is clamped to `[0, hull.max_speed]`.
        Depth is clamped to `[0, hull.max_depth]`.
        """
        ship = self._ship
        caps = getattr(ship, "capabilities", None)
        if caps is not None and not getattr(caps, "can_set_nav", True):
            return ControlResult.fail("set_nav unsupported")
        if heading is not None:
            ship.kin.heading = float(heading) % 360.0
        if speed is not None:
            ship.kin.speed = max(0.0, min(ship.hull.max_speed, float(speed)))
        if depth is not None:
            ship.kin.depth = max(0.0, min(ship.hull.max_depth, float(depth)))
        return ControlResult.success()

    # -------------------------------------------------------------------- #
    # Weapons
    # -------------------------------------------------------------------- #

    def fire_torpedo(
        self,
        bearing: Optional[float] = None,
        run_depth: Optional[float] = None,
        enable_range: Optional[float] = None,
        doctrine: str = "passive_then_active",
    ) -> ControlResult:
        """AI-style quick-launch torpedo. Consumes inventory; 5s cooldown."""
        ship = self._ship
        caps = getattr(ship, "capabilities", None)
        if caps is None or not getattr(caps, "has_torpedoes", False):
            return ControlResult.fail("no torpedoes")
        bearing_v = float(bearing) if bearing is not None else float(ship.kin.heading)
        run_depth_v = float(run_depth) if run_depth is not None else float(ship.kin.depth)
        enable_range_v = float(enable_range) if enable_range is not None else 800.0
        res = try_launch_torpedo_quick(ship, bearing_v, run_depth_v, enable_range_v, doctrine)
        if not res.get("ok"):
            return ControlResult.fail(str(res.get("error", "fire failed")))
        torp = res.get("data")
        if torp:
            self._world.torpedoes.append(torp)
        return ControlResult.success(torp)

    def drop_depth_charges(
        self,
        spread_meters: float = 20.0,
        min_depth: float = 30.0,
        max_depth: float = 50.0,
        spread_size: int = 3,
    ) -> ControlResult:
        ship = self._ship
        caps = getattr(ship, "capabilities", None)
        if caps is None or not getattr(caps, "has_depth_charges", False):
            return ControlResult.fail("no depth charges")
        res = try_drop_depth_charges(
            ship,
            int(spread_meters),
            int(min_depth),
            int(max_depth),
            int(spread_size),
        )
        if not res.get("ok"):
            return ControlResult.fail(str(res.get("error", "drop failed")))
        spawned: List[Any] = list(res.get("data", []) or [])
        for dc in spawned:
            self._world.depth_charges.append(dc)
        return ControlResult.success(spawned)

    def deploy_countermeasure(self, type_: str) -> ControlResult:
        ship = self._ship
        caps = getattr(ship, "capabilities", None)
        cms = getattr(caps, "countermeasures", None) if caps else None
        if not cms or type_ not in cms:
            return ControlResult.fail(f"countermeasure '{type_}' unsupported")
        res = try_deploy_countermeasure(ship, type_)
        if not res.get("ok"):
            return ControlResult.fail(str(res.get("error", "deploy failed")))
        cm = res.get("data")
        if cm:
            self._world.countermeasures.append(cm)
        return ControlResult.success(cm)

    # -------------------------------------------------------------------- #
    # Sensors
    # -------------------------------------------------------------------- #

    def active_ping(self) -> ControlResult:
        """Emit an active sonar ping. Returns raw responses; 12s cooldown.

        The simulation-level reaction (counter-detection contacts on
        adversaries, transient UI events) is the caller's responsibility.
        """
        ship = self._ship
        caps = getattr(ship, "capabilities", None)
        if caps is None or not getattr(caps, "has_active_sonar", False):
            return ControlResult.fail("no active sonar")
        if getattr(ship, "active_sonar_cooldown", 0.0) > 0.0:
            return ControlResult.fail("ping on cooldown")
        responses: List[Tuple[str, float, float, float]] = _do_active_ping(
            ship,
            [s for s in self._world.all_ships() if s.id != ship.id],
        )
        ship.active_sonar_cooldown = 12.0
        return ControlResult.success(responses)
