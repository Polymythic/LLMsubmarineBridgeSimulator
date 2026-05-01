"""Direct unit tests for `ShipControls`.

These tests don't construct a Simulation. They verify that the action surface
correctly mutates ship + world state and gates against ship capabilities.
"""
import pytest

from backend.models import ShipCapabilities
from backend.sim.control import ControlResult, ShipControls
from backend.sim.ecs import World

from conftest import make_ship


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _destroyer_caps() -> ShipCapabilities:
    return ShipCapabilities(
        can_set_nav=True,
        has_active_sonar=True,
        has_torpedoes=True,
        has_depth_charges=True,
        countermeasures=[],
    )


def _convoy_caps() -> ShipCapabilities:
    return ShipCapabilities(
        can_set_nav=True,
        has_active_sonar=False,
        has_torpedoes=False,
        has_depth_charges=False,
        countermeasures=[],
    )


def _make_controls(side: str = "RED", caps: ShipCapabilities = None) -> ShipControls:
    ship = make_ship(id_="red-01", side=side, ship_class="Destroyer", x=3000.0, y=0.0)
    if caps is not None:
        ship.capabilities = caps
    world = World()
    world.add_ship(ship)
    return ShipControls(ship, world)


# --------------------------------------------------------------------------- #
# set_nav
# --------------------------------------------------------------------------- #

def test_set_nav_updates_kinematics():
    ctl = _make_controls(caps=_destroyer_caps())
    r = ctl.set_nav(heading=90.0, speed=12.0, depth=0.0)
    assert r.ok
    assert ctl.ship.kin.heading == pytest.approx(90.0)
    assert ctl.ship.kin.speed == pytest.approx(12.0)
    assert ctl.ship.kin.depth == pytest.approx(0.0)


def test_set_nav_normalizes_heading_modulo_360():
    ctl = _make_controls(caps=_destroyer_caps())
    ctl.set_nav(heading=450.0)
    assert ctl.ship.kin.heading == pytest.approx(90.0)


def test_set_nav_clamps_speed_to_hull_max():
    ctl = _make_controls(caps=_destroyer_caps())
    ctl.set_nav(speed=ctl.ship.hull.max_speed * 100.0)
    assert ctl.ship.kin.speed == pytest.approx(ctl.ship.hull.max_speed)


def test_set_nav_clamps_depth_to_hull_max():
    ctl = _make_controls(caps=_destroyer_caps())
    ctl.set_nav(depth=ctl.ship.hull.max_depth + 1000.0)
    assert ctl.ship.kin.depth == pytest.approx(ctl.ship.hull.max_depth)


def test_set_nav_none_args_leave_axes_unchanged():
    ctl = _make_controls(caps=_destroyer_caps())
    ctl.set_nav(heading=180.0, speed=5.0, depth=10.0)
    ctl.set_nav(heading=None, speed=None, depth=None)
    assert ctl.ship.kin.heading == pytest.approx(180.0)
    assert ctl.ship.kin.speed == pytest.approx(5.0)
    assert ctl.ship.kin.depth == pytest.approx(10.0)


def test_set_nav_unsupported_when_capability_missing():
    caps = ShipCapabilities(can_set_nav=False)
    ctl = _make_controls(caps=caps)
    r = ctl.set_nav(heading=90.0)
    assert not r.ok
    assert "set_nav" in r.error


# --------------------------------------------------------------------------- #
# fire_torpedo
# --------------------------------------------------------------------------- #

def test_fire_torpedo_appends_torpedo_to_world():
    ctl = _make_controls(caps=_destroyer_caps())
    assert ctl._world.torpedoes == []
    r = ctl.fire_torpedo(bearing=270.0, run_depth=100.0, enable_range=1000.0)
    assert r.ok
    assert len(ctl._world.torpedoes) == 1


def test_fire_torpedo_rejected_without_capability():
    ctl = _make_controls(caps=_convoy_caps())
    r = ctl.fire_torpedo(bearing=0.0)
    assert not r.ok
    assert "torpedo" in r.error.lower()
    assert ctl._world.torpedoes == []


# --------------------------------------------------------------------------- #
# drop_depth_charges
# --------------------------------------------------------------------------- #

def test_drop_depth_charges_appends_to_world():
    ctl = _make_controls(caps=_destroyer_caps())
    ctl.ship.weapons.depth_charges_stored = 30
    r = ctl.drop_depth_charges(spread_meters=20, min_depth=30, max_depth=60, spread_size=4)
    assert r.ok, r.error
    assert len(ctl._world.depth_charges) == 4


def test_drop_depth_charges_rejected_without_capability():
    ctl = _make_controls(caps=_convoy_caps())
    r = ctl.drop_depth_charges()
    assert not r.ok
    assert ctl._world.depth_charges == []


# --------------------------------------------------------------------------- #
# deploy_countermeasure
# --------------------------------------------------------------------------- #

def test_deploy_countermeasure_appends_to_world_when_supported():
    caps = ShipCapabilities(countermeasures=["noisemaker"])
    ctl = _make_controls(caps=caps)
    r = ctl.deploy_countermeasure("noisemaker")
    assert r.ok, r.error
    assert len(ctl._world.countermeasures) == 1


def test_deploy_countermeasure_rejected_when_type_unsupported():
    caps = ShipCapabilities(countermeasures=["noisemaker"])
    ctl = _make_controls(caps=caps)
    r = ctl.deploy_countermeasure("decoy")
    assert not r.ok


# --------------------------------------------------------------------------- #
# active_ping
# --------------------------------------------------------------------------- #

def test_active_ping_returns_responses_and_sets_cooldown():
    caps = _destroyer_caps()
    ctl = _make_controls(caps=caps)
    # Add a target so the ping has something to detect
    target = make_ship(id_="ownship", side="BLUE", x=500.0, y=0.0, depth=100.0)
    ctl._world.add_ship(target)

    r = ctl.active_ping()
    assert r.ok
    assert ctl.ship.active_sonar_cooldown > 0.0


def test_active_ping_rejected_on_cooldown():
    ctl = _make_controls(caps=_destroyer_caps())
    ctl.ship.active_sonar_cooldown = 5.0
    r = ctl.active_ping()
    assert not r.ok
    assert "cooldown" in r.error


def test_active_ping_rejected_without_capability():
    ctl = _make_controls(caps=_convoy_caps())
    r = ctl.active_ping()
    assert not r.ok


# --------------------------------------------------------------------------- #
# ControlResult helpers
# --------------------------------------------------------------------------- #

def test_control_result_helpers():
    ok = ControlResult.success("payload")
    assert ok.ok is True
    assert ok.error is None
    assert ok.data == "payload"

    failed = ControlResult.fail("nope")
    assert failed.ok is False
    assert failed.error == "nope"
    assert failed.data is None
