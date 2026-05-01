"""Phase 0 safety net: integration tests for the full tick path.

These exercise the seam that Phase 1 and Phase 2 will refactor:
LLM tool call → orchestrator → inline dispatch in `loop.py:975-1153` → ship
state mutation. Tests construct a real `Simulation` with the AI orchestrator
enabled, inject a `StubLLMEngine`, run one or more ticks, and assert that
world state changed as expected.
"""
import asyncio

import pytest

from conftest import StubLLMEngine, install_stub_engines, make_test_simulation, make_ship


@pytest.fixture
def ai_orchestrator_enabled(monkeypatch):
    """Enable USE_AI_ORCHESTRATOR for a single test, restore after.

    `CONFIG` is a frozen dataclass instance, so we can't monkeypatch its
    attributes directly. Instead we set the env var, then call
    `reload_from_env()` to rebuild CONFIG. After the test, we restore env
    (via monkeypatch) and reload again to revert CONFIG.
    """
    monkeypatch.setenv("USE_AI_ORCHESTRATOR", "1")
    # Tighten cadences so the AI fires on the first tick we run
    monkeypatch.setenv("AI_FLEET_CADENCE_S", "0.1")
    monkeypatch.setenv("AI_SHIP_CADENCE_S", "0.1")

    from backend.config import reload_from_env
    reload_from_env()
    yield
    reload_from_env()


async def _run_until_ai_quiescent(sim, ticks: int = 1, dt: float = 1.0) -> None:
    """Tick the simulation `ticks` times and drain the background AI tasks."""
    for _ in range(ticks):
        await sim.tick(dt)
    # AI runs are scheduled as background asyncio.Tasks; wait for all to finish
    if getattr(sim, "_ai_pending", None):
        pending = list(sim._ai_pending)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


# --------------------------------------------------------------------------- #
# Full LLM → world-state mutation path
# --------------------------------------------------------------------------- #

def test_ai_set_nav_changes_destroyer_heading(ai_orchestrator_enabled):
    sim = make_test_simulation()
    red = make_ship(id_="red-01", side="RED", ship_class="Destroyer", x=3000.0, y=0.0, heading=0.0)
    sim.world.add_ship(red)

    stub = StubLLMEngine(ship_responses={
        "red-01": [{
            "tool": "set_nav",
            "arguments": {"heading": 90.0, "speed": 10.0, "depth": 0.0},
            "summary": "head east",
        }]
    })
    install_stub_engines(sim._ai_orch, stub)

    asyncio.run(_run_until_ai_quiescent(sim, ticks=1))

    red_after = sim.world.get_ship("red-01")
    assert red_after.kin.heading == pytest.approx(90.0, abs=0.5)
    assert red_after.kin.speed == pytest.approx(10.0, abs=0.5)


def test_ai_set_nav_clamps_speed_to_hull_max(ai_orchestrator_enabled):
    sim = make_test_simulation()
    red = make_ship(id_="red-01", side="RED", ship_class="Destroyer", x=3000.0, y=0.0)
    sim.world.add_ship(red)

    stub = StubLLMEngine(ship_responses={
        "red-01": [{
            "tool": "set_nav",
            "arguments": {"heading": 0.0, "speed": red.hull.max_speed * 10.0, "depth": 0.0},
            "summary": "flank speed",
        }]
    })
    install_stub_engines(sim._ai_orch, stub)

    asyncio.run(_run_until_ai_quiescent(sim, ticks=1))

    red_after = sim.world.get_ship("red-01")
    assert red_after.kin.speed <= red.hull.max_speed + 1e-6


def test_tick_runs_without_red_ships(ai_orchestrator_enabled):
    """No RED ships: tick should still succeed and not invoke ship AI."""
    sim = make_test_simulation()

    stub = StubLLMEngine()
    install_stub_engines(sim._ai_orch, stub)

    asyncio.run(_run_until_ai_quiescent(sim, ticks=2))

    # Ownship still present and unchanged
    own = sim.world.get_ship("ownship")
    assert own is not None
    # Ship-level AI was never called (no RED ships exist)
    assert all(call[0] != "ownship" for call in stub.ship_calls)


# --------------------------------------------------------------------------- #
# Smoke: many ticks with stub engine, no exceptions
# --------------------------------------------------------------------------- #

def test_destroyer_can_be_driven_by_scripted_controller(ai_orchestrator_enabled):
    """Litmus test 1, structural: a single destroyer is driven by a
    `ScriptedShipController` instead of the LLM, with no other code changes.

    This is the contract that lets us swap in a human-driven console later.
    The orchestrator is still installed (for fleet-level prompts) but the
    per-ship controller is replaced.
    """
    from backend.sim.control import ScriptedShipController, SetNavAction

    sim = make_test_simulation()
    red = make_ship(id_="red-01", side="RED", ship_class="Destroyer", x=3000.0, y=0.0, heading=0.0)
    sim.world.add_ship(red)

    # Replace the per-ship controller. No other plumbing needed.
    sim._ship_controller = ScriptedShipController({
        "red-01": [[SetNavAction(heading=180.0, speed=14.0, depth=0.0)]],
    })

    asyncio.run(_run_until_ai_quiescent(sim, ticks=1))

    red_after = sim.world.get_ship("red-01")
    assert red_after.kin.heading == pytest.approx(180.0, abs=0.5)
    assert red_after.kin.speed == pytest.approx(14.0, abs=0.5)


def test_extended_tick_smoke_with_stub(ai_orchestrator_enabled):
    """Run many ticks with the stub returning hold-course; no exceptions."""
    sim = make_test_simulation()
    red = make_ship(id_="red-01", side="RED", ship_class="Destroyer", x=3000.0, y=0.0)
    sim.world.add_ship(red)

    stub = StubLLMEngine()  # default no-op responses
    install_stub_engines(sim._ai_orch, stub)

    asyncio.run(_run_until_ai_quiescent(sim, ticks=10, dt=0.5))

    # Sanity: still have both ships, no NaNs
    own = sim.world.get_ship("ownship")
    red_after = sim.world.get_ship("red-01")
    assert own is not None and red_after is not None
    for ship in (own, red_after):
        for v in (ship.kin.x, ship.kin.y, ship.kin.heading, ship.kin.speed, ship.kin.depth):
            assert v == v  # NaN check
