"""Typed actions a `ShipController` produces.

Each `Action` is a frozen dataclass that knows how to apply itself to a
`ShipControls` instance and returns a `ControlResult`. This is the stable
internal contract between "who decided" (controller) and "what happens"
(hands), independent of how the decision was made (LLM, human, scripted).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .hands import ControlResult, ShipControls


@dataclass(frozen=True)
class Action:
    """Base class for ship actions. Subclasses implement `apply`."""

    def apply(self, controls: ShipControls) -> ControlResult:  # pragma: no cover
        raise NotImplementedError

    @property
    def name(self) -> str:
        """A short, stable identifier for telemetry/logging."""
        return type(self).__name__


@dataclass(frozen=True)
class SetNavAction(Action):
    heading: Optional[float] = None
    speed: Optional[float] = None
    depth: Optional[float] = None

    def apply(self, controls: ShipControls) -> ControlResult:
        return controls.set_nav(heading=self.heading, speed=self.speed, depth=self.depth)


@dataclass(frozen=True)
class FireTorpedoAction(Action):
    bearing: Optional[float] = None
    run_depth: Optional[float] = None
    enable_range: Optional[float] = None
    doctrine: str = "passive_then_active"

    def apply(self, controls: ShipControls) -> ControlResult:
        return controls.fire_torpedo(
            bearing=self.bearing,
            run_depth=self.run_depth,
            enable_range=self.enable_range,
            doctrine=self.doctrine,
        )


@dataclass(frozen=True)
class DropDepthChargesAction(Action):
    spread_meters: float = 20.0
    min_depth: float = 30.0
    max_depth: float = 50.0
    spread_size: int = 3

    def apply(self, controls: ShipControls) -> ControlResult:
        return controls.drop_depth_charges(
            spread_meters=self.spread_meters,
            min_depth=self.min_depth,
            max_depth=self.max_depth,
            spread_size=self.spread_size,
        )


@dataclass(frozen=True)
class DeployCountermeasureAction(Action):
    type_: str = "noisemaker"

    def apply(self, controls: ShipControls) -> ControlResult:
        return controls.deploy_countermeasure(self.type_)


@dataclass(frozen=True)
class ActivePingAction(Action):

    def apply(self, controls: ShipControls) -> ControlResult:
        return controls.active_ping()
