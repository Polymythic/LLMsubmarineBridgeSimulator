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


def test_doctrine_below_investigate_confidence_transits():
    """Confidence below INVESTIGATE_CONFIDENCE (0.3) is too faint to act on."""
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.15, estimated_pos=(2000.0, 0.0))]
    rec = doctrine_for(_destroyer(), contacts, fleet_destination=(10000.0, 0.0))
    assert rec.action == "TRANSIT"


def test_doctrine_moderate_confidence_contact_investigates():
    """A contact between INVESTIGATE_CONFIDENCE and ACTIONABLE_CONFIDENCE
    triggers an INVESTIGATE recommendation: vector toward bearing without
    engaging or lighting up active sonar."""
    contacts = [ContactBelief(id="c1", bearing_deg=120.0, confidence=0.5, estimated_pos=(3000.0, 0.0))]
    rec = doctrine_for(_destroyer(), contacts, fleet_destination=(10000.0, 0.0))
    assert rec.action == "INVESTIGATE"
    assert rec.target_id == "c1"
    assert rec.suggested_heading == pytest.approx(120.0)
    # Moderate speed (~70% of max), not flank
    assert rec.suggested_speed_kn is not None
    assert rec.suggested_speed_kn < _destroyer().hull.max_speed


def test_doctrine_high_confidence_contact_in_torpedo_envelope_engages():
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.9, estimated_pos=(3000.0, 0.0))]
    rec = doctrine_for(_destroyer(), contacts)
    assert rec.action == "ENGAGE_TORPEDO"
    assert rec.target_id == "c1"
    assert rec.suggested_heading == pytest.approx(90.0)


def test_doctrine_allows_first_two_torpedoes_in_salvo():
    """Within the salvo cap (SALVO_MAX=3), repeated engagements are allowed —
    a 2-shot salvo is normal doctrine."""
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.9, estimated_pos=(3000.0, 0.0))]
    for n in (0, 1, 2):
        rec = doctrine_for(_destroyer(), contacts, recent_torp_fires=n)
        assert rec.action == "ENGAGE_TORPEDO", f"fire #{n+1} should still be allowed"


def test_doctrine_suppresses_torpedo_after_salvo_cap():
    """At SALVO_MAX fires, doctrine demotes to CLOSE so the captain assesses
    before continuing — prevents straight-line magazine dumps."""
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.9, estimated_pos=(3000.0, 0.0))]
    rec = doctrine_for(_destroyer(), contacts, recent_torp_fires=3)
    assert rec.action == "CLOSE"
    assert rec.target_id == "c1"
    assert "salvo cap" in rec.reason.lower()


def test_doctrine_dc_still_allowed_when_salvo_exhausted():
    """The salvo cap only gates ENGAGE_TORPEDO. A DC-eligible contact still
    gets ENGAGE_DC because depth charges are an independent weapon system."""
    # Contact too close for torpedoes (< 800m min) but inside DC envelope.
    contacts = [ContactBelief(id="c1", bearing_deg=0.0, confidence=0.9, estimated_pos=(0.0, 500.0))]
    rec = doctrine_for(_destroyer(), contacts, recent_torp_fires=5)
    assert rec.action == "ENGAGE_DC"
    assert rec.target_id == "c1"


def test_doctrine_resumes_engagement_after_salvo_window_clears():
    """Once recent_torp_fires returns to 0 (window elapsed), ENGAGE_TORPEDO
    is re-enabled."""
    contacts = [ContactBelief(id="c1", bearing_deg=90.0, confidence=0.9, estimated_pos=(3000.0, 0.0))]
    rec = doctrine_for(_destroyer(), contacts, recent_torp_fires=0)
    assert rec.action == "ENGAGE_TORPEDO"


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


# --------------------------------------------------------------------------- #
# scan_threats — torpedoes in water, friendly hits, self hits
# --------------------------------------------------------------------------- #

class _StubWorld:
    """Minimal world stub for testing scan_threats without a Simulation."""

    def __init__(self, ships, torpedoes=None):
        self._ships = {s.id: s for s in ships}
        self.torpedoes = list(torpedoes or [])

    def get_ship(self, sid):
        return self._ships.get(sid)


def test_scan_threats_detects_hostile_torpedo_in_range():
    from backend.sim.tactical import scan_threats, ThreatAlert
    me = make_ship(id_="red-01", side="RED", x=0.0, y=0.0)
    world = _StubWorld([me], torpedoes=[
        {"id": "torp-1", "side": "BLUE", "x": 1000.0, "y": 0.0, "depth": 100, "armed": True},
    ])
    threats = scan_threats(me, world, recent_combat_events=[])
    assert len(threats) == 1
    t = threats[0]
    assert t.kind == "torpedo_in_water"
    assert t.severity == "critical"
    assert t.bearing_deg == pytest.approx(90.0, abs=0.5)
    assert t.range_m == pytest.approx(1000.0, abs=1.0)


def test_scan_threats_ignores_own_torpedoes():
    from backend.sim.tactical import scan_threats
    me = make_ship(id_="red-01", side="RED")
    world = _StubWorld([me], torpedoes=[
        {"id": "torp-friendly", "side": "RED", "x": 500.0, "y": 0.0},
    ])
    assert scan_threats(me, world, []) == []


def test_scan_threats_ignores_torpedoes_beyond_passive_range():
    from backend.sim.tactical import scan_threats, TORPEDO_PASSIVE_DETECTION_M
    me = make_ship(id_="red-01", side="RED")
    far = TORPEDO_PASSIVE_DETECTION_M + 500.0
    world = _StubWorld([me], torpedoes=[
        {"id": "t1", "side": "BLUE", "x": far, "y": 0.0},
    ])
    assert scan_threats(me, world, []) == []


def test_scan_threats_self_hit():
    from backend.sim.tactical import scan_threats
    me = make_ship(id_="red-01", side="RED")
    world = _StubWorld([me])
    events = [{"kind": "torpedo.detonated", "target": "red-01"}]
    threats = scan_threats(me, world, events)
    assert len(threats) == 1
    assert threats[0].kind == "self_hit"
    assert threats[0].severity == "critical"


def test_scan_threats_friendly_hit():
    from backend.sim.tactical import scan_threats
    me = make_ship(id_="red-a-dd-01", side="RED", x=0.0, y=0.0)
    peer = make_ship(id_="red-a-cv-01", side="RED", x=2000.0, y=0.0)
    world = _StubWorld([me, peer])
    events = [{"kind": "torpedo.detonated", "target": "red-a-cv-01"}]
    threats = scan_threats(me, world, events)
    assert len(threats) == 1
    t = threats[0]
    assert t.kind == "friendly_hit"
    assert t.severity == "warning"
    assert t.source_id == "red-a-cv-01"
    # Bearing TO the hit friendly (east of me)
    assert t.bearing_deg == pytest.approx(90.0, abs=0.5)


def test_scan_threats_ignores_enemy_hits():
    """A hit on a non-friendly is not OUR threat (nice problem to have)."""
    from backend.sim.tactical import scan_threats
    me = make_ship(id_="red-01", side="RED")
    enemy = make_ship(id_="ownship", side="BLUE")
    world = _StubWorld([me, enemy])
    events = [{"kind": "torpedo.detonated", "target": "ownship"}]
    assert scan_threats(me, world, events) == []


def test_scan_threats_severity_ordering():
    """Critical threats sort before warning."""
    from backend.sim.tactical import scan_threats
    me = make_ship(id_="red-a-dd-01", side="RED", x=0.0, y=0.0)
    peer = make_ship(id_="red-a-cv-01", side="RED", x=2000.0, y=0.0)
    world = _StubWorld([me, peer], torpedoes=[
        {"id": "torp-1", "side": "BLUE", "x": 500.0, "y": 0.0},
    ])
    events = [{"kind": "depth_charge.detonated", "target": "red-a-cv-01"}]
    threats = scan_threats(me, world, events)
    assert len(threats) == 2
    # Critical (torpedo) first; warning (friendly_hit) second
    assert threats[0].severity == "critical"
    assert threats[1].severity == "warning"


# --------------------------------------------------------------------------- #
# doctrine_for with threats
# --------------------------------------------------------------------------- #

def test_doctrine_torpedo_in_water_engages_back_bearing_when_armed():
    """A torpedo-in-water threat with a destroyer that has torpedoes should
    counter-fire on the bearing."""
    from backend.sim.tactical import ThreatAlert
    threats = [ThreatAlert(kind="torpedo_in_water", severity="critical", bearing_deg=270.0, range_m=2000)]
    rec = doctrine_for(_destroyer(), contacts=[], threats=threats)
    assert rec.action == "ENGAGE_TORPEDO"
    assert rec.suggested_heading == pytest.approx(270.0)


def test_doctrine_torpedo_in_water_evades_when_unarmed():
    from backend.sim.tactical import ThreatAlert
    ship = make_ship(id_="cv-01")
    ship.capabilities = ShipCapabilities(has_torpedoes=False, has_depth_charges=False)
    threats = [ThreatAlert(kind="torpedo_in_water", severity="critical", bearing_deg=180.0, range_m=1500)]
    rec = doctrine_for(ship, contacts=[], threats=threats)
    assert rec.action == "EVADE"
    # Perpendicular to the incoming bearing (90° offset)
    assert rec.suggested_heading == pytest.approx(270.0, abs=0.5)
    assert rec.suggested_speed_kn == pytest.approx(ship.hull.max_speed)


def test_doctrine_self_hit_engages_best_contact():
    from backend.sim.tactical import ThreatAlert
    contacts = [ContactBelief(id="attacker", bearing_deg=315.0, confidence=0.6, estimated_pos=(-2000.0, 2000.0))]
    threats = [ThreatAlert(kind="self_hit", severity="critical", source_id="red-01")]
    rec = doctrine_for(_destroyer(), contacts=contacts, threats=threats)
    assert rec.action == "ENGAGE_TORPEDO"  # destroyer has torpedoes
    assert rec.target_id == "attacker"


def test_doctrine_self_hit_with_no_contact_evades():
    from backend.sim.tactical import ThreatAlert
    threats = [ThreatAlert(kind="self_hit", severity="critical", source_id="red-01")]
    rec = doctrine_for(_destroyer(), contacts=[], threats=threats)
    assert rec.action == "EVADE"


def test_doctrine_friendly_hit_closes_on_bearing():
    from backend.sim.tactical import ThreatAlert
    threats = [ThreatAlert(
        kind="friendly_hit", severity="warning",
        bearing_deg=90.0, range_m=2000.0, source_id="red-a-cv-01",
    )]
    rec = doctrine_for(_destroyer(), contacts=[], threats=threats)
    assert rec.action == "CLOSE"
    assert rec.suggested_heading == pytest.approx(90.0)
    assert rec.target_id == "red-a-cv-01"


def test_doctrine_threat_overrides_transit():
    """Threat override beats normal TRANSIT logic even if a fleet destination is set."""
    from backend.sim.tactical import ThreatAlert
    threats = [ThreatAlert(kind="torpedo_in_water", severity="critical", bearing_deg=180.0, range_m=1000)]
    rec = doctrine_for(
        _destroyer(),
        contacts=[],
        fleet_destination=(10000.0, 0.0),  # would normally be TRANSIT
        threats=threats,
    )
    assert rec.action != "TRANSIT"
