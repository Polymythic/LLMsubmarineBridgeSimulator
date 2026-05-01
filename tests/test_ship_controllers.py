"""Tests for `ShipController`, `Action` dataclasses, and `tool_calls_to_actions`."""
import asyncio

import pytest

from backend.models import ShipCapabilities
from backend.sim.ai_orchestrator import AgentsOrchestrator
from backend.sim.control import (
    ActivePingAction,
    DeployCountermeasureAction,
    DropDepthChargesAction,
    FireTorpedoAction,
    LLMShipController,
    ScriptedShipController,
    SetNavAction,
    ShipControls,
    tool_calls_to_actions,
)
from backend.sim.ecs import World

from conftest import StubLLMEngine, install_stub_engines, make_ship


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _destroyer_caps() -> ShipCapabilities:
    return ShipCapabilities(
        can_set_nav=True,
        has_active_sonar=True,
        has_torpedoes=True,
        has_depth_charges=True,
        countermeasures=[],
    )


def _make_world_with_destroyer() -> World:
    own = make_ship(id_="ownship", side="BLUE")
    red = make_ship(id_="red-01", side="RED", ship_class="Destroyer", x=3000.0, y=0.0)
    red.capabilities = _destroyer_caps()
    red.weapons.depth_charges_stored = 30
    w = World()
    w.add_ship(own)
    w.add_ship(red)
    return w


# --------------------------------------------------------------------------- #
# tool_calls_to_actions
# --------------------------------------------------------------------------- #

def test_tool_calls_to_actions_set_nav():
    actions = tool_calls_to_actions([{
        "tool": "set_nav",
        "arguments": {"heading": 90.0, "speed": 12.0, "depth": 0.0},
    }])
    assert len(actions) == 1
    a = actions[0]
    assert isinstance(a, SetNavAction)
    assert a.heading == 90.0
    assert a.speed == 12.0


def test_tool_calls_to_actions_unwraps_list_of_one():
    actions = tool_calls_to_actions([{
        "tool": "set_nav",
        "arguments": {"heading": [120.0], "speed": 10.0, "depth": 0.0},
    }])
    assert actions[0].heading == 120.0


def test_tool_calls_to_actions_fire_torpedo_aliases_quick_launch():
    """The orchestrator may emit either 'fire_torpedo' or 'launch_torpedo_quick';
    both translate to FireTorpedoAction."""
    actions = tool_calls_to_actions([
        {"tool": "fire_torpedo", "arguments": {"bearing": 270.0}},
        {"tool": "launch_torpedo_quick", "arguments": {"bearing": 90.0}},
    ])
    assert all(isinstance(a, FireTorpedoAction) for a in actions)
    assert actions[0].bearing == 270.0
    assert actions[1].bearing == 90.0


def test_tool_calls_to_actions_drop_depth_charges():
    actions = tool_calls_to_actions([{
        "tool": "drop_depth_charges",
        "arguments": {"spread_meters": 30, "minDepth": 25, "maxDepth": 80, "spreadSize": 5},
    }])
    a = actions[0]
    assert isinstance(a, DropDepthChargesAction)
    assert a.spread_size == 5
    assert a.max_depth == 80.0


def test_tool_calls_to_actions_drops_unknown_tools():
    actions = tool_calls_to_actions([
        {"tool": "set_fleet_intent", "arguments": {}},
        {"tool": "write_journal", "arguments": {"text": "x"}},
        {"tool": "set_nav", "arguments": {"heading": 0.0}},
    ])
    # set_fleet_intent and write_journal are fleet-level; only set_nav remains.
    assert len(actions) == 1
    assert isinstance(actions[0], SetNavAction)


# --------------------------------------------------------------------------- #
# Action.apply through ShipControls
# --------------------------------------------------------------------------- #

def test_set_nav_action_apply_changes_kinematics():
    world = _make_world_with_destroyer()
    red = world.get_ship("red-01")
    controls = ShipControls(red, world)

    SetNavAction(heading=270.0, speed=8.0, depth=0.0).apply(controls)

    assert red.kin.heading == pytest.approx(270.0)
    assert red.kin.speed == pytest.approx(8.0)


def test_fire_torpedo_action_apply_appends_torpedo():
    world = _make_world_with_destroyer()
    red = world.get_ship("red-01")
    controls = ShipControls(red, world)

    r = FireTorpedoAction(bearing=270.0, run_depth=100.0).apply(controls)

    assert r.ok
    assert len(world.torpedoes) == 1


def test_active_ping_action_apply_returns_responses():
    world = _make_world_with_destroyer()
    red = world.get_ship("red-01")
    controls = ShipControls(red, world)

    r = ActivePingAction().apply(controls)
    assert r.ok
    assert red.active_sonar_cooldown > 0.0


# --------------------------------------------------------------------------- #
# ScriptedShipController
# --------------------------------------------------------------------------- #

def test_scripted_controller_replays_queued_actions():
    ctrl = ScriptedShipController({
        "red-01": [
            [SetNavAction(heading=90.0)],
            [FireTorpedoAction(bearing=270.0)],
        ]
    })

    first = asyncio.run(ctrl.step("red-01"))
    second = asyncio.run(ctrl.step("red-01"))
    third = asyncio.run(ctrl.step("red-01"))

    assert isinstance(first[0], SetNavAction)
    assert isinstance(second[0], FireTorpedoAction)
    assert third == []  # queue empty


def test_scripted_controller_unknown_ship_returns_empty():
    ctrl = ScriptedShipController()
    actions = asyncio.run(ctrl.step("nobody"))
    assert actions == []


def test_scripted_controller_queue_method():
    ctrl = ScriptedShipController()
    ctrl.queue("red-01", [SetNavAction(heading=45.0)])
    actions = asyncio.run(ctrl.step("red-01"))
    assert isinstance(actions[0], SetNavAction)


# --------------------------------------------------------------------------- #
# LLMShipController
# --------------------------------------------------------------------------- #

def test_llm_controller_translates_orchestrator_output():
    world = _make_world_with_destroyer()
    stub = StubLLMEngine(ship_responses={
        "red-01": [{
            "tool": "set_nav",
            "arguments": {"heading": 180.0, "speed": 6.0, "depth": 0.0},
            "summary": "head south",
        }]
    })
    orch = AgentsOrchestrator(lambda: world, storage_engine=None, run_id=0)
    install_stub_engines(orch, stub)

    ctrl = LLMShipController(lambda: orch)
    actions = asyncio.run(ctrl.step("red-01"))

    assert len(actions) == 1
    assert isinstance(actions[0], SetNavAction)
    assert actions[0].heading == 180.0


def test_llm_controller_handles_missing_orchestrator():
    """If the orchestrator is None, the controller produces an empty step
    rather than crashing. Useful while the simulation is loading."""
    ctrl = LLMShipController(lambda: None)
    actions = asyncio.run(ctrl.step("any-ship"))
    assert actions == []
