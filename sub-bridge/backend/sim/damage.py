from __future__ import annotations
from ..models import Ship


def step_damage(ship: Ship, dt: float, pump_effect: float = 0.0) -> None:
    # Flooding decays with pumps; otherwise persists
    if ship.damage.flooding_rate > 0.0:
        ship.damage.flooding_rate = max(0.0, ship.damage.flooding_rate - pump_effect * dt)


def step_engineering(ship: Ship, dt: float) -> None:
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

    # Effects:
    # - Propulsion MW caps achievable speed handled in physics via reactor.output_mw equivalence.
    # - Sensors MW can throttle sonar update richness (not implemented here; exposed via telemetry).
    # Apply hull damage effects to sonar performance
    hull_damage_factor = max(0.1, 1.0 - ship.damage.hull)  # Sonar heavily affected by hull damage
    damage_sensors_factor = max(0.2, hull_damage_factor)  # Sonar still functional at 20% damage
    
    # Apply damage effects to sonar performance
    ship.acoustics.passive_snr_penalty_db = max(0.0, ship.acoustics.passive_snr_penalty_db + (1.0 - damage_sensors_factor) * 15.0)
    ship.acoustics.bearing_noise_extra = max(0.0, ship.acoustics.bearing_noise_extra + (1.0 - damage_sensors_factor) * 5.0)
    ship.acoustics.active_range_noise_add_m = max(0.0, ship.acoustics.active_range_noise_add_m + (1.0 - damage_sensors_factor) * 200.0)
    ship.acoustics.active_bearing_noise_extra = max(0.0, ship.acoustics.active_bearing_noise_extra + (1.0 - damage_sensors_factor) * 2.0)
    # - Weapons MW affects tube timers (scale timers by power factor).
    # - Engineering MW accelerates maintenance countdowns.
    # Apply hull damage effects to weapons performance
    hull_damage_factor = max(0.3, 1.0 - ship.damage.hull)  # Weapons less affected by hull damage
    damage_weapons_factor = max(0.5, hull_damage_factor)  # Weapons still functional at 50% damage
    
    ship.weapons.reload_time_s = 45.0 / max(0.2, (mw_weapons / max(1.0, ship.reactor.max_mw))) / damage_weapons_factor
    ship.weapons.flood_time_s = 8.0 / max(0.2, (mw_weapons / max(1.0, ship.reactor.max_mw))) / damage_weapons_factor
    ship.weapons.doors_time_s = 3.0 / max(0.2, (mw_weapons / max(1.0, ship.reactor.max_mw))) / damage_weapons_factor

    # Maintenance progression/decay
    maint = ship.maintenance.levels
    # Progress proportional to engineering power; decay when neglected
    prog_rate = (mw_engineering / max(1.0, ship.reactor.max_mw)) * 0.1  # per second toward 1.0
    decay_rate = 0.01  # per second toward 0.0 if neglected (baseline wear)
    for key in list(maint.keys()):
        # If some power to engineering, progress; else slight decay
        if mw_engineering > 0.1:
            maint[key] = min(1.0, maint[key] + prog_rate * dt)
        else:
            maint[key] = max(0.0, maint[key] - decay_rate * dt)

    # Battery drain proportional to propulsion demand beyond reactor output (simple model)
    speed_factor = max(0.0, min(1.0, ship.kin.speed / max(1.0, ship.hull.max_speed)))
    drain_rate = 1.0 * speed_factor  # % per minute at full speed
    ship.reactor.battery_pct = max(0.0, ship.reactor.battery_pct - (drain_rate / 60.0) * dt)
    if ship.reactor.scrammed and ship.reactor.battery_pct <= 0.0:
        ship.reactor.output_mw = 0.0

    # System failures if maintenance too low
    ship.systems.rudder_ok = ship.maintenance.levels.get("rudder", 1.0) > 0.2
    ship.systems.ballast_ok = ship.maintenance.levels.get("ballast", 1.0) > 0.2
    ship.systems.sonar_ok = ship.maintenance.levels.get("sonar", 1.0) > 0.2
    ship.systems.radio_ok = ship.maintenance.levels.get("radio", 1.0) > 0.2
    ship.systems.periscope_ok = ship.maintenance.levels.get("periscope", 1.0) > 0.2
    ship.systems.tubes_ok = ship.maintenance.levels.get("tubes", 1.0) > 0.2
