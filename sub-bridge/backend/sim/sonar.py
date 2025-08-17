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
        # Compass bearing: 0=N, 90=E, 180=S, 270=W
        brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
        rel = angle_diff(brg, self_ship.kin.heading)
        if abs(rel) > 180 - BAFFLES_DEG / 2:
            continue
        # Source level from target class/speed (fallback to default curve), with surface/periscope penalties
        speed_key = min(sorted(other.acoustics.source_level_by_speed.keys()), key=lambda k: abs(k - abs(other.kin.speed)))
        src_lvl = other.acoustics.source_level_by_speed.get(speed_key, 110.0)
        # If target is at/near surface, increase detectability due to wave slap/exhaust
        if other.kin.depth <= 1.0:
            src_lvl += 6.0
        # If periscope or radio mast up on other side (if flags exist on that ship), add small penalty
        mast_bonus = 0.0
        if hasattr(other, "_periscope_raised") and getattr(other, "_periscope_raised"):
            mast_bonus += 2.0
        if hasattr(other, "_radio_raised") and getattr(other, "_radio_raised"):
            mast_bonus += 2.0
        src_lvl += mast_bonus
        # Transmission loss with simple absorption toggle via thermocline
        tl_geo = 20.0 * math.log10(max(1.0, rng))
        layer_atten = 4.0 if self_ship.acoustics.thermocline_on else 0.0
        tl = tl_geo + layer_atten
        ambient = 60.0
        # Apply passive SNR penalty from degraded systems
        penalty = getattr(self_ship.acoustics, "passive_snr_penalty_db", 0.0)
        snr_db = max(0.0, src_lvl - tl - ambient - penalty)
        # Detectability soft-knee mapping to 0..1
        detect = max(0.0, min(1.0, snr_db / 30.0))
        # Gate very weak signals: hide from UI and mark bearing/range unknown
        if detect < 0.15:
            continue
        # Bearing error grows as target slows (harder to localize) and with ownship degradation
        sigma = max(1.0, 10.0 - other.kin.speed * 0.3 + self_ship.acoustics.bearing_noise_extra)
        noisy_bearing = normalize_angle_deg(brg + random.gauss(0, sigma))
        confidence = min(1.0, detect * 1.2)
        # Store last computed detectability on target for debug use (optional)
        other.acoustics.last_snr_db = snr_db
        other.acoustics.last_detectability = detect
        
        # Realistic classification based on signal quality and ship characteristics
        classified_as = _classify_ship_passive(other, detect, snr_db, rng)
        
        contacts.append(
            TelemetryContact(
                id=other.id,
                bearing=noisy_bearing,
                strength=detect,
                classifiedAs=classified_as,
                confidence=confidence,
                bearingKnown=True,
                rangeKnown=False,
                detectability=detect,
                snrDb=snr_db,
                bearingSigmaDeg=sigma,
            )
        )
    return contacts


def passive_projectiles(self_ship: Ship, torpedoes: List[dict] | None, depth_charges: List[dict] | None) -> List[TelemetryContact]:
    """Render torpedoes and depth charges as passive contacts for sonar UI.

    - Uses a simplified source level model for moving torpedoes and sinking depth charges
    - Applies same baffles and transmission loss as ships
    - Classifies as "Torpedo?" or "DepthCharge?" with confidence scaled by detectability
    """
    if getattr(self_ship, "systems", None) is not None and not self_ship.systems.sonar_ok:
        return []
    contacts: List[TelemetryContact] = []
    torps = torpedoes or []
    dcs = depth_charges or []
    # Common environment terms
    ambient = 60.0
    layer_atten = 4.0 if self_ship.acoustics.thermocline_on else 0.0
    for t in torps:
        try:
            tx = float(t.get("x", 0.0)); ty = float(t.get("y", 0.0))
            dx = tx - self_ship.kin.x; dy = ty - self_ship.kin.y
            rng = math.hypot(dx, dy)
            brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
            rel = angle_diff(brg, self_ship.kin.heading)
            if abs(rel) > 180 - BAFFLES_DEG / 2:
                continue
            speed = float(t.get("speed", 35.0))
            src_lvl = 145.0 + 0.2 * speed  # loud propulsors
            tl_geo = 20.0 * math.log10(max(1.0, rng))
            tl = tl_geo + layer_atten
            penalty = getattr(self_ship.acoustics, "passive_snr_penalty_db", 0.0)
            snr_db = max(0.0, src_lvl - tl - ambient - penalty)
            detect = max(0.0, min(1.0, snr_db / 30.0))
            if detect < 0.12:
                continue
            sigma = max(0.8, 6.0 - 0.05 * speed)
            noisy_bearing = normalize_angle_deg(brg + random.gauss(0, sigma))
            confidence = min(1.0, detect * 1.3)
            tid = t.get("id") or f"torpedo_{int(tx)}_{int(ty)}"
            contacts.append(TelemetryContact(
                id=str(tid),
                bearing=noisy_bearing,
                strength=detect,
                classifiedAs="Torpedo?",
                confidence=confidence,
                bearingKnown=True,
                rangeKnown=False,
                detectability=detect,
                snrDb=snr_db,
                bearingSigmaDeg=sigma,
            ))
        except Exception:
            continue
    for dc in dcs:
        try:
            dx = float(dc.get("x", 0.0)) - self_ship.kin.x
            dy = float(dc.get("y", 0.0)) - self_ship.kin.y
            rng = math.hypot(dx, dy)
            brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
            rel = angle_diff(brg, self_ship.kin.heading)
            if abs(rel) > 180 - BAFFLES_DEG / 2:
                continue
            # Depth charges are moderately loud while sinking; louder initially
            sink_rate = float(dc.get("sink_rate_mps", 5.0))
            src_lvl = 125.0 + 2.0 * min(5.0, sink_rate)
            tl_geo = 20.0 * math.log10(max(1.0, rng))
            tl = tl_geo + layer_atten
            penalty = getattr(self_ship.acoustics, "passive_snr_penalty_db", 0.0)
            snr_db = max(0.0, src_lvl - tl - ambient - penalty)
            detect = max(0.0, min(1.0, snr_db / 30.0))
            if detect < 0.1:
                continue
            sigma = 8.0
            noisy_bearing = normalize_angle_deg(brg + random.gauss(0, sigma))
            confidence = min(1.0, detect * 1.1)
            dcid = dc.get("id") or f"depth_charge_{int(dc.get('x',0))}_{int(dc.get('y',0))}"
            contacts.append(TelemetryContact(
                id=str(dcid),
                bearing=noisy_bearing,
                strength=detect,
                classifiedAs="DepthCharge?",
                confidence=confidence,
                bearingKnown=True,
                rangeKnown=False,
                detectability=detect,
                snrDb=snr_db,
                bearingSigmaDeg=sigma,
            ))
        except Exception:
            continue
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
        # Compass bearing: 0=N, 90=E, 180=S, 270=W
        brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
        base_rng_noise = max(1.0, rng + random.gauss(0, rng * 0.02 + 5.0))
        rng_noise = base_rng_noise + getattr(self_ship.acoustics, "active_range_noise_add_m", 0.0)
        brg_noise = normalize_angle_deg(
            brg + random.gauss(0, 1.5 + max(0.0, getattr(self_ship.acoustics, "active_bearing_noise_extra", 0.0)))
        )
        # Simple active strength model: stronger when closer; clamp 0..1
        strength = max(0.0, min(1.0, 1.0 / (1.0 + (rng_noise / 2000.0))))
        out.append((other.id, rng_noise, brg_noise, strength))
    return out


def _classify_ship_passive(ship: Ship, detectability: float, snr_db: float, range_m: float) -> str:
    """
    Realistic passive sonar classification based on signal quality and ship characteristics.
    
    Args:
        ship: Target ship
        detectability: Detection strength (0.0 to 1.0)
        snr_db: Signal-to-noise ratio in dB
        range_m: Range to target in meters
    
    Returns:
        Classification string with confidence indicators
    """
    # Base classification from ship class
    base_class = getattr(ship, "ship_class", None)
    
    # Signal quality affects classification confidence
    if detectability >= 0.8 and snr_db >= 25:
        # Strong signal: confident classification
        if base_class == "SSN":
            return "SSN"  # Clear submarine signature
        elif base_class == "Convoy":
            return "Merchant/Convoy"  # Commercial vessel signature
        elif base_class == "Destroyer":
            return "Warship"  # Military vessel signature
        elif base_class is None:
            return "Unknown"  # No class information
        else:
            return base_class
    elif detectability >= 0.6 and snr_db >= 20:
        # Medium signal: probable classification
        if base_class == "SSN":
            return "SSN?"  # Probable submarine
        elif base_class == "Convoy":
            return "Merchant?"  # Probable commercial vessel
        elif base_class == "Destroyer":
            return "Warship?"  # Probable military vessel
        elif base_class is None:
            return "Unknown"  # No class information
        else:
            return f"{base_class}?"
    elif detectability >= 0.4 and snr_db >= 15:
        # Weak signal: possible classification
        if base_class == "SSN":
            return "Submarine?"  # Possible submarine
        elif base_class == "Convoy":
            return "Vessel?"  # Possible vessel
        elif base_class == "Destroyer":
            return "Contact?"  # Possible contact
        elif base_class is None:
            return "Unknown"  # No class information
        else:
            return "Contact?"
    else:
        # Very weak signal: uncertain
        return "Unknown"
