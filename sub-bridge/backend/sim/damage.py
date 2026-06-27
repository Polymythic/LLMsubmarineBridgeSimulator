from __future__ import annotations
from ..models import Ship, COMPARTMENT_NAMES

# Pump configuration
PUMP_COUNT = 2  # Number of assignable pumps
PUMP_RATE = 0.15  # Flooding reduction per second per pump

# Flooding mechanics
# Healing is slow — a torpedo breach (0.6 rate) takes ~3 minutes to seal even
# with damage control. This is intentional: a torpedo hit should keep flooding
# until pumps are assigned and damage control has time to work.
BREACH_HEALING_RATE = 0.003  # Breach rate heals per second
FLOOD_SPREAD_THRESHOLD = 0.5  # Flooding must exceed this to spread to neighbors
FLOOD_SPREAD_RATE_FACTOR = 0.05  # Spread rate = factor * (source_level - threshold)


def step_damage(ship: Ship, dt: float, pump_assignments: dict = None) -> dict:
    """Process all compartment flooding, breach healing, and pump effects.

    Args:
        ship: The ship to process damage for
        dt: Delta time in seconds
        pump_assignments: Dict mapping pump number (1 or 2) to compartment index (0-5)
                         e.g., {1: 0, 2: 5} means pump 1 on fore, pump 2 on stern

    Returns:
        Dict with system failure states based on compartment flooding
    """
    if pump_assignments is None:
        pump_assignments = {}

    compartments = ship.damage.compartments

    # Reset pump_active flags and set based on assignments
    for comp in compartments:
        comp.pump_active = False

    # Mark compartments that have pumps assigned
    pumped_compartments = set()
    for pump_num, comp_idx in pump_assignments.items():
        if 0 <= comp_idx < len(compartments):
            compartments[comp_idx].pump_active = True
            pumped_compartments.add(comp_idx)

    # Process each compartment
    for i, comp in enumerate(compartments):
        # 1. Water ingress from breaches
        if comp.breach_rate > 0:
            comp.flooding_level = min(1.0, comp.flooding_level + comp.breach_rate * dt)

        # 2. Breach healing (damage control teams working)
        if comp.breach_rate > 0:
            comp.breach_rate = max(0.0, comp.breach_rate - BREACH_HEALING_RATE * dt)

        # 3. Pump effect (if assigned)
        if comp.pump_active and comp.flooding_level > 0:
            comp.flooding_level = max(0.0, comp.flooding_level - PUMP_RATE * dt)

    # 4. Flooding spread between adjacent compartments
    _process_flood_spread(compartments, dt)

    # 5. Update overall hull damage. Naval combat is not "average integrity"
    # — losing one compartment outright is far worse than scattered damage.
    # Formula: dominated by the worst compartment + count of destroyed ones.
    ship.damage.hull = compute_hull_damage(compartments)

    # 6. Calculate system failures based on compartment flooding
    system_failures = _calculate_system_failures(compartments)

    return system_failures


def compute_hull_damage(compartments: list) -> float:
    """Aggregate compartment damage into a single hull-damage value [0, 1].

    A ship is destroyed when any combination of:
      * 2+ compartments are fully destroyed (keel break);
      * a critical compartment (engine or reactor) is destroyed AND fully
        flooded (catastrophic loss); or
      * the worst compartment plus distributed damage exceeds the threshold.

    Coefficients tuned so:
      * single torpedo hit (primary 0.85 / adjacent 0.30) → ~0.45 hull damage;
      * two hits in the same area (typically destroying the primary
        compartment) → ~0.95 hull damage / mission-killed;
      * three hits in the same area or any 2 destroyed compartments → 1.0.
    """
    if not compartments:
        return 0.0
    losses = [1.0 - c.hull_integrity for c in compartments]
    max_loss = max(losses)
    avg_loss = sum(losses) / len(compartments)
    # "Effectively destroyed" — anything that has lost ≥ 85% integrity
    # counts. This catches compartments saturated by repeated adjacent
    # damage (e.g., comp at 0.1 from 3x adjacent torpedo hits).
    destroyed = sum(1 for c in compartments if c.hull_integrity <= 0.15)

    # Critical compartment loss (engine or reactor) + fully flooded = doomed.
    critical_indices = (3, 4)  # Aft (Reactor), Engine Room
    critical_lost = any(
        i < len(compartments)
        and compartments[i].hull_integrity <= 0.0
        and compartments[i].flooding_level >= 0.8
        for i in critical_indices
    )
    if critical_lost:
        return 1.0

    hull = 0.4 * destroyed + 0.5 * max_loss + 0.1 * avg_loss
    return max(0.0, min(1.0, hull))


def _process_flood_spread(compartments: list, dt: float) -> None:
    """Handle flooding spread between adjacent compartments."""
    # Calculate spread amounts first, then apply (to avoid order dependency)
    spread_amounts = [0.0] * len(compartments)

    for i, comp in enumerate(compartments):
        if comp.flooding_level > FLOOD_SPREAD_THRESHOLD:
            spread_rate = FLOOD_SPREAD_RATE_FACTOR * (comp.flooding_level - FLOOD_SPREAD_THRESHOLD)

            # Spread to left neighbor
            if i > 0:
                # Spread rate reduced if target is also flooding
                target_factor = max(0.1, 1.0 - compartments[i - 1].flooding_level)
                spread_amounts[i - 1] += spread_rate * target_factor * dt

            # Spread to right neighbor
            if i < len(compartments) - 1:
                target_factor = max(0.1, 1.0 - compartments[i + 1].flooding_level)
                spread_amounts[i + 1] += spread_rate * target_factor * dt

    # Apply spread amounts
    for i, amount in enumerate(spread_amounts):
        compartments[i].flooding_level = min(1.0, compartments[i].flooding_level + amount)


def _calculate_system_failures(compartments: list) -> dict:
    """Calculate system degradation based on compartment flooding levels.

    Returns dict with degradation factors (1.0 = full capability, 0.0 = offline)
    """
    failures = {
        # Compartment 0 - Fore (Torpedo Room)
        "torpedo_loading_factor": 1.0,
        "forward_sonar_factor": 1.0,

        # Compartment 1 - Forward (Crew)
        "crew_efficiency_factor": 1.0,

        # Compartment 2 - Control Room
        "periscope_factor": 1.0,
        "radio_factor": 1.0,
        "navigation_factor": 1.0,

        # Compartment 3 - Aft (Reactor)
        "reactor_factor": 1.0,

        # Compartment 4 - Engine Room
        "propulsion_factor": 1.0,

        # Compartment 5 - Stern (Steering)
        "rudder_factor": 1.0,
    }

    # Compartment 0 - Fore (Torpedo Room)
    fore_level = compartments[0].flooding_level
    if fore_level >= 1.0:
        failures["torpedo_loading_factor"] = 0.0  # Tubes offline
        failures["forward_sonar_factor"] = 0.0
    elif fore_level >= 0.75:
        failures["torpedo_loading_factor"] = 0.5  # Loading slowed 50%
        failures["forward_sonar_factor"] = 0.5

    # Compartment 1 - Forward (Crew)
    crew_level = compartments[1].flooding_level
    if crew_level >= 1.0:
        failures["crew_efficiency_factor"] = 0.0  # Tasks auto-fail
    elif crew_level >= 0.75:
        failures["crew_efficiency_factor"] = 0.5  # Maintenance slowed

    # Compartment 2 - Control Room
    control_level = compartments[2].flooding_level
    if control_level >= 1.0:
        failures["periscope_factor"] = 0.0  # Blind
        failures["radio_factor"] = 0.0  # No comms
        failures["navigation_factor"] = 0.0
    elif control_level >= 0.75:
        failures["periscope_factor"] = 0.5  # Detection range -50%
        failures["radio_factor"] = 0.5
        failures["navigation_factor"] = 0.5

    # Compartment 3 - Aft (Reactor)
    reactor_level = compartments[3].flooding_level
    if reactor_level >= 1.0:
        failures["reactor_factor"] = 0.1  # Emergency power only
    elif reactor_level >= 0.75:
        failures["reactor_factor"] = 0.5  # Max power -50%

    # Compartment 4 - Engine Room
    engine_level = compartments[4].flooding_level
    if engine_level >= 1.0:
        failures["propulsion_factor"] = 0.0  # Propulsion offline
    elif engine_level >= 0.75:
        failures["propulsion_factor"] = 0.5  # Max speed -50%

    # Compartment 5 - Stern (Steering)
    stern_level = compartments[5].flooding_level
    if stern_level >= 1.0:
        failures["rudder_factor"] = 0.0  # Cannot maneuver
    elif stern_level >= 0.75:
        failures["rudder_factor"] = 0.5  # Turn rate -50%

    return failures


def apply_compartment_damage(ship: Ship, compartment_idx: int, breach_rate_add: float, integrity_loss: float) -> None:
    """Apply damage to a specific compartment.

    Args:
        ship: Ship to damage
        compartment_idx: Index of compartment (0-5)
        breach_rate_add: Amount to add to breach rate
        integrity_loss: Amount of hull integrity to lose (0.0-1.0)
    """
    if not 0 <= compartment_idx < len(ship.damage.compartments):
        return

    comp = ship.damage.compartments[compartment_idx]
    comp.breach_rate = min(1.0, comp.breach_rate + breach_rate_add)
    comp.hull_integrity = max(0.0, comp.hull_integrity - integrity_loss)


def get_compartment_for_hit_position(hit_position: str) -> int:
    """Determine which compartment was hit based on position description.

    Args:
        hit_position: "bow", "midship", or "stern"

    Returns:
        Primary compartment index (0-5)
    """
    import random

    if hit_position == "bow":
        return random.choice([0, 1])  # Fore or Forward
    elif hit_position == "midship":
        return random.choice([2, 3])  # Control or Reactor
    else:  # stern
        return random.choice([4, 5])  # Engine or Stern


def step_engineering(ship: Ship, dt: float) -> None:
    """Process engineering systems (power, maintenance, etc.)"""
    # SCRAM reduces available reactor power
    if ship.reactor.scrammed:
        ship.reactor.output_mw = min(ship.reactor.output_mw, 10.0)

    # Shared power budget allocations (fractions sum ~1.0)
    alloc = ship.power
    total_mw = max(0.0, min(ship.reactor.max_mw, ship.reactor.output_mw))
    mw_propulsion = total_mw * max(0.0, min(1.0, alloc.helm))
    mw_sensors = total_mw * max(0.0, min(1.0, alloc.sonar))
    mw_weapons = total_mw * max(0.0, min(1.0, alloc.weapons))
    mw_engineering = total_mw * max(0.0, min(1.0, alloc.engineering))

    # Apply hull damage effects to sonar performance
    hull_damage_factor = max(0.1, 1.0 - ship.damage.hull)
    damage_sensors_factor = max(0.2, hull_damage_factor)

    # Apply damage effects to sonar performance
    ship.acoustics.passive_snr_penalty_db = max(0.0, ship.acoustics.passive_snr_penalty_db + (1.0 - damage_sensors_factor) * 15.0)
    ship.acoustics.bearing_noise_extra = max(0.0, ship.acoustics.bearing_noise_extra + (1.0 - damage_sensors_factor) * 5.0)
    ship.acoustics.active_range_noise_add_m = max(0.0, ship.acoustics.active_range_noise_add_m + (1.0 - damage_sensors_factor) * 200.0)
    ship.acoustics.active_bearing_noise_extra = max(0.0, ship.acoustics.active_bearing_noise_extra + (1.0 - damage_sensors_factor) * 2.0)

    # Apply hull damage effects to weapons performance
    hull_damage_factor = max(0.3, 1.0 - ship.damage.hull)
    damage_weapons_factor = max(0.5, hull_damage_factor)

    ship.weapons.reload_time_s = 45.0 / max(0.2, (mw_weapons / max(1.0, ship.reactor.max_mw))) / damage_weapons_factor
    ship.weapons.flood_time_s = 8.0 / max(0.2, (mw_weapons / max(1.0, ship.reactor.max_mw))) / damage_weapons_factor
    ship.weapons.doors_time_s = 3.0 / max(0.2, (mw_weapons / max(1.0, ship.reactor.max_mw))) / damage_weapons_factor

    # Maintenance progression/decay
    maint = ship.maintenance.levels
    prog_rate = (mw_engineering / max(1.0, ship.reactor.max_mw)) * 0.1
    decay_rate = 0.01
    for key in list(maint.keys()):
        if mw_engineering > 0.1:
            maint[key] = min(1.0, maint[key] + prog_rate * dt)
        else:
            maint[key] = max(0.0, maint[key] - decay_rate * dt)

    # Battery drain proportional to propulsion demand
    speed_factor = max(0.0, min(1.0, ship.kin.speed / max(1.0, ship.hull.max_speed)))
    drain_rate = 1.0 * speed_factor
    ship.reactor.battery_pct = max(0.0, ship.reactor.battery_pct - (drain_rate / 60.0) * dt)
    if ship.reactor.scrammed and ship.reactor.battery_pct <= 0.0:
        ship.reactor.output_mw = 0.0

    # System failures if maintenance too low
    ship.systems.rudder_ok = ship.maintenance.levels.get("rudder", 1.0) > 0.2
    ship.systems.ballast_ok = ship.maintenance.levels.get("ballast", 1.0) > 0.2
    ship.systems.planes_ok = ship.maintenance.levels.get("planes", 1.0) > 0.2
    ship.systems.sonar_ok = ship.maintenance.levels.get("sonar", 1.0) > 0.2
    ship.systems.radio_ok = ship.maintenance.levels.get("radio", 1.0) > 0.2
    ship.systems.periscope_ok = ship.maintenance.levels.get("periscope", 1.0) > 0.2
    ship.systems.tubes_ok = ship.maintenance.levels.get("tubes", 1.0) > 0.2
