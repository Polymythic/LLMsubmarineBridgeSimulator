"""Tests for sub-bridge/backend/sim/noise.py"""
import os, sys, math
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from conftest import make_ship, make_world
from backend.sim.noise import NoiseEngine, _sum_db


class _FakeLoopState:
    """Minimal stand-in for loop state attributes the noise engine reads."""
    def __init__(self):
        self._periscope_raised = False
        self._radio_raised = False
        self._pump_fwd = False
        self._pump_aft = False
        self._active_tasks = {}


def test_sum_db_empty_returns_zero():
    assert _sum_db([]) == 0.0


def test_sum_db_single_value():
    result = _sum_db([80.0])
    assert abs(result - 80.0) < 0.01


def test_sum_db_two_equal_values():
    # Two equal dB sources sum to ~+3dB
    result = _sum_db([80.0, 80.0])
    assert abs(result - 83.01) < 0.1


def test_helm_noise_scales_with_speed():
    engine = NoiseEngine()
    own_slow = make_ship(speed=2.0)
    own_fast = make_ship(speed=20.0)
    world = make_world(own_slow)
    ls = _FakeLoopState()
    levels_slow = engine.tick(own_slow, world, 0.1, ls)
    engine2 = NoiseEngine()
    levels_fast = engine2.tick(own_fast, world, 0.1, ls)
    assert levels_fast["helm"] > levels_slow["helm"]


def test_engineering_noise_scales_with_reactor():
    engine = NoiseEngine()
    own = make_ship()
    own.reactor.output_mw = 20.0
    own.reactor.max_mw = 100.0
    world = make_world(own)
    ls = _FakeLoopState()
    levels_low = engine.tick(own, world, 0.1, ls)

    engine2 = NoiseEngine()
    own2 = make_ship()
    own2.reactor.output_mw = 90.0
    own2.reactor.max_mw = 100.0
    levels_high = engine2.tick(own2, world, 0.1, ls)
    assert levels_high["engineering"] > levels_low["engineering"]


def test_weapons_noise_during_tube_operations():
    from backend.models import Tube
    engine = NoiseEngine()
    own = make_ship()
    own.weapons.tubes = [Tube(idx=1, state="Empty", timer_s=5.0, next_state="Loaded")]
    world = make_world(own)
    ls = _FakeLoopState()
    levels = engine.tick(own, world, 0.1, ls)
    assert levels["weapons"] > 0


def test_impulse_decays_over_time():
    engine = NoiseEngine()
    engine.add_impulse("weapons", 80.0, 0.5)
    own = make_ship()
    world = make_world(own)
    ls = _FakeLoopState()
    levels1 = engine.tick(own, world, 0.3, ls)
    assert levels1["weapons"] > 0
    # After 0.5s total, impulse should be gone
    levels2 = engine.tick(own, world, 0.3, ls)
    # The impulse TTL was 0.5, we've ticked 0.6 total — it should be expired
    # but sustained noise may remain; impulse contribution should be 0
    # We verify indirectly: weapons level should be lower (no impulse contribution)
    # A clean test: use a fresh engine with ONLY impulse
    engine3 = NoiseEngine()
    engine3.add_impulse("helm", 90.0, 0.2)
    own2 = make_ship(speed=0.0)
    own2.hull.max_speed = 25.0
    levels_a = engine3.tick(own2, world, 0.1, _FakeLoopState())
    levels_b = engine3.tick(own2, world, 0.2, _FakeLoopState())
    # After 0.3s total, impulse (0.2s duration) is gone
    # helm will still have sustained noise from speed=0 but impulse is gone
    assert levels_b["helm"] < levels_a["helm"]


def test_depth_charge_creates_impulse():
    engine = NoiseEngine()
    own = make_ship()
    world = make_world(own)
    world.depth_charges = []
    ls = _FakeLoopState()
    engine.tick(own, world, 0.1, ls)  # baseline
    # Add a depth charge
    world.depth_charges.append({"x": 100, "y": 200})
    levels = engine.tick(own, world, 0.1, ls)
    assert levels["weapons"] > 60  # 80dB impulse should be prominent


def test_mast_raised_adds_sonar_noise():
    engine = NoiseEngine()
    own = make_ship(speed=0.0)
    world = make_world(own)
    ls_down = _FakeLoopState()
    ls_up = _FakeLoopState()
    ls_up._periscope_raised = True
    levels_down = engine.tick(own, world, 0.1, ls_down)
    engine2 = NoiseEngine()
    levels_up = engine2.tick(own, world, 0.1, ls_up)
    assert levels_up["sonar"] > levels_down["sonar"]


def test_total_is_sum_of_stations():
    engine = NoiseEngine()
    own = make_ship(speed=10.0)
    own.reactor.output_mw = 50.0
    world = make_world(own)
    ls = _FakeLoopState()
    levels = engine.tick(own, world, 0.1, ls)
    # Total should be the dB sum of all stations
    manual_total = _sum_db([levels["helm"], levels["sonar"], levels["weapons"], levels["engineering"]])
    assert abs(levels["total"] - manual_total) < 2.0  # allow jitter
