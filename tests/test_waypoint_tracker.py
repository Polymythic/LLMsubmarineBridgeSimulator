"""Tests for sub-bridge/backend/sim/waypoints.py"""
import os, sys, math
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from conftest import make_ship, make_world
from backend.models import Waypoint, WaypointRoute
from backend.sim.waypoints import WaypointTracker


def _make_tracker_and_world(*ships):
    world = make_world(*ships)
    tracker = WaypointTracker(world_getter=lambda: world)
    return tracker, world


def test_initialize_from_mission_brief():
    ship = make_ship("convoy1", side="RED", ship_class="Convoy")
    tracker, world = _make_tracker_and_world(ship)
    brief = {
        "waypoint_routes": {
            "convoy1": [{"x": 1000, "y": 2000, "name": "Alpha"}]
        }
    }
    tracker.initialize(brief)
    assert tracker._initialized is True
    assert "convoy1" in tracker._waypoint_routes


def test_step_detects_arrival():
    ship = make_ship("convoy1", side="RED", ship_class="Convoy", x=1000, y=2000)
    ship.route = WaypointRoute(
        waypoints=[Waypoint(x=1000, y=2000, name="Alpha")],
        arrival_threshold_m=100.0,
    )
    tracker, world = _make_tracker_and_world(ship)
    tracker._initialized = True
    events = tracker.step(dt=1.0)
    assert len(events) == 1
    assert events[0]["type"] == "waypoint_reached"
    assert events[0]["ship_id"] == "convoy1"
    assert events[0]["waypoint_name"] == "Alpha"


def test_step_advances_current_idx():
    ship = make_ship("convoy1", side="RED", ship_class="Convoy", x=100, y=100)
    ship.route = WaypointRoute(
        waypoints=[
            Waypoint(x=100, y=100, name="A"),
            Waypoint(x=5000, y=5000, name="B"),
        ],
        arrival_threshold_m=200.0,
    )
    tracker, world = _make_tracker_and_world(ship)
    tracker._initialized = True
    tracker.step(dt=1.0)
    assert ship.route.current_idx == 1


def test_step_loop_route_wraps():
    ship = make_ship("patrol", side="RED", ship_class="Destroyer", x=50, y=50)
    ship.route = WaypointRoute(
        waypoints=[Waypoint(x=50, y=50, name="A")],
        arrival_threshold_m=100.0,
        loop=True,
    )
    tracker, world = _make_tracker_and_world(ship)
    tracker._initialized = True
    tracker.step(dt=1.0)
    assert ship.route.current_idx == 0  # wrapped back


def test_get_progress_returns_correct_state():
    ship = make_ship("convoy1", side="RED", ship_class="Convoy", x=0, y=0)
    ship.route = WaypointRoute(
        waypoints=[
            Waypoint(x=1000, y=1000, name="A"),
            Waypoint(x=2000, y=2000, name="B"),
        ],
    )
    tracker, world = _make_tracker_and_world(ship)
    tracker._initialized = True
    progress = tracker.get_progress("convoy1")
    assert progress is not None
    assert progress["current_idx"] == 0
    assert progress["total"] == 2
    assert progress["completed"] is False


def test_calculate_heading_to_waypoint():
    # Ship at origin, waypoint at (1000, 0) — due East = heading 90
    ship = make_ship("s1", side="BLUE", x=0, y=0)
    ship.route = WaypointRoute(waypoints=[Waypoint(x=1000, y=0)])
    tracker, world = _make_tracker_and_world(ship)
    tracker._initialized = True
    heading = tracker.calculate_heading_to_waypoint("s1")
    assert heading is not None
    assert abs(heading - 90.0) < 0.1


def test_distance_to_next_waypoint():
    ship = make_ship("s1", side="BLUE", x=0, y=0)
    ship.route = WaypointRoute(waypoints=[Waypoint(x=300, y=400)])
    tracker, world = _make_tracker_and_world(ship)
    tracker._initialized = True
    dist = tracker.distance_to_next_waypoint("s1")
    assert dist is not None
    assert abs(dist - 500.0) < 0.1  # 3-4-5 triangle


def test_no_events_when_not_initialized():
    ship = make_ship("s1", x=100, y=100)
    ship.route = WaypointRoute(
        waypoints=[Waypoint(x=100, y=100)],
        arrival_threshold_m=200.0,
    )
    tracker, world = _make_tracker_and_world(ship)
    # NOT initialized
    events = tracker.step(dt=1.0)
    assert events == []
