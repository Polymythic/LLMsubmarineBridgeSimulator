"""
Intercept System - Game Rules System

Manages intercepted enemy communications. The submarine can intercept
both scripted messages and AI-generated fleet commander communications.
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..models import InterceptedComm


class InterceptSystem:
    """Manages intercepted enemy communications."""

    def __init__(
        self,
        world_getter: Callable[[], Any],
        sim_getter: Callable[[], Any],
    ):
        """
        Args:
            world_getter: Callable that returns the World object
            sim_getter: Callable that returns the Simulation object
        """
        self._world_getter = world_getter
        self._sim_getter = sim_getter

        # Pending intercepts (not yet delivered)
        self._pending: List[InterceptedComm] = []

        # Delivered intercepts (for history)
        self._delivered: List[InterceptedComm] = []

        # Scheduled scripted intercepts from mission
        self._schedule: List[Dict[str, Any]] = []
        self._schedule_idx = 0

        # Simulation time tracking
        self._sim_time_s = 0.0

        self._initialized = False

    def initialize(self, mission_brief: Dict[str, Any]) -> None:
        """Load intercept schedule from mission brief."""
        self._schedule = mission_brief.get("intercept_schedule", [])
        self._schedule.sort(key=lambda x: x.get("at_s", 0))
        self._schedule_idx = 0
        self._pending.clear()
        self._delivered.clear()
        self._initialized = True

    def add_scripted_intercept(
        self,
        text: str,
        bearing: Optional[float] = None,
        partial: bool = False,
        source_ship: Optional[str] = None,
    ) -> None:
        """Queue a scripted intercept from scenario trigger.

        Args:
            text: The message text
            bearing: Optional bearing to transmission source
            partial: If True, apply signal degradation
            source_ship: Optional ship ID that is the source
        """
        # Calculate bearing if source ship specified
        if source_ship and bearing is None:
            bearing = self._calculate_bearing_to_ship(source_ship)

        # Apply degradation if partial
        intercepted_text = self._degrade_message(text, 0.6) if partial else text
        confidence = 0.6 if partial else 1.0

        intercept = InterceptedComm(
            source="scripted",
            original_text=text,
            intercepted_text=intercepted_text,
            bearing=bearing,
            timestamp=datetime.utcnow().isoformat(),
            confidence=confidence,
        )

        self._pending.append(intercept)

    def capture_fleet_intent(
        self,
        intent: Dict[str, Any],
        source_pos: Optional[Tuple[float, float]] = None,
    ) -> None:
        """Potentially intercept a fleet commander intent.

        The fleet commander's orders might be transmitted by radio and
        intercepted by the submarine.

        Args:
            intent: The fleet intent dict
            source_pos: Position of the transmitting ship (x, y)
        """
        # Determine if this intent should be interceptable
        # For now, only intercept intents that contain tactical orders
        if not intent:
            return

        # Extract meaningful tactical info from intent
        text = self._intent_to_intercept_text(intent)
        if not text:
            return

        # Calculate interception probability based on distance
        intercept_prob = self._calculate_intercept_probability(source_pos)

        if random.random() > intercept_prob:
            return  # Failed to intercept

        # Calculate bearing
        bearing = None
        if source_pos:
            bearing = self._calculate_bearing_to_point(source_pos)

        # Apply signal degradation based on distance
        quality = intercept_prob  # Use probability as quality metric
        intercepted_text = self._degrade_message(text, quality)

        intercept = InterceptedComm(
            source="fleet_commander",
            original_text=text,
            intercepted_text=intercepted_text,
            bearing=bearing,
            timestamp=datetime.utcnow().isoformat(),
            confidence=quality,
        )

        self._pending.append(intercept)

    def step(self, dt: float, sim_time_s: float) -> List[InterceptedComm]:
        """Process pending intercepts and scheduled messages.

        Args:
            dt: Time delta since last step
            sim_time_s: Current simulation time

        Returns:
            List of intercepts ready for delivery
        """
        if not self._initialized:
            return []

        self._sim_time_s = sim_time_s
        ready: List[InterceptedComm] = []

        # Check for scheduled intercepts
        while self._schedule_idx < len(self._schedule):
            sched = self._schedule[self._schedule_idx]
            at_s = sched.get("at_s", 0)

            if sim_time_s >= at_s:
                # Time to queue this intercept
                self.add_scripted_intercept(
                    text=sched.get("text", ""),
                    partial=sched.get("partial", False),
                    source_ship=sched.get("source_ship"),
                )
                self._schedule_idx += 1
            else:
                break

        # Check delivery conditions for pending intercepts
        if self._can_receive_intercepts():
            ready = self._pending.copy()
            self._delivered.extend(ready)
            self._pending.clear()

        return ready

    def _can_receive_intercepts(self) -> bool:
        """Check if submarine can receive radio intercepts.

        Requires:
        - Radio raised
        - Depth <= 20m (radio depth)
        """
        sim = self._sim_getter()

        # Check if radio is raised
        radio_raised = getattr(sim, "_radio_raised", False)
        if not radio_raised:
            return False

        # Check depth — `world.ships` is a Dict[str, Ship]; iterate values.
        world = self._world_getter()
        ownship = next((s for s in world.all_ships() if s.id == "ownship"), None)
        if not ownship:
            return False

        return ownship.kin.depth <= 20.0

    def _calculate_bearing_to_ship(self, ship_id: str) -> Optional[float]:
        """Calculate bearing from ownship to specified ship."""
        world = self._world_getter()

        ownship = next((s for s in world.all_ships() if s.id == "ownship"), None)
        target = next((s for s in world.all_ships() if s.id == ship_id), None)

        if not ownship or not target:
            return None

        return self._calculate_bearing_to_point((target.kin.x, target.kin.y))

    def _calculate_bearing_to_point(
        self,
        point: Tuple[float, float]
    ) -> Optional[float]:
        """Calculate bearing from ownship to a point."""
        world = self._world_getter()

        ownship = next((s for s in world.all_ships() if s.id == "ownship"), None)
        if not ownship:
            return None

        dx = point[0] - ownship.kin.x
        dy = point[1] - ownship.kin.y

        # Calculate bearing (0 = North, 90 = East)
        bearing = math.degrees(math.atan2(dx, dy))
        if bearing < 0:
            bearing += 360

        return bearing

    def _calculate_intercept_probability(
        self,
        source_pos: Optional[Tuple[float, float]]
    ) -> float:
        """Calculate probability of intercepting a transmission.

        Based on distance from source.
        """
        if not source_pos:
            return 0.3  # Low probability without position

        world = self._world_getter()
        ownship = next((s for s in world.all_ships() if s.id == "ownship"), None)

        if not ownship:
            return 0.0

        dx = source_pos[0] - ownship.kin.x
        dy = source_pos[1] - ownship.kin.y
        distance = math.sqrt(dx * dx + dy * dy)

        # Probability decreases with distance
        # 100% at 0m, ~50% at 5km, ~25% at 10km
        prob = math.exp(-distance / 10000)
        return max(0.1, min(1.0, prob))

    def _degrade_message(self, text: str, quality: float) -> str:
        """Apply signal degradation to message.

        Args:
            text: Original message text
            quality: Signal quality (0.0-1.0), lower = more degradation

        Returns:
            Degraded message text
        """
        if quality >= 0.95:
            return text

        words = text.split()
        if not words:
            return text

        result = []
        for word in words:
            # Probability of word being garbled
            garble_prob = 1.0 - quality

            if random.random() < garble_prob * 0.3:
                # Completely garble the word
                result.append("[GARBLED]")
            elif random.random() < garble_prob * 0.2:
                # Partial garble
                if len(word) > 3:
                    result.append(word[:2] + "..." + word[-1])
                else:
                    result.append("...")
            else:
                result.append(word)

        # Add static markers for low quality
        if quality < 0.5:
            result.insert(0, "[STATIC]")
            if len(result) > 3:
                result.insert(len(result) // 2, "[...]")

        return " ".join(result)

    def _intent_to_intercept_text(self, intent: Dict[str, Any]) -> Optional[str]:
        """Convert fleet intent to interceptable message text.

        Extracts tactical information from intent and formats as
        a radio transmission.
        """
        parts = []

        # Look for navigation orders
        ship_orders = intent.get("ships", {})
        for ship_id, orders in ship_orders.items():
            if isinstance(orders, dict):
                # Check for significant orders
                if "heading" in orders or "speed" in orders:
                    heading = orders.get("heading", "current")
                    speed = orders.get("speed", "current")
                    parts.append(f"{ship_id}: heading {heading}, speed {speed}")

        # Look for tactical mode changes
        if "alert_level" in intent:
            parts.append(f"Alert level: {intent['alert_level']}")

        # Look for weapon orders
        if "weapons_free" in intent:
            parts.append("Weapons free")

        if not parts:
            return None

        return "FLEET: " + "; ".join(parts)

    def get_all_intercepts(self) -> List[InterceptedComm]:
        """Get all delivered intercepts."""
        return self._delivered.copy()

    def get_pending_count(self) -> int:
        """Get count of pending intercepts."""
        return len(self._pending)

    def reset(self) -> None:
        """Reset intercept system state."""
        self._pending.clear()
        self._delivered.clear()
        self._schedule.clear()
        self._schedule_idx = 0
        self._sim_time_s = 0.0
        self._initialized = False
