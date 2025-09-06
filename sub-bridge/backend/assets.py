from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any, List, Optional

from pydantic import BaseModel, Field, ValidationError

from . import models


ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
MISSIONS_DIR = ASSETS_DIR / "missions"
SHIPS_CATALOG_PATH = ASSETS_DIR / "ships" / "catalog.json"


class MissionShipSpawn(BaseModel):
    id: str
    side: str
    class_name: str = Field(alias="class")
    spawn: Dict[str, float]


class MissionConfig(BaseModel):
    id: str
    title: str
    objective: str
    roe: List[str] = Field(default_factory=list)
    target_wp: Optional[List[float]] = None
    environment: Dict[str, Any] = Field(default_factory=dict)
    ships: List[MissionShipSpawn]
    triggers: List[Dict[str, Any]] = Field(default_factory=list)
    # New fields for richer UX and AI prompting
    captain_summary: Optional[str] = None  # deprecated; use blue_captain_summary
    blue_captain_summary: Optional[str] = None
    red_mission_summary: Optional[str] = None
    blue_mission_summary: Optional[str] = None
    # Structured mission supplements (passed through to AI)
    side_objectives: Dict[str, Any] = Field(default_factory=dict)
    success_criteria: Dict[str, Any] = Field(default_factory=dict)
    # Ship-specific behavior instructions for AI
    ship_behaviors: Dict[str, str] = Field(default_factory=dict)
    # Legacy prompt fields (no longer used by orchestrator)
    ai_fleet_prompt: Optional[str] = None
    ai_ship_prompts: Dict[str, str] = Field(default_factory=dict)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_ship_catalog(path: Path = SHIPS_CATALOG_PATH) -> None:
    """Load ship catalog JSON and update models.SHIP_CATALOG in-place."""
    if not path.exists():
        print(f"ERROR: Ship catalog not found at {path}")
        return
    try:
        data = _read_json(path)
    except Exception as e:
        print(f"ERROR: Failed to read ship catalog: {e}")
        return
    # Build new catalog
    new_catalog: Dict[str, models.ShipDef] = {}
    try:
        for key, entry in data.items():
            capabilities = models.ShipCapabilities(**entry.get("capabilities", {}))
            hull = models.Hull(**entry.get("hull", {}))
            # Weapons
            w = entry.get("weapons", {}) or {}
            tubes = w.get("tubes")
            ws = models.WeaponsSuite(
                tube_count=w.get("tube_count", 6),
                torpedoes_stored=w.get("torpedoes_stored", 6),
                reload_time_s=w.get("reload_time_s", 45.0),
                flood_time_s=w.get("flood_time_s", 8.0),
                doors_time_s=w.get("doors_time_s", 3.0),
                depth_charges_stored=w.get("depth_charges_stored", 0),
                depth_charge_cooldown_s=w.get("depth_charge_cooldown_s", 2.0),
                tubes=[models.Tube(**t) for t in tubes] if isinstance(tubes, list) else models.WeaponsSuite().tubes,
            )
            acoustics = models.Acoustics(**entry.get("acoustics", {}))
            new_catalog[key] = models.ShipDef(
                name=entry.get("name", key),
                ship_class=entry.get("ship_class", key),
                capabilities=capabilities,
                default_hull=hull,
                default_weapons=ws,
                default_acoustics=acoustics,
            )
        # Update in place so existing imports see the change
        models.SHIP_CATALOG.clear()
        models.SHIP_CATALOG.update(new_catalog)
        print(f"INFO: Loaded {len(new_catalog)} ship definitions from catalog")
    except Exception as e:
        print(f"ERROR: Failed to build ship catalog: {e}")
        # Keep existing catalog if building fails


def load_mission_by_id(mission_id: str) -> Optional[MissionConfig]:
    path = (MISSIONS_DIR / f"{mission_id}.json").resolve()
    if not path.exists():
        return None
    try:
        data = _read_json(path)
        return MissionConfig(**data)
    except ValidationError:
        return None


def apply_mission_to_world(mission: MissionConfig, world_getter, set_mission_brief) -> None:
    """Configure world state per mission. Overwrites world and sets mission brief.

    - world_getter: callable returning the authoritative World object
    - set_mission_brief: callable accepting a dict to store in Simulation.mission_brief
    """
    world = world_getter()
    # Reset world
    world.ships.clear()
    # Spawn ships from mission using catalog defaults
    for s in mission.ships:
        cat = models.SHIP_CATALOG.get(s.class_name)
        if not cat:
            continue
        # Build Ship
        kin = models.Kinematics(
            x=float(s.spawn.get("x", 0.0)),
            y=float(s.spawn.get("y", 0.0)),
            depth=float(s.spawn.get("depth", 0.0)),
            heading=float(s.spawn.get("heading", 0.0)),
            speed=float(s.spawn.get("speed", 0.0)),
        )
        ship = models.Ship(
            id=s.id,
            side=str(s.side).upper(),
            kin=kin,
            hull=cat.default_hull.model_copy(deep=True),
            acoustics=cat.default_acoustics.model_copy(deep=True),
            weapons=cat.default_weapons.model_copy(deep=True),
            reactor=models.Reactor(output_mw=50.0, max_mw=100.0),
            damage=models.DamageState(),
            ship_class=cat.ship_class,
            capabilities=cat.capabilities.model_copy(deep=True),
        )
        world.add_ship(ship)
    # Mission brief for UI/AI
    brief = {
        "title": mission.title,
        "objective": mission.objective,
        "roe": mission.roe,
        "target_wp": mission.target_wp,
        # Prefer new fields; fall back to deprecated captain_summary for BLUE UI
        "blue_captain_summary": mission.blue_captain_summary or mission.captain_summary,
        # Side mission summaries for AI and UI
        "red_mission_summary": mission.red_mission_summary,
        "blue_mission_summary": mission.blue_mission_summary,
        # Structured mission supplements for AI
        "side_objectives": mission.side_objectives,
        "success_criteria": mission.success_criteria,
        # Ship-specific behavior instructions for AI
        "ship_behaviors": mission.ship_behaviors,
        "comms_schedule": [
            {"at_s": float(t.get("at_s", 0.0)), "msg": t.get("comms")}
            for t in mission.triggers if "comms" in t
        ],
    }
    set_mission_brief(brief)


