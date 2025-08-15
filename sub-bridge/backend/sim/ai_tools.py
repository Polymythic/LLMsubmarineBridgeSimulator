from __future__ import annotations
import random
from ..models import Ship


AI_TOOL_SCHEMA = {
    "set_nav": {
        "description": "Set navigation orders: heading (deg 0-359.9), speed (kn ≥0), depth (m ≥0)",
        "args": {
            "heading": "float",
            "speed": "float",
            "depth": "float",
        },
    },
    "fire_torpedo": {
        "description": "Fire a torpedo if available: tube index, true bearing, run depth, enable range",
        "args": {
            "tube": "int",
            "bearing": "float",
            "run_depth": "float",
            "enable_range": "float",
        },
    },
    "deploy_countermeasure": {
        "description": "Deploy a countermeasure if supported by the platform",
        "args": {
            "type": "str (noisemaker|decoy)",
        },
    },
    "drop_depth_charges": {
        "description": "Drop a spread of depth charges. Consumes inventory and enforces cooldown.",
        "args": {
            "spread_meters": "float (radius around ship position; e.g., 20)",
            "minDepth": "float (min detonation depth; >=15m)",
            "maxDepth": "float (max detonation depth)",
            "spreadSize": "int (1..10 number of charges in this drop)",
        },
    },
    "launch_torpedo_quick": {
        "description": "AI-only: quickly launch a torpedo without tube prep. 5s cooldown, consumes inventory.",
        "args": {
            "bearing": "float (true bearing deg)",
            "run_depth": "float (m)",
            "enable_range": "float (m, optional)",
            "doctrine": "str (optional)"
        }
    },
}


class LocalAIStub:
    def propose_orders(self, ship: Ship) -> dict:
        cons = ship.ai_profile.constraints if ship.ai_profile else {"maxSpeed": 15.0, "maxDepth": 300.0, "turnRate": 7.0}
        new_heading = (ship.kin.heading + random.uniform(-15, 15)) % 360
        new_speed = max(3.0, min(cons.get("maxSpeed", 15.0), ship.kin.speed + random.uniform(-1, 1)))
        new_depth = max(50.0, min(cons.get("maxDepth", 300.0), ship.kin.depth + random.uniform(-5, 5)))
        return {"tool": "set_nav", "arguments": {"heading": new_heading, "speed": new_speed, "depth": new_depth}}
