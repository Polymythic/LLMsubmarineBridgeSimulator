from __future__ import annotations
from ..models import Ship


def step_damage(ship: Ship, dt: float, pump_effect: float = 0.0) -> None:
    # Flooding decays with pumps; otherwise persists
    if ship.damage.flooding_rate > 0.0:
        ship.damage.flooding_rate = max(0.0, ship.damage.flooding_rate - pump_effect * dt)


def step_engineering(ship: Ship, dt: float) -> None:
    # SCRAM reduces available reactor power and drains battery when moving
    if ship.reactor.scrammed:
        ship.reactor.output_mw = min(ship.reactor.output_mw, 10.0)
    # Drain battery when shaft power required beyond reactor output
    speed_factor = max(0.0, min(1.0, ship.kin.speed / max(1.0, ship.hull.max_speed)))
    # Simple drain proportional to speed
    drain_rate = 1.0 * speed_factor  # % per minute at full speed
    ship.reactor.battery_pct = max(0.0, ship.reactor.battery_pct - (drain_rate / 60.0) * dt)
    # If battery is empty and scrammed, cap reactor output to zero-equivalent
    if ship.reactor.scrammed and ship.reactor.battery_pct <= 0.0:
        ship.reactor.output_mw = 0.0
