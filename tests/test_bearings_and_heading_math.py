import os
import sys
import math

# Ensure sub-bridge backend is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from backend.sim.physics import integrate_kinematics
from backend.sim.weapons import step_torpedo
from backend.sim.ecs import World
from backend.models import Ship, Kinematics, Hull, Acoustics, WeaponsSuite, Reactor, DamageState


def make_ship(id_: str = "ownship", x: float = 0.0, y: float = 0.0, depth: float = 100.0, heading: float = 0.0, speed: float = 0.0) -> Ship:
    return Ship(
        id=id_,
        side="BLUE" if id_ == "ownship" else "RED",
        kin=Kinematics(x=x, y=y, depth=depth, heading=heading, speed=speed),
        hull=Hull(),
        acoustics=Acoustics(),
        weapons=WeaponsSuite(),
        reactor=Reactor(),
        damage=DamageState(),
    )


def test_true_and_relative_bearings_quadrants():
    # Define ownship heading and compute true/relative bearings to cardinal points
    own_heading = 45.0
    # For a target at East (true 90), relative should be 45
    brg_true_e = (math.degrees(math.atan2(1000.0 - 0.0, 0.0 - 0.0)) % 360.0)
    brg_rel_e = (brg_true_e - own_heading + 360.0) % 360.0
    assert brg_true_e == 90.0
    assert brg_rel_e == 45.0

    # South (true 180) => relative 135
    brg_true_s = (math.degrees(math.atan2(0.0 - 0.0, -1000.0 - 0.0)) % 360.0)
    brg_rel_s = (brg_true_s - own_heading + 360.0) % 360.0
    assert brg_true_s == 180.0
    assert brg_rel_s == 135.0

    # West (true 270) => relative 225
    brg_true_w = (math.degrees(math.atan2(-1000.0 - 0.0, 0.0 - 0.0)) % 360.0)
    brg_rel_w = (brg_true_w - own_heading + 360.0) % 360.0
    assert brg_true_w == 270.0
    assert brg_rel_w == 225.0

    # North (true 0) => relative 315
    brg_true_n = (math.degrees(math.atan2(0.0 - 0.0, 1000.0 - 0.0)) % 360.0)
    brg_rel_n = (brg_true_n - own_heading + 360.0) % 360.0
    assert brg_true_n == 0.0
    assert brg_rel_n == 315.0


def test_integrate_kinematics_moves_along_compass_axes():
    # Use a fixed speed and heading, and verify axis-aligned motion respects compass convention
    ship = make_ship(heading=0.0, speed=10.0)  # 10 kn northbound
    # Maintain speed and depth
    cav, h, s, d = integrate_kinematics(ship, ordered_heading=0.0, ordered_speed=10.0, ordered_depth=ship.kin.depth, dt=1.0)
    assert h == 0.0
    # North → y increases, x ~ 0
    assert ship.kin.y > 0.0
    assert abs(ship.kin.x) < ship.kin.y * 0.1

    # Eastbound
    ship = make_ship(heading=90.0, speed=10.0)
    _ = integrate_kinematics(ship, ordered_heading=90.0, ordered_speed=10.0, ordered_depth=ship.kin.depth, dt=1.0)
    # East → x increases, y ~ 0
    assert ship.kin.x > 0.0
    assert abs(ship.kin.y) < ship.kin.x * 0.1

    # Southbound
    ship = make_ship(heading=180.0, speed=10.0)
    _ = integrate_kinematics(ship, ordered_heading=180.0, ordered_speed=10.0, ordered_depth=ship.kin.depth, dt=1.0)
    assert ship.kin.y < 0.0
    assert abs(ship.kin.x) < abs(ship.kin.y) * 0.1

    # Westbound
    ship = make_ship(heading=270.0, speed=10.0)
    _ = integrate_kinematics(ship, ordered_heading=270.0, ordered_speed=10.0, ordered_depth=ship.kin.depth, dt=1.0)
    assert ship.kin.x < 0.0
    assert abs(ship.kin.y) < abs(ship.kin.x) * 0.1


def test_torpedo_guidance_steers_toward_compass_bearing():
    # Build a world with ownship and one target to the East
    world = World()
    own = make_ship(id_="ownship", x=0.0, y=0.0, heading=0.0, speed=0.0)
    tgt = make_ship(id_="red-01", x=1000.0, y=0.0, heading=0.0, speed=0.0)
    world.add_ship(own)
    world.add_ship(tgt)

    # Create a torpedo dict similar to fire outcome, already armed for guidance test
    t = {
        "x": 0.0,
        "y": 0.0,
        "depth": own.kin.depth,
        "heading": 0.0,  # initially north
        "speed": 20.0,  # kn
        "armed": True,   # force armed to exercise guidance immediately
        "enable_range_m": 800.0,
        "run_time": 0.0,
        "max_run_time": 60.0,
        "target_id": None,
        "name": "Mk48",
        # Use a wide seeker cone so the target at 90° off the nose is within FOV
        "seeker_cone": 200.0,
        "side": own.side,
        "spoofed_timer": 0.0,
    }

    # Step and ensure heading increases toward 90° and x increases
    step_torpedo(t, world, dt=1.0)
    assert 0.0 < t["heading"] <= 20.0  # turn rate cap 20 deg/s
    # After a few steps, heading should approach 90 and x should increase
    for _ in range(5):
        step_torpedo(t, world, dt=1.0)
    assert t["heading"] > 50.0
    assert t["x"] > 0.0


