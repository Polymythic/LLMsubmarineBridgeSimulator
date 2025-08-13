from __future__ import annotations
import math
import random
from typing import Optional, Callable
from ..models import Ship, Tube, TorpedoDef


def step_tubes(ship: Ship, dt: float) -> None:
    ws = ship.weapons
    for t in ws.tubes:
        if t.timer_s > 0.0:
            t.timer_s = max(0.0, t.timer_s - dt)
            if t.timer_s == 0.0 and t.next_state is not None:
                t.state = t.next_state
                t.next_state = None


esspoof_prob = 0.2  # chance to be spoofed when a countermeasure effect occurs


def try_load_tube(ship: Ship, tube_idx: int, weapon_name: str = "Mk48") -> bool:
    if getattr(ship, "systems", None) is not None and not ship.systems.tubes_ok:
        return False
    ws = ship.weapons
    tube = _get_tube(ship, tube_idx)
    if tube is None or tube.state != "Empty" or ship.weapons.torpedoes_stored <= 0:
        return False
    if tube.timer_s > 0.0:
        return False
    tube.weapon = TorpedoDef(name=weapon_name)
    tube.next_state = "Loaded"
    tube.timer_s = ws.reload_time_s * max(1.0, ws.time_penalty_multiplier)
    ship.weapons.torpedoes_stored -= 1
    return True


def try_flood_tube(ship: Ship, tube_idx: int) -> bool:
    if getattr(ship, "systems", None) is not None and not ship.systems.tubes_ok:
        return False
    ws = ship.weapons
    tube = _get_tube(ship, tube_idx)
    if tube is None or tube.state != "Loaded":
        return False
    if tube.timer_s > 0.0:
        return False
    tube.next_state = "Flooded"
    tube.timer_s = ws.flood_time_s * max(1.0, ws.time_penalty_multiplier)
    return True


def try_set_doors(ship: Ship, tube_idx: int, open_state: bool) -> bool:
    if getattr(ship, "systems", None) is not None and not ship.systems.tubes_ok:
        return False
    ws = ship.weapons
    tube = _get_tube(ship, tube_idx)
    if tube is None:
        return False
    if tube.timer_s > 0.0:
        return False
    if open_state and tube.state == "Flooded":
        tube.next_state = "DoorsOpen"
        tube.timer_s = ws.doors_time_s * max(1.0, ws.time_penalty_multiplier)
        return True
    if not open_state and tube.state == "DoorsOpen":
        tube.next_state = "Flooded"
        tube.timer_s = ws.doors_time_s
        return True
    return False


def try_fire(ship: Ship, tube_idx: int, bearing_deg: float, run_depth: float, enable_range_m: float = None, doctrine: str = "passive_then_active"):
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
        "enable_range_m": (enable_range_m if enable_range_m is not None else tube.weapon.enable_range_m),
        "seeker_range_m": getattr(tube.weapon, "seeker_range_m", 4000.0),
        "run_time": 0.0,
        "max_run_time": tube.weapon.max_run_time_s,
        "target_id": None,
        "name": tube.weapon.name,
        "seeker_cone": tube.weapon.seeker_cone_deg,
        "side": ship.side,
        "spoofed_timer": 0.0,
        "run_depth": run_depth,
        "doctrine": doctrine,
    }
    tube.weapon = None
    tube.state = "Empty"
    tube.timer_s = 0.0
    tube.next_state = None
    return torp


def step_torpedo(t: dict, world, dt: float, on_event: Optional[Callable[[str, dict], None]] = None) -> None:
    dx = t["x"] - world.ships["ownship"].kin.x
    dy = t["y"] - world.ships["ownship"].kin.y
    dist_from_shooter = math.hypot(dx, dy)
    if not t["armed"] and dist_from_shooter >= t["enable_range_m"]:
        t["armed"] = True
        if on_event:
            on_event("torpedo.armed", {"name": t["name"]})

    # Spoof timer decay
    if t.get("spoofed_timer", 0.0) > 0.0:
        t["spoofed_timer"] = max(0.0, t["spoofed_timer"] - dt)

    # Detonation check against opposing ships
    for ship in world.all_ships():
        if ship.side == t.get("side"):
            continue
        rng = math.hypot(ship.kin.x - t["x"], ship.kin.y - t["y"])
        if t["armed"] and rng < 30.0:  # proximity fuze
            ship.damage.hull = min(1.0, ship.damage.hull + 0.5)
            ship.damage.flooding_rate = min(10.0, ship.damage.flooding_rate + 2.0)
            if on_event:
                on_event("torpedo.detonated", {"target": ship.id, "range_m": rng})
            t["run_time"] = t["max_run_time"] + 1.0
            return

    # Guidance
    target = _nearest_target(t, world)
    if target is not None and t["armed"]:
        # Chance to be spoofed by a countermeasure; here we model as periodic effect
        if t.get("spoofed_timer", 0.0) == 0.0 and random.random() < 0.02:
            t["spoofed_timer"] = 3.0
            if on_event:
                on_event("torpedo.spoofed", {"seconds": t["spoofed_timer"]})
        # Compute desired heading using compass bearing (0°=N, 90°=E)
        dx = target.kin.x - t["x"]
        dy = target.kin.y - t["y"]
        desired = (math.degrees(math.atan2(dx, dy)) % 360.0)
        dh = ((desired - t["heading"] + 540) % 360) - 180
        # If spoofed, add jitter and reduce turn authority
        if t.get("spoofed_timer", 0.0) > 0.0:
            dh += random.uniform(-30.0, 30.0)
            max_turn = 10.0 * dt
        else:
            max_turn = 20.0 * dt
        t["heading"] = (t["heading"] + max(-max_turn, min(max_turn, dh))) % 360

    # Move torpedo using compass convention (0°=N, 90°=E)
    mps = t["speed"] * 0.514444
    heading_rad = math.radians(t["heading"])
    t["x"] += math.sin(heading_rad) * mps * dt
    t["y"] += math.cos(heading_rad) * mps * dt
    t["run_time"] += dt


def _nearest_target(t: dict, world):
    nearest = None
    nearest_d = 1e12
    for ship in world.all_ships():
        if ship.side == t.get("side"):
            continue
        dx = ship.kin.x - t["x"]
        dy = ship.kin.y - t["y"]
        rng = math.hypot(dx, dy)
        # Seeker range gating with simple environmental effect (thermocline reduces range)
        own = world.ships.get("ownship")
        env_mult = 0.6 if getattr(getattr(own, "acoustics", None), "thermocline_on", False) else 1.0
        if rng > t.get("seeker_range_m", 4000.0) * env_mult:
            continue
        # Compass bearing from torpedo to target
        bearing = (math.degrees(math.atan2(dx, dy)) % 360.0)
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
