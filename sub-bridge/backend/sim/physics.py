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

    reactor_cap_speed = hull.max_speed * (ship.reactor.output_mw / max(1.0, ship.reactor.max_mw))
    target_speed = clamp(ordered_speed, 0.0, reactor_cap_speed)

    if target_speed > kin.speed:
        kin.speed = min(target_speed, kin.speed + hull.accel_max * dt)
    else:
        kin.speed = max(target_speed, kin.speed - hull.decel_max * dt)

    # Rudder failure disables turning
    rudder_ok = getattr(ship, "systems", None) is None or ship.systems.rudder_ok
    dh = ((ordered_heading - kin.heading + 540) % 360) - 180
    max_turn = hull.turn_rate_max * dt
    turn = 0.0 if not rudder_ok else clamp(dh, -max_turn, max_turn)
    kin.heading = (kin.heading + turn) % 360

    ballast_ok = getattr(ship, "systems", None) is None or ship.systems.ballast_ok
    max_depth_rate = (6.0 if ballast_boost else 3.0) if ballast_ok else 0.5
    dz = ordered_depth - kin.depth
    step = clamp(dz, -max_depth_rate * dt, max_depth_rate * dt)
    kin.depth = max(0.0, kin.depth + step)

    sog_mps = kin.speed * KNOTS_TO_MPS
    kin.x += math.cos(kin.heading * DEG_TO_RAD) * sog_mps * dt
    kin.y += math.sin(kin.heading * DEG_TO_RAD) * sog_mps * dt

    cav = kin.speed > cavitation_speed_for_depth(kin.depth)
    return cav, kin.heading, kin.speed, kin.depth
