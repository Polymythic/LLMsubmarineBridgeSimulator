from __future__ import annotations
import math
import random
from typing import List, Tuple
from ..models import Ship, TelemetryContact


BAFFLES_DEG = 60.0


def normalize_angle_deg(angle: float) -> float:
    return angle % 360.0


def angle_diff(a: float, b: float) -> float:
    return ((a - b + 540) % 360) - 180


def passive_contacts(self_ship: Ship, others: List[Ship]) -> List[TelemetryContact]:
    # Sonar failure disables passive contact generation
    if getattr(self_ship, "systems", None) is not None and not self_ship.systems.sonar_ok:
        return []
    contacts: List[TelemetryContact] = []
    for other in others:
        if other.id == self_ship.id:
            continue
        dx = other.kin.x - self_ship.kin.x
        dy = other.kin.y - self_ship.kin.y
        rng = math.hypot(dx, dy)
        brg = normalize_angle_deg(math.degrees(math.atan2(dy, dx)))
        rel = angle_diff(brg, self_ship.kin.heading)
        if abs(rel) > 180 - BAFFLES_DEG / 2:
            continue
        speed_key = min(sorted(self_ship.acoustics.source_level_by_speed.keys()), key=lambda k: abs(k - abs(other.kin.speed)))
        src_lvl = other.acoustics.source_level_by_speed.get(speed_key, 110.0)
        tl = 20 * math.log10(max(1.0, rng))
        ambient = 60.0
        snr = max(0.0, src_lvl - tl - ambient)
        strength = max(0.0, min(1.0, snr / 30.0))
        sigma = max(1.0, 10.0 - other.kin.speed * 0.3)
        noisy_bearing = normalize_angle_deg(brg + random.gauss(0, sigma))
        confidence = min(1.0, strength * 1.2)
        contacts.append(
            TelemetryContact(
                id=other.id,
                bearing=noisy_bearing,
                strength=strength,
                classifiedAs="SSN?",
                confidence=confidence,
            )
        )
    return contacts


class ActivePingState:
    def __init__(self, cooldown_s: float = 12.0) -> None:
        self.cooldown_s = cooldown_s
        self.timer = 0.0

    def can_ping(self) -> bool:
        return self.timer <= 0.0

    def tick(self, dt: float) -> None:
        if self.timer > 0.0:
            self.timer -= dt

    def start(self) -> bool:
        if self.can_ping():
            self.timer = self.cooldown_s
            return True
        return False


def active_ping(self_ship: Ship, others: List[Ship]) -> List[Tuple[str, float, float, float]]:
    # Sonar failure prevents active returns
    if getattr(self_ship, "systems", None) is not None and not self_ship.systems.sonar_ok:
        return []
    out: List[Tuple[str, float, float, float]] = []
    for other in others:
        if other.id == self_ship.id:
            continue
        dx = other.kin.x - self_ship.kin.x
        dy = other.kin.y - self_ship.kin.y
        rng = math.hypot(dx, dy)
        brg = normalize_angle_deg(math.degrees(math.atan2(dy, dx)))
        rng_noise = max(1.0, rng + random.gauss(0, rng * 0.02 + 5.0))
        brg_noise = normalize_angle_deg(brg + random.gauss(0, 1.5))
        # Simple active strength model: stronger when closer; clamp 0..1
        strength = max(0.0, min(1.0, 1.0 / (1.0 + (rng_noise / 2000.0))))
        out.append((other.id, rng_noise, brg_noise, strength))
    return out
