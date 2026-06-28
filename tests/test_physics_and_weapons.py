import math
import os
import sys

# Add project sub-bridge to import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from backend.sim.physics import (
    clamp,
    cavitation_speed_for_depth,
    integrate_kinematics,
    planes_depth_rate,
    BALLAST_FLOOR_RATE,
    BALLAST_BOOST_RATE,
    PLANES_REF_SPEED,
)
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


def test_planes_term_scales_with_speed_squared():
    # Diving-planes lift is ~v^2: doubling speed quadruples the planes contribution
    # (within the sub-reference-speed regime where it isn't yet clamped).
    r_low = planes_depth_rate(PLANES_REF_SPEED / 4.0)
    r_high = planes_depth_rate(PLANES_REF_SPEED / 2.0)
    assert r_low > 0.0
    assert math.isclose(r_high, 4.0 * r_low, rel_tol=1e-6)
    # Saturates at/above the reference speed
    assert planes_depth_rate(PLANES_REF_SPEED) == planes_depth_rate(PLANES_REF_SPEED * 2)


def test_depth_rate_increases_with_speed():
    # A faster boat changes depth faster than a slow one (planes authority).
    def achieved_rate(speed):
        ship = make_own()
        ship.kin.speed = speed
        integrate_kinematics(ship, ship.kin.heading, speed, 200.0, dt=1.0)
        return ship.kin.depth_rate
    slow = achieved_rate(2.0)
    fast = achieved_rate(PLANES_REF_SPEED)
    assert fast > slow
    # Stopped boat still has the speed-independent ballast floor available.
    assert achieved_rate(0.0) > 0.0
    assert math.isclose(achieved_rate(0.0), BALLAST_FLOOR_RATE, rel_tol=1e-6)


def test_planes_failure_forces_ballast_only_depth_control():
    # With planes down, depth rate collapses to the ballast floor regardless of speed.
    ship = make_own()
    ship.kin.speed = PLANES_REF_SPEED
    ship.systems.planes_ok = False
    integrate_kinematics(ship, ship.kin.heading, PLANES_REF_SPEED, 200.0, dt=1.0)
    assert math.isclose(ship.kin.depth_rate, BALLAST_FLOOR_RATE, rel_tol=1e-6)


def test_ballast_boost_raises_the_floor():
    # Pump assignment (emergency blow/flood) raises the speed-independent floor.
    ship = make_own()  # speed 0 -> planes contribute nothing
    integrate_kinematics(ship, ship.kin.heading, 0.0, 200.0, dt=1.0, ballast_boost=True)
    assert math.isclose(ship.kin.depth_rate, BALLAST_BOOST_RATE, rel_tol=1e-6)


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


# -------------------- 3D torpedo physics --------------------

def _world_with_target(tx, ty, tdepth, tspeed=0.0):
    """World containing a far-away ownship (so its safety/detonation logic is
    inert) plus a single RED target at the given 3D position."""
    from backend.sim.ecs import World
    from backend.models import Ship as MShip
    own = make_own()
    own.kin.x = -10000.0
    own.kin.y = -10000.0
    world = World()
    world.add_ship(own)
    tgt = MShip(
        id="red-01", side="RED",
        kin=Kinematics(x=tx, y=ty, depth=tdepth, heading=0.0, speed=tspeed),
        hull=Hull(), acoustics=Acoustics(), weapons=WeaponsSuite(), reactor=Reactor(), damage=DamageState(),
    )
    world.add_ship(tgt)
    return world, own, tgt


def _make_torp(x=0.0, y=0.0, depth=50.0, heading=0.0, run_depth=50.0, side="BLUE", armed=True):
    return {
        "id": "t-test",
        "shooter_id": "ownship",
        "x": x, "y": y, "depth": depth,
        "heading": heading, "speed": 45.0,
        "armed": armed, "enable_range_m": 800.0,
        "seeker_range_m": 4000.0, "run_time": 5.0, "max_run_time": 600.0,
        "target_id": None, "name": "Mk48", "seeker_cone": 35.0,
        "side": side, "spoofed_timer": 0.0, "run_depth": run_depth,
        "doctrine": "passive_then_active", "pn_nav_const": 3.0, "los_prev": None,
    }


def test_torpedo_homes_in_depth_and_hits_deep_target():
    # Target dead ahead (North) at 1500 m, but 200 m deeper than the torpedo.
    from backend.sim.weapons import step_torpedo
    world, own, tgt = _world_with_target(0.0, 1500.0, 250.0)
    t = _make_torp(x=0.0, y=0.0, depth=50.0, heading=0.0, run_depth=50.0)
    for _ in range(2000):
        step_torpedo(t, world, dt=0.1)
        if t["run_time"] > t["max_run_time"]:
            break
    # Terminal vertical homing closed the depth gap ...
    assert t["depth"] > 200.0
    # ... and the 3D proximity fuze detonated on the target.
    assert t["run_time"] > t["max_run_time"]
    assert tgt.damage.hull > 0.0


def test_coarse_run_depth_still_acquires_deeper_target():
    # Fire with a deliberately-wrong run_depth (50 m) at a target 250 m deeper.
    # At 700 m horizontal that is ~20° elevation — outside the horizontal cone
    # half-angle (17.5°) but inside the wider vertical acquisition envelope, so
    # the torpedo must still acquire, home in depth, and hit.
    from backend.sim.weapons import step_torpedo
    world, own, tgt = _world_with_target(0.0, 700.0, 300.0)
    t = _make_torp(x=0.0, y=0.0, depth=50.0, heading=0.0, run_depth=50.0)
    for _ in range(2000):
        step_torpedo(t, world, dt=0.1)
        if t["run_time"] > t["max_run_time"]:
            break
    assert t["depth"] > 250.0           # homed down toward the deep target
    assert t["run_time"] > t["max_run_time"]
    assert tgt.damage.hull > 0.0


def test_depth_separation_prevents_2d_aligned_detonation():
    # Target shares the torpedo's x/y but is 380 m away in depth. The old 2D
    # fuze (hypot of x/y only) would have detonated immediately; the 3D fuze
    # must not.
    from backend.sim.weapons import step_torpedo
    world, own, tgt = _world_with_target(0.0, 0.0, 400.0)
    t = _make_torp(x=0.0, y=0.0, depth=20.0, heading=0.0, run_depth=20.0)
    step_torpedo(t, world, dt=0.1)
    assert t["run_time"] < t["max_run_time"]  # did not detonate
    assert tgt.damage.hull == 0.0


def test_torpedo_transits_to_run_depth_when_no_target():
    # No target within seeker range: the torpedo should drive toward its ordered
    # run depth and not overshoot it.
    from backend.sim.weapons import step_torpedo
    world, own, tgt = _world_with_target(0.0, 50000.0, 100.0)  # target far out of range
    t = _make_torp(x=0.0, y=0.0, depth=100.0, heading=0.0, run_depth=30.0, armed=False)
    for _ in range(50):
        step_torpedo(t, world, dt=0.1)
    assert t["depth"] < 100.0   # descended toward run depth
    assert t["depth"] >= 30.0   # but did not pass below it
