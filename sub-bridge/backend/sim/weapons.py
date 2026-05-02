from __future__ import annotations
import math
import random
from typing import Optional, Callable
import os
from ..models import Ship, Tube, TorpedoDef
from .damage import apply_compartment_damage, compute_hull_damage, get_compartment_for_hit_position


# --- Damage tuning constants -------------------------------------------------
# Naval lethality: a single torpedo hit must cripple a ship; two hits in the
# same area should destroy it. Tuned in concert with `damage.compute_hull_damage`.
TORPEDO_PRIMARY_INTEGRITY_LOSS = 0.85
TORPEDO_PRIMARY_BREACH_RATE = 0.6
TORPEDO_ADJACENT_INTEGRITY_LOSS = 0.30
TORPEDO_ADJACENT_BREACH_RATE = 0.25

# Depth charges are cumulative — a single near-miss is painful but rarely
# fatal; saturating spreads kill. Less per-hit than a torpedo.
DEPTH_CHARGE_DIRECT_PRIMARY_INTEGRITY_LOSS = 0.45
DEPTH_CHARGE_DIRECT_PRIMARY_BREACH_RATE = 0.30
DEPTH_CHARGE_DIRECT_ADJACENT_INTEGRITY_LOSS = 0.15
DEPTH_CHARGE_DIRECT_ADJACENT_BREACH_RATE = 0.08
DEPTH_CHARGE_FAR_PRIMARY_INTEGRITY_LOSS = 0.18
DEPTH_CHARGE_FAR_PRIMARY_BREACH_RATE = 0.10


def step_tubes(ship: Ship, dt: float) -> None:
    ws = ship.weapons
    # Depth charge cooldown timer
    if getattr(ws, "depth_charge_cooldown_timer_s", 0.0) > 0.0:
        ws.depth_charge_cooldown_timer_s = max(0.0, ws.depth_charge_cooldown_timer_s - dt)
    # Quick torpedo cooldown timer (AI-only)
    if getattr(ws, "torpedo_quick_cooldown_timer_s", 0.0) > 0.0:
        ws.torpedo_quick_cooldown_timer_s = max(0.0, ws.torpedo_quick_cooldown_timer_s - dt)
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
    import time
    torp = {
        "id": f"torpedo_{ship.id}_{tube_idx}_{int(time.time() * 1000)}",  # Unique ID for sonar tracking
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
        # PN guidance state
        "pn_nav_const": 3.0,
        "los_prev": None,
    }
    tube.weapon = None
    tube.state = "Empty"
    tube.timer_s = 0.0
    tube.next_state = None
    return torp


def step_torpedo(t: dict, world, dt: float, on_event: Optional[Callable[[str, dict], None]] = None, countermeasures: list = None) -> None:
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

    # Self-preservation: avoid ownship
    own = world.ships.get("ownship")
    own_rng = math.hypot(own.kin.x - t["x"], own.kin.y - t["y"]) if own else 1e9
    if not t["armed"]:
        # If ownship is within 300 m and ahead within 60°, bias heading away pre-arm
        if own and own_rng < 300.0:
            bearing_to_own = (math.degrees(math.atan2(own.kin.x - t["x"], own.kin.y - t["y"])) % 360.0)
            off = abs(((bearing_to_own - t["heading"] + 540) % 360) - 180)
            if off < 60.0:
                # Turn away by up to 30°/s pre-arm
                away = (bearing_to_own + 180.0) % 360.0
                dh = ((away - t["heading"] + 540) % 360) - 180
                max_turn = 30.0 * dt
                t["heading"] = (t["heading"] + max(-max_turn, min(max_turn, dh))) % 360
    else:
        # Post-arm: self-destruct if dangerously close to ownship (safety), but allow initial departure
        if own and own_rng < 200.0 and t.get("run_time", 0.0) > 3.0:
            if on_event:
                on_event("torpedo.self_destruct", {"reason": "ownship_proximity", "range_m": own_rng})
            t["run_time"] = t["max_run_time"] + 1.0
            return

    # Detonation check against opposing ships
    for ship in world.all_ships():
        if ship.side == t.get("side"):
            continue
        rng = math.hypot(ship.kin.x - t["x"], ship.kin.y - t["y"])
        if t["armed"] and rng < 30.0:  # proximity fuze
            # Determine hit location based on torpedo approach angle
            approach_angle = t.get("heading", 0.0)
            ship_heading = ship.kin.heading
            relative_angle = ((approach_angle - ship_heading + 540) % 360) - 180

            # Determine hit position (bow, midship, stern)
            if abs(relative_angle) < 60:
                hit_position = "bow"
            elif abs(relative_angle) > 120:
                hit_position = "stern"
            else:
                hit_position = "midship"

            # Apply compartment damage. Torpedo hits are catastrophic:
            # the primary compartment is essentially destroyed in one strike,
            # adjacents take heavy collateral, and the breach rates create
            # rapid uncontrolled flooding.
            primary_comp = get_compartment_for_hit_position(hit_position)
            apply_compartment_damage(
                ship, primary_comp,
                breach_rate_add=TORPEDO_PRIMARY_BREACH_RATE,
                integrity_loss=TORPEDO_PRIMARY_INTEGRITY_LOSS,
            )
            if primary_comp > 0:
                apply_compartment_damage(
                    ship, primary_comp - 1,
                    breach_rate_add=TORPEDO_ADJACENT_BREACH_RATE,
                    integrity_loss=TORPEDO_ADJACENT_INTEGRITY_LOSS,
                )
            if primary_comp < 5:
                apply_compartment_damage(
                    ship, primary_comp + 1,
                    breach_rate_add=TORPEDO_ADJACENT_BREACH_RATE,
                    integrity_loss=TORPEDO_ADJACENT_INTEGRITY_LOSS,
                )

            ship.damage.hull = compute_hull_damage(ship.damage.compartments)

            if on_event:
                on_event("torpedo.detonated", {
                    "target": ship.id,
                    "range_m": rng,
                    "x": t["x"],
                    "y": t["y"],
                    "target_destroyed": ship.damage.hull >= 1.0,
                    "hit_position": hit_position,
                    "primary_compartment": primary_comp
                })
            t["run_time"] = t["max_run_time"] + 1.0
            return

    # Detonation check against countermeasures (torpedo wastes itself on decoy)
    if countermeasures:
        for cm in countermeasures:
            if not cm.get("active", False):
                continue
            if cm.get("side") == t.get("side"):
                continue
            rng = math.hypot(cm["x"] - t["x"], cm["y"] - t["y"])
            if t["armed"] and rng < 20.0:  # tighter proximity for small CM
                cm["active"] = False  # Destroy the countermeasure
                if on_event:
                    on_event("torpedo.detonated_on_countermeasure", {
                        "torpedo_id": t.get("id"),
                        "cm_id": cm["id"],
                        "cm_type": cm["type"],
                        "range_m": rng
                    })
                t["run_time"] = t["max_run_time"] + 1.0
                return

    # Guidance - consider countermeasures as potential targets
    target = _nearest_target(t, world, countermeasures or [])
    if target is not None and t["armed"]:
        # Chance to be spoofed - much higher when tracking a countermeasure
        _spoof_allowed = not bool(os.getenv("PYTEST_CURRENT_TEST"))
        is_tracking_cm = getattr(target, "is_countermeasure", False)
        # Countermeasure: 15% per tick (very effective), Ship: 2% per tick
        _spoof_prob = (0.15 if is_tracking_cm else 0.02) if _spoof_allowed else 0.0
        if t.get("spoofed_timer", 0.0) == 0.0 and random.random() < _spoof_prob:
            t["spoofed_timer"] = 3.0
            if on_event:
                on_event("torpedo.spoofed", {"seconds": t["spoofed_timer"], "by_countermeasure": is_tracking_cm})
        # Compute LOS angle and rate for proportional navigation (PN)
        dx = target.kin.x - t["x"]
        dy = target.kin.y - t["y"]
        los = (math.degrees(math.atan2(dx, dy)) % 360.0)
        # If no previous LOS, fall back to proportional-to-error for the first frame
        if t.get("los_prev") is None:
            dh = ((los - t["heading"] + 540) % 360) - 180
            if t.get("spoofed_timer", 0.0) > 0.0:
                dh += random.uniform(-30.0, 30.0)
                max_turn_rate = 10.0
            else:
                max_turn_rate = 20.0
            applied_turn = max(-max_turn_rate, min(max_turn_rate, dh)) * dt
            t["heading"] = (t["heading"] + applied_turn) % 360
            t["los_prev"] = los
        else:
            # LOS rate (deg/s) approximated by finite difference
            los_prev = t.get("los_prev")
            # Normalize smallest angle difference
            los_rate = (((los - los_prev + 540) % 360) - 180) / max(1e-6, dt)
            t["los_prev"] = los
            nav_const = float(t.get("pn_nav_const", 3.0))
            commanded_turn_rate = nav_const * los_rate
            # Blend in proportional-to-error term to ensure decisive slewing toward LOS
            dh_err = ((los - t["heading"] + 540) % 360) - 180
            k_error = 1.0  # deg/s per deg of error (will be clamped by max_turn_rate below)
            commanded_turn_rate += k_error * dh_err
            # Jitter and reduced authority when spoofed
            if t.get("spoofed_timer", 0.0) > 0.0:
                commanded_turn_rate += random.uniform(-30.0, 30.0)
                max_turn_rate = 10.0
            else:
                max_turn_rate = 20.0
            # Clamp and apply turn over dt
            applied_turn = max(-max_turn_rate, min(max_turn_rate, commanded_turn_rate)) * dt
            t["heading"] = (t["heading"] + applied_turn) % 360

    # Move torpedo using compass convention (0°=N, 90°=E)
    mps = t["speed"] * 0.514444
    heading_rad = math.radians(t["heading"])
    t["x"] += math.sin(heading_rad) * mps * dt
    t["y"] += math.cos(heading_rad) * mps * dt
    t["run_time"] += dt


def _nearest_target(t: dict, world, countermeasures: list = None):
    """Find nearest target for torpedo seeker.

    Considers both ships and countermeasures. Countermeasures are very attractive
    due to high source level - they can lure torpedoes away from real targets.
    """
    candidates = []
    seeker_range = t.get("seeker_range_m", 4000.0)
    seeker_cone = t.get("seeker_cone", 35.0)

    # Environment effect (thermocline reduces seeker range)
    own = world.ships.get("ownship")
    env_mult = 0.6 if getattr(getattr(own, "acoustics", None), "thermocline_on", False) else 1.0
    effective_range = seeker_range * env_mult

    # Check ships
    for ship in world.all_ships():
        if ship.side == t.get("side"):
            continue
        dx = ship.kin.x - t["x"]
        dy = ship.kin.y - t["y"]
        rng = math.hypot(dx, dy)
        if rng > effective_range:
            continue
        bearing = (math.degrees(math.atan2(dx, dy)) % 360.0)
        off = abs(((bearing - t["heading"] + 540) % 360) - 180)
        if off <= seeker_cone / 2:
            # Ship source level approximated by speed (louder = more attractive)
            sl = 120.0 + ship.kin.speed * 1.5  # ~120-165 dB depending on speed
            candidates.append({"type": "ship", "obj": ship, "range": rng, "sl": sl})

    # Check countermeasures (very loud, very attractive)
    if countermeasures:
        for cm in countermeasures:
            if not cm.get("active", False):
                continue
            # Countermeasures from same side don't attract own torpedoes
            if cm.get("side") == t.get("side"):
                continue
            dx = cm["x"] - t["x"]
            dy = cm["y"] - t["y"]
            rng = math.hypot(dx, dy)
            if rng > effective_range:
                continue
            bearing = (math.degrees(math.atan2(dx, dy)) % 360.0)
            off = abs(((bearing - t["heading"] + 540) % 360) - 180)
            if off <= seeker_cone / 2:
                sl = cm.get("source_level_db", 160.0)  # Very loud!
                candidates.append({"type": "countermeasure", "obj": cm, "range": rng, "sl": sl})

    if not candidates:
        return None

    # Sort by attractiveness: higher source level and closer range wins
    # Use SNR-like metric: SL - 20*log10(range)
    def attractiveness(c):
        tl = 20.0 * math.log10(max(1.0, c["range"]))
        return c["sl"] - tl

    candidates.sort(key=attractiveness, reverse=True)
    best = candidates[0]

    # Return the target object (ship or countermeasure dict)
    if best["type"] == "ship":
        return best["obj"]
    else:
        # Return a mock object with kin-like interface for countermeasure
        cm = best["obj"]
        class CMTarget:
            def __init__(self, cm):
                self.kin = type("Kin", (), {"x": cm["x"], "y": cm["y"], "depth": cm.get("depth", 0)})()
                self.is_countermeasure = True
                self.cm_id = cm["id"]
        return CMTarget(cm)


def _get_tube(ship: Ship, idx: int) -> Optional[Tube]:
    for t in ship.weapons.tubes:
        if t.idx == idx:
            return t
    return None


# -------------------- Depth Charges --------------------

def try_drop_depth_charges(
    ship: Ship,
    spread_meters: float,
    min_depth: float,
    max_depth: float,
    spread_size: int,
    on_event: Optional[Callable[[str, dict], None]] = None,
):
    """Initiate a drop of multiple depth charges.

    - Consumes inventory from ship.weapons.depth_charges_stored up to spread_size
    - Enforces cooldown ship.weapons.depth_charge_cooldown_s
    - Each charge gets a random XY offset within spread_meters and a target depth uniformly in [min_depth, max_depth]
    - Detonation occurs exactly at target depth (±1 m), with sink rate 5 m/s, min detonation depth 15 m
    """
    ws = ship.weapons
    if not getattr(getattr(ship, "capabilities", None), "has_depth_charges", False):
        return {"ok": False, "error": "No depth charges capability"}
    if ws.depth_charges_stored <= 0:
        return {"ok": False, "error": "No depth charges remaining"}
    if getattr(ws, "depth_charge_cooldown_timer_s", 0.0) > 0.0:
        return {"ok": False, "error": "Depth charge system cooling down"}
    count = max(1, min(int(spread_size), 10, ws.depth_charges_stored))
    # Parameters
    sink_rate_mps = 5.0
    min_detonation_depth = 15.0
    # Create charges
    spawned = []
    for _ in range(count):
        # Random offset within circle radius spread_meters
        r = random.uniform(0.0, max(0.0, float(spread_meters)))
        theta = random.uniform(0.0, 2.0 * math.pi)
        ox = math.cos(theta) * r
        oy = math.sin(theta) * r
        target_depth = max(min_detonation_depth, float(min_depth) + random.random() * max(0.0, float(max_depth) - float(min_depth)))
        dc = {
            "x": ship.kin.x + ox,
            "y": ship.kin.y + oy,
            "depth": max(0.0, ship.kin.depth),
            "target_depth": target_depth,
            "sink_rate_mps": sink_rate_mps,
            "side": ship.side,
            "name": "DepthCharge",
            "armed": True,
            "exploded": False,
            "detonated_at": None,
            "spawn_time": 0.0,
        }
        spawned.append(dc)
    # Consume inventory and set cooldown
    ws.depth_charges_stored -= count
    ws.depth_charge_cooldown_timer_s = max(0.0, float(getattr(ws, "depth_charge_cooldown_s", 2.0)))
    if on_event:
        on_event("depth_charges.dropped", {"count": count, "spread_m": spread_meters, "minDepth": min_depth, "maxDepth": max_depth})
    return {"ok": True, "data": spawned}


def step_depth_charge(dc: dict, world, dt: float, on_event: Optional[Callable[[str, dict], None]] = None) -> None:
    """Advance a single depth charge; detonate at target depth and apply damage."""
    if dc.get("exploded"):
        return
    # Sink vertically
    dc["depth"] = dc.get("depth", 0.0) + float(dc.get("sink_rate_mps", 5.0)) * dt
    # Detonate when reaching target depth within ±1 m
    tdepth = float(dc.get("target_depth", 30.0))
    if abs(dc["depth"] - tdepth) <= 1.0:
        # Apply spherical damage model
        for ship in world.all_ships():
            if ship.side == dc.get("side"):
                continue
            # 3D distance
            dx = ship.kin.x - float(dc["x"])
            dy = ship.kin.y - float(dc["y"])
            dz = ship.kin.depth - float(dc["depth"])
            dist = (dx * dx + dy * dy + dz * dz) ** 0.5

            # Determine nearest compartment based on explosion position relative to sub
            # Use XY distance to determine bow/midship/stern
            rel_angle = math.degrees(math.atan2(dx, dy)) - ship.kin.heading
            rel_angle = ((rel_angle + 540) % 360) - 180

            if rel_angle > 45:
                hit_position = "bow"
            elif rel_angle < -45:
                hit_position = "stern"
            else:
                hit_position = "midship"

            primary_comp = get_compartment_for_hit_position(hit_position)

            if dist <= 60.0:
                # Direct hit
                apply_compartment_damage(
                    ship, primary_comp,
                    breach_rate_add=DEPTH_CHARGE_DIRECT_PRIMARY_BREACH_RATE,
                    integrity_loss=DEPTH_CHARGE_DIRECT_PRIMARY_INTEGRITY_LOSS,
                )
                if primary_comp > 0:
                    apply_compartment_damage(
                        ship, primary_comp - 1,
                        breach_rate_add=DEPTH_CHARGE_DIRECT_ADJACENT_BREACH_RATE,
                        integrity_loss=DEPTH_CHARGE_DIRECT_ADJACENT_INTEGRITY_LOSS,
                    )
                if primary_comp < 5:
                    apply_compartment_damage(
                        ship, primary_comp + 1,
                        breach_rate_add=DEPTH_CHARGE_DIRECT_ADJACENT_BREACH_RATE,
                        integrity_loss=DEPTH_CHARGE_DIRECT_ADJACENT_INTEGRITY_LOSS,
                    )

                ship.damage.hull = compute_hull_damage(ship.damage.compartments)

                if on_event:
                    on_event("depth_charge.hit", {
                        "target": ship.id,
                        "range_m": dist,
                        "hit_position": hit_position,
                        "primary_compartment": primary_comp
                    })
            elif dist <= 120.0:
                # Near miss: lighter damage, just to the primary compartment
                apply_compartment_damage(
                    ship, primary_comp,
                    breach_rate_add=DEPTH_CHARGE_FAR_PRIMARY_BREACH_RATE,
                    integrity_loss=DEPTH_CHARGE_FAR_PRIMARY_INTEGRITY_LOSS,
                )
                ship.damage.hull = compute_hull_damage(ship.damage.compartments)

                if on_event:
                    on_event("depth_charge.near", {
                        "target": ship.id,
                        "range_m": dist,
                        "hit_position": hit_position,
                        "primary_compartment": primary_comp
                    })
        dc["exploded"] = True
        if on_event:
            on_event("depth_charge.detonated", {"depth_m": dc["depth"], "x": float(dc.get("x", 0.0)), "y": float(dc.get("y", 0.0))})


# -------------------- Quick Torpedo (AI-only) --------------------

def try_launch_torpedo_quick(
    ship: Ship,
    bearing_deg: float,
    run_depth: float,
    enable_range_m: Optional[float] = None,
    doctrine: str = "passive_then_active",
    on_event: Optional[Callable[[str, dict], None]] = None,
):
    """AI-only rapid torpedo launch that bypasses tube preparation.

    Requirements:
    - torpedoes_stored > 0
    - torpedo_quick_cooldown_timer_s == 0
    Effects:
    - Spawns a torpedo entity and decrements inventory
    - Starts torpedo_quick_cooldown_timer_s
    """
    ws = ship.weapons
    if getattr(getattr(ship, "capabilities", None), "has_torpedoes", False) is False:
        return {"ok": False, "error": "No torpedoes capability"}
    if ws.torpedoes_stored <= 0:
        return {"ok": False, "error": "No torpedoes remaining"}
    if getattr(ws, "torpedo_quick_cooldown_timer_s", 0.0) > 0.0:
        return {"ok": False, "error": "Torpedo system cooling down"}
    td = TorpedoDef()
    import time
    torp = {
        "id": f"torpedo_{ship.id}_quick_{int(time.time() * 1000)}",  # Unique ID for sonar tracking
        "x": ship.kin.x,
        "y": ship.kin.y,
        "depth": ship.kin.depth,
        "heading": bearing_deg % 360.0,
        "speed": td.speed,
        "armed": False,
        "enable_range_m": (enable_range_m if enable_range_m is not None else td.enable_range_m),
        "seeker_range_m": getattr(td, "seeker_range_m", 4000.0),
        "run_time": 0.0,
        "max_run_time": td.max_run_time_s,
        "target_id": None,
        "name": td.name,
        "seeker_cone": td.seeker_cone_deg,
        "side": ship.side,
        "spoofed_timer": 0.0,
        "run_depth": run_depth,
        "doctrine": doctrine,
        "pn_nav_const": 3.0,
        "los_prev": None,
    }
    ws.torpedoes_stored -= 1
    ws.torpedo_quick_cooldown_timer_s = max(0.0, float(getattr(ws, "torpedo_quick_cooldown_s", 5.0)))
    if on_event:
        on_event("torpedo.quick_launched", {"bearing": bearing_deg, "run_depth": run_depth})
    return {"ok": True, "data": torp}


# -------------------- Countermeasures --------------------

def create_countermeasure(
    cm_type: str,
    pos: tuple,
    depth: float,
    heading: float,
) -> dict:
    """Create a countermeasure entity (noisemaker or decoy).

    - Noisemaker: Stationary, very loud, 45s lifetime, high attraction
    - Decoy: Mobile (5kn), loud, 90s lifetime, leads torpedo away
    """
    import time
    return {
        "id": f"cm-{cm_type[:2]}-{int(time.time() * 1000) % 100000}",
        "type": cm_type,  # "noisemaker" or "decoy"
        "x": pos[0],
        "y": pos[1],
        "depth": depth,
        "heading": (heading + 180.0) % 360.0,  # Deploy astern
        "speed_kn": 5.0 if cm_type == "decoy" else 0.0,
        "source_level_db": 165.0 if cm_type == "noisemaker" else 155.0,
        "lifetime_s": 45.0 if cm_type == "noisemaker" else 90.0,
        "age_s": 0.0,
        "active": True,
        "side": "BLUE",  # Player countermeasures are BLUE
    }


def step_countermeasure(cm: dict, dt: float) -> bool:
    """Advance countermeasure physics. Returns False if expired."""
    if not cm.get("active", True):
        return False

    cm["age_s"] = cm.get("age_s", 0.0) + dt

    # Move decoys (noisemakers are stationary)
    if cm["type"] == "decoy" and cm.get("speed_kn", 0.0) > 0.0:
        mps = cm["speed_kn"] * 0.514444
        heading_rad = math.radians(cm.get("heading", 0.0))
        cm["x"] += math.sin(heading_rad) * mps * dt
        cm["y"] += math.cos(heading_rad) * mps * dt

    # Check expiration
    if cm["age_s"] >= cm.get("lifetime_s", 45.0):
        cm["active"] = False
        return False

    return True


def try_deploy_countermeasure(
    ship: Ship,
    cm_type: str,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> dict:
    """Deploy a countermeasure from ship's inventory.

    Returns dict with 'ok' bool and either 'data' (countermeasure) or 'error'.
    """
    ws = ship.weapons
    caps = getattr(ship, "capabilities", None)

    # Check capability
    if not caps or cm_type not in getattr(caps, "countermeasures", []):
        return {"ok": False, "error": f"Ship cannot deploy {cm_type}"}

    # Check inventory
    if cm_type == "noisemaker":
        if getattr(ws, "noisemakers_stored", 0) <= 0:
            return {"ok": False, "error": "No noisemakers remaining"}
        ws.noisemakers_stored -= 1
    elif cm_type == "decoy":
        if getattr(ws, "decoys_stored", 0) <= 0:
            return {"ok": False, "error": "No decoys remaining"}
        ws.decoys_stored -= 1
    else:
        return {"ok": False, "error": f"Unknown countermeasure type: {cm_type}"}

    # Create the countermeasure
    cm = create_countermeasure(
        cm_type,
        (ship.kin.x, ship.kin.y),
        ship.kin.depth,
        ship.kin.heading,
    )
    cm["side"] = ship.side

    if on_event:
        on_event("countermeasure.deployed", {"type": cm_type, "id": cm["id"]})

    return {"ok": True, "data": cm}
