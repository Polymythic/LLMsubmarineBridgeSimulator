"""Tests for the reactor power-routing model (sub-bridge/backend/sim/power.py).

Locks in two things:
  1. The legacy invariant — at the default 25% split every route formula reduces
     to the historical behavior, so AI ships and existing balance are unchanged.
  2. The new routing effects — deviating from 25% actually changes speed ceiling,
     sonar gain, reload, and the reactor's acoustic signature.
"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from conftest import make_ship
from backend.sim import power
from backend.sim.physics import integrate_kinematics
from backend.sim.damage import step_engineering


def _ship(mw=60.0, max_mw=100.0, helm=.25, sonar=.25, weapons=.25, eng=.25):
    s = make_ship()
    s.reactor.output_mw = mw
    s.reactor.max_mw = max_mw
    s.power.helm, s.power.sonar, s.power.weapons, s.power.engineering = helm, sonar, weapons, eng
    return s


# --- Legacy invariant --------------------------------------------------------

def test_speed_cap_matches_legacy_at_default_split():
    # speed_cap_fraction at helm=25% must equal output_mw/max_mw for any reactor.
    for mw, mx in [(0, 100), (30, 100), (60, 100), (100, 100), (90, 120), (45, 80)]:
        s = _ship(mw=mw, max_mw=mx)
        assert abs(power.speed_cap_fraction(s) - (mw / mx)) < 1e-9


def test_routes_neutral_at_default_split():
    s = _ship(mw=60)  # 15 MW per route — the historical nominal operating point
    assert abs(power.sonar_snr_bonus_db(s)) < 1e-9
    assert abs(power.reload_multiplier(s) - 1.0) < 1e-9
    assert abs(power.maintenance_rate(s) - 0.015) < 1e-9


def test_default_ship_unchanged_by_new_speed_cap():
    # A default-allocation ship integrates to the same speed cap as the old
    # output_mw/max_mw rule (no propulsion starvation at 25% helm).
    s = _ship(mw=100, max_mw=100)
    s.kin.speed = 0.0
    for _ in range(400):  # plenty of time to reach the cap
        integrate_kinematics(s, ordered_speed=99.0, ordered_heading=0.0, ordered_depth=0.0, dt=1.0)
    assert s.kin.speed >= s.hull.max_speed - 0.5  # full speed available at 25% helm


# --- New routing effects -----------------------------------------------------

def test_helm_route_throttles_and_enables_speed():
    assert power.speed_cap_fraction(_ship(mw=100, helm=.10)) < 0.5   # starved -> capped
    assert power.speed_cap_fraction(_ship(mw=100, helm=.40)) >= 0.999  # surged -> full


def test_sonar_route_changes_processing_gain():
    assert power.sonar_snr_bonus_db(_ship(mw=100, sonar=.40)) > 1.0    # boost
    assert power.sonar_snr_bonus_db(_ship(mw=40, sonar=.10)) < -1.0    # going deaf


def test_sonar_route_writes_acoustics_field():
    s = _ship(mw=100, sonar=.40)
    step_engineering(s, dt=1.0)
    assert s.acoustics.passive_snr_power_db > 1.0


def test_weapons_route_scales_reload():
    assert power.reload_multiplier(_ship(mw=100, weapons=.50)) < 1.0   # surge -> faster
    assert power.reload_multiplier(_ship(mw=40, weapons=.10)) > 1.0    # starve -> slower


def test_reactor_signature_couples_to_mw():
    assert power.reactor_noise_points(_ship(mw=20)) == 0.0             # below quiet floor
    assert power.reactor_noise_points(_ship(mw=100)) > power.reactor_noise_points(_ship(mw=60))
    assert power.reactor_noise_points(_ship(mw=100)) <= power.REACTOR_NOISE_MAX + 1e-9
