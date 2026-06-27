"""Phase 0 safety net: lock current behavior of the LLM → tool-call seam.

These tests construct an `AgentsOrchestrator` directly with a `StubLLMEngine`
and assert the shape of `run_fleet()` / `run_ship()` outputs. This is the
data that `loop.py:856-1154` consumes to mutate ship state.

After Phase 2, `run_ship` is expected to return typed `Action` objects
instead of raw `tool_calls_validated`. These tests will then be updated to
match — but until then, they lock the current contract.
"""
import asyncio
from typing import Any, Dict

import pytest

from backend.sim.ai_orchestrator import AgentsOrchestrator
from backend.sim.ecs import World

from conftest import StubLLMEngine, install_stub_engines, make_ship


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _make_world_with_red_destroyer() -> World:
    own = make_ship(id_="ownship", side="BLUE", x=0.0, y=0.0)
    red = make_ship(id_="red-01", side="RED", ship_class="Destroyer", x=3000.0, y=0.0)
    w = World()
    w.add_ship(own)
    w.add_ship(red)
    return w


def _make_orch_with_stub(world: World, stub: StubLLMEngine) -> AgentsOrchestrator:
    orch = AgentsOrchestrator(lambda: world, storage_engine=None, run_id=0)
    install_stub_engines(orch, stub)
    return orch


# --------------------------------------------------------------------------- #
# Ship-level tool calls
# --------------------------------------------------------------------------- #

def test_run_ship_set_nav_returns_validated_tool_call():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine(ship_responses={
        "red-01": [{
            "tool": "set_nav",
            "arguments": {"heading": 90.0, "speed": 12.0, "depth": 0.0},
            "summary": "head east",
        }]
    })
    orch = _make_orch_with_stub(world, stub)

    result = asyncio.run(orch.run_ship("red-01"))

    validated = result.get("tool_calls_validated", [])
    assert len(validated) == 1
    tc = validated[0]
    assert tc["tool"] == "set_nav"
    assert tc["arguments"]["heading"] == pytest.approx(90.0)
    assert tc["arguments"]["speed"] == pytest.approx(12.0)


def test_run_ship_fire_torpedo_returns_validated_tool_call():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine(ship_responses={
        "red-01": [{
            "tool": "fire_torpedo",
            "arguments": {"tube": 1, "bearing": 270.0, "run_depth": 100.0, "enable_range": 1500.0},
            "summary": "engage contact",
        }]
    })
    orch = _make_orch_with_stub(world, stub)

    result = asyncio.run(orch.run_ship("red-01"))
    validated = result.get("tool_calls_validated", [])
    assert len(validated) == 1
    assert validated[0]["tool"] == "fire_torpedo"
    assert validated[0]["arguments"]["bearing"] == pytest.approx(270.0)


def test_run_ship_drop_depth_charges_returns_validated_tool_call():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine(ship_responses={
        "red-01": [{
            "tool": "drop_depth_charges",
            "arguments": {"spread_meters": 30.0, "minDepth": 30.0, "maxDepth": 80.0, "spreadSize": 4},
            "summary": "drop a spread",
        }]
    })
    orch = _make_orch_with_stub(world, stub)

    result = asyncio.run(orch.run_ship("red-01"))
    validated = result.get("tool_calls_validated", [])
    assert len(validated) == 1
    assert validated[0]["tool"] == "drop_depth_charges"
    assert validated[0]["arguments"]["spreadSize"] == 4


def test_run_ship_unknown_tool_returns_no_validated_call_or_intent_fallback():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine(ship_responses={
        "red-01": [{
            "tool": "this_tool_does_not_exist",
            "arguments": {},
            "summary": "broken",
        }]
    })
    orch = _make_orch_with_stub(world, stub)

    result = asyncio.run(orch.run_ship("red-01"))
    validated = result.get("tool_calls_validated", [])
    # Either no action applied, or an intent-derived nav fallback — never the
    # original unknown tool.
    if validated:
        assert validated[0]["tool"] == "set_nav"
    assert "Unknown tool" in (result.get("error") or "")


def test_run_ship_records_engine_call_input():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine()
    orch = _make_orch_with_stub(world, stub)

    asyncio.run(orch.run_ship("red-01"))

    # The orchestrator passed a ship summary to the stub (with a prompt hint
    # appended); we just assert the call was made with the right ship id.
    assert any(call[0] == "red-01" for call in stub.ship_calls)


# --------------------------------------------------------------------------- #
# CRITICAL ORDERS injection (regression guards)
#
# The {{CRITICAL_ORDERS}} placeholder in ship_commander_user.md was being
# pre-replaced with "" while building the prompt, so the later replace was a
# silent no-op — every mission `ship_behaviors` order and every fleet attack
# directive was dropped before reaching the captain LLM. Separately, the
# ship_behaviors source read `world.mission_brief` (never populated) instead
# of the orchestrator's mirrored `_mission_brief`. These lock both fixes.
# --------------------------------------------------------------------------- #

def _last_user_prompt_for(stub: StubLLMEngine, ship_id: str) -> str:
    """Return the assembled user prompt the orchestrator sent for `ship_id`."""
    for sid, summary in reversed(stub.ship_calls):
        if sid == ship_id:
            hint = summary.get("_prompt_hint", {}) if isinstance(summary, dict) else {}
            return hint.get("user_prompt", "") or ""
    return ""


def test_run_ship_injects_mission_ship_behavior_as_critical_orders():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine()
    orch = _make_orch_with_stub(world, stub)
    # Source is the orchestrator's mirrored mission brief, not world.*
    orch._mission_brief = {"ship_behaviors": {"red-01": "RAM THE NEAREST CONVOY ESCORT"}}

    asyncio.run(orch.run_ship("red-01"))

    prompt = _last_user_prompt_for(stub, "red-01")
    assert "CRITICAL ORDERS" in prompt
    assert "RAM THE NEAREST CONVOY ESCORT" in prompt
    # Placeholder must be fully resolved — no literal token left behind.
    assert "{{CRITICAL_ORDERS}}" not in prompt


def test_run_ship_injects_fleet_attack_directive_as_critical_orders():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine()
    orch = _make_orch_with_stub(world, stub)
    # A fleet note containing an attack keyword aimed at this ship must reach
    # the captain as a critical order.
    orch._last_fleet_intent = {
        "objectives": {},
        "summary": "prosecute the contact",
        "notes": [{"ship_id": "red-01", "text": "ATTACK contact bearing 270 immediately"}],
    }

    asyncio.run(orch.run_ship("red-01"))

    prompt = _last_user_prompt_for(stub, "red-01")
    assert "CRITICAL ORDERS" in prompt
    assert "ATTACK contact bearing 270 immediately" in prompt


def test_run_ship_resolves_critical_orders_placeholder_when_none():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine()
    orch = _make_orch_with_stub(world, stub)
    # No mission orders, no fleet attack notes.

    asyncio.run(orch.run_ship("red-01"))

    prompt = _last_user_prompt_for(stub, "red-01")
    # The placeholder is always resolved (to empty), never left dangling.
    assert "{{CRITICAL_ORDERS}}" not in prompt
    assert "CRITICAL ORDERS - YOU MUST FOLLOW" not in prompt


# --------------------------------------------------------------------------- #
# Fleet-level tool calls
# --------------------------------------------------------------------------- #

def test_run_fleet_returns_set_fleet_intent_tool_call():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine(fleet_responses=[{
        "objectives": {
            "red-01": {"destination": [10000.0, 0.0], "speed_kn": 10.0, "goal": "patrol east"}
        },
        "emcon": {"active_ping_allowed": False, "radio_discipline": "restricted"},
        "summary": "patrol east at 10 kn",
        "notes": [],
    }])
    orch = _make_orch_with_stub(world, stub)

    result = asyncio.run(orch.run_fleet())
    validated = result.get("tool_calls_validated", [])
    # Fleet always emits at least one set_fleet_intent
    set_intent = [tc for tc in validated if tc.get("tool") == "set_fleet_intent"]
    assert len(set_intent) == 1
    args = set_intent[0]["arguments"]
    assert "objectives" in args
    assert "red-01" in args["objectives"]


def test_run_fleet_journal_entry_emits_write_journal_tool_call():
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine(fleet_responses=[{
        "objectives": {
            "red-01": {"destination": [10000.0, 0.0], "speed_kn": 10.0, "goal": "patrol east"}
        },
        "emcon": {"active_ping_allowed": False, "radio_discipline": "restricted"},
        "summary": "patrol east",
        "notes": [],
        "journal_entry": "First contact reported at 0930Z.",
    }])
    orch = _make_orch_with_stub(world, stub)

    result = asyncio.run(orch.run_fleet())
    validated = result.get("tool_calls_validated", [])
    journals = [tc for tc in validated if tc.get("tool") == "write_journal"]
    assert len(journals) == 1
    assert "First contact" in journals[0]["arguments"]["text"]


def test_run_fleet_default_response_still_produces_set_fleet_intent():
    """Even with the stub's default no-op response, the orchestrator emits a
    `set_fleet_intent` tool call (possibly with empty objectives or normalized
    defaults). This locks the invariant: every fleet run produces an intent."""
    world = _make_world_with_red_destroyer()
    stub = StubLLMEngine()  # Falls through to default fleet response
    orch = _make_orch_with_stub(world, stub)

    result = asyncio.run(orch.run_fleet())
    validated = result.get("tool_calls_validated", [])
    assert any(tc.get("tool") == "set_fleet_intent" for tc in validated)
