"""
Waypoint Tracker - Game Rules System

This is a MONITOR, not a navigator. It tracks ship positions against waypoint
definitions and emits events when waypoints are reached.

Navigation decisions are made by the Fleet Commander AI, which orders ships
to waypoints. This system only verifies that waypoints have been reached.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

from ..models import Ship, Waypoint, WaypointRoute


class WaypointTracker:
    """Monitors ship positions and detects waypoint arrivals."""

    def __init__(self, world_getter: Callable[[], Any]):
        """
        Args:
            world_getter: Callable that returns the World object
        """
        self._world_getter = world_getter
        self._waypoint_routes: Dict[str, WaypointRoute] = {}  # Cached routes from mission
        self._initialized = False

    def initialize(self, mission_brief: Dict[str, Any]) -> None:
        """Initialize waypoint data from mission brief.

        This caches the waypoint routes for quick access. The actual routes
        are also stored on Ship objects for persistence.
        """
        self._waypoint_routes.clear()
        waypoint_routes = mission_brief.get("waypoint_routes", {})

        for ship_id, wp_list in waypoint_routes.items():
            if wp_list:
                waypoints = [Waypoint(**wp) for wp in wp_list]
                self._waypoint_routes[ship_id] = WaypointRoute(waypoints=waypoints)

        self._initialized = True

    def step(self, dt: float) -> List[Dict[str, Any]]:
        """Check all ships for waypoint arrivals.

        Returns:
            List of waypoint events, each containing:
                - type: "waypoint_reached"
                - ship_id: ID of ship that reached waypoint
                - waypoint_idx: Index of waypoint reached
                - waypoint_name: Name of waypoint (if any)
                - waypoint: Full waypoint data
        """
        if not self._initialized:
            return []

        events = []
        world = self._world_getter()

        for ship in world.ships.values():
            if not ship.route or not ship.route.waypoints:
                continue

            # Check if we've already reached all waypoints
            if ship.route.current_idx >= len(ship.route.waypoints):
                continue

            # Get current target waypoint
            current_wp = ship.route.waypoints[ship.route.current_idx]

            # Calculate distance to waypoint
            dx = current_wp.x - ship.kin.x
            dy = current_wp.y - ship.kin.y
            distance = math.sqrt(dx * dx + dy * dy)

            # Check if within arrival threshold
            if distance <= ship.route.arrival_threshold_m:
                # Waypoint reached!
                waypoint_idx = ship.route.current_idx
                ship.route.current_idx += 1

                # Handle looping routes
                if ship.route.loop and ship.route.current_idx >= len(ship.route.waypoints):
                    ship.route.current_idx = 0

                events.append({
                    "type": "waypoint_reached",
                    "ship_id": ship.id,
                    "waypoint_idx": waypoint_idx,
                    "waypoint_name": current_wp.name,
                    "waypoint": {
                        "x": current_wp.x,
                        "y": current_wp.y,
                        "name": current_wp.name,
                    },
                    "next_waypoint_idx": ship.route.current_idx if ship.route.current_idx < len(ship.route.waypoints) else None,
                })

        return events

    def get_progress(self, ship_id: str) -> Optional[Dict[str, Any]]:
        """Get waypoint progress for a specific ship.

        Returns:
            Dict with progress info, or None if ship has no route:
                - current_idx: Index of next waypoint to reach
                - total: Total number of waypoints
                - current_waypoint: Current target waypoint data (or None if complete)
                - completed: True if all waypoints reached
                - waypoints: Full list of waypoints
        """
        world = self._world_getter()
        ship = world.ships.get(ship_id)

        if not ship or not ship.route:
            return None

        route = ship.route
        current_wp = None
        if route.current_idx < len(route.waypoints):
            wp = route.waypoints[route.current_idx]
            current_wp = {
                "x": wp.x,
                "y": wp.y,
                "name": wp.name,
                "speed_kn": wp.speed_kn,
            }

        return {
            "current_idx": route.current_idx,
            "total": len(route.waypoints),
            "current_waypoint": current_wp,
            "completed": route.current_idx >= len(route.waypoints),
            "waypoints": [
                {"x": wp.x, "y": wp.y, "name": wp.name, "speed_kn": wp.speed_kn}
                for wp in route.waypoints
            ],
        }

    def get_all_progress(self) -> Dict[str, Dict[str, Any]]:
        """Get waypoint progress for all ships with routes.

        Returns:
            Dict mapping ship_id to progress info
        """
        world = self._world_getter()
        progress = {}

        for ship in world.ships.values():
            if ship.route:
                prog = self.get_progress(ship.id)
                if prog:
                    progress[ship.id] = prog

        return progress

    def get_waypoints_for_ship(self, ship_id: str) -> List[Waypoint]:
        """Get the waypoint list for a specific ship.

        Returns:
            List of Waypoint objects, or empty list if no route
        """
        world = self._world_getter()
        ship = world.ships.get(ship_id)

        if not ship or not ship.route:
            return []

        return ship.route.waypoints

    def get_next_waypoint(self, ship_id: str) -> Optional[Waypoint]:
        """Get the next waypoint a ship should navigate to.

        Returns:
            Next Waypoint, or None if route complete or no route
        """
        world = self._world_getter()
        ship = world.ships.get(ship_id)

        if not ship or not ship.route:
            return None

        if ship.route.current_idx >= len(ship.route.waypoints):
            return None

        return ship.route.waypoints[ship.route.current_idx]

    def calculate_heading_to_waypoint(self, ship_id: str) -> Optional[float]:
        """Calculate heading from ship's current position to next waypoint.

        This is a helper for the AI - it calculates what heading would be needed
        to reach the next waypoint, but does NOT set that heading.

        Returns:
            Heading in degrees (0-360), or None if no next waypoint
        """
        world = self._world_getter()
        ship = world.ships.get(ship_id)

        if not ship or not ship.route:
            return None

        next_wp = self.get_next_waypoint(ship_id)
        if not next_wp:
            return None

        dx = next_wp.x - ship.kin.x
        dy = next_wp.y - ship.kin.y

        # Calculate heading (0 = North, 90 = East)
        heading = math.degrees(math.atan2(dx, dy))
        if heading < 0:
            heading += 360

        return heading

    def distance_to_next_waypoint(self, ship_id: str) -> Optional[float]:
        """Calculate distance from ship to next waypoint.

        Returns:
            Distance in meters, or None if no next waypoint
        """
        world = self._world_getter()
        ship = world.ships.get(ship_id)

        if not ship or not ship.route:
            return None

        next_wp = self.get_next_waypoint(ship_id)
        if not next_wp:
            return None

        dx = next_wp.x - ship.kin.x
        dy = next_wp.y - ship.kin.y

        return math.sqrt(dx * dx + dy * dy)

    def reset(self) -> None:
        """Reset waypoint tracker state for mission transitions."""
        self._waypoint_routes.clear()
        self._initialized = False
