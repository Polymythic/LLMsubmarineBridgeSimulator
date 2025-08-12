import math
import os
import sys

# Add project sub-bridge to import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from backend.sim.physics import clamp, cavitation_speed_for_depth
from backend.sim.weapons import _get_tube, try_load_tube, try_flood_tube, try_set_doors, try_fire, step_tubes
from backend.models import Ship, Kinematics, Hull, Acoustics, WeaponsSuite, Reactor, DamageState


def make_own():
    return Ship(
        id="ownship",
        side="BLUE",
        kin=Kinematics(depth=100.0, heading=0.0, speed=0.0),
        hull=Hull(),
        acoustics=Acoustics(),
        weapons=WeaponsSuite(),
        reactor=Reactor(output_mw=60.0, max_mw=100.0),
        damage=DamageState(),
    )


def test_clamp_and_cavitation_curve():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
    # cavitation threshold grows with depth
    assert cavitation_speed_for_depth(0) <= cavitation_speed_for_depth(100)


def test_tube_state_machine_timing():
    ship = make_own()
    # load starts timer to Loaded
    ok = try_load_tube(ship, 1, "Mk48")
    assert ok
    t1 = _get_tube(ship, 1)
    assert t1.timer_s > 0 and t1.next_state == "Loaded"
    # step until loaded
    dt_total = 0.0
    while t1.timer_s > 0 and dt_total < 60:
        step_tubes(ship, 1.0)
        dt_total += 1.0
    assert t1.state == "Loaded"
    # flood
    assert try_flood_tube(ship, 1)
    while t1.timer_s > 0 and dt_total < 120:
        step_tubes(ship, 1.0)
        dt_total += 1.0
    assert t1.state == "Flooded"
    # doors open
    assert try_set_doors(ship, 1, True)
    while t1.timer_s > 0 and dt_total < 200:
        step_tubes(ship, 1.0)
        dt_total += 1.0
    assert t1.state == "DoorsOpen"
    # fire
    torp = try_fire(ship, 1, 90.0, 100.0)
    assert torp is not None
    assert t1.state == "Empty"
