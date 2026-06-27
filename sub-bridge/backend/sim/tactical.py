"""Tactical compute layer — pure functions a captain "consults" before deciding.

Bearings, ranges, intercept solutions, weapon-envelope checks, doctrine
recommendations. No state, no I/O, no LLM, no asyncio. Same shape as
`SimulationCore`: callable from a plain pytest, reusable across LLM
controllers, scripted controllers, and (future) human consoles.

Design intent: the captain LLM should not do trigonometry. It should read
*answers* — bearing to a contact, whether a target is in weapon envelope,
which doctrine the situation calls for — and choose a Hand. This module is
where those answers are computed.

Conventions:
    - Position is `(x, y)` in meters. X east, Y north.
    - Bearing is compass: 0°=North, 90°=East, 180°=South, 270°=West.
    - Speed is **knots** at the public API; converted to m/s internally.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..models import Ship


# Knots → meters per second.
KN_TO_MS = 0.5144444

# Passive detection range for torpedoes (they are very loud). Beyond this
# range, captains do not "hear" the torpedo even though it exists.
TORPEDO_PASSIVE_DETECTION_M = 10000.0


# --------------------------------------------------------------------------- #
# Geometry primitives
# --------------------------------------------------------------------------- #

def bearing_to(from_pos: Tuple[float, float], to_pos: Tuple[float, float]) -> float:
    """Compass bearing from `from_pos` to `to_pos`, normalized to [0, 360).

    Uses the simulator's convention: 0=N, 90=E. Returns 0.0 when the two
    points coincide rather than raising.
    """
    dx = to_pos[0] - from_pos[0]
    dy = to_pos[1] - from_pos[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(dx, dy)) % 360.0


def range_to(from_pos: Tuple[float, float], to_pos: Tuple[float, float]) -> float:
    """Straight-line range in meters."""
    dx = to_pos[0] - from_pos[0]
    dy = to_pos[1] - from_pos[1]
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------- #
# Intercept solution
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class InterceptSolution:
    """Result of solving for a course that intercepts a moving target.

    `feasible=False` when the hunter is too slow to catch the target on the
    current relative geometry; in that case `heading` is the direct bearing
    to the target's *current* position (best fallback).
    """

    feasible: bool
    heading: float
    time_s: float
    intercept_pos: Tuple[float, float]


def intercept_solution(
    hunter_pos: Tuple[float, float],
    hunter_speed_kn: float,
    target_pos: Tuple[float, float],
    target_heading_deg: float,
    target_speed_kn: float,
) -> InterceptSolution:
    """Solve for the bearing the hunter should steer to intercept the target.

    Uses the standard intercept quadratic: |hunter_pos + hunter_vel * t| =
    |target_pos + target_vel * t|. When the hunter is faster, returns the
    earliest positive root. When slower / no real root, falls back to the
    direct bearing.
    """
    h_speed = max(0.0, hunter_speed_kn) * KN_TO_MS
    t_speed = max(0.0, target_speed_kn) * KN_TO_MS

    dx = target_pos[0] - hunter_pos[0]
    dy = target_pos[1] - hunter_pos[1]

    # Target velocity components (compass bearing convention)
    th = math.radians(target_heading_deg)
    tvx = t_speed * math.sin(th)
    tvy = t_speed * math.cos(th)

    # Quadratic from (dx + tvx·t)² + (dy + tvy·t)² = (h_speed·t)²:
    #   a·t² + b·t + c = 0   where a = t_speed² − h_speed²
    a = t_speed * t_speed - h_speed * h_speed
    b = 2.0 * (dx * tvx + dy * tvy)
    c = dx * dx + dy * dy

    fallback_heading = bearing_to(hunter_pos, target_pos)

    # No-motion edge case: any positive time is fine; just steer at the target.
    if h_speed < 1e-6:
        return InterceptSolution(False, fallback_heading, 0.0, target_pos)

    if abs(a) < 1e-6:
        # Linear: hunter and target same speed. b * t + c = 0
        if abs(b) < 1e-9:
            return InterceptSolution(False, fallback_heading, 0.0, target_pos)
        t = -c / b
        if t <= 0.0:
            return InterceptSolution(False, fallback_heading, 0.0, target_pos)
    else:
        disc = b * b - 4 * a * c
        if disc < 0.0:
            return InterceptSolution(False, fallback_heading, 0.0, target_pos)
        sq = math.sqrt(disc)
        t1 = (-b - sq) / (2 * a)
        t2 = (-b + sq) / (2 * a)
        positives = [x for x in (t1, t2) if x > 0.0]
        if not positives:
            return InterceptSolution(False, fallback_heading, 0.0, target_pos)
        t = min(positives)

    intercept_x = target_pos[0] + tvx * t
    intercept_y = target_pos[1] + tvy * t
    heading = bearing_to(hunter_pos, (intercept_x, intercept_y))
    return InterceptSolution(True, heading, t, (intercept_x, intercept_y))


# --------------------------------------------------------------------------- #
# Weapon envelopes
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EnvelopeReport:
    in_range: bool
    range_m: float
    optimal_min_m: float
    optimal_max_m: float
    reason: str


# Practical engagement envelopes (m). Tuned conservative — captain LLMs can
# always choose to fire outside if context warrants ("deviate:" rationale).
_TORPEDO_OPTIMAL_MIN = 800.0     # below enable_range, weapon doesn't arm
_TORPEDO_OPTIMAL_MAX = 6000.0    # beyond seeker acquisition reliability
_DEPTH_CHARGE_OPTIMAL_MAX = 1500.0  # DCs are dropped at the ship's position; useful only when target is close
_ACTIVE_PING_OPTIMAL_MAX = 15000.0


def weapon_envelope(
    ship: Ship,
    target_pos: Tuple[float, float],
    weapon_kind: str,
) -> EnvelopeReport:
    """Return whether `target_pos` is in `weapon_kind`'s practical envelope.

    `weapon_kind` ∈ {"fire_torpedo", "drop_depth_charges", "active_ping"}.
    Capability checks are out of scope here — this is *geometry only*. The
    caller (or `doctrine_for`) is responsible for capability gating.
    """
    rng = range_to((ship.kin.x, ship.kin.y), target_pos)

    if weapon_kind == "fire_torpedo":
        in_range = _TORPEDO_OPTIMAL_MIN <= rng <= _TORPEDO_OPTIMAL_MAX
        reason = (
            f"range {rng:.0f}m in [{_TORPEDO_OPTIMAL_MIN:.0f}, {_TORPEDO_OPTIMAL_MAX:.0f}]"
            if in_range
            else (
                f"range {rng:.0f}m too short (< {_TORPEDO_OPTIMAL_MIN:.0f})"
                if rng < _TORPEDO_OPTIMAL_MIN
                else f"range {rng:.0f}m too long (> {_TORPEDO_OPTIMAL_MAX:.0f})"
            )
        )
        return EnvelopeReport(in_range, rng, _TORPEDO_OPTIMAL_MIN, _TORPEDO_OPTIMAL_MAX, reason)

    if weapon_kind == "drop_depth_charges":
        in_range = rng <= _DEPTH_CHARGE_OPTIMAL_MAX
        reason = (
            f"range {rng:.0f}m within DC envelope ({_DEPTH_CHARGE_OPTIMAL_MAX:.0f})"
            if in_range
            else f"range {rng:.0f}m too long (> {_DEPTH_CHARGE_OPTIMAL_MAX:.0f}); close first"
        )
        return EnvelopeReport(in_range, rng, 0.0, _DEPTH_CHARGE_OPTIMAL_MAX, reason)

    if weapon_kind == "active_ping":
        in_range = rng <= _ACTIVE_PING_OPTIMAL_MAX
        reason = f"range {rng:.0f}m vs ping limit {_ACTIVE_PING_OPTIMAL_MAX:.0f}"
        return EnvelopeReport(in_range, rng, 0.0, _ACTIVE_PING_OPTIMAL_MAX, reason)

    return EnvelopeReport(False, rng, 0.0, 0.0, f"unknown weapon_kind '{weapon_kind}'")


# --------------------------------------------------------------------------- #
# Doctrine recommendation
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ContactBelief:
    """Minimal contact info doctrine_for needs.

    Decoupled from the simulator's `TelemetryContact` so this module can be
    tested without importing the sonar layer.
    """

    id: str
    bearing_deg: float
    confidence: float
    estimated_pos: Optional[Tuple[float, float]] = None  # if known, else None


@dataclass(frozen=True)
class DoctrineRecommendation:
    action: str
    """One of: ENGAGE_TORPEDO | ENGAGE_DC | CLOSE | INVESTIGATE | EVADE | TRANSIT | HOLD."""
    reason: str
    target_id: Optional[str] = None
    suggested_heading: Optional[float] = None
    suggested_speed_kn: Optional[float] = None


# Confidence threshold for treating a contact as actionable for engagement.
# Mirrors the orchestrator's existing `ai_fleet_trigger_conf_threshold`.
ACTIONABLE_CONFIDENCE = 0.7

# Lower bound for treating a contact as worth investigating without engaging.
# Below this, the contact is too faint to act on and the ship should hold or
# follow the fleet. Between this and ACTIONABLE_CONFIDENCE, the captain
# should vector toward the bearing for better passive geometry but should
# NOT light up active sonar or expend weapons.
INVESTIGATE_CONFIDENCE = 0.3


# --------------------------------------------------------------------------- #
# Threat alerts — triggered events that override the role's default doctrine
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ThreatAlert:
    """An unambiguous alert that should override role-default behavior.

    Distinct from a contact: contacts are noisy and uncertain; threats are
    triggered events (torpedo IS in the water; friendly WAS hit). Captains
    are expected to drop EMCON and prosecute when threats are present.
    """

    kind: str
    """One of: torpedo_in_water | self_hit | friendly_hit."""
    severity: str
    """One of: info | warning | critical."""
    bearing_deg: Optional[float] = None
    range_m: Optional[float] = None
    source_id: Optional[str] = None
    detail: str = ""


def scan_threats(
    ship: Ship,
    world,
    recent_combat_events: Sequence[dict],
) -> List[ThreatAlert]:
    """Identify active threats relevant to `ship`.

    `recent_combat_events` is a sequence of `{kind, target, ...}` dicts the
    caller has accumulated over a short window (typically the last ~60s).
    Each event represents a detonation against a specific target.

    Returns alerts sorted with `critical` severity first.
    """
    threats: List[ThreatAlert] = []
    self_pos = (ship.kin.x, ship.kin.y)

    # Hostile torpedoes within passive detection range — they are very loud.
    for t in getattr(world, "torpedoes", []) or []:
        if t.get("side") == ship.side:
            continue
        try:
            tx = float(t.get("x", 0.0))
            ty = float(t.get("y", 0.0))
        except (TypeError, ValueError):
            continue
        rng = range_to(self_pos, (tx, ty))
        if rng > TORPEDO_PASSIVE_DETECTION_M:
            continue
        brg = bearing_to(self_pos, (tx, ty))
        threats.append(ThreatAlert(
            kind="torpedo_in_water",
            severity="critical",
            bearing_deg=round(brg, 1),
            range_m=round(rng, 0),
            source_id=str(t.get("id") or ""),
            detail=f"hostile torpedo bearing {brg:.0f}°, range {rng:.0f} m",
        ))

    # Recent combat detonations against friendly ships.
    for ev in recent_combat_events:
        if ev.get("kind") not in ("torpedo.detonated", "depth_charge.detonated"):
            continue
        target_id = ev.get("target")
        if not target_id:
            continue
        try:
            target = world.get_ship(target_id)
        except Exception:
            continue
        if target is None or target.side != ship.side:
            continue
        if target_id == ship.id:
            threats.append(ThreatAlert(
                kind="self_hit",
                severity="critical",
                bearing_deg=None,
                range_m=0.0,
                source_id=ship.id,
                detail=f"taking weapons damage ({ev.get('kind')})",
            ))
        else:
            tpos = (target.kin.x, target.kin.y)
            brg = bearing_to(self_pos, tpos)
            rng = range_to(self_pos, tpos)
            threats.append(ThreatAlert(
                kind="friendly_hit",
                severity="warning",
                bearing_deg=round(brg, 1),
                range_m=round(rng, 0),
                source_id=target_id,
                detail=f"friendly {target_id} hit, bearing {brg:.0f}°",
            ))

    # Severity ordering: critical first, warning next, info last. Within a
    # severity, preserve detection order.
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    threats.sort(key=lambda a: severity_rank.get(a.severity, 3))
    return threats


SALVO_MAX = 3
"""Max torpedoes a single captain may fire in `SALVO_WINDOW_S` before the
doctrine ladder forces a hold/close cooldown. Real WWII sub doctrine ran
2-4 torpedoes per coordinated salvo, then waited to assess. Without this
cap, captains would empty their entire magazine in a straight line at the
first confirmed contact, leaving the ship defenseless against incoming
weapons or follow-on contacts."""

SALVO_WINDOW_S = 120.0
"""Window over which `recent_torp_fires` is computed by the caller."""


def doctrine_for(
    ship: Ship,
    contacts: Sequence[ContactBelief],
    fleet_destination: Optional[Tuple[float, float]] = None,
    fleet_speed_kn: Optional[float] = None,
    threats: Optional[Sequence[ThreatAlert]] = None,
    recent_torp_fires: int = 0,
) -> DoctrineRecommendation:
    """Pick a default doctrine. The LLM may override with a `deviate:` note.

    Priority order:
        0. THREAT OVERRIDE (highest):
           a. `self_hit` → max-aggression engagement on best contact, or
              evasive set_nav at flank if no contact (fight back or run).
           b. `torpedo_in_water` → engage on torpedo bearing (fire back at
              shooter direction) if able; otherwise evade (perpendicular
              run from torpedo bearing at flank).
           c. `friendly_hit` → CLOSE on the hit ship's bearing; active
              sonar authorized by role doctrine.
        1. High-confidence contact in torpedo envelope → ENGAGE_TORPEDO.
           Suppressed to CLOSE when this captain has already fired
           `SALVO_MAX` torpedoes within `SALVO_WINDOW_S`; salvo discipline
           keeps reserves for incoming threats and forces an assessment
           pause between attacks. Threat-driven engagement bypasses the
           cap (you fire back on a torpedo-in-water regardless).
        2. High-confidence contact in DC envelope → ENGAGE_DC.
        3. High-confidence contact known position → CLOSE.
        4. Moderate-confidence contact (INVESTIGATE_CONFIDENCE..ACTIONABLE)
           → INVESTIGATE: vector toward bearing at moderate speed for
           better passive geometry, do not light up.
        5. Fleet destination set → TRANSIT.
        6. Else HOLD.
    """
    caps = getattr(ship, "capabilities", None)
    has_torpedoes = bool(caps and getattr(caps, "has_torpedoes", False))
    has_depth_charges = bool(caps and getattr(caps, "has_depth_charges", False))
    threats = list(threats or [])
    salvo_exhausted = int(recent_torp_fires) >= SALVO_MAX

    # 0. Threat overrides take precedence over normal doctrine.
    rec = _threat_doctrine(ship, threats, contacts, has_torpedoes, has_depth_charges)
    if rec is not None:
        return rec

    actionable: List[ContactBelief] = [
        c for c in contacts if c.confidence >= ACTIONABLE_CONFIDENCE and c.estimated_pos is not None
    ]
    actionable.sort(key=lambda c: c.confidence, reverse=True)

    for c in actionable:
        assert c.estimated_pos is not None
        if has_torpedoes and not salvo_exhausted:
            env = weapon_envelope(ship, c.estimated_pos, "fire_torpedo")
            if env.in_range:
                return DoctrineRecommendation(
                    action="ENGAGE_TORPEDO",
                    reason=f"contact {c.id} {env.reason}",
                    target_id=c.id,
                    suggested_heading=c.bearing_deg,
                )
        if has_depth_charges:
            env = weapon_envelope(ship, c.estimated_pos, "drop_depth_charges")
            if env.in_range:
                return DoctrineRecommendation(
                    action="ENGAGE_DC",
                    reason=f"contact {c.id} {env.reason}",
                    target_id=c.id,
                    suggested_heading=c.bearing_deg,
                )

    if actionable:
        # Either out of weapon envelope, or torpedo suppressed because a
        # previous shot is still in the water. Close on the contact and
        # let the next decision cycle re-evaluate.
        c = actionable[0]
        target = c.estimated_pos
        assert target is not None
        sol = intercept_solution(
            (ship.kin.x, ship.kin.y),
            ship.hull.max_speed,
            target,
            target_heading_deg=0.0,  # unknown course; treat as static for the suggestion
            target_speed_kn=0.0,
        )
        if salvo_exhausted and has_torpedoes:
            reason = (
                f"contact {c.id} confidence {c.confidence:.2f}; salvo cap "
                f"reached — close and assess before re-engaging"
            )
        else:
            reason = (
                f"contact {c.id} confidence {c.confidence:.2f} outside weapon "
                f"envelope; close to engage"
            )
        return DoctrineRecommendation(
            action="CLOSE",
            reason=reason,
            target_id=c.id,
            suggested_heading=sol.heading,
            suggested_speed_kn=ship.hull.max_speed,
        )

    # Moderate-confidence contacts → investigate without lighting up.
    investigate_band: List[ContactBelief] = [
        c for c in contacts
        if INVESTIGATE_CONFIDENCE <= c.confidence < ACTIONABLE_CONFIDENCE
    ]
    if investigate_band:
        investigate_band.sort(key=lambda c: c.confidence, reverse=True)
        c = investigate_band[0]
        # Vector toward the contact's bearing at moderate speed (~70% of max).
        # Captains should improve passive geometry, not commit to engagement.
        return DoctrineRecommendation(
            action="INVESTIGATE",
            reason=f"contact {c.id} confidence {c.confidence:.2f} below engagement; close passively",
            target_id=c.id,
            suggested_heading=c.bearing_deg,
            suggested_speed_kn=ship.hull.max_speed * 0.7,
        )

    if fleet_destination is not None:
        sol_heading = bearing_to((ship.kin.x, ship.kin.y), fleet_destination)
        return DoctrineRecommendation(
            action="TRANSIT",
            reason="no actionable contacts; following fleet destination",
            suggested_heading=sol_heading,
            suggested_speed_kn=fleet_speed_kn if fleet_speed_kn is not None else ship.hull.max_speed * 0.6,
        )

    return DoctrineRecommendation(action="HOLD", reason="no contacts and no fleet destination")


def _threat_doctrine(
    ship: Ship,
    threats: Sequence[ThreatAlert],
    contacts: Sequence[ContactBelief],
    has_torpedoes: bool,
    has_depth_charges: bool,
) -> Optional[DoctrineRecommendation]:
    """If any threats are active, return an override recommendation."""
    if not threats:
        return None

    # Pick the most severe threat first.
    self_hits = [t for t in threats if t.kind == "self_hit"]
    torpedoes = [t for t in threats if t.kind == "torpedo_in_water"]
    friendly_hits = [t for t in threats if t.kind == "friendly_hit"]

    if self_hits:
        # Fire back at any reasonable contact. If we have one, engage it.
        best = max(contacts, key=lambda c: c.confidence, default=None)
        if best is not None and best.estimated_pos is not None:
            if has_torpedoes:
                return DoctrineRecommendation(
                    action="ENGAGE_TORPEDO",
                    reason="self under attack; weapons free",
                    target_id=best.id,
                    suggested_heading=best.bearing_deg,
                )
            if has_depth_charges:
                return DoctrineRecommendation(
                    action="ENGAGE_DC",
                    reason="self under attack; saturate area",
                    target_id=best.id,
                    suggested_heading=best.bearing_deg,
                )
        # No actionable contact: evade at flank, perpendicular to current heading.
        evasive_heading = (ship.kin.heading + 90.0) % 360.0
        return DoctrineRecommendation(
            action="EVADE",
            reason="self under attack with no firing solution; evade at flank",
            suggested_heading=evasive_heading,
            suggested_speed_kn=ship.hull.max_speed,
        )

    if torpedoes:
        t = torpedoes[0]
        # If we can engage on the torpedo bearing (likely back-bearing to
        # shooter), do so. Else evade perpendicular to the torpedo run.
        if has_torpedoes and t.bearing_deg is not None:
            return DoctrineRecommendation(
                action="ENGAGE_TORPEDO",
                reason=f"torpedo in water bearing {t.bearing_deg:.0f}°; counter-fire on bearing",
                target_id=t.source_id,
                suggested_heading=t.bearing_deg,
            )
        # Evade: turn perpendicular to the torpedo's incoming bearing.
        if t.bearing_deg is not None:
            evade = (t.bearing_deg + 90.0) % 360.0
        else:
            evade = (ship.kin.heading + 90.0) % 360.0
        return DoctrineRecommendation(
            action="EVADE",
            reason=f"torpedo in water bearing {t.bearing_deg}; perpendicular evasion at flank",
            suggested_heading=evade,
            suggested_speed_kn=ship.hull.max_speed,
        )

    if friendly_hits:
        # Friendly was hit — close on the bearing to support / hunt.
        f = friendly_hits[0]
        return DoctrineRecommendation(
            action="CLOSE",
            reason=f"friendly {f.source_id} hit; close on bearing {f.bearing_deg} to prosecute",
            target_id=f.source_id,
            suggested_heading=f.bearing_deg,
            suggested_speed_kn=ship.hull.max_speed,
        )

    return None
