"""
Trigger Manager - Game Rules System

Manages scenario triggers that fire based on conditions.
Supports both legacy time-based triggers and new condition-based triggers.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from ..models import ScenarioCondition, ScenarioAction, ScenarioTrigger
from .conditions import ConditionEvaluator


class TriggerManager:
    """Manages scenario triggers and executes actions when conditions are met."""

    def __init__(
        self,
        condition_evaluator: ConditionEvaluator,
        world_getter: Callable[[], Any],
        sim_getter: Callable[[], Any],
    ):
        """
        Args:
            condition_evaluator: The ConditionEvaluator instance
            world_getter: Callable that returns the World object
            sim_getter: Callable that returns the Simulation object
        """
        self._evaluator = condition_evaluator
        self._world_getter = world_getter
        self._sim_getter = sim_getter
        self._triggers: List[ScenarioTrigger] = []
        self._initialized = False

    def initialize(self, mission_brief: Dict[str, Any]) -> None:
        """Load triggers from mission brief.

        Handles both:
        - Legacy triggers: {"at_s": 60, "comms": "message"}
        - New triggers: {"id": "x", "condition": {...}, "actions": [...]}
        """
        self._triggers.clear()

        # Load new-style scenario triggers
        scenario_triggers = mission_brief.get("scenario_triggers", [])
        for trigger_dict in scenario_triggers:
            try:
                # Parse condition
                cond_dict = trigger_dict.get("condition", {})
                condition = ScenarioCondition(**cond_dict)

                # Parse actions
                actions = []
                for action_dict in trigger_dict.get("actions", []):
                    actions.append(ScenarioAction(**action_dict))

                trigger = ScenarioTrigger(
                    id=trigger_dict.get("id", f"trigger_{len(self._triggers)}"),
                    condition=condition,
                    actions=actions,
                    once=trigger_dict.get("once", True),
                    fired=False,
                )
                self._triggers.append(trigger)
            except Exception as e:
                print(f"Warning: Failed to parse scenario trigger: {e}")

        # Convert legacy time-based triggers to new format
        # (These are already handled by the existing comms_schedule system,
        # but we add them here for consistency if scenario_triggers is used)
        legacy_triggers = mission_brief.get("triggers", [])
        for legacy in legacy_triggers:
            if "at_s" in legacy and "comms" not in legacy:
                # Non-comms legacy trigger - convert to new format
                try:
                    condition = ScenarioCondition(
                        type="time_elapsed",
                        params={"at_s": float(legacy.get("at_s", 0))}
                    )
                    actions = []

                    # Handle various legacy action types
                    if "spawn" in legacy:
                        actions.append(ScenarioAction(
                            type="spawn_ship",
                            params=legacy["spawn"]
                        ))

                    if actions:
                        trigger = ScenarioTrigger(
                            id=f"legacy_{len(self._triggers)}",
                            condition=condition,
                            actions=actions,
                            once=True,
                            fired=False,
                        )
                        self._triggers.append(trigger)
                except Exception:
                    pass

        self._initialized = True

    def step(self, dt: float, sim_time_s: float) -> List[Dict[str, Any]]:
        """Check all triggers and execute actions for those that fire.

        Args:
            dt: Time delta since last step
            sim_time_s: Current simulation time in seconds

        Returns:
            List of action events that were triggered
        """
        if not self._initialized:
            return []

        # Update evaluator's time
        self._evaluator.set_sim_time(sim_time_s)

        events = []

        for trigger in self._triggers:
            # Skip already-fired one-time triggers
            if trigger.fired and trigger.once:
                continue

            # Evaluate condition
            if self._evaluator.evaluate(trigger.condition):
                # Trigger fires! Execute actions
                trigger.fired = True

                for action in trigger.actions:
                    event = self._execute_action(action, trigger.id)
                    if event:
                        events.append(event)

        return events

    def _execute_action(
        self,
        action: ScenarioAction,
        trigger_id: str
    ) -> Dict[str, Any]:
        """Execute a single action and return an event dict.

        Returns:
            Event dict describing what happened
        """
        params = action.params

        if action.type == "send_comms":
            # Send message to captain comms (friendly comms)
            return {
                "type": "send_comms",
                "trigger_id": trigger_id,
                "text": params.get("text", ""),
                "priority": params.get("priority", "normal"),
            }

        elif action.type == "broadcast_intercept":
            # Broadcast enemy message (interceptable)
            return {
                "type": "broadcast_intercept",
                "trigger_id": trigger_id,
                "text": params.get("text", ""),
                "partial": params.get("partial", False),
                "source_ship": params.get("source_ship"),
            }

        elif action.type == "change_behavior":
            # Update ship behavior instruction
            ship_id = params.get("ship_id")
            behavior = params.get("behavior", "")
            if ship_id:
                return {
                    "type": "change_behavior",
                    "trigger_id": trigger_id,
                    "ship_id": ship_id,
                    "behavior": behavior,
                }

        elif action.type == "end_scenario":
            # End the mission with specified outcome
            return {
                "type": "end_scenario",
                "trigger_id": trigger_id,
                "outcome": params.get("outcome", "draw"),
                "reason": params.get("reason", "Scenario trigger"),
            }

        elif action.type == "set_ai_mode":
            # Change AI behavior mode
            ship_id = params.get("ship_id")
            mode = params.get("mode", "normal")
            if ship_id:
                return {
                    "type": "set_ai_mode",
                    "trigger_id": trigger_id,
                    "ship_id": ship_id,
                    "mode": mode,
                }

        # Unknown action type
        return {
            "type": "unknown",
            "trigger_id": trigger_id,
            "action_type": action.type,
            "params": params,
        }

    def get_trigger_states(self) -> List[Dict[str, Any]]:
        """Get current state of all triggers (for debugging).

        Returns:
            List of trigger state dicts
        """
        return [
            {
                "id": t.id,
                "fired": t.fired,
                "once": t.once,
                "condition_type": t.condition.type,
            }
            for t in self._triggers
        ]

    def reset(self) -> None:
        """Reset all triggers to unfired state."""
        self._triggers.clear()
        self._initialized = False
