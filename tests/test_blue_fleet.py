"""Tests for the BLUE Fleet Commander pure module + orchestrator wiring.

Covers:
- BlueIntelBuffer sampling and age-gated release
- Snapshot lossiness (positions to grid, heading bins, integer speed)
- Submarines (depth > 30m) excluded from snapshots
- build_prompt_payload structure
- StubEngine radio brief shape
- Orchestrator run_blue_fleet end-to-end with stub engine
"""
from __future__ import annotations

import asyncio

import pytest

from backend.sim.blue_fleet import (
    BlueIntelBuffer,
    BlueIntelSnapshot,
    build_prompt_payload,
    sample_red_state,
)
from backend.sim.ai_engines import StubEngine

from conftest import make_ship, make_world


# --------------------------------------------------------------------------- #
# Snapshot capture
# --------------------------------------------------------------------------- #

def test_sample_red_state_only_captures_red_ships():
    own = make_ship(id_="ownship", side="BLUE", x=0, y=0, depth=100)
    red1 = make_ship(id_="red-1", side="RED", x=5000, y=2000, depth=0, heading=183.0, speed=12.5)
    red2 = make_ship(id_="red-2", side="RED", x=-3000, y=8000, depth=0, heading=270.0, speed=9.0)
    blue_friendly = make_ship(id_="blue-2", side="BLUE", x=1000, y=1000, depth=0)
    world = make_world(own, red1, red2, blue_friendly)

    snap = sample_red_state(world, sim_time_s=300.0)

    ids = sorted(c["id"] for c in snap.contacts)
    assert ids == ["red-1", "red-2"]
    assert snap.sim_time_s == 300.0


def test_sample_red_state_excludes_submerged_submarines():
    own = make_ship(id_="ownship", side="BLUE")
    red_surface = make_ship(id_="red-dd", side="RED", depth=0)
    red_sub = make_ship(id_="red-ss", side="RED", depth=80.0)
    world = make_world(own, red_surface, red_sub)

    snap = sample_red_state(world, sim_time_s=0.0)
    ids = [c["id"] for c in snap.contacts]
    assert ids == ["red-dd"]


def test_snapshot_position_rounded_to_grid():
    own = make_ship(id_="ownship", side="BLUE")
    red = make_ship(id_="red-1", side="RED", x=5523.0, y=2189.0, depth=0, heading=183.0, speed=12.5)
    world = make_world(own, red)
    snap = sample_red_state(world, sim_time_s=0.0)
    c = snap.contacts[0]
    # 1 km grid: 5523 → 6000, 2189 → 2000
    assert c["pos_grid"] == [6000.0, 2000.0]
    # 30° heading bin: 183 → 180
    assert c["heading_bin_deg"] == 180.0
    # speed rounded to integer (banker's rounding — exact half goes to even)
    assert float(c["speed_kn_round"]).is_integer()
    assert abs(c["speed_kn_round"] - 12.5) <= 0.5


# --------------------------------------------------------------------------- #
# Buffer aging
# --------------------------------------------------------------------------- #

def test_buffer_releases_only_aged_snapshots():
    buf = BlueIntelBuffer()
    own = make_ship(id_="ownship", side="BLUE")
    red = make_ship(id_="red-1", side="RED")
    world = make_world(own, red)

    buf.record_sample(world, sim_time_s=0.0)
    buf.record_sample(world, sim_time_s=600.0)
    buf.record_sample(world, sim_time_s=1200.0)

    # Now is 1300s. min_age 900s → only the 0s and 400s old snapshots should be released
    out = buf.releasable(now_s=1300.0, min_age_s=900.0)
    times = [s.sim_time_s for s in out]
    assert times == [0.0]  # only the 1300-old one passes


def test_buffer_releasable_oldest_first():
    buf = BlueIntelBuffer()
    own = make_ship(id_="ownship", side="BLUE")
    red = make_ship(id_="red-1", side="RED")
    world = make_world(own, red)
    for t in [0.0, 600.0, 1200.0]:
        buf.record_sample(world, sim_time_s=t)
    out = buf.releasable(now_s=10000.0, min_age_s=0.0)
    assert [s.sim_time_s for s in out] == [0.0, 600.0, 1200.0]


def test_buffer_caps_max_snapshots():
    buf = BlueIntelBuffer(max_snapshots=3)
    own = make_ship(id_="ownship", side="BLUE")
    red = make_ship(id_="red-1", side="RED")
    world = make_world(own, red)
    for t in range(10):
        buf.record_sample(world, sim_time_s=float(t))
    assert len(buf.snapshots) == 3
    assert [s.sim_time_s for s in buf.snapshots] == [7.0, 8.0, 9.0]


# --------------------------------------------------------------------------- #
# Prompt payload
# --------------------------------------------------------------------------- #

def test_build_prompt_payload_includes_intel_with_age():
    snap = BlueIntelSnapshot(sim_time_s=300.0, contacts=[{"id": "red-1"}], source="intercept")
    payload = build_prompt_payload(
        snapshots=[snap],
        now_s=2000.0,
        mission_brief={"id": "m1", "title": "Test", "scenario_context": {"BLUE": "patrol"}},
        ownship_summary={"depth_m": 18, "speed_kn": 4.0},
    )
    assert payload["mission"]["id"] == "m1"
    assert payload["mission"]["blue_context"] == "patrol"
    assert payload["intel_snapshots"][0]["as_of_s_ago"] == 1700
    assert payload["intel_snapshots"][0]["contacts"][0]["id"] == "red-1"


def test_build_prompt_payload_handles_missing_mission_brief():
    payload = build_prompt_payload(
        snapshots=[],
        now_s=100.0,
        mission_brief=None,
        ownship_summary={"depth_m": 50, "speed_kn": 0.0},
    )
    assert payload["mission"]["id"] is None
    assert payload["intel_snapshots"] == []


# --------------------------------------------------------------------------- #
# Stub engine produces a parseable brief
# --------------------------------------------------------------------------- #

def test_stub_engine_radio_brief_with_no_intel():
    engine = StubEngine()
    payload = {"intel_snapshots": [], "mission": {}, "ownship_reported": {}}
    brief = asyncio.run(engine.propose_radio_brief(payload))
    assert "messages" in brief
    assert isinstance(brief["messages"], list)
    assert len(brief["messages"]) >= 1
    # Tag should be COMSUBPAC for the baseline order
    assert any(m.get("tag") == "COMSUBPAC" for m in brief["messages"])


def test_stub_engine_radio_brief_with_intel_includes_ultra_line():
    engine = StubEngine()
    payload = {
        "intel_snapshots": [
            {"as_of_s_ago": 1800, "source": "intercept",
             "contacts": [{"id": "red-1"}, {"id": "red-2"}]}
        ],
        "mission": {},
        "ownship_reported": {},
    }
    brief = asyncio.run(engine.propose_radio_brief(payload))
    tags = [m.get("tag") for m in brief["messages"]]
    assert "ULTRA" in tags
    ultra = next(m for m in brief["messages"] if m["tag"] == "ULTRA")
    assert "30" in ultra["text"]  # 1800s ago = 30 min
    assert "2" in ultra["text"]  # 2 contacts


# --------------------------------------------------------------------------- #
# Orchestrator integration with stub engine
# --------------------------------------------------------------------------- #

def test_orchestrator_run_blue_fleet_appends_messages_with_stub():
    """End-to-end: orchestrator builds payload from buffer, calls stub engine,
    returns messages. No mutation of sim state — the loop is responsible for
    surfacing comms; here we just verify the return shape."""
    from backend.sim.ai_orchestrator import AgentsOrchestrator

    own = make_ship(id_="ownship", side="BLUE")
    red = make_ship(id_="red-1", side="RED", x=4000, y=0, heading=90, speed=10)
    world = make_world(own, red)

    orch = AgentsOrchestrator(world_getter=lambda: world, storage_engine=None, run_id=1)
    orch.set_blue_fleet_engine("stub", "stub")
    # Take a sample at t=0; brief at t=2000 with 900s min age → snapshot is 2000s old, releasable
    orch.record_blue_intel_sample(sim_time_s=0.0)

    result = asyncio.run(orch.run_blue_fleet(
        sim_time_s=2000.0,
        intel_min_age_s=900.0,
        ownship_summary={"depth_m": 18, "speed_kn": 4.0},
        mission_brief={"id": "test", "scenario_context": {"BLUE": "patrol box"}},
    ))
    assert "messages" in result
    assert len(result["messages"]) >= 1
    # State updated so cadence accounting works
    assert orch._blue_last_brief_sim_time_s == 2000.0


def test_orchestrator_blue_fleet_no_intel_when_too_fresh():
    """If no snapshot is old enough, the prompt has empty intel — brief still
    returns (the LLM/stub may emit just a routine line)."""
    from backend.sim.ai_orchestrator import AgentsOrchestrator

    own = make_ship(id_="ownship", side="BLUE")
    red = make_ship(id_="red-1", side="RED")
    world = make_world(own, red)

    orch = AgentsOrchestrator(world_getter=lambda: world, storage_engine=None, run_id=1)
    orch.set_blue_fleet_engine("stub", "stub")
    orch.record_blue_intel_sample(sim_time_s=0.0)
    # Ask 200s later with a 900s floor → no snapshots releasable
    result = asyncio.run(orch.run_blue_fleet(
        sim_time_s=200.0,
        intel_min_age_s=900.0,
        ownship_summary={"depth_m": 18},
    ))
    assert "messages" in result
    # Stub returns at least the COMSUBPAC line even with no intel
    assert any(m.get("tag") == "COMSUBPAC" for m in result["messages"])
