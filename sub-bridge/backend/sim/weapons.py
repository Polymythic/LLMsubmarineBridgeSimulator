from __future__ import annotations
import math
from typing import Optional
from ..models import Ship, Tube, TorpedoDef


def step_tubes(weapons, dt: float) -> None:
    pass


def try_load_tube(ship: Ship, tube_idx: int, weapon_name: str = "Mk48") -> bool:
    tube = _get_tube(ship, tube_idx)
    if tube is None or tube.state != "Empty" or ship.weapons.torpedoes_stored <= 0:
        return False
    tube.weapon = TorpedoDef(name=weapon_name)
    tube.state = "Loaded"
    ship.weapons.torpedoes_stored -= 1
    return True


def try_flood_tube(ship: Ship, tube_idx: int) -> bool:
    tube = _get_tube(ship, tube_idx)
    if tube is None or tube.state != "Loaded":
        return False
    tube.state = "Flooded"
    return True


def try_set_doors(ship: Ship, tube_idx: int, open_state: bool) -> bool:
    tube = _get_tube(ship, tube_idx)
    if tube is None:
        return False
    if open_state and tube.state == "Flooded":
        tube.state = "DoorsOpen"
        return True
    if not open_state and tube.state == "DoorsOpen":
        tube.state = "Flooded"
        return True
    return False


def try_fire(ship: Ship, tube_idx: int, bearing_deg: float, run_depth: float):
    tube = _get_tube(ship, tube_idx)
    if tube is None or tube.state != "DoorsOpen" or tube.weapon is None:
        return None
    torp = {
        "x": ship.kin.x,
        "y": ship.kin.y,
        "depth": ship.kin.depth,
        "heading": bearing_deg % 360.0,
        "speed": tube.weapon.speed,
        "armed": False,
        "enable_range_m": tube.weapon.enable_range_m,
        "run_time": 0.0,
        "max_run_time": tube.weapon.max_run_time_s,
        "target_id": None,
        "name": tube.weapon.name,
        "seeker_cone": tube.weapon.seeker_cone_deg,
    }
    tube.weapon = None
    tube.state = "Empty"
    return torp


def step_torpedo(t: dict, world, dt: float) -> None:
    dx = t["x"] - world.ships["ownship"].kin.x
    dy = t["y"] - world.ships["ownship"].kin.y
    dist_from_shooter = math.hypot(dx, dy)
    if not t["armed"] and dist_from_shooter >= t["enable_range_m"]:
        t["armed"] = True

    target = _nearest_target(t, world)
    if target is not None and t["armed"]:
        desired = math.degrees(math.atan2(target.kin.y - t["y"], target.kin.x - t["x"])) % 360.0
        dh = ((desired - t["heading"] + 540) % 360) - 180
        max_turn = 20.0 * dt
        t["heading"] = (t["heading"] + max(-max_turn, min(max_turn, dh))) % 360

    mps = t["speed"] * 0.514444
    t["x"] += math.cos(math.radians(t["heading"])) * mps * dt
    t["y"] += math.sin(math.radians(t["heading"])) * mps * dt
    t["run_time"] += dt


def _nearest_target(t: dict, world):
    nearest = None
    nearest_d = 1e12
    for ship in world.all_ships():
        if ship.id.startswith("own"):
            continue
        dx = ship.kin.x - t["x"]
        dy = ship.kin.y - t["y"]
        rng = math.hypot(dx, dy)
        bearing = (math.degrees(math.atan2(dy, dx)) % 360.0)
        off = abs(((bearing - t["heading"] + 540) % 360) - 180)
        if off <= t["seeker_cone"] / 2 and rng < nearest_d:
            nearest_d = rng
            nearest = ship
    return nearest


def _get_tube(ship: Ship, idx: int) -> Optional[Tube]:
    for t in ship.weapons.tubes:
        if t.idx == idx:
            return t
    return None
