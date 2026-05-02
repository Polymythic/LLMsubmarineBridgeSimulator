"""
Victory Evaluator - Game Rules System

Evaluates mission success/failure conditions based on success_criteria
defined in mission JSON.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..models import MissionOutcome


class VictoryEvaluator:
    """Evaluates mission victory/defeat conditions."""

    def __init__(
        self,
        world_getter: Callable[[], Any],
        waypoint_tracker: Optional[Any] = None,
    ):
        """
        Args:
            world_getter: Callable that returns the World object
            waypoint_tracker: Optional WaypointTracker for waypoint-based criteria
        """
        self._world_getter = world_getter
        self._waypoint_tracker = waypoint_tracker
        self._criteria: Dict[str, Any] = {}
        self._outcome = MissionOutcome()
        self._initialized = False

    def initialize(self, mission_brief: Dict[str, Any]) -> None:
        """Load success criteria from mission brief."""
        self._criteria = mission_brief.get("success_criteria", {})
        self._outcome = MissionOutcome()
        self._initialized = True

    def set_waypoint_tracker(self, tracker: Any) -> None:
        """Set the waypoint tracker for waypoint-based criteria."""
        self._waypoint_tracker = tracker

    def step(self, sim_time_s: float) -> Optional[MissionOutcome]:
        """Check victory/defeat conditions.

        Args:
            sim_time_s: Current simulation time in seconds

        Returns:
            MissionOutcome if status changed, None otherwise
        """
        if not self._initialized:
            return None

        # Already ended
        if self._outcome.status != "ongoing":
            return None

        # Hard defeat: any neutral ship destroyed (rules-of-engagement violation)
        if self._criteria.get("defeat_on_neutral_kill", False):
            world = self._world_getter()
            for s in world.ships.values():
                if getattr(s, "side", None) == "NEUTRAL" and s.damage.hull >= 1.0:
                    self._outcome.status = "defeat"
                    self._outcome.reason = (
                        f"Neutral vessel {s.id} destroyed — rules-of-engagement violation"
                    )
                    self._outcome.ended_at = datetime.utcnow().isoformat()
                    return self._outcome

        # Check RED victory (BLUE defeat)
        red_criteria = self._criteria.get("RED", {})
        if red_criteria and self._check_side_victory("RED", red_criteria, sim_time_s):
            self._outcome.status = "defeat"
            self._outcome.reason = "RED achieved objective"
            self._outcome.ended_at = datetime.utcnow().isoformat()
            return self._outcome

        # Check BLUE victory
        blue_criteria = self._criteria.get("BLUE", {})
        if blue_criteria and self._check_side_victory("BLUE", blue_criteria, sim_time_s):
            self._outcome.status = "victory"
            self._outcome.reason = "BLUE achieved objective"
            self._outcome.ended_at = datetime.utcnow().isoformat()
            return self._outcome

        # Check global timeout
        timeout_s = self._criteria.get("timeout_s")
        if timeout_s and sim_time_s >= timeout_s:
            # Determine winner based on who is closer to objective
            winner = self._determine_timeout_winner()
            self._outcome.status = winner
            self._outcome.reason = f"Time limit reached ({timeout_s}s)"
            self._outcome.ended_at = datetime.utcnow().isoformat()
            return self._outcome

        return None

    def _check_side_victory(
        self,
        side: str,
        criteria: Dict[str, Any],
        sim_time_s: float
    ) -> bool:
        """Check if a side has achieved its victory conditions.

        Supports:
        - reach_wp_within_m: Ships reached final waypoint within distance
        - min_survivors: Minimum ships surviving
        - timeout_s: Side-specific timeout
        - ships_destroyed: Target ships destroyed
        """
        world = self._world_getter()

        # Check waypoint reaching (convoy escort missions)
        if "reach_wp_within_m" in criteria:
            threshold = criteria["reach_wp_within_m"]
            if self._check_waypoint_reached(side, threshold):
                return True

        # Check ship survival requirements
        if "min_survivors" in criteria:
            min_survivors = criteria["min_survivors"]
            if not self._check_min_survivors(side, min_survivors):
                # Failed survival check - opposite side wins
                return False

        # Check if specific ships destroyed (for attack missions)
        if "ships_destroyed" in criteria:
            destroy_criteria = criteria["ships_destroyed"]
            if self._check_ships_destroyed(destroy_criteria):
                return True

        # Check side-specific timeout
        if "timeout_s" in criteria:
            if sim_time_s >= criteria["timeout_s"]:
                # If side has timeout and it elapsed, they might win
                # (depends on other conditions being met)
                pass

        # Check "disable_or_delay" condition (convoy disruption)
        if "disable_or_delay" in criteria:
            delay_criteria = criteria["disable_or_delay"]
            if self._check_convoy_delayed(delay_criteria, sim_time_s):
                return True

        return False

    def _check_waypoint_reached(self, side: str, threshold_m: float) -> bool:
        """Check if ships on the given side have reached their final waypoints."""
        if not self._waypoint_tracker:
            return False

        world = self._world_getter()
        ships_with_routes = [s for s in world.ships.values() if s.side == side and s.route]

        if not ships_with_routes:
            return False

        # Check if all ships with routes have completed them
        for ship in ships_with_routes:
            progress = self._waypoint_tracker.get_progress(ship.id)
            if not progress:
                return False
            if not progress.get("completed", False):
                return False

        return True

    def _check_min_survivors(self, side: str, min_count: int) -> bool:
        """Check if side has at least min_count ships alive."""
        world = self._world_getter()

        # Count ships on side that are not destroyed (hull damage < 1.0)
        alive_count = sum(
            1 for s in world.ships.values()
            if s.side == side and s.damage.hull < 1.0
        )

        return alive_count >= min_count

    def _check_ships_destroyed(self, criteria: Dict[str, Any]) -> bool:
        """Check if target ships have been destroyed.

        Criteria format:
            targets: List of ship IDs to destroy
            min_count: Minimum number that must be destroyed
        """
        targets = criteria.get("targets", [])
        min_count = criteria.get("min_count", len(targets))

        if not targets:
            return False

        world = self._world_getter()

        # Count destroyed targets
        destroyed_count = 0
        for target_id in targets:
            ship = world.ships.get(target_id)
            if ship is None:
                # Ship not found - might have been removed, count as destroyed
                destroyed_count += 1
            elif ship.damage.hull >= 1.0:
                destroyed_count += 1

        return destroyed_count >= min_count

    def _check_convoy_delayed(
        self,
        criteria: Dict[str, Any],
        sim_time_s: float
    ) -> bool:
        """Check if convoy has been delayed/disabled.

        Criteria format:
            convoy_speed_below_kn: Convoy speed threshold
            duration_s: How long convoy must be slowed
        """
        speed_threshold = criteria.get("convoy_speed_below_kn", 3)
        duration_required = criteria.get("duration_s", 60)

        world = self._world_getter()

        # Find convoy ships (RED Convoy class)
        convoy_ships = [
            s for s in world.ships.values()
            if s.side == "RED" and s.ship_class == "Convoy" and s.damage.hull < 1.0
        ]

        if not convoy_ships:
            # No convoy ships alive
            return True

        # Check if all convoy ships are slowed
        all_slowed = all(s.kin.speed < speed_threshold for s in convoy_ships)

        # For now, just check current state
        # A more sophisticated version would track duration
        return all_slowed

    def _determine_timeout_winner(self) -> str:
        """Determine winner when time runs out.

        Returns "victory", "defeat", or "draw"
        """
        world = self._world_getter()

        # Count surviving ships per side
        blue_survivors = sum(1 for s in world.ships.values() if s.side == "BLUE" and s.damage.hull < 1.0)
        red_survivors = sum(1 for s in world.ships.values() if s.side == "RED" and s.damage.hull < 1.0)

        # Simple heuristic: more survivors wins
        if blue_survivors > red_survivors:
            return "victory"
        elif red_survivors > blue_survivors:
            return "defeat"
        else:
            return "draw"

    def get_outcome(self) -> MissionOutcome:
        """Get current mission outcome."""
        return self._outcome

    def force_end(self, status: str, reason: str) -> MissionOutcome:
        """Force mission to end with specified outcome.

        Args:
            status: "victory", "defeat", or "draw"
            reason: Reason for ending

        Returns:
            Updated MissionOutcome
        """
        self._outcome.status = status
        self._outcome.reason = reason
        self._outcome.ended_at = datetime.utcnow().isoformat()
        return self._outcome

    def reset(self) -> None:
        """Reset to ongoing state."""
        self._outcome = MissionOutcome()
        self._initialized = False
        self._criteria = {}
