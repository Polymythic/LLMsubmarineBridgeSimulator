"""Direct unit tests for `SimulationCore.step_physics`.

No Simulation, no asyncio, no BUS, no AI. Pure pytest construction of a
World and a core; tick repeatedly and inspect the `CoreStepResult`.
"""
import pytest

from backend.models import ShipCapabilities
from backend.sim.core import CoreStepResult, SimulationCore
from backend.sim.ecs import World

from conftest import make_ship


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_world(ownship=None, *others) -> World:
    own = ownship if ownship is not None else make_ship(id_="ownship", side="BLUE")
    w = World()
    w.add_ship(own)
    for s in others:
        w.add_ship(s)
    return w


# --------------------------------------------------------------------------- #
# Kinematics
# --------------------------------------------------------------------------- #

def test_step_physics_advances_ownship_heading_toward_ordered():
    own = make_ship(id_="ownship", heading=0.0, speed=0.0)
    world = _make_world(own)
    core = SimulationCore(world)

    for _ in range(50):
        core.step_physics(dt=0.5, ordered={"heading": 90.0, "speed": 0.0, "depth": own.kin.depth})

    # Should be turning toward 90; not strict equality (turn rate limited)
    assert own.kin.heading > 30.0


def test_step_physics_advances_enemy_kinematics():
    own = make_ship(id_="ownship", side="BLUE")
    enemy = make_ship(id_="red-01", side="RED", x=0.0, y=0.0, heading=0.0, speed=10.0)
    world = _make_world(own, enemy)
    core = SimulationCore(world)

    y_start = enemy.kin.y
    for _ in range(20):
        core.step_physics(dt=1.0, ordered={"heading": 0.0, "speed": 0.0, "depth": 100.0})

    assert enemy.kin.y > y_start  # moved north (heading 0)


def test_step_physics_enemy_static_keeps_enemies_in_place():
    own = make_ship(id_="ownship", side="BLUE")
    enemy = make_ship(id_="red-01", side="RED", x=0.0, y=0.0, heading=0.0, speed=10.0)
    world = _make_world(own, enemy)
    core = SimulationCore(world)

    y_start = enemy.kin.y
    for _ in range(5):
        core.step_physics(
            dt=1.0,
            ordered={"heading": 0.0, "speed": 0.0, "depth": 100.0},
            enemy_static=True,
        )

    assert enemy.kin.y == pytest.approx(y_start)


# --------------------------------------------------------------------------- #
# Result shape
# --------------------------------------------------------------------------- #

def test_step_physics_returns_core_step_result():
    own = make_ship(id_="ownship")
    world = _make_world(own)
    core = SimulationCore(world)

    result = core.step_physics(
        dt=0.1,
        ordered={"heading": own.kin.heading, "speed": own.kin.speed, "depth": own.kin.depth},
    )
    assert isinstance(result, CoreStepResult)
    assert isinstance(result.events, list)
    assert isinstance(result.cavitation, bool)
    assert isinstance(result.system_failures, dict)
    assert isinstance(result.sonar_explosions, list)
    assert isinstance(result.destroyed_ship_ids, list)


def test_step_physics_no_ownship_returns_empty_result():
    world = World()  # no ownship
    core = SimulationCore(world)
    result = core.step_physics(dt=1.0, ordered={"heading": 0, "speed": 0, "depth": 0})
    assert result.events == []


# --------------------------------------------------------------------------- #
# Destruction
# --------------------------------------------------------------------------- #

def test_step_physics_emits_ship_destroyed_event_when_hull_full():
    own = make_ship(id_="ownship", side="BLUE")
    enemy = make_ship(id_="red-01", side="RED", x=2000.0, y=0.0)
    enemy.damage.hull = 1.0
    world = _make_world(own, enemy)
    core = SimulationCore(world)

    result = core.step_physics(
        dt=0.1,
        ordered={"heading": 0.0, "speed": 0.0, "depth": 100.0},
        enemy_static=True,
    )

    destroyed = [e for e in result.events if e.kind == "ship.destroyed"]
    assert len(destroyed) == 1
    assert destroyed[0].payload["ship_id"] == "red-01"
    assert "red-01" in result.destroyed_ship_ids


# --------------------------------------------------------------------------- #
# Damage / engineering
# --------------------------------------------------------------------------- #

def test_step_physics_returns_system_failures_dict():
    own = make_ship(id_="ownship")
    # Force severe damage to surface failure factors
    own.damage.hull = 0.5
    world = _make_world(own)
    core = SimulationCore(world)

    result = core.step_physics(
        dt=0.5,
        ordered={"heading": own.kin.heading, "speed": own.kin.speed, "depth": own.kin.depth},
    )
    # system_failures may be empty if no flooding triggered; just assert shape
    assert isinstance(result.system_failures, dict)


# --------------------------------------------------------------------------- #
# Pump assignments / ballast boost
# --------------------------------------------------------------------------- #

def test_step_physics_pump_assignments_default_empty_is_safe():
    """No pumps assigned: physics still runs without error."""
    own = make_ship(id_="ownship")
    world = _make_world(own)
    core = SimulationCore(world)
    core.step_physics(
        dt=0.1,
        ordered={"heading": own.kin.heading, "speed": own.kin.speed, "depth": own.kin.depth},
    )
