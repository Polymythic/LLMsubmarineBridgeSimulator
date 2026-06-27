"""Tests for sub-bridge/backend/sim/conditions.py and triggers.py"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

import pytest

from conftest import make_ship, make_world
from backend.models import ScenarioCondition, ScenarioAction, ScenarioTrigger
from backend.sim.conditions import ConditionEvaluator
from backend.sim.triggers import TriggerManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_evaluator(*ships):
    world = make_world(*ships)
    ev = ConditionEvaluator(
        world_getter=lambda: world,
        sim_getter=lambda: None,
    )
    return ev, world


# ---------------------------------------------------------------------------
# ConditionEvaluator tests
# ---------------------------------------------------------------------------

def test_time_elapsed_true_when_past():
    ev, _ = _make_evaluator()
    ev.set_sim_time(120.0)
    cond = ScenarioCondition(type="time_elapsed", params={"at_s": 60})
    assert ev.evaluate(cond) is True


def test_time_elapsed_false_when_before():
    ev, _ = _make_evaluator()
    ev.set_sim_time(30.0)
    cond = ScenarioCondition(type="time_elapsed", params={"at_s": 60})
    assert ev.evaluate(cond) is False


def test_ship_destroyed_at_full_damage():
    ship = make_ship("target1", side="RED")
    ship.damage.hull = 1.0
    ev, _ = _make_evaluator(ship)
    cond = ScenarioCondition(type="ship_destroyed", params={"ship_id": "target1"})
    assert ev.evaluate(cond) is True


def test_ship_destroyed_missing_ship():
    ev, _ = _make_evaluator()
    cond = ScenarioCondition(type="ship_destroyed", params={"ship_id": "nonexistent"})
    # Missing ship is treated as destroyed
    assert ev.evaluate(cond) is True


def test_damage_threshold():
    ship = make_ship("sub1", side="BLUE")
    ship.damage.hull = 0.6
    ev, _ = _make_evaluator(ship)
    cond = ScenarioCondition(type="damage_threshold", params={
        "ship_id": "sub1", "damage_type": "hull", "threshold": 0.5
    })
    assert ev.evaluate(cond) is True

    cond2 = ScenarioCondition(type="damage_threshold", params={
        "ship_id": "sub1", "damage_type": "hull", "threshold": 0.9
    })
    assert ev.evaluate(cond2) is False


def test_distance_to_ship():
    s1 = make_ship("s1", side="BLUE", x=0, y=0)
    s2 = make_ship("s2", side="RED", x=300, y=400)
    ev, _ = _make_evaluator(s1, s2)
    cond = ScenarioCondition(type="distance_to", params={
        "from_ship": "s1", "to_ship": "s2", "max_distance_m": 600
    })
    assert ev.evaluate(cond) is True  # distance=500 < 600

    cond2 = ScenarioCondition(type="distance_to", params={
        "from_ship": "s1", "to_ship": "s2", "max_distance_m": 400
    })
    assert ev.evaluate(cond2) is False  # distance=500 > 400


def test_distance_to_point():
    s1 = make_ship("s1", side="BLUE", x=0, y=0)
    ev, _ = _make_evaluator(s1)
    cond = ScenarioCondition(type="distance_to", params={
        "from_ship": "s1", "to_point": [300, 400], "max_distance_m": 600
    })
    assert ev.evaluate(cond) is True


def test_all_of_compound_condition():
    ev, _ = _make_evaluator()
    ev.set_sim_time(120.0)
    cond = ScenarioCondition(type="all_of", params={
        "conditions": [
            {"type": "time_elapsed", "params": {"at_s": 60}},
            {"type": "time_elapsed", "params": {"at_s": 100}},
        ]
    })
    assert ev.evaluate(cond) is True

    ev.set_sim_time(80.0)
    assert ev.evaluate(cond) is False  # second subcondition fails


def test_any_of_compound_condition():
    ev, _ = _make_evaluator()
    ev.set_sim_time(80.0)
    cond = ScenarioCondition(type="any_of", params={
        "conditions": [
            {"type": "time_elapsed", "params": {"at_s": 60}},
            {"type": "time_elapsed", "params": {"at_s": 200}},
        ]
    })
    assert ev.evaluate(cond) is True  # first subcondition passes


# ---------------------------------------------------------------------------
# TriggerManager tests
# ---------------------------------------------------------------------------

def _make_trigger_manager(*ships):
    world = make_world(*ships)
    ev = ConditionEvaluator(
        world_getter=lambda: world,
        sim_getter=lambda: None,
    )
    tm = TriggerManager(
        condition_evaluator=ev,
        world_getter=lambda: world,
        sim_getter=lambda: None,
    )
    return tm, ev, world


def test_initialize_parses_scenario_triggers():
    tm, ev, _ = _make_trigger_manager()
    brief = {
        "scenario_triggers": [
            {
                "id": "t1",
                "condition": {"type": "time_elapsed", "params": {"at_s": 30}},
                "actions": [{"type": "send_comms", "params": {"text": "Hello"}}],
                "once": True,
            }
        ]
    }
    tm.initialize(brief)
    assert len(tm._triggers) == 1
    assert tm._triggers[0].id == "t1"


def test_step_fires_when_condition_met():
    tm, ev, _ = _make_trigger_manager()
    brief = {
        "scenario_triggers": [
            {
                "id": "t1",
                "condition": {"type": "time_elapsed", "params": {"at_s": 10}},
                "actions": [{"type": "send_comms", "params": {"text": "Go"}}],
            }
        ]
    }
    tm.initialize(brief)
    events = tm.step(dt=1.0, sim_time_s=15.0)
    assert len(events) == 1
    assert events[0]["type"] == "send_comms"
    assert events[0]["text"] == "Go"


def test_once_trigger_does_not_refire():
    tm, ev, _ = _make_trigger_manager()
    brief = {
        "scenario_triggers": [
            {
                "id": "t1",
                "condition": {"type": "time_elapsed", "params": {"at_s": 10}},
                "actions": [{"type": "send_comms", "params": {"text": "Go"}}],
                "once": True,
            }
        ]
    }
    tm.initialize(brief)
    tm.step(dt=1.0, sim_time_s=15.0)
    events2 = tm.step(dt=1.0, sim_time_s=20.0)
    assert len(events2) == 0


def test_action_send_comms():
    tm, ev, _ = _make_trigger_manager()
    brief = {
        "scenario_triggers": [
            {
                "id": "comms1",
                "condition": {"type": "time_elapsed", "params": {"at_s": 0}},
                "actions": [{"type": "send_comms", "params": {"text": "Urgent!", "priority": "high"}}],
            }
        ]
    }
    tm.initialize(brief)
    events = tm.step(dt=0.1, sim_time_s=1.0)
    assert events[0]["priority"] == "high"


def test_action_end_scenario():
    tm, ev, _ = _make_trigger_manager()
    brief = {
        "scenario_triggers": [
            {
                "id": "end1",
                "condition": {"type": "time_elapsed", "params": {"at_s": 0}},
                "actions": [{"type": "end_scenario", "params": {"outcome": "victory", "reason": "Test"}}],
            }
        ]
    }
    tm.initialize(brief)
    events = tm.step(dt=0.1, sim_time_s=1.0)
    assert events[0]["type"] == "end_scenario"
    assert events[0]["outcome"] == "victory"


def test_reset_clears_fired_state():
    tm, ev, _ = _make_trigger_manager()
    brief = {
        "scenario_triggers": [
            {
                "id": "t1",
                "condition": {"type": "time_elapsed", "params": {"at_s": 0}},
                "actions": [{"type": "send_comms", "params": {"text": "Hi"}}],
                "once": True,
            }
        ]
    }
    tm.initialize(brief)
    tm.step(dt=0.1, sim_time_s=1.0)
    assert tm._triggers[0].fired is True
    tm.reset()
    assert tm._triggers[0].fired is False
