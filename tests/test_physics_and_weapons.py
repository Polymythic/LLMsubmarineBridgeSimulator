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


def test_torpedo_arming_and_pn_guidance_and_safety():
    ship = make_own()
    # Prepare tube and fire
    assert try_load_tube(ship, 1, "Mk48")
    # Fast-forward load
    for _ in range(50):
        step_tubes(ship, 1.0)
    assert try_flood_tube(ship, 1)
    for _ in range(10):
        step_tubes(ship, 1.0)
    assert try_set_doors(ship, 1, True)
    for _ in range(5):
        step_tubes(ship, 1.0)
    torp = try_fire(ship, 1, 0.0, 50.0, enable_range_m=500.0)
    assert torp is not None
    # World with one target ahead at ~1500 m
    from backend.sim.ecs import World
    from backend.models import Ship as MShip
    world = World()
    world.add_ship(ship)
    target = MShip(
        id="red-01", side="RED",
        kin=Kinematics(x=0.0, y=1500.0, depth=50.0, heading=180.0, speed=0.0),
        hull=Hull(), acoustics=Acoustics(), weapons=WeaponsSuite(), reactor=Reactor(), damage=DamageState()
    )
    world.add_ship(target)
    # Step until armed
    run = 0.0
    while not torp["armed"] and run < 60.0:
        from backend.sim.weapons import step_torpedo
        step_torpedo(torp, world, dt=1.0)
        run += 1.0
    assert torp["armed"]
    # After arming, heading should trend toward target bearing (0° to 0 for target at North)
    h0 = torp["heading"]
    from backend.sim.weapons import step_torpedo
    for _ in range(5):
        step_torpedo(torp, world, dt=1.0)
    h1 = torp["heading"]
    # Heading deviation should reduce toward 0°
    def angdiff(a,b):
        return abs(((a-b+540)%360)-180)
    assert angdiff(h1, 0.0) <= angdiff(h0, 0.0) + 1e-3
    # Safety: place ownship very close ahead pre-arm and ensure it turns away (simulate new torpedo)
    # Reload and prep again for second fire
    assert try_load_tube(ship, 1, "Mk48")
    for _ in range(50):
        step_tubes(ship, 1.0)
    assert try_flood_tube(ship, 1)
    for _ in range(10):
        step_tubes(ship, 1.0)
    assert try_set_doors(ship, 1, True)
    for _ in range(5):
        step_tubes(ship, 1.0)
    torp2 = try_fire(ship, 1, 0.0, 50.0, enable_range_m=1000.0)
    assert torp2 is not None
    ship.kin.x = 0.0; ship.kin.y = 10.0
    run2 = 0.0
    turned = False
    while not torp2["armed"] and run2 < 3.0:
        h_before = torp2["heading"]
        step_torpedo(torp2, world, dt=0.5)
        h_after = torp2["heading"]
        if angdiff(h_after, 180.0) < angdiff(h_before, 180.0):
            turned = True
        run2 += 0.5
    assert turned
