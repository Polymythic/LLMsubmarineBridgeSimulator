"""Tests for sub-bridge/backend/sim/victory.py"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from conftest import make_ship, make_world
from backend.models import WaypointRoute, Waypoint
from backend.sim.victory import VictoryEvaluator
from backend.sim.waypoints import WaypointTracker


def _make_evaluator(*ships, criteria=None, with_waypoints=False):
    world = make_world(*ships)
    wt = None
    if with_waypoints:
        wt = WaypointTracker(world_getter=lambda: world)
        wt._initialized = True
    ve = VictoryEvaluator(world_getter=lambda: world, waypoint_tracker=wt)
    if criteria is not None:
        ve.initialize({"success_criteria": criteria})
    return ve, world


def test_not_initialized_returns_none():
    ve, _ = _make_evaluator()
    assert ve.step(sim_time_s=100.0) is None


def test_blue_victory_on_ships_destroyed():
    target = make_ship("enemy1", side="RED", ship_class="Destroyer")
    target.damage.hull = 1.0  # destroyed
    criteria = {
        "BLUE": {
            "ships_destroyed": {
                "targets": ["enemy1"],
                "min_count": 1,
            }
        }
    }
    ve, _ = _make_evaluator(target, criteria=criteria)
    outcome = ve.step(sim_time_s=10.0)
    assert outcome is not None
    assert outcome.status == "victory"


def test_red_victory_on_ships_destroyed_condition():
    # RED wins when it destroys BLUE targets
    blue1 = make_ship("blue1", side="BLUE")
    blue1.damage.hull = 1.0  # destroyed
    red1 = make_ship("red1", side="RED")
    criteria = {
        "RED": {
            "ships_destroyed": {
                "targets": ["blue1"],
                "min_count": 1,
            }
        }
    }
    ve, _ = _make_evaluator(blue1, red1, criteria=criteria)
    outcome = ve.step(sim_time_s=10.0)
    assert outcome is not None
    assert outcome.status == "defeat"  # RED victory = BLUE defeat


def test_timeout_determines_winner():
    blue1 = make_ship("blue1", side="BLUE")
    red1 = make_ship("red1", side="RED")
    red1.damage.hull = 1.0  # dead
    criteria = {"timeout_s": 100}
    ve, _ = _make_evaluator(blue1, red1, criteria=criteria)
    outcome = ve.step(sim_time_s=100.0)
    assert outcome is not None
    assert outcome.status == "victory"  # BLUE has more survivors


def test_timeout_draw():
    blue1 = make_ship("blue1", side="BLUE")
    red1 = make_ship("red1", side="RED")
    criteria = {"timeout_s": 50}
    ve, _ = _make_evaluator(blue1, red1, criteria=criteria)
    outcome = ve.step(sim_time_s=50.0)
    assert outcome is not None
    assert outcome.status == "draw"


def test_waypoint_reached_victory():
    convoy = make_ship("convoy1", side="RED", ship_class="Convoy")
    convoy.route = WaypointRoute(
        waypoints=[Waypoint(x=100, y=200)],
        current_idx=1,  # already past the only waypoint
    )
    criteria = {
        "RED": {
            "reach_wp_within_m": 200,
        }
    }
    ve, _ = _make_evaluator(convoy, criteria=criteria, with_waypoints=True)
    outcome = ve.step(sim_time_s=10.0)
    assert outcome is not None
    assert outcome.status == "defeat"  # RED victory = BLUE defeat


def test_convoy_delayed_victory():
    convoy = make_ship("c1", side="RED", ship_class="Convoy", speed=1.0)
    criteria = {
        "BLUE": {
            "disable_or_delay": {
                "convoy_speed_below_kn": 3,
                "duration_s": 60,
            }
        }
    }
    ve, _ = _make_evaluator(convoy, criteria=criteria)
    outcome = ve.step(sim_time_s=10.0)
    assert outcome is not None
    assert outcome.status == "victory"


def test_already_ended_returns_none():
    ve, _ = _make_evaluator(criteria={"timeout_s": 10})
    ve.step(sim_time_s=10.0)  # ends it
    assert ve.step(sim_time_s=20.0) is None


def test_force_end():
    ve, _ = _make_evaluator(criteria={})
    outcome = ve.force_end("defeat", "Captain surrendered")
    assert outcome.status == "defeat"
    assert outcome.reason == "Captain surrendered"
    assert outcome.ended_at is not None


def test_reset_clears_outcome():
    ve, _ = _make_evaluator(criteria={})
    ve.force_end("victory", "test")
    ve.reset()
    assert ve.get_outcome().status == "ongoing"
