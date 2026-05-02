"""Tests for the Phase 6 scenario system: roles, task groups, scenario context.

Covers:
- Mission JSON with the new fields parses cleanly through `MissionConfig`.
- `apply_mission_to_world` propagates the new fields into `mission_brief`.
- `_build_ship_summary` projects `role`, `task_group_context`, and
  `tactical_briefing` for ships that have a role assignment.
- Legacy missions without the new fields still produce a valid summary
  (fields are empty / None, no exceptions).
- The role-prompt loader finds existing roles and warns on missing ones.
"""
import asyncio

import pytest

from backend.assets import MissionConfig, apply_mission_to_world, load_mission_by_id
from backend.models import ShipCapabilities
from backend.sim.ai_orchestrator import (
    AgentsOrchestrator,
    _load_role_prompt,
)
from backend.sim.ecs import World

from conftest import StubLLMEngine, install_stub_engines, make_ship


# --------------------------------------------------------------------------- #
# Mission schema parses
# --------------------------------------------------------------------------- #

def test_interdict_dual_convoys_parses_with_new_fields():
    mission = load_mission_by_id("interdict_dual_convoys")
    assert mission is not None
    # New scenario_context block populated for both sides
    assert "RED" in mission.scenario_context
    assert "BLUE" in mission.scenario_context
    assert "narrative" in mission.scenario_context["RED"]
    # Two RED task groups
    assert "RED" in mission.task_groups
    red_groups = mission.task_groups["RED"]
    assert "CONVOY_A" in red_groups
    assert "CONVOY_B" in red_groups
    assert red_groups["CONVOY_A"]["lead"] == "red-a-dd-01"
    assert "red-a-cv-01" in red_groups["CONVOY_A"]["protected"]
    # Per-ship role index
    assert mission.ship_roles["red-a-dd-01"]["role"] == "convoy_escort_destroyer"
    assert mission.ship_roles["red-a-cv-01"]["role"] == "convoy_cargo"


def test_legacy_mission_still_parses_without_new_fields():
    """A mission JSON that omits the Phase 6 fields entirely must still
    parse cleanly — the new fields default to empty dicts. Confirms the
    schema is backward-compatible."""
    minimal = {
        "id": "legacy_test",
        "title": "Legacy Test",
        "objective": "anything",
        "ships": [
            {"id": "ownship", "side": "BLUE", "class": "SSN",
             "spawn": {"x": 0, "y": 0, "depth": 0, "heading": 0, "speed": 0}},
        ],
    }
    mission = MissionConfig(**minimal)
    assert mission.scenario_context == {}
    assert mission.task_groups == {}
    assert mission.ship_roles == {}


# --------------------------------------------------------------------------- #
# apply_mission_to_world plumbs the new fields into mission_brief
# --------------------------------------------------------------------------- #

def test_apply_mission_propagates_scenario_fields_to_brief():
    mission = load_mission_by_id("interdict_dual_convoys")
    assert mission is not None
    world = World()
    captured = {}
    apply_mission_to_world(mission, lambda: world, lambda b: captured.update(b))
    assert captured["scenario_context"]["RED"]["primary_objective"]
    assert "CONVOY_A" in captured["task_groups"]["RED"]
    assert captured["ship_roles"]["red-a-dd-01"]["role"] == "convoy_escort_destroyer"


# --------------------------------------------------------------------------- #
# _load_role_prompt
# --------------------------------------------------------------------------- #

def test_load_role_prompt_returns_doctrine_text():
    text = _load_role_prompt("convoy_escort_destroyer")
    assert "Convoy Escort Destroyer" in text
    assert "EMCON" in text or "emcon" in text.lower()


def test_load_role_prompt_missing_returns_empty():
    assert _load_role_prompt("nonexistent_role_xyz") == ""


def test_load_role_prompt_empty_name_returns_empty():
    assert _load_role_prompt("") == ""


# --------------------------------------------------------------------------- #
# _build_ship_summary projects role + task_group_context + tactical_briefing
# --------------------------------------------------------------------------- #

def _orch_with_world(world: World) -> AgentsOrchestrator:
    return AgentsOrchestrator(lambda: world, storage_engine=None, run_id=0)


def test_ship_summary_projects_role_and_task_group():
    """Build a synthetic mission_brief with task groups and ship roles, then
    confirm `_build_ship_summary` exposes them on a relevant ship."""
    own = make_ship(id_="ownship", side="BLUE", x=10000.0, y=10000.0)
    dd = make_ship(id_="red-a-dd-01", side="RED", ship_class="Destroyer", x=0.0, y=0.0)
    dd.capabilities = ShipCapabilities(has_torpedoes=True, has_depth_charges=True, has_active_sonar=True)
    cv = make_ship(id_="red-a-cv-01", side="RED", ship_class="Convoy", x=-200.0, y=-100.0)
    cv.capabilities = ShipCapabilities(has_torpedoes=False, has_depth_charges=False, has_active_sonar=False)
    world = World()
    for s in (own, dd, cv):
        world.add_ship(s)

    orch = _orch_with_world(world)
    orch._mission_brief = {
        "ship_roles": {
            "red-a-dd-01": {"role": "convoy_escort_destroyer", "task_group": "CONVOY_A"},
            "red-a-cv-01": {"role": "convoy_cargo",            "task_group": "CONVOY_A"},
        },
        "task_groups": {
            "RED": {
                "CONVOY_A": {
                    "doctrine": "convoy_escort",
                    "lead": "red-a-dd-01",
                    "members": ["red-a-dd-01", "red-a-cv-01"],
                    "protected": ["red-a-cv-01"],
                    "formation": "screen-ahead",
                }
            }
        },
    }

    summary = orch._build_ship_summary(dd)
    assert summary["role"] == "convoy_escort_destroyer"
    tg = summary["task_group_context"]
    assert tg is not None
    assert tg["name"] == "CONVOY_A"
    assert tg["is_lead"] is True
    assert tg["doctrine"] == "convoy_escort"
    assert "red-a-cv-01" in tg["protected"]
    # Peers list should include the cargo, not the lead itself
    peer_ids = [p["id"] for p in tg["peers"]]
    assert peer_ids == ["red-a-cv-01"]


def test_ship_summary_tactical_briefing_recommends_transit_with_fleet_destination():
    own = make_ship(id_="ownship", side="BLUE", x=20000.0, y=20000.0)
    dd = make_ship(id_="red-a-dd-01", side="RED", ship_class="Destroyer", x=0.0, y=0.0)
    dd.capabilities = ShipCapabilities(has_torpedoes=True, has_depth_charges=True, has_active_sonar=True)
    world = World()
    for s in (own, dd):
        world.add_ship(s)

    orch = _orch_with_world(world)
    orch._mission_brief = {}
    # Inject a fleet intent that orders DD-01 to a destination.
    orch._last_fleet_intent = {
        "objectives": {"red-a-dd-01": {"destination": [10000.0, 0.0], "speed_kn": 12.0, "goal": "patrol east"}}
    }

    summary = orch._build_ship_summary(dd)
    tb = summary["tactical_briefing"]
    assert tb is not None
    # No actionable contacts → expect TRANSIT toward fleet destination
    assert tb["doctrine_recommendation"] == "TRANSIT"
    assert tb["suggested_heading"] == pytest.approx(90.0, abs=0.5)
    assert tb["suggested_speed_kn"] == pytest.approx(12.0)
    assert "fleet_destination_bearing" in tb
    assert tb["fleet_destination_range_m"] == pytest.approx(10000.0, abs=1.0)


def test_ship_summary_legacy_mission_has_empty_role_fields():
    """A ship in a mission without `ship_roles`/`task_groups` should still
    receive a summary; new fields are just empty / None."""
    dd = make_ship(id_="red-01", side="RED", ship_class="Destroyer")
    dd.capabilities = ShipCapabilities()
    world = World()
    world.add_ship(dd)

    orch = _orch_with_world(world)
    orch._mission_brief = {}  # legacy mission

    summary = orch._build_ship_summary(dd)
    assert summary["role"] == ""
    assert summary["task_group_context"] is None
    # Tactical briefing still computes (HOLD because no contacts and no fleet dest)
    assert summary["tactical_briefing"] is not None
    assert summary["tactical_briefing"]["doctrine_recommendation"] == "HOLD"


# --------------------------------------------------------------------------- #
# Fleet summary surfaces scenario_context and task_groups
# --------------------------------------------------------------------------- #

def test_fleet_summary_includes_red_scenario_context_and_task_groups():
    own = make_ship(id_="ownship", side="BLUE")
    dd = make_ship(id_="red-a-dd-01", side="RED", ship_class="Destroyer")
    cv = make_ship(id_="red-a-cv-01", side="RED", ship_class="Convoy")
    world = World()
    for s in (own, dd, cv):
        world.add_ship(s)

    orch = _orch_with_world(world)
    orch._mission_brief = {
        "scenario_context": {
            "RED": {"narrative": "TEST RED narrative", "primary_objective": "Deliver"},
            "BLUE": {"narrative": "TEST BLUE narrative"},
        },
        "task_groups": {
            "RED": {"CONVOY_A": {"members": ["red-a-dd-01", "red-a-cv-01"]}}
        },
        "ship_roles": {
            "red-a-dd-01": {"role": "convoy_escort_destroyer", "task_group": "CONVOY_A"}
        },
    }

    summary = orch._build_fleet_summary()
    mission = summary["mission"]
    # RED scenario context surfaced; BLUE not (privacy boundary)
    assert mission["scenario_context"]["narrative"] == "TEST RED narrative"
    assert mission["scenario_context"]["primary_objective"] == "Deliver"
    # Task groups surfaced
    assert "CONVOY_A" in mission["task_groups"]
    # Ship roles index passed through
    assert mission["ship_roles"]["red-a-dd-01"]["role"] == "convoy_escort_destroyer"
