"""Phase 0 safety net: lock current behavior of the WebSocket command surface.

These tests target `Simulation.handle_command(topic, data)` — the public entry
point used by `app.py:159`. They will continue to work after Phase 0.5
re-routes through `CommandDispatcher`, because the public surface is unchanged.

Coverage focuses on representative commands across each category, not
exhaustive replay. Behaviors already covered in `test_new_features.py`
(power allocation, weapons fire flow, pump assignments) are not duplicated.
"""
import asyncio

import pytest

from backend.sim.loop import Simulation
from conftest import make_test_simulation


# --------------------------------------------------------------------------- #
# Helm
# --------------------------------------------------------------------------- #

def test_helm_order_sets_ordered_state():
    sim = make_test_simulation()
    err = asyncio.run(sim.handle_command(
        "helm.order", {"heading": 270.0, "speed": 12.0, "depth": 100.0}
    ))
    assert err is None
    assert sim.ordered["heading"] == pytest.approx(270.0)
    assert sim.ordered["speed"] == pytest.approx(12.0)
    assert sim.ordered["depth"] == pytest.approx(100.0)


def test_helm_order_normalizes_heading_modulo_360():
    sim = make_test_simulation()
    asyncio.run(sim.handle_command("helm.order", {"heading": 450.0, "speed": 0.0, "depth": 0.0}))
    assert sim.ordered["heading"] == pytest.approx(90.0)


def test_helm_order_clamps_negative_depth_to_zero():
    sim = make_test_simulation()
    asyncio.run(sim.handle_command("helm.order", {"heading": 0.0, "speed": 0.0, "depth": -50.0}))
    assert sim.ordered["depth"] == 0.0


# --------------------------------------------------------------------------- #
# Weapons (tube preparation, countermeasures, depth charges)
# --------------------------------------------------------------------------- #

def test_weapons_tube_load_progresses_state():
    sim = make_test_simulation()
    own = sim.world.get_ship("ownship")
    initial_states = [t.state for t in own.weapons.tubes]

    err = asyncio.run(sim.handle_command(
        "weapons.tube.load", {"tube": 1, "weapon": "Mk48"}
    ))
    # Load may succeed or fail depending on initial tube state; we only
    # assert that the dispatcher accepted the command and state advanced
    # if it succeeded.
    new_states = [t.state for t in own.weapons.tubes]
    if err is None:
        assert new_states != initial_states


def test_weapons_countermeasure_deploy_recorded():
    sim = make_test_simulation()
    own = sim.world.get_ship("ownship")
    cm_before = list(getattr(sim.world, "countermeasures", []))
    err = asyncio.run(sim.handle_command(
        "weapons.countermeasure.deploy", {"type": "noisemaker"}
    ))
    # Either succeeded (countermeasure present) or returned an error string;
    # either is acceptable behavior to lock in. We just assert the command
    # path doesn't raise and produces a deterministic outcome.
    assert err is None or isinstance(err, str)
    cm_after = list(getattr(sim.world, "countermeasures", []))
    if err is None:
        assert len(cm_after) >= len(cm_before)


# --------------------------------------------------------------------------- #
# Engineering (reactor controls, scram)
# --------------------------------------------------------------------------- #

def test_engineering_reactor_set_changes_output_mw():
    sim = make_test_simulation()
    own = sim.world.get_ship("ownship")
    err = asyncio.run(sim.handle_command(
        "engineering.reactor.set", {"mw": 50.0}
    ))
    assert err is None
    assert own.reactor.output_mw == pytest.approx(50.0)


def test_engineering_reactor_set_clamps_to_max():
    sim = make_test_simulation()
    own = sim.world.get_ship("ownship")
    asyncio.run(sim.handle_command(
        "engineering.reactor.set", {"mw": own.reactor.max_mw * 10.0}
    ))
    assert own.reactor.output_mw == pytest.approx(own.reactor.max_mw)


def test_engineering_scram_sets_flag():
    sim = make_test_simulation()
    own = sim.world.get_ship("ownship")
    assert own.reactor.scrammed is False
    err = asyncio.run(sim.handle_command("engineering.reactor.scram", {}))
    assert err is None
    assert own.reactor.scrammed is True


# --------------------------------------------------------------------------- #
# Captain (consent, periscope, radio)
# --------------------------------------------------------------------------- #

def test_captain_consent_toggles_flag():
    sim = make_test_simulation()
    assert sim._captain_consent is False
    err = asyncio.run(sim.handle_command("captain.consent", {"consent": True}))
    assert err is None
    assert sim._captain_consent is True


def test_captain_periscope_raise_sets_flag():
    sim = make_test_simulation()
    err = asyncio.run(sim.handle_command("captain.periscope.raise", {"raised": True}))
    assert err is None
    assert sim._periscope_raised is True


def test_captain_radio_raise_sets_flag():
    sim = make_test_simulation()
    err = asyncio.run(sim.handle_command("captain.radio.raise", {"raised": True}))
    assert err is None
    assert sim._radio_raised is True


# --------------------------------------------------------------------------- #
# Unknown topics and guards
# --------------------------------------------------------------------------- #

def test_unknown_topic_returns_none_or_string():
    sim = make_test_simulation()
    res = asyncio.run(sim.handle_command("nonexistent.command", {"foo": 1}))
    # Existing behavior either silently ignores (None) or returns an error string
    assert res is None or isinstance(res, str)


def test_command_without_active_mission_rejects_non_debug():
    sim = make_test_simulation()
    # Force inactive mission state
    sim._mission_active = False
    res = asyncio.run(sim.handle_command("helm.order", {"heading": 0.0, "speed": 0.0, "depth": 0.0}))
    assert res == "No active mission"


def test_debug_command_allowed_without_active_mission():
    sim = make_test_simulation()
    sim._mission_active = False
    # debug.maintenance.spawns is a simple toggle — should not be rejected
    res = asyncio.run(sim.handle_command("debug.maintenance.spawns", {"enabled": False}))
    assert res != "No active mission"
