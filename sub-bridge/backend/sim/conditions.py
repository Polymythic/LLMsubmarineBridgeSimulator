"""
Condition Evaluator - Game Rules System

Evaluates scenario conditions to determine when triggers should fire.
Supports various condition types including waypoint checks, damage,
contact detection, distance, and compound conditions.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional

from ..models import ScenarioCondition


class ConditionEvaluator:
    """Evaluates scenario conditions against current simulation state."""

    def __init__(
        self,
        world_getter: Callable[[], Any],
        sim_getter: Callable[[], Any],
        waypoint_tracker: Optional[Any] = None,
    ):
        """
        Args:
            world_getter: Callable that returns the World object
            sim_getter: Callable that returns the Simulation object
            waypoint_tracker: Optional WaypointTracker for waypoint conditions
        """
        self._world_getter = world_getter
        self._sim_getter = sim_getter
        self._waypoint_tracker = waypoint_tracker
        self._sim_time_s = 0.0

    def set_sim_time(self, sim_time_s: float) -> None:
        """Update the current simulation time for time-based conditions."""
        self._sim_time_s = sim_time_s

    def set_waypoint_tracker(self, tracker: Any) -> None:
        """Set the waypoint tracker for waypoint conditions."""
        self._waypoint_tracker = tracker

    def reset(self) -> None:
        """Reset condition evaluator state for mission transitions."""
        self._sim_time_s = 0.0

    def evaluate(self, condition: ScenarioCondition) -> bool:
        """Evaluate a condition against current simulation state.

        Args:
            condition: The ScenarioCondition to evaluate

        Returns:
            True if condition is met, False otherwise
        """
        evaluators = {
            "waypoint_reached": self._eval_waypoint_reached,
            "ship_destroyed": self._eval_ship_destroyed,
            "contact_detected": self._eval_contact_detected,
            "damage_threshold": self._eval_damage_threshold,
            "time_elapsed": self._eval_time_elapsed,
            "distance_to": self._eval_distance_to,
            "all_of": self._eval_all_of,
            "any_of": self._eval_any_of,
        }

        evaluator = evaluators.get(condition.type)
        if not evaluator:
            return False

        try:
            return evaluator(condition.params)
        except Exception:
            # Log error in production, but don't crash the simulation
            return False

    def _eval_waypoint_reached(self, params: Dict[str, Any]) -> bool:
        """Check if ship has reached specified waypoint index.

        Params:
            ship_id: ID of ship to check
            waypoint_idx: Index of waypoint (ship must have passed this index)
        """
        if not self._waypoint_tracker:
            return False

        ship_id = params.get("ship_id")
        waypoint_idx = params.get("waypoint_idx", 0)

        if not ship_id:
            return False

        progress = self._waypoint_tracker.get_progress(ship_id)
        if not progress:
            return False

        # Ship has reached waypoint if current_idx > waypoint_idx
        # (current_idx is the NEXT waypoint to reach)
        return progress["current_idx"] > waypoint_idx

    def _eval_ship_destroyed(self, params: Dict[str, Any]) -> bool:
        """Check if ship hull damage exceeds threshold.

        Params:
            ship_id: ID of ship to check
            damage_threshold: Damage level considered destroyed (default 1.0)
        """
        ship_id = params.get("ship_id")
        threshold = params.get("damage_threshold", 1.0)

        if not ship_id:
            return False

        world = self._world_getter()
        ship = world.ships.get(ship_id)

        if not ship:
            # Ship doesn't exist - might have been removed, consider destroyed
            return True

        return ship.damage.hull >= threshold

    def _eval_contact_detected(self, params: Dict[str, Any]) -> bool:
        """Check if observer has detected target with sufficient confidence.

        Params:
            observer: Ship ID of the observing ship
            target: Ship ID of the target (or "ownship" for player sub)
            confidence_min: Minimum confidence level required
        """
        observer_id = params.get("observer")
        target_id = params.get("target")
        confidence_min = params.get("confidence_min", 0.5)

        if not observer_id or not target_id:
            return False

        # Get the simulation's contact data
        sim = self._sim_getter()

        # Check if simulation has passive_contacts method
        if not hasattr(sim, "get_contacts_for_ship"):
            # Fall back to checking ship's contact_tracks
            world = self._world_getter()
            observer = world.ships.get(observer_id)
            if not observer:
                return False

            # Check contact tracks on the observer
            for track in observer.contact_tracks:
                if track.contact_id == target_id and track.track_confidence >= confidence_min:
                    return True
            return False

        # Use simulation's contact system
        contacts = sim.get_contacts_for_ship(observer_id)
        for contact in contacts:
            contact_id = contact.get("id") or contact.get("contact_id")
            confidence = contact.get("confidence", 0)
            if contact_id == target_id and confidence >= confidence_min:
                return True

        return False

    def _eval_damage_threshold(self, params: Dict[str, Any]) -> bool:
        """Check if ship damage exceeds specified threshold.

        Params:
            ship_id: ID of ship to check
            damage_type: Type of damage (hull, sensors, propulsion)
            threshold: Minimum damage level
        """
        ship_id = params.get("ship_id")
        damage_type = params.get("damage_type", "hull")
        threshold = params.get("threshold", 0.5)

        if not ship_id:
            return False

        world = self._world_getter()
        ship = world.ships.get(ship_id)

        if not ship:
            return False

        damage_value = getattr(ship.damage, damage_type, 0)
        return damage_value >= threshold

    def _eval_time_elapsed(self, params: Dict[str, Any]) -> bool:
        """Check if simulation time has reached specified value.

        Params:
            at_s: Time in seconds that must have elapsed
        """
        at_s = params.get("at_s", 0)
        return self._sim_time_s >= at_s

    def _eval_distance_to(self, params: Dict[str, Any]) -> bool:
        """Check distance between two entities.

        Params:
            from_ship: Ship ID of first entity
            to_ship: Ship ID of second entity (optional)
            to_point: [x, y] coordinates (optional, used if to_ship not specified)
            max_distance_m: Maximum distance for condition to be true
            min_distance_m: Minimum distance for condition to be true (optional)
        """
        from_ship_id = params.get("from_ship")
        to_ship_id = params.get("to_ship")
        to_point = params.get("to_point")
        max_distance = params.get("max_distance_m")
        min_distance = params.get("min_distance_m", 0)

        if not from_ship_id:
            return False

        world = self._world_getter()
        from_ship = world.ships.get(from_ship_id)

        if not from_ship:
            return False

        # Determine target position
        if to_ship_id:
            to_ship = world.ships.get(to_ship_id)
            if not to_ship:
                return False
            target_x, target_y = to_ship.kin.x, to_ship.kin.y
        elif to_point:
            target_x, target_y = to_point[0], to_point[1]
        else:
            return False

        # Calculate distance
        dx = target_x - from_ship.kin.x
        dy = target_y - from_ship.kin.y
        distance = math.sqrt(dx * dx + dy * dy)

        # Check bounds
        if max_distance is not None and distance > max_distance:
            return False
        if min_distance is not None and distance < min_distance:
            return False

        return True

    def _eval_all_of(self, params: Dict[str, Any]) -> bool:
        """Check if all sub-conditions are true.

        Params:
            conditions: List of condition dicts to evaluate
        """
        conditions = params.get("conditions", [])
        if not conditions:
            return True

        for cond_dict in conditions:
            sub_condition = ScenarioCondition(**cond_dict)
            if not self.evaluate(sub_condition):
                return False

        return True

    def _eval_any_of(self, params: Dict[str, Any]) -> bool:
        """Check if any sub-condition is true.

        Params:
            conditions: List of condition dicts to evaluate
        """
        conditions = params.get("conditions", [])
        if not conditions:
            return False

        for cond_dict in conditions:
            sub_condition = ScenarioCondition(**cond_dict)
            if self.evaluate(sub_condition):
                return True

        return False
