"""Ship decision-makers.

A `ShipController` decides what action(s) a ship should take this step. It
returns typed `Action` objects; a separate caller is responsible for applying
those actions through `ShipControls` and handling any post-apply side
effects.

Concrete implementations:

- `LLMShipController` — wraps an `AgentsOrchestrator` and translates the
  orchestrator's validated tool calls into `Action` objects.
- `ScriptedShipController` — replays a queue of pre-set actions; used in
  tests and reusable for demos / regression scenarios.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

from .actions import (
    Action,
    ActivePingAction,
    DeployCountermeasureAction,
    DropDepthChargesAction,
    FireTorpedoAction,
    SetNavAction,
)

if TYPE_CHECKING:  # avoid runtime import cycle
    from ..ai_orchestrator import AgentsOrchestrator


def _unwrap_scalar(v: Any) -> Any:
    """LLM tool calls occasionally wrap scalars in single-element lists."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tool_calls_to_actions(tool_calls: Sequence[Dict[str, Any]]) -> List[Action]:
    """Translate orchestrator-validated tool calls into typed `Action`s.

    Unknown tools are silently dropped. The orchestrator already filters most
    invalid tools; anything that slips through is a no-op here rather than a
    crash.
    """
    out: List[Action] = []
    for tc in tool_calls:
        tool = tc.get("tool")
        args = tc.get("arguments", {}) or {}

        if tool == "set_nav":
            out.append(
                SetNavAction(
                    heading=_to_float(_unwrap_scalar(args.get("heading"))),
                    speed=_to_float(_unwrap_scalar(args.get("speed"))),
                    depth=_to_float(_unwrap_scalar(args.get("depth"))),
                )
            )
        elif tool in ("fire_torpedo", "launch_torpedo_quick"):
            out.append(
                FireTorpedoAction(
                    bearing=_to_float(_unwrap_scalar(args.get("bearing"))),
                    run_depth=_to_float(_unwrap_scalar(args.get("run_depth"))),
                    enable_range=_to_float(_unwrap_scalar(args.get("enable_range"))),
                    doctrine=str(args.get("doctrine") or "passive_then_active"),
                )
            )
        elif tool == "drop_depth_charges":
            out.append(
                DropDepthChargesAction(
                    spread_meters=_to_float(_unwrap_scalar(args.get("spread_meters"))) or 20.0,
                    min_depth=_to_float(_unwrap_scalar(args.get("minDepth"))) or 30.0,
                    max_depth=_to_float(_unwrap_scalar(args.get("maxDepth"))) or 50.0,
                    spread_size=int(_to_float(_unwrap_scalar(args.get("spreadSize"))) or 3),
                )
            )
        elif tool == "deploy_countermeasure":
            out.append(DeployCountermeasureAction(type_=str(args.get("type", "noisemaker"))))
        elif tool == "active_ping":
            out.append(ActivePingAction())
        # Unknown tools (e.g., set_fleet_intent, write_journal) are
        # intentionally not actions for a ship and are dropped here.
    return out


class ShipController:
    """Abstract decision-maker for a single ship."""

    async def step(self, ship_id: str) -> Sequence[Action]:
        """Decide what action(s) the ship should take. Empty = no-op."""
        raise NotImplementedError


class LLMShipController(ShipController):
    """Drive a ship by calling `AgentsOrchestrator.run_ship`.

    Takes a getter rather than the orchestrator directly so that
    `Simulation` can rebuild the orchestrator (e.g., on debug.restart) without
    leaving this controller holding a stale reference.
    """

    def __init__(self, orchestrator_getter: Callable[[], "Optional[AgentsOrchestrator]"]) -> None:
        self._get_orch = orchestrator_getter

    async def step(self, ship_id: str) -> Sequence[Action]:
        orch = self._get_orch()
        if orch is None:
            return []
        result = await orch.run_ship(ship_id)
        return tool_calls_to_actions(result.get("tool_calls_validated", []))


class ScriptedShipController(ShipController):
    """Replay a pre-set sequence of action lists per ship.

    Each call to `step(ship_id)` pops the next list of actions from that
    ship's queue. When the queue is empty, returns an empty list (no-op).
    """

    def __init__(self, actions_by_ship: Optional[Dict[str, List[List[Action]]]] = None) -> None:
        self._queues: Dict[str, List[List[Action]]] = {
            sid: list(seq) for sid, seq in (actions_by_ship or {}).items()
        }

    def queue(self, ship_id: str, actions: Sequence[Action]) -> None:
        """Append a list of actions to be returned on the next `step(ship_id)`."""
        self._queues.setdefault(ship_id, []).append(list(actions))

    async def step(self, ship_id: str) -> Sequence[Action]:
        q = self._queues.get(ship_id)
        if not q:
            return []
        return q.pop(0)
