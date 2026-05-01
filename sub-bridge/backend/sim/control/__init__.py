"""Ship control layer.

Three responsibilities:

- **Hands** (`ShipControls`): the single chokepoint for ship-state mutation.
- **Actions** (`Action` and subclasses): typed records of what to do.
- **Controllers** (`ShipController` and subclasses): decide what actions to
  take (LLM, scripted, eventually human).
"""
from .actions import (
    Action,
    ActivePingAction,
    DeployCountermeasureAction,
    DropDepthChargesAction,
    FireTorpedoAction,
    SetNavAction,
)
from .controllers import (
    LLMShipController,
    ScriptedShipController,
    ShipController,
    tool_calls_to_actions,
)
from .hands import ControlResult, ShipControls

__all__ = [
    "Action",
    "ActivePingAction",
    "ControlResult",
    "DeployCountermeasureAction",
    "DropDepthChargesAction",
    "FireTorpedoAction",
    "LLMShipController",
    "ScriptedShipController",
    "SetNavAction",
    "ShipController",
    "ShipControls",
    "tool_calls_to_actions",
]
