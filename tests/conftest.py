"""Shared test fixtures for submarine bridge simulator tests."""
import os
import sys
from typing import Any, Dict, List, Optional

import pytest

# Ensure sub-bridge backend is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from backend.models import (
    Ship, Kinematics, Hull, Acoustics, WeaponsSuite, Reactor,
    DamageState, PowerAllocations, SystemsStatus, MaintenanceState,
    Waypoint, WaypointRoute, CompartmentState,
)
from backend.sim.ecs import World
from backend.sim.ai_engines import BaseEngine


def make_ship(
    id_: str = "ownship",
    side: str = None,
    ship_class: str = "SSN",
    x: float = 0.0,
    y: float = 0.0,
    depth: float = 100.0,
    heading: float = 0.0,
    speed: float = 0.0,
) -> Ship:
    """Create a test ship with sensible defaults and full subsystems."""
    if side is None:
        side = "BLUE" if id_ == "ownship" else "RED"
    return Ship(
        id=id_,
        side=side,
        ship_class=ship_class,
        kin=Kinematics(x=x, y=y, depth=depth, heading=heading, speed=speed),
        hull=Hull(max_depth=300.0, max_speed=25.0, quiet_speed=5.0),
        acoustics=Acoustics(
            thermocline_on=False,
            source_level_by_speed={5: 110.0, 15: 125.0, 25: 135.0},
        ),
        weapons=WeaponsSuite(tube_count=4, torpedoes_stored=16, tubes=[]),
        reactor=Reactor(),
        damage=DamageState(),
        power=PowerAllocations(),
        systems=SystemsStatus(),
        maintenance=MaintenanceState(),
    )


def make_world(*ships: Ship) -> World:
    """Create a World and add ships to it."""
    world = World()
    for ship in ships:
        world.add_ship(ship)
    return world


def make_test_simulation():
    """Construct a `Simulation` with an active default ownship for tests.

    `loop.py:_init_default_world()` skips mission loading when
    `PYTEST_CURRENT_TEST` is set, leaving an empty world and an inactive
    mission. Tests that need an `ownship` and `_mission_active=True` use
    this helper instead of `Simulation()` directly.
    """
    from backend.sim.loop import Simulation

    sim = Simulation()
    if sim.world.get_ship("ownship") is None:
        own = make_ship(id_="ownship")
        sim.world.add_ship(own)
        sim.ordered = {
            "heading": own.kin.heading,
            "speed": own.kin.speed,
            "depth": own.kin.depth,
        }
    sim._mission_active = True
    return sim


class StubLLMEngine(BaseEngine):
    """Test engine returning canned responses.

    Construct with ordered queues of fleet/ship responses; each call pops the
    next response. Recorded inputs are exposed for assertions. Unlike the
    production StubEngine, this one is intended to drive the orchestrator end
    to end in tests, so its return shapes match what real LLM engines produce.
    """

    def __init__(
        self,
        fleet_responses: Optional[List[Dict[str, Any]]] = None,
        ship_responses: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        default_fleet: Optional[Dict[str, Any]] = None,
        default_ship: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.fleet_responses = list(fleet_responses or [])
        self.ship_responses = {sid: list(seq) for sid, seq in (ship_responses or {}).items()}
        self.default_fleet = default_fleet
        self.default_ship = default_ship
        self.fleet_calls: List[Dict[str, Any]] = []
        self.ship_calls: List[tuple] = []
        self._last_call_meta: Dict[str, Any] = {"provider": "test_stub"}

    async def propose_fleet_intent(self, fleet_summary: Dict[str, Any]) -> Dict[str, Any]:
        self.fleet_calls.append(fleet_summary)
        if self.fleet_responses:
            return self.fleet_responses.pop(0)
        if self.default_fleet is not None:
            return self.default_fleet
        return {
            "objectives": {},
            "emcon": {"active_ping_allowed": False, "radio_discipline": "restricted"},
            "summary": "stub fleet hold",
            "notes": [],
        }

    async def propose_ship_tool(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        self.ship_calls.append((ship.id, ship_summary))
        queue = self.ship_responses.get(ship.id)
        if queue:
            return queue.pop(0)
        if self.default_ship is not None:
            return self.default_ship
        return {
            "tool": "set_nav",
            "arguments": {
                "heading": ship.kin.heading,
                "speed": ship.kin.speed,
                "depth": ship.kin.depth,
            },
            "summary": "hold course",
        }


@pytest.fixture
def stub_llm_engine() -> StubLLMEngine:
    """Empty stub engine; tests can populate response queues as needed."""
    return StubLLMEngine()


def install_stub_engines(orchestrator, stub: StubLLMEngine, kind: str = "test") -> None:
    """Inject `stub` as both fleet and ship engine on `orchestrator`.

    Sets `_*_engine_kind` to a non-"stub" value to bypass the disabled-stub
    policy guard in `AgentsOrchestrator.run_ship`.
    """
    orchestrator._fleet_engine = stub
    orchestrator._ship_engine = stub
    orchestrator._fleet_engine_kind = kind
    orchestrator._ship_engine_kind = kind
    orchestrator._fleet_model = "test"
    orchestrator._ship_model = "test"
