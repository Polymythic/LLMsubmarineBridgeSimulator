"""Tests for the tactical compute layer.

Pure-function tests. No Simulation, no orchestrator, no LLM, no asyncio.
"""
import math

import pytest

from backend.models import ShipCapabilities
from backend.sim.tactical import (
    ACTIONABLE_CONFIDENCE,
    ContactBelief,
    DoctrineRecommendation,
    EnvelopeReport,
    InterceptSolution,
    bearing_to,
    doctrine_for,
    intercept_solution,
    range_to,
    weapon_envelope,
)

from conftest import make_ship


# --------------------------------------------------------------------------- #
# bearing_to
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("to_pos,expected", [
    ((0.0, 1000.0), 0.0),    # north
    ((1000.0, 0.0), 90.0),   # east
    ((0.0, -1000.0), 180.0), # south
    ((-1000.0, 0.0), 270.0), # west
    ((1000.0, 1000.0), 45.0),
    ((-1000.0, 1000.0), 315.0),
])
def test_bearing_compass_convention(to_pos, expected):
    assert bearing_to((0.0, 0.0), to_pos) == pytest.approx(expected, abs=0.5)


def test_bearing_normalized_to_0_360():
    # All bearings in [0, 360)
    for to in [(1, 1), (-1, 1), (-1, -1), (1, -1)]:
        b = bearing_to((0, 0), to)
        assert 0.0 <= b < 360.0


def test_bearing_coincident_points_returns_zero():
    assert bearing_to((100.0, 200.0), (100.0, 200.0)) == 0.0


# --------------------------------------------------------------------------- #
# range_to
# --------------------------------------------------------------------------- #

def test_range_basic():
    assert range_to((0, 0), (3, 4)) == pytest.approx(5.0)


def test_range_symmetric():
    a, b = (123.0, -456.0), (789.0, 100.0)
    assert range_to(a, b) == pytest.approx(range_to(b, a))


# --------------------------------------------------------------------------- #
# intercept_solution
# --------------------------------------------------------------------------- #

def test_intercept_static_target_heading_is_direct_bearing():
    sol = intercept_solution(
        hunter_pos=(0.0, 0.0),
        hunter_speed_kn=20.0,
        target_pos=(1000.0, 0.0),
        target_heading_deg=0.0,
        target_speed_kn=0.0,
    )
    assert sol.feasible
    assert sol.heading == pytest.approx(90.0, abs=0.5)
    assert sol.time_s > 0.0


def test_intercept_faster_hunter_solves_lead():
    """Hunter at (0,0) chasing target at (5000,0) moving north at 10kn.
    Hunter at 30kn should be able to lead the target."""
    sol = intercept_solution(
        hunter_pos=(0.0, 0.0),
        hunter_speed_kn=30.0,
        target_pos=(5000.0, 0.0),
        target_heading_deg=0.0,  # north
        target_speed_kn=10.0,
    )
    assert sol.feasible
    # Intercept point should be north-east of (5000, 0): heading > 90 (north of east)
    assert 0.0 < sol.heading < 90.0
    # Verify the intercept geometry: at time t, both reach the same point.
    hx = sol.intercept_pos[0]
    hy = sol.intercept_pos[1]
    assert hx == pytest.approx(5000.0, abs=1e-3)  # target moves due north, x stays at 5000
    assert hy > 0.0


def test_intercept_slower_hunter_falls_back_to_direct_bearing():
    sol = intercept_solution(
        hunter_pos=(0.0, 0.0),
        hunter_speed_kn=5.0,
        target_pos=(0.0, 1000.0),
        target_heading_deg=0.0,
        target_speed_kn=20.0,  # target much faster, fleeing north
    )
    assert not sol.feasible
    # Direct bearing to target's current position (north → 0°)
    assert sol.heading == pytest.approx(0.0, abs=0.5)


def test_intercept_zero_speed_hunter_returns_infeasible():
    sol = intercept_solution((0, 0), 0.0, (1000, 0), 0.0, 0.0)
    assert not sol.feasible


# --------------------------------------------------------------------------- #
# weapon_envelope
# --------------------------------------------------------------------------- #

def _ship_with_caps(**caps_kwargs):
    ship = make_ship(id_="test")
    ship.capabilities = ShipCapabilities(**caps_kwargs)
    return ship


def test_torpedo_envelope_in_range():
    ship = _ship_with_caps(has_torpedoes=True)
    ship.kin.x, ship.kin.y = 0.0, 0.0
    rep = weapon_envelope(ship, (3000.0, 0.0), "fire_torpedo")
    assert rep.in_range
    assert rep.range_m == pytest.approx(3000.0)


def test_torpedo_envelope_too_short():
    ship = _ship_with_caps(has_torpedoes=True)
    rep = weapon_envelope(ship, (200.0, 0.0), "fire_torpedo")
    assert not rep.in_range
    assert "too short" in rep.reason


def test_torpedo_envelope_too_long():
    ship = _ship_with_caps(has_torpedoes=True)
    rep = weapon_envelope(ship, (10000.0, 0.0), "fire_torpedo")
    assert not rep.in_range
    assert "too long" in rep.reason


def test_depth_charge_envelope_in_range():
    ship = _ship_with_caps(has_depth_charges=True)
    rep = weapon_envelope(ship, (500.0, 500.0), "drop_depth_charges")
    assert rep.in_range


def test_depth_charge_envelope_out_of_range():
    ship = _ship_with_caps(has_depth_charges=True)
    rep = weapon_envelope(ship, (3000.0, 0.0), "drop_depth_charges")
    assert not rep.in_range
    assert "close" in rep.reason


def test_unknown_weapon_kind_returns_failure():
    ship = _ship_with_caps()
    rep = weapon_envelope(ship, (100.0, 100.0), "lasers")
    assert not rep.in_range
    assert "unknown" in rep.reason.lower()


# --------------------------------------------------------------------------- #
# doctrine_for
# --------------------------------------------------------------------------- #

def _destroyer():
    ship = make_ship(id_="red-01")
    ship.capabilities = ShipCapabilities(
        has_torpedoes=True, has_depth_charges=True, has_active_sonar=True,
    )
    ship.kin.x, ship.kin.y = 0.0, 0.0
    return ship


def test_doctrine_no_contacts_no_fleet_returns_hold():
    rec = doctrine_for(_destroyer(), contacts=[], fleet_destination=None)
    assert rec.action == "HOLD"


def test_doctrine_no_contacts_with_fleet_returns_transit():
    rec = doctrine_for(_destroyer(), contacts=[], fleet_destination=(10000.0, 0.0), fleet_speed_kn=15.0)
    assert rec.action == "TRANSIT"
    assert rec.suggested_heading == pytest.approx(90.0, abs=0.5)
    assert rec.suggested_speed_kn == pytest.approx(15.0)


def test_doctrine_low_confidence_contact_does_not_engage():
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.3, estimated_pos=(2000.0, 0.0))]
    rec = doctrine_for(_destroyer(), contacts, fleet_destination=(10000.0, 0.0))
    assert rec.action == "TRANSIT"  # confidence below ACTIONABLE_CONFIDENCE


def test_doctrine_high_confidence_contact_in_torpedo_envelope_engages():
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.9, estimated_pos=(3000.0, 0.0))]
    rec = doctrine_for(_destroyer(), contacts)
    assert rec.action == "ENGAGE_TORPEDO"
    assert rec.target_id == "c1"
    assert rec.suggested_heading == pytest.approx(90.0)


def test_doctrine_high_confidence_contact_in_dc_envelope_engages_dc():
    """Contact too close for torpedoes (< min) but inside DC envelope."""
    contacts = [ContactBelief(id="c1", bearing_deg=0.0, confidence=0.85, estimated_pos=(0.0, 500.0))]
    rec = doctrine_for(_destroyer(), contacts)
    assert rec.action == "ENGAGE_DC"
    assert rec.target_id == "c1"


def test_doctrine_high_confidence_contact_too_far_says_close():
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.9, estimated_pos=(20000.0, 0.0))]
    rec = doctrine_for(_destroyer(), contacts)
    assert rec.action == "CLOSE"
    assert rec.target_id == "c1"
    assert rec.suggested_heading == pytest.approx(90.0, abs=0.5)


def test_doctrine_picks_highest_confidence_contact():
    contacts = [
        ContactBelief(id="weak", bearing_deg=0.0, confidence=0.75, estimated_pos=(3000.0, 0.0)),
        ContactBelief(id="strong", bearing_deg=180.0, confidence=0.95, estimated_pos=(0.0, -3000.0)),
    ]
    rec = doctrine_for(_destroyer(), contacts)
    assert rec.target_id == "strong"


def test_doctrine_skips_contacts_with_no_position():
    contacts = [
        ContactBelief(id="bearing-only", bearing_deg=90.0, confidence=0.99, estimated_pos=None),
        ContactBelief(id="positioned", bearing_deg=270.0, confidence=0.8, estimated_pos=(-3000.0, 0.0)),
    ]
    rec = doctrine_for(_destroyer(), contacts)
    # Bearing-only contact can't be engaged — pick the positioned one.
    assert rec.target_id == "positioned"


def test_doctrine_ship_without_torpedoes_falls_back_to_dc():
    ship = make_ship(id_="dc-only")
    ship.capabilities = ShipCapabilities(has_torpedoes=False, has_depth_charges=True)
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.9, estimated_pos=(500.0, 0.0))]
    rec = doctrine_for(ship, contacts)
    assert rec.action == "ENGAGE_DC"


def test_doctrine_ship_with_no_weapons_close_or_transit_only():
    ship = make_ship(id_="unarmed")
    ship.capabilities = ShipCapabilities(has_torpedoes=False, has_depth_charges=False)
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.9, estimated_pos=(3000.0, 0.0))]
    rec = doctrine_for(ship, contacts)
    # Unarmed ship can't engage; should fall through to CLOSE
    assert rec.action == "CLOSE"


# --------------------------------------------------------------------------- #
# Sanity: dataclass shape (used by other modules / prompts)
# --------------------------------------------------------------------------- #

def test_dataclasses_are_immutable():
    rep = EnvelopeReport(True, 100.0, 0.0, 1000.0, "ok")
    with pytest.raises(Exception):
        rep.in_range = False  # frozen


def test_constants_are_reasonable():
    # Sanity check that ACTIONABLE_CONFIDENCE is in the trigger zone the
    # orchestrator uses elsewhere; this catches accidental drift.
    assert 0.5 <= ACTIONABLE_CONFIDENCE <= 0.9
