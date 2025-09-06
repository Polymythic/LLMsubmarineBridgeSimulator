from __future__ import annotations
import math
from typing import Tuple
from ..models import Ship


KNOTS_TO_MPS = 0.514444
DEG_TO_RAD = math.pi / 180.0
RAD_TO_DEG = 180.0 / math.pi


def cavitation_speed_for_depth(depth_m: float) -> float:
    return max(5.0, min(30.0, 0.08 * depth_m + 5.0))


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def integrate_kinematics(
    ship: Ship,
    ordered_heading: float,
    ordered_speed: float,
    ordered_depth: float,
    dt: float,
    ballast_boost: bool = False,
) -> Tuple[bool, float, float, float]:
    hull = ship.hull
    kin = ship.kin

    # Apply hull damage effects to performance
    hull_damage_factor = max(0.1, 1.0 - ship.damage.hull)  # 0.1 = 10% performance at 100% damage
    damage_accel_factor = max(0.2, hull_damage_factor)  # Acceleration less affected than top speed
    damage_turn_factor = max(0.3, hull_damage_factor)  # Turning moderately affected

    reactor_cap_speed = hull.max_speed * (ship.reactor.output_mw / max(1.0, ship.reactor.max_mw)) * hull_damage_factor
    target_speed = clamp(ordered_speed, 0.0, reactor_cap_speed)

    if target_speed > kin.speed:
        kin.speed = min(target_speed, kin.speed + hull.accel_max * damage_accel_factor * dt)
    else:
        kin.speed = max(target_speed, kin.speed - hull.decel_max * damage_accel_factor * dt)

    # Rudder failure disables turning
    rudder_ok = getattr(ship, "systems", None) is None or ship.systems.rudder_ok
    dh = ((ordered_heading - kin.heading + 540) % 360) - 180
    max_turn = hull.turn_rate_max * damage_turn_factor * dt
    turn = 0.0 if not rudder_ok else clamp(dh, -max_turn, max_turn)
    kin.heading = (kin.heading + turn) % 360

    ballast_ok = getattr(ship, "systems", None) is None or ship.systems.ballast_ok
    base_depth_rate = (6.0 if ballast_boost else 3.0) if ballast_ok else 0.5
    max_depth_rate = base_depth_rate * damage_turn_factor  # Depth control also affected by damage
    # Enforce platform depth limits for surface vessels and subs alike
    limited_ordered_depth = clamp(ordered_depth, 0.0, hull.max_depth)
    dz = limited_ordered_depth - kin.depth
    step = clamp(dz, -max_depth_rate * dt, max_depth_rate * dt)
    kin.depth = clamp(kin.depth + step, 0.0, hull.max_depth)

    # Move using compass convention (0°=North, 90°=East):
    # x increases to the East → sin(heading), y increases to the North → cos(heading)
    sog_mps = kin.speed * KNOTS_TO_MPS
    kin.x += math.sin(kin.heading * DEG_TO_RAD) * sog_mps * dt
    kin.y += math.cos(kin.heading * DEG_TO_RAD) * sog_mps * dt

    cav = kin.speed > cavitation_speed_for_depth(kin.depth)
    return cav, kin.heading, kin.speed, kin.depth
