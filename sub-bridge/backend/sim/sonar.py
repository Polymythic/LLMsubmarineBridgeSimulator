from __future__ import annotations
import math
import random
import time
from typing import List, Tuple, Dict, Any, Optional, TYPE_CHECKING
from ..models import Ship, TelemetryContact

if TYPE_CHECKING:
    from .contact_registry import ContactRegistry


BAFFLES_DEG = 60.0
# Torpedo tonal "ID card" (kHz) for the sonar tonal filter. Torpedoes aren't
# catalog ships, so their card is seeded here. Shares 5.0/12.0 kHz with the
# Destroyer card by design (telling an inbound fish from its escort is hard);
# 14.5 kHz is the unique seeker discriminator.
TORPEDO_TONAL_LINES = [2.0, 5.0, 8.8, 12.0, 14.5]
# Per-type torpedo tonal "ID cards" (kHz), keyed by the torpedo's `name`. A fish
# carries the card of its model so the operator can tell, e.g., an inbound
# Soviet 53-65 from a Western Mk48. Deliberate cross-collisions (5.0 with the
# Destroyer card; 8.8/12.0 shared among Western/Soviet fish) keep narrow bands
# ambiguous; each model keeps one high discriminator. Unknown names fall back to
# the Mk48 reference card.
TORPEDO_TONAL_CARDS = {
    "Mk48": TORPEDO_TONAL_LINES,                # Western heavyweight (reference)
    "53-65": [2.4, 5.0, 7.6, 11.2, 14.8],       # Soviet wake-homing 53 cm
    "SET-65": [1.8, 4.2, 8.8, 12.0, 13.0],      # Soviet ASW homing
    "Tigerfish": [2.0, 4.8, 9.0, 11.8, 14.2],   # RN Mk24 Tigerfish
}
# Submarine tonal "ID card" (kHz) — a decoy mimics a sub's signature, so it
# carries these lines and reads like a submarine on the narrowband filter (it
# survives a sub-hunt passband rather than vanishing). Mirrors the SSN card in
# assets/ships/catalog.json; used as a fallback when the observing sub carries
# no card of its own.
SUB_TONAL_LINES = [0.9, 2.1, 4.0, 6.3, 8.5]
CONTACT_PERSISTENCE_SECONDS = 8.0  # How long contacts persist after dropping below threshold
CONTACT_DECAY_RATE = 0.15  # Confidence decay per second when contact is fading
ARRAY_GAIN_DB = 18.0  # Passive sonar array gain from beamforming (typical for spherical/cylindrical array)

# Module-level contact memory: {observer_id: {target_id: {"last_seen": float, "last_confidence": float, "last_bearing": float}}}
_contact_memory: Dict[str, Dict[str, Dict[str, Any]]] = {}


def clear_contact_memory() -> None:
    """Clear all contact memory for mission transitions."""
    _contact_memory.clear()


def normalize_angle_deg(angle: float) -> float:
    return angle % 360.0


def angle_diff(a: float, b: float) -> float:
    return ((a - b + 540) % 360) - 180


def passive_contacts(
    self_ship: Ship,
    others: List[Ship],
    contact_registry: Optional['ContactRegistry'] = None,
    current_time_override: Optional[float] = None,
) -> List[TelemetryContact]:
    """Generate passive sonar contacts with hysteresis to prevent flickering.

    Args:
        self_ship: The observing ship
        others: List of other ships to potentially detect
        contact_registry: Optional registry for anonymous contact designations.
            If provided, contacts will use "Contact-N" IDs and sonar-based classification.
            If None, contacts use actual ship IDs (for backwards compatibility/testing).
        current_time_override: Optional time to use instead of time.time()

    Returns:
        List of detected contacts
    """
    # Sonar failure disables passive contact generation
    if getattr(self_ship, "systems", None) is not None and not self_ship.systems.sonar_ok:
        return []

    # Initialize contact memory for this observer
    observer_id = self_ship.id
    if observer_id not in _contact_memory:
        _contact_memory[observer_id] = {}

    current_time = current_time_override if current_time_override is not None else time.time()
    contacts: List[TelemetryContact] = []
    detected_ids: set = set()  # Track which targets were detected this tick

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
        # Transmission loss with depth-dependent thermocline effects
        tl_geo = 20.0 * math.log10(max(1.0, rng))
        # Thermocline creates shadow zone when source and receiver are on opposite sides
        layer_atten = 0.0
        if self_ship.acoustics.thermocline_on:
            thermo_depth = getattr(self_ship.acoustics, 'thermocline_depth_m', 50.0)
            self_below = self_ship.kin.depth > thermo_depth
            other_below = other.kin.depth > thermo_depth
            if self_below != other_below:  # On opposite sides of thermocline
                layer_atten = 8.0  # Strong attenuation across layer
        tl = tl_geo + layer_atten
        ambient = 60.0
        # Apply passive SNR penalty from degraded systems
        penalty = getattr(self_ship.acoustics, "passive_snr_penalty_db", 0.0)
        # Sonar equation: SNR = SL - TL - NL + AG (array gain from beamforming)
        snr_db = max(0.0, src_lvl - tl - ambient + ARRAY_GAIN_DB - penalty)
        # Detectability soft-knee mapping to 0..1
        detect = max(0.0, min(1.0, snr_db / 30.0))

        # Bearing error: faster targets produce more Doppler shift = BETTER localization
        # At 0 kn: sigma ~8° (hard to localize stationary target)
        # At 20 kn: sigma ~2° (easy to localize fast, loud target)
        base_sigma = max(1.5, 8.0 - other.kin.speed * 0.3)
        sigma = base_sigma + self_ship.acoustics.bearing_noise_extra
        noisy_bearing = normalize_angle_deg(brg + random.gauss(0, sigma))

        # Hysteresis: use memory to prevent flickering
        memory = _contact_memory[observer_id].get(other.id, {})

        if detect >= 0.15:
            # Contact above threshold - update memory and emit contact
            detected_ids.add(other.id)
            confidence = min(1.0, detect * 1.2)
            _contact_memory[observer_id][other.id] = {
                "last_seen": current_time,
                "last_confidence": confidence,
                "last_bearing": noisy_bearing,
                "last_detect": detect,
                "last_snr": snr_db,
                "last_sigma": sigma,
            }

            # Store last computed detectability on target for debug use (optional)
            other.acoustics.last_snr_db = snr_db
            other.acoustics.last_detectability = detect

            # Determine contact ID and classification based on registry
            if contact_registry is not None:
                # Use anonymous designation
                contact_id = contact_registry.get_or_create_designation(other.id, current_time)
                # Check if this contact has been identified by captain
                if contact_registry.is_identified(contact_id):
                    # Identified: show actual ship type
                    classified_as = contact_registry.get_identified_class(contact_id) or "Unknown"
                else:
                    # Unidentified: show sonar-only classification (no ship type)
                    classified_as = _classify_sonar_signature(other, detect, snr_db)
            else:
                # No registry (backwards compatibility): use actual IDs
                contact_id = other.id
                classified_as = _classify_ship_passive(other, detect, snr_db, rng)

            contacts.append(
                TelemetryContact(
                    id=contact_id,
                    bearing=noisy_bearing,
                    strength=detect,
                    classifiedAs=classified_as,
                    confidence=confidence,
                    bearingKnown=True,
                    rangeKnown=False,
                    detectability=detect,
                    snrDb=snr_db,
                    bearingSigmaDeg=sigma,
                    tonalLines=list(other.acoustics.tonal_lines),
                )
            )
        elif memory and current_time - memory.get("last_seen", 0) < CONTACT_PERSISTENCE_SECONDS:
            # Contact below threshold but within persistence window - emit fading contact
            detected_ids.add(other.id)
            time_since_seen = current_time - memory["last_seen"]
            decay_factor = max(0.0, 1.0 - time_since_seen * CONTACT_DECAY_RATE)
            fading_confidence = memory["last_confidence"] * decay_factor
            fading_detect = memory["last_detect"] * decay_factor

            if fading_confidence > 0.1:  # Only emit if still somewhat confident
                # Determine contact ID and classification based on registry
                if contact_registry is not None:
                    contact_id = contact_registry.get_or_create_designation(other.id, current_time)
                    if contact_registry.is_identified(contact_id):
                        classified_as = contact_registry.get_identified_class(contact_id) or "Unknown"
                    else:
                        classified_as = _classify_sonar_signature(other, fading_detect, memory.get("last_snr", 0))
                else:
                    contact_id = other.id
                    classified_as = _classify_ship_passive(other, fading_detect, memory.get("last_snr", 0), rng)

                contacts.append(
                    TelemetryContact(
                        id=contact_id,
                        bearing=memory["last_bearing"],  # Use last known bearing
                        strength=fading_detect,
                        classifiedAs=classified_as + " (fading)" if decay_factor < 0.7 else classified_as,
                        confidence=fading_confidence,
                        bearingKnown=True,
                        rangeKnown=False,
                        detectability=fading_detect,
                        snrDb=memory.get("last_snr", 0) * decay_factor,
                        bearingSigmaDeg=memory.get("last_sigma", 5.0),
                        tonalLines=list(other.acoustics.tonal_lines),
                    )
                )

    # Clean up stale memory entries (older than 2x persistence window)
    stale_threshold = current_time - (CONTACT_PERSISTENCE_SECONDS * 2)
    stale_ids = [tid for tid, mem in _contact_memory[observer_id].items()
                 if mem.get("last_seen", 0) < stale_threshold]
    for tid in stale_ids:
        del _contact_memory[observer_id][tid]

    return contacts


def passive_projectiles(self_ship: Ship, torpedoes: List[dict] | None, depth_charges: List[dict] | None) -> List[TelemetryContact]:
    """Render torpedoes as passive contacts for sonar UI.

    - Uses a simplified source level model for moving torpedoes
    - Applies same baffles and transmission loss as ships
    - Classifies as "Torpedo?" with confidence scaled by detectability
    - Note: Depth charges are silent while sinking and only create explosion contacts when they detonate
    """
    if getattr(self_ship, "systems", None) is not None and not self_ship.systems.sonar_ok:
        return []
    contacts: List[TelemetryContact] = []
    torps = torpedoes or []
    # Note: depth_charges parameter kept for compatibility but not used
    # Depth charges are silent while sinking and only create explosion contacts when they detonate
    # Common environment terms
    ambient = 60.0
    thermo_depth = getattr(self_ship.acoustics, 'thermocline_depth_m', 50.0)
    for t in torps:
        try:
            tx = float(t.get("x", 0.0)); ty = float(t.get("y", 0.0))
            torp_depth = float(t.get("depth", 0.0))
            dx = tx - self_ship.kin.x; dy = ty - self_ship.kin.y
            rng = math.hypot(dx, dy)
            brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
            rel = angle_diff(brg, self_ship.kin.heading)
            if abs(rel) > 180 - BAFFLES_DEG / 2:
                continue
            speed = float(t.get("speed", 35.0))
            # Torpedoes are very loud - much louder than ships at similar speeds
            # Mk48 torpedo has ~150-160 dB source level at 35 knots
            src_lvl = 150.0 + 0.3 * speed  # Very loud propulsors
            tl_geo = 20.0 * math.log10(max(1.0, rng))
            # Depth-dependent thermocline effect
            layer_atten = 0.0
            if self_ship.acoustics.thermocline_on:
                self_below = self_ship.kin.depth > thermo_depth
                torp_below = torp_depth > thermo_depth
                if self_below != torp_below:
                    layer_atten = 8.0
            tl = tl_geo + layer_atten
            penalty = getattr(self_ship.acoustics, "passive_snr_penalty_db", 0.0)
            # Sonar equation: SNR = SL - TL - NL + AG
            snr_db = max(0.0, src_lvl - tl - ambient + ARRAY_GAIN_DB - penalty)
            detect = max(0.0, min(1.0, snr_db / 25.0))  # Lower threshold for torpedoes
            if detect < 0.08:  # Lower detection threshold for torpedoes
                continue
            sigma = max(0.8, 6.0 - 0.05 * speed)
            noisy_bearing = normalize_angle_deg(brg + random.gauss(0, sigma))
            confidence = min(1.0, detect * 1.3)
            tid = t.get("id", f"torpedo_{int(tx)}_{int(ty)}")
            # Classify torpedoes based on side and signal strength
            side = t.get("side", "unknown")
            if side == self_ship.side:
                classified_as = "Own Torpedo" if detect > 0.6 else "Own Torpedo?"
                # Own fish: emit no tonal card (None => all-pass) so the operator's
                # narrowband filter can't dim their own weapon off their own scope.
                torp_lines = None
            else:
                classified_as = "Enemy Torpedo" if detect > 0.6 else "Enemy Torpedo?"
                # Enemy fish stays filterable, with the card of its own model so
                # the operator can tell an inbound 53-65 from a Mk48 (and the
                # seeker discriminator line from its launching escort). Unknown
                # models fall back to the Mk48 reference card.
                torp_lines = list(TORPEDO_TONAL_CARDS.get(t.get("name", "Mk48"), TORPEDO_TONAL_LINES))

            contacts.append(TelemetryContact(
                id=str(tid),
                bearing=noisy_bearing,
                strength=detect,
                classifiedAs=classified_as,
                confidence=confidence,
                bearingKnown=True,
                rangeKnown=False,
                detectability=detect,
                snrDb=snr_db,
                bearingSigmaDeg=sigma,
                tonalLines=torp_lines,
            ))
        except Exception:
            continue
    # Depth charges are silent while sinking and only create explosion contacts when they detonate
    # The existing explosion overlay system handles visual representation of explosions
    return contacts


def explosion_contacts(self_ship: Ship, explosions: List[dict] | None) -> List[TelemetryContact]:
    """Create sonar contacts for explosions (depth charge detonations).
    
    - Explosions are very loud and detectable from long range
    - Short duration contacts (5-10 seconds)
    - Classified as "Explosion" with high confidence
    """
    if getattr(self_ship, "systems", None) is not None and not self_ship.systems.sonar_ok:
        return []
    contacts: List[TelemetryContact] = []
    explosions_list = explosions or []
    
    # Common environment terms
    ambient = 60.0
    # Note: Explosions are so loud (180 dB) that thermocline has minimal effect
    # Without explosion depth info, we skip layer attenuation for explosions
    layer_atten = 0.0

    for exp in explosions_list:
        try:
            # Explosion data structure: {"bearing": float, "at": timestamp}
            bearing = float(exp.get("bearing", 0.0))
            explosion_time = exp.get("at", "")
            
            # Check if explosion is recent (within last 8 seconds)
            import time
            try:
                exp_timestamp = time.mktime(time.strptime(explosion_time, "%Y-%m-%dT%H:%M:%SZ"))
                current_time = time.time()
                if current_time - exp_timestamp > 8.0:  # Explosion older than 8 seconds
                    continue
            except:
                continue  # Skip if timestamp parsing fails
            
            # Explosions are very loud - detectable from long range
            # Assume explosion is at a reasonable range for detection calculation
            estimated_range = 2000.0  # 2km - explosions are loud enough to be detected from this range
            src_lvl = 180.0  # Very loud explosion (depth charge ~180 dB)
            tl_geo = 20.0 * math.log10(max(1.0, estimated_range))
            tl = tl_geo + layer_atten
            penalty = getattr(self_ship.acoustics, "passive_snr_penalty_db", 0.0)
            snr_db = max(0.0, src_lvl - tl - ambient - penalty)
            detect = max(0.0, min(1.0, snr_db / 20.0))  # Very high detectability
            
            if detect < 0.3:  # Even weak explosions should be detectable
                continue
                
            # Explosions have very low bearing noise (they're loud and clear)
            sigma = 1.0
            noisy_bearing = normalize_angle_deg(bearing + random.gauss(0, sigma))
            confidence = 0.95  # High confidence for explosions
            
            exp_id = f"explosion_{int(exp_timestamp)}"
            contacts.append(TelemetryContact(
                id=exp_id,
                bearing=noisy_bearing,
                strength=detect,
                classifiedAs="Explosion",
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


def countermeasure_contacts(self_ship: Ship, countermeasures: List[dict] | None) -> List[TelemetryContact]:
    """Create sonar contacts for deployed countermeasures (noisemakers and decoys).

    - Countermeasures are very loud (160-165 dB) to attract torpedo seekers
    - Own countermeasures classified as "Own Noisemaker" / "Own Decoy"
    - Enemy noisemakers classified as "Enemy Noisemaker"
    - Enemy decoys are intentionally NOT labeled as decoys: they carry the sub
      tonal card and present as a generic "Submerged Contact" so the deception
      survives on the operator's scope (see the classification block below).
    """
    if getattr(self_ship, "systems", None) is not None and not self_ship.systems.sonar_ok:
        return []
    contacts: List[TelemetryContact] = []
    cms = countermeasures or []
    ambient = 60.0
    thermo_depth = getattr(self_ship.acoustics, 'thermocline_depth_m', 50.0)

    for cm in cms:
        if not cm.get("active", False):
            continue
        try:
            cx = float(cm.get("x", 0.0))
            cy = float(cm.get("y", 0.0))
            cm_depth = float(cm.get("depth", 0.0))
            dx = cx - self_ship.kin.x
            dy = cy - self_ship.kin.y
            rng = math.hypot(dx, dy)
            brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
            rel = angle_diff(brg, self_ship.kin.heading)
            # Check baffles
            if abs(rel) > 180 - BAFFLES_DEG / 2:
                continue
            # Source level from countermeasure
            src_lvl = float(cm.get("source_level_db", 160.0))
            tl_geo = 20.0 * math.log10(max(1.0, rng))
            # Thermocline effect
            layer_atten = 0.0
            if self_ship.acoustics.thermocline_on:
                self_below = self_ship.kin.depth > thermo_depth
                cm_below = cm_depth > thermo_depth
                if self_below != cm_below:
                    layer_atten = 8.0
            tl = tl_geo + layer_atten
            penalty = getattr(self_ship.acoustics, "passive_snr_penalty_db", 0.0)
            snr_db = max(0.0, src_lvl - tl - ambient + ARRAY_GAIN_DB - penalty)
            detect = max(0.0, min(1.0, snr_db / 25.0))
            if detect < 0.1:
                continue
            sigma = 2.0  # Noisemakers are loud but diffuse
            noisy_bearing = normalize_angle_deg(brg + random.gauss(0, sigma))
            confidence = min(1.0, detect * 1.2)
            cm_id = cm.get("id", f"cm_{int(cx)}_{int(cy)}")
            cm_type = cm.get("type", "noisemaker")
            cm_side = cm.get("side", "BLUE")
            # Classification based on ownership
            if cm_side == self_ship.side:
                if cm_type == "noisemaker":
                    classified_as = "Own Noisemaker" if detect > 0.5 else "Own Noisemaker?"
                else:
                    classified_as = "Own Decoy" if detect > 0.5 else "Own Decoy?"
            else:
                if cm_type == "noisemaker":
                    classified_as = "Enemy Noisemaker" if detect > 0.5 else "Enemy Noisemaker?"
                else:
                    # Enemy decoy: deliberately NOT labeled "Decoy". It emits the
                    # submarine tonal card (cm_lines below) so it reads like a sub
                    # on the narrowband filter; revealing "Enemy Decoy" in the
                    # contacts table / waterfall color would blow that deception
                    # for free and defeat the whole mechanic. Present it as a
                    # generic submerged contact — indistinguishable from a real,
                    # unidentified sub until the operator works the tonals or the
                    # captain gets a visual. (See SONAR_TONAL_FILTER_PLAN.md §6.)
                    classified_as = "Submerged Contact" if detect >= 0.4 else "Unknown Contact"

            # Tonal signature for the narrowband filter:
            #  - Noisemakers are full-spectrum broadband -> None (all-pass): they
            #    can't be narrowbanded out, and they clear on their own.
            #  - Decoys mimic a submarine -> carry the sub card, so they read like
            #    a sub on the filter and survive a sub-hunt passband instead of
            #    vanishing (the same deception they play on a torpedo seeker).
            if cm_type == "noisemaker":
                cm_lines = None
            else:
                cm_lines = list(self_ship.acoustics.tonal_lines) or list(SUB_TONAL_LINES)

            contacts.append(TelemetryContact(
                id=str(cm_id),
                bearing=noisy_bearing,
                strength=detect,
                classifiedAs=classified_as,
                confidence=confidence,
                bearingKnown=True,
                rangeKnown=False,
                detectability=detect,
                snrDb=snr_db,
                bearingSigmaDeg=sigma,
                tonalLines=cm_lines,
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


def _classify_sonar_signature(ship: Ship, detectability: float, snr_db: float) -> str:
    """
    Classify contact based on acoustic signature ONLY - does not reveal actual ship type.
    Returns generic descriptors based on observable acoustic characteristics.

    This is used for unidentified contacts. Once the captain identifies a contact
    via periscope, the actual ship type is revealed.

    Args:
        ship: Target ship (used for speed/depth, not type)
        detectability: Detection strength (0.0 to 1.0)
        snr_db: Signal-to-noise ratio in dB

    Returns:
        Generic classification string like "Surface Contact", "Submerged Contact"
    """
    # Determine if contact is on surface or submerged (sonar can tell this)
    is_surface = ship.kin.depth <= 5.0

    # Speed-based hints (from doppler analysis)
    speed = abs(ship.kin.speed)
    if speed > 15:
        speed_desc = "Fast"
    elif speed > 8:
        speed_desc = "Medium"
    elif speed > 2:
        speed_desc = "Slow"
    else:
        speed_desc = "Stationary"

    # Build classification based on signal quality
    if detectability >= 0.7 and snr_db >= 22:
        # Good signal: can determine surface/submerged and speed
        if is_surface:
            return f"{speed_desc} Surface Contact"
        else:
            return f"{speed_desc} Submerged Contact"
    elif detectability >= 0.4 and snr_db >= 15:
        # Medium signal: can determine surface/submerged
        if is_surface:
            return "Surface Contact"
        else:
            return "Submerged Contact"
    else:
        # Weak signal: uncertain
        return "Unknown Contact"


def _classify_ship_passive(ship: Ship, detectability: float, snr_db: float, range_m: float) -> str:
    """
    Full classification revealing actual ship type. Only used for IDENTIFIED contacts.

    Args:
        ship: Target ship
        detectability: Detection strength (0.0 to 1.0)
        snr_db: Signal-to-noise ratio in dB
        range_m: Range to target in meters

    Returns:
        Classification string with actual ship type
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
