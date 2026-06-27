from __future__ import annotations
import math
from typing import Tuple
from ..models import Ship


KNOTS_TO_MPS = 0.514444
DEG_TO_RAD = math.pi / 180.0
RAD_TO_DEG = 180.0 / math.pi


def cavitation_speed_for_depth(depth_m: float) -> float:
    return max(5.0, min(30.0, 0.08 * depth_m + 5.0))


# --- Depth-control model -------------------------------------------------
# The depth-change rate ceiling is the sum of two independent contributions:
#   1. A ballast/trim "floor" that is available at any speed (incl. dead stop).
#   2. A diving-planes term that scales with the square of speed (hydrodynamic
#      lift on the bow/stern planes is ~v^2). Planes do almost nothing slow and
#      dominate at speed. They are NOT gated on the ballast system, so a boat
#      that loses ballast can still control depth as long as it has way on.
# All constants are m/s and are intentionally easy to tune for game balance.
BALLAST_FLOOR_RATE = 0.6    # normal ballast/trim authority, speed-independent
BALLAST_BOOST_RATE = 1.8    # ballast floor when pumps are assigned (blow/flood)
BALLAST_FAILED_RATE = 0.2   # residual authority when the ballast system is down
PLANES_MAX_RATE = 5.0       # diving-planes contribution at/above the ref speed
PLANES_REF_SPEED = 18.0     # knots at which the planes reach full effectiveness


def planes_depth_rate(speed_kn: float) -> float:
    """Diving-planes depth-rate contribution (m/s) for a given speed (kn)."""
    speed_frac = clamp(speed_kn / PLANES_REF_SPEED, 0.0, 1.0)
    return PLANES_MAX_RATE * (speed_frac ** 2)


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

    # Depth rate = speed-independent ballast floor + speed^2 diving-planes term.
    ballast_ok = getattr(ship, "systems", None) is None or ship.systems.ballast_ok
    if not ballast_ok:
        ballast_rate = BALLAST_FAILED_RATE
    elif ballast_boost:
        ballast_rate = BALLAST_BOOST_RATE
    else:
        ballast_rate = BALLAST_FLOOR_RATE
    # Diving planes can be lost independently of ballast; when they fail the
    # boat falls back to ballast-only depth control regardless of speed.
    planes_ok = getattr(ship, "systems", None) is None or ship.systems.planes_ok
    # Planes use actual speed (you only get the lift you currently have way for).
    planes_rate = planes_depth_rate(kin.speed) if planes_ok else 0.0
    max_depth_rate = (ballast_rate + planes_rate) * damage_turn_factor
    # Enforce platform depth limits for surface vessels and subs alike
    limited_ordered_depth = clamp(ordered_depth, 0.0, hull.max_depth)
    dz = limited_ordered_depth - kin.depth
    step = clamp(dz, -max_depth_rate * dt, max_depth_rate * dt)
    kin.depth = clamp(kin.depth + step, 0.0, hull.max_depth)
    kin.depth_rate = step / dt if dt > 0 else 0.0  # signed achieved rate (m/s)

    # Move using compass convention (0°=North, 90°=East):
    # x increases to the East → sin(heading), y increases to the North → cos(heading)
    sog_mps = kin.speed * KNOTS_TO_MPS
    kin.x += math.sin(kin.heading * DEG_TO_RAD) * sog_mps * dt
    kin.y += math.cos(kin.heading * DEG_TO_RAD) * sog_mps * dt

    cav = kin.speed > cavitation_speed_for_depth(kin.depth)
    return cav, kin.heading, kin.speed, kin.depth
