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
    """One of: ENGAGE_TORPEDO | ENGAGE_DC | CLOSE | TRANSIT | HOLD."""
    reason: str
    target_id: Optional[str] = None
    suggested_heading: Optional[float] = None
    suggested_speed_kn: Optional[float] = None


# Confidence threshold for treating a contact as actionable. Mirrors the
# orchestrator's existing `ai_fleet_trigger_conf_threshold` default.
ACTIONABLE_CONFIDENCE = 0.7


def doctrine_for(
    ship: Ship,
    contacts: Sequence[ContactBelief],
    fleet_destination: Optional[Tuple[float, float]] = None,
    fleet_speed_kn: Optional[float] = None,
) -> DoctrineRecommendation:
    """Pick a default doctrine. The LLM may override with a `deviate:` note.

    Priority:
        1. If high-confidence contact in torpedo envelope (and ship has
           torpedoes) → ENGAGE_TORPEDO.
        2. Else if high-confidence contact in DC envelope (and ship has DC)
           → ENGAGE_DC.
        3. Else if a high-confidence contact has a known position → CLOSE.
        4. Else if a fleet destination is set → TRANSIT.
        5. Else HOLD.
    """
    caps = getattr(ship, "capabilities", None)
    has_torpedoes = bool(caps and getattr(caps, "has_torpedoes", False))
    has_depth_charges = bool(caps and getattr(caps, "has_depth_charges", False))

    actionable: List[ContactBelief] = [
        c for c in contacts if c.confidence >= ACTIONABLE_CONFIDENCE and c.estimated_pos is not None
    ]
    actionable.sort(key=lambda c: c.confidence, reverse=True)

    for c in actionable:
        assert c.estimated_pos is not None
        if has_torpedoes:
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
        # In-bound contact but out of weapon envelope → close on it.
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
        return DoctrineRecommendation(
            action="CLOSE",
            reason=f"contact {c.id} confidence {c.confidence:.2f} outside weapon envelope; close to engage",
            target_id=c.id,
            suggested_heading=sol.heading,
            suggested_speed_kn=ship.hull.max_speed,
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
