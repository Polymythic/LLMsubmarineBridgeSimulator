"""Tests for sub-bridge/backend/sim/damage.py"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from conftest import make_ship
from backend.sim.damage import (
    step_damage, _process_flood_spread, _calculate_system_failures,
    apply_compartment_damage, get_compartment_for_hit_position,
    step_engineering, PUMP_RATE, BREACH_HEALING_RATE, FLOOD_SPREAD_THRESHOLD,
)
from backend.models import CompartmentState


def test_breach_increases_flooding():
    ship = make_ship()
    ship.damage.compartments[0].breach_rate = 0.1
    step_damage(ship, dt=1.0)
    assert ship.damage.compartments[0].flooding_level > 0.0


def test_breach_healing_reduces_breach_rate():
    ship = make_ship()
    ship.damage.compartments[0].breach_rate = 0.5
    step_damage(ship, dt=1.0)
    assert ship.damage.compartments[0].breach_rate < 0.5


def test_pump_reduces_flooding():
    ship = make_ship()
    ship.damage.compartments[2].flooding_level = 0.4
    step_damage(ship, dt=1.0, pump_assignments={1: 2})
    assert ship.damage.compartments[2].flooding_level < 0.4


def test_pump_only_affects_assigned_compartment():
    ship = make_ship()
    ship.damage.compartments[2].flooding_level = 0.4
    ship.damage.compartments[3].flooding_level = 0.3
    step_damage(ship, dt=1.0, pump_assignments={1: 2})
    # Compartment 3 should not decrease (no spread from 0.3 < threshold either)
    assert ship.damage.compartments[3].flooding_level == 0.3


def test_flood_spread_between_adjacent_compartments():
    ship = make_ship()
    ship.damage.compartments[2].flooding_level = 0.8  # above threshold
    step_damage(ship, dt=1.0)
    # Neighbors (1 and 3) should have increased flooding
    assert ship.damage.compartments[1].flooding_level > 0.0
    assert ship.damage.compartments[3].flooding_level > 0.0


def test_flood_spread_does_not_skip_compartments():
    ship = make_ship()
    ship.damage.compartments[2].flooding_level = 0.8
    step_damage(ship, dt=1.0)
    # Compartment 0 and 4 should NOT be affected (not adjacent)
    assert ship.damage.compartments[0].flooding_level == 0.0
    assert ship.damage.compartments[4].flooding_level == 0.0


def test_system_failures_from_flooding():
    comps = [CompartmentState() for _ in range(6)]
    comps[0].flooding_level = 0.8  # Fore
    failures = _calculate_system_failures(comps)
    assert failures["torpedo_loading_factor"] == 0.5
    assert failures["forward_sonar_factor"] == 0.5


def test_system_failures_at_75_percent():
    comps = [CompartmentState() for _ in range(6)]
    comps[5].flooding_level = 0.75
    failures = _calculate_system_failures(comps)
    assert failures["rudder_factor"] == 0.5


def test_system_failures_at_100_percent():
    comps = [CompartmentState() for _ in range(6)]
    comps[0].flooding_level = 1.0
    comps[4].flooding_level = 1.0
    comps[5].flooding_level = 1.0
    failures = _calculate_system_failures(comps)
    assert failures["torpedo_loading_factor"] == 0.0
    assert failures["forward_sonar_factor"] == 0.0
    assert failures["propulsion_factor"] == 0.0
    assert failures["rudder_factor"] == 0.0


def test_apply_compartment_damage():
    ship = make_ship()
    apply_compartment_damage(ship, compartment_idx=1, breach_rate_add=0.3, integrity_loss=0.2)
    assert ship.damage.compartments[1].breach_rate == 0.3
    assert ship.damage.compartments[1].hull_integrity == 0.8


def test_get_compartment_for_hit_position():
    import random
    random.seed(42)
    bow_hits = {get_compartment_for_hit_position("bow") for _ in range(20)}
    mid_hits = {get_compartment_for_hit_position("midship") for _ in range(20)}
    stern_hits = {get_compartment_for_hit_position("stern") for _ in range(20)}
    assert bow_hits.issubset({0, 1})
    assert mid_hits.issubset({2, 3})
    assert stern_hits.issubset({4, 5})


def test_step_engineering_scram():
    ship = make_ship()
    ship.reactor.scrammed = True
    ship.reactor.output_mw = 60.0
    step_engineering(ship, dt=1.0)
    assert ship.reactor.output_mw <= 10.0


def test_step_engineering_power_allocation():
    ship = make_ship()
    ship.reactor.output_mw = 100.0
    ship.reactor.max_mw = 100.0
    ship.power.helm = 0.5
    ship.power.sonar = 0.2
    ship.power.weapons = 0.2
    ship.power.engineering = 0.1
    step_engineering(ship, dt=1.0)
    # Weapons reload/flood/doors times should be affected by allocation
    # With 0.2 allocation, MW_weapons = 20, factor = 20/100 = 0.2
    # reload = 45 / 0.2 / damage_factor => should be > 45
    assert ship.weapons.reload_time_s > 45.0


def test_step_engineering_battery_drain():
    ship = make_ship()
    ship.kin.speed = 10.0
    ship.hull.max_speed = 25.0
    initial_battery = ship.reactor.battery_pct
    step_engineering(ship, dt=10.0)
    assert ship.reactor.battery_pct < initial_battery


def test_step_engineering_system_failures_from_maintenance():
    ship = make_ship()
    ship.maintenance.levels["rudder"] = 0.1  # below 0.2 threshold
    ship.maintenance.levels["sonar"] = 0.1
    step_engineering(ship, dt=0.01)  # tiny dt so maintenance doesn't recover
    assert ship.systems.rudder_ok is False
    assert ship.systems.sonar_ok is False
