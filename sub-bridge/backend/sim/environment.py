"""Persistent environment conditions (time of day + weather) for a mission.

Pure compute, mirroring the style of ``tactical.py``: no I/O, no Simulation,
no LLM — callable from a plain pytest. Conditions are loaded once from the
mission's ``environment`` block and held constant for the whole mission.

The only mechanical effect today is on the captain's periscope (visual
detection): poor visibility shrinks the spotting range, lowers the per-cycle
detection probability, and widens the heading-estimate sigma. The
``visual_visibility_factor`` is deliberately a single scalar so radio-mast or
sonar degradations can hook in later without reshaping this module.

Severity is the "Strong" profile chosen 2026-06-28: factors multiply across
the two axes (time of day x weather), so e.g. a foggy night ~= 0.55 * 0.35.
All numbers below are intended to be tuned freely.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

# Base optical limit for periscope spotting on a clear day (meters). This
# mirrors the historical 15 km gate in loop.py's periscope routine.
BASE_VISUAL_RANGE_M = 15000.0
# Floor on spotting range: even near-blind, a contact a short distance off
# the bow is theoretically spottable. Keeps fog/night from zeroing the scope.
MIN_VISUAL_RANGE_M = 1000.0
# Floor on the visibility factor so detection never becomes flat-impossible.
MIN_VISIBILITY = 0.05
# Heading-estimate sigma (degrees) added at zero visibility. Scales with
# (1 - visibility): clear day adds 0, near-blind adds ~this much extra spread.
HEADING_SIGMA_MAX_PENALTY_DEG = 12.0

# Exponential range falloff. Visual detection through the atmosphere follows
# Beer-Lambert / Koschmieder contrast attenuation, ~ e^(-k * r / V), where V is
# the visibility-limited range. DETECTION_DECAY = k sets how much contrast is
# left at that range: e^(-3) ~= 5%. The textbook Koschmieder value (2% contrast
# threshold) is ln(1/0.02) ~= 3.912; we use a slightly gentler 3.0 for feel.
# Raise it toward 3.912 for a harsher, more "textbook" falloff.
DETECTION_DECAY = 3.0

# Depth (m) at/above which the hull is considered surfaced and fully visible.
SURFACED_DEPTH_M = 5.0
# Detectability of a raised mast (periscope/radio feather) relative to a fully
# surfaced hull. A thin mast is far harder to spot, so spotting it is much less
# likely than spotting a surfaced submarine — before weather/range are applied.
MAST_DETECTABILITY = 0.30

# Strong-severity multipliers. Each axis contributes a factor in (0, 1].
_TIME_OF_DAY_FACTORS = {
    "day": 1.0,
    "dawn": 0.8,
    "dusk": 0.8,
    "night": 0.55,
}
_WEATHER_FACTORS = {
    "clear": 1.0,
    "calm": 1.0,  # legacy alias used by existing mission files
    "fog": 0.35,
    "rain": 0.6,
}

_DEFAULT_TIME = "day"
_DEFAULT_WEATHER = "clear"


@dataclass(frozen=True)
class EnvironmentConditions:
    """Immutable weather + time-of-day for a mission (held constant)."""

    time_of_day: str = _DEFAULT_TIME
    weather: str = _DEFAULT_WEATHER

    @classmethod
    def from_mission_env(
        cls, env: Optional[Mapping[str, Any]]
    ) -> "EnvironmentConditions":
        """Parse a mission ``environment`` block.

        Accepts ``timeOfDay``/``time_of_day`` and ``weather``. Unknown or
        missing values fall back to a clear day. Comparison is
        case-insensitive; ``calm`` is treated as ``clear``.
        """
        env = env or {}
        tod = str(
            env.get("timeOfDay", env.get("time_of_day", _DEFAULT_TIME))
        ).strip().lower()
        wx = str(env.get("weather", _DEFAULT_WEATHER)).strip().lower()
        if tod not in _TIME_OF_DAY_FACTORS:
            tod = _DEFAULT_TIME
        if wx not in _WEATHER_FACTORS:
            wx = _DEFAULT_WEATHER
        return cls(time_of_day=tod, weather=wx)

    def visual_visibility_factor(self) -> float:
        """Combined visibility in [MIN_VISIBILITY, 1.0]; 1.0 = clear day."""
        tod = _TIME_OF_DAY_FACTORS.get(self.time_of_day, 1.0)
        wx = _WEATHER_FACTORS.get(self.weather, 1.0)
        return max(MIN_VISIBILITY, tod * wx)

    def label(self) -> str:
        """Human-readable summary for the captain UI, e.g. 'Night, fog'."""
        tod = self.time_of_day.capitalize()
        if self.weather in ("clear", "calm"):
            return f"{tod}, clear"
        return f"{tod}, {self.weather}"

    def to_dict(self) -> Dict[str, Any]:
        """Telemetry projection for the captain station."""
        return {
            "timeOfDay": self.time_of_day,
            "weather": self.weather,
            "visibility": round(self.visual_visibility_factor(), 3),
            "label": self.label(),
        }


@dataclass(frozen=True)
class PeriscopeModifiers:
    """Visibility-derived knobs the periscope routine applies each cycle."""

    visibility: float          # multiplier on per-cycle detection probability
    max_range_m: float         # hard gate on how far a contact can be spotted
    heading_sigma_add: float   # extra degrees of heading-estimate uncertainty


def periscope_modifiers(conditions: EnvironmentConditions) -> PeriscopeModifiers:
    """Pure mapping from conditions to the periscope's visibility knobs.

    Single source of truth for how environment degrades the scope, so the
    loop wiring stays a thin application of these three values.
    """
    vis = conditions.visual_visibility_factor()
    return PeriscopeModifiers(
        visibility=vis,
        max_range_m=max(MIN_VISUAL_RANGE_M, BASE_VISUAL_RANGE_M * vis),
        heading_sigma_add=(1.0 - vis) * HEADING_SIGMA_MAX_PENALTY_DEG,
    )


def detection_falloff(dist_m: float, max_range_m: float) -> float:
    """Exponential (Beer-Lambert / Koschmieder) range falloff in [0, 1].

    Returns ~1.0 at point-blank and decays smoothly with distance, reaching
    ~e^(-DETECTION_DECAY) (~5%) at ``max_range_m`` — the visibility-limited
    range. Poor weather/night shrinks ``max_range_m``, which steepens the decay
    (you must be closer), so visibility and range are one coherent mechanism
    rather than a flat penalty plus a hard cliff.
    """
    if max_range_m <= 0.0:
        return 0.0
    return math.exp(-DETECTION_DECAY * max(0.0, dist_m) / max_range_m)


@dataclass(frozen=True)
class VisualExposure:
    """How visible the sub is to an enemy lookout, before weather/range."""

    exposed: bool         # can the enemy see it at all?
    detectability: float  # multiplier vs a fully surfaced hull (1.0)
    mode: str             # "surface" | "periscope" | "none"


def sub_visual_exposure(
    depth_m: float, periscope_up: bool, radio_up: bool
) -> VisualExposure:
    """Visual exposure of the player sub to enemy lookouts.

    The sub is exposed when its hull is surfaced OR when any mast (periscope /
    radio) is raised — a mast breaks the surface even at periscope depth. A
    surfaced hull is fully detectable; a lone mast is a small feather, so it is
    much harder to spot. Weather/time-of-day visibility and range falloff are
    applied on top of the returned ``detectability`` by the caller.
    """
    if depth_m <= SURFACED_DEPTH_M:
        return VisualExposure(True, 1.0, "surface")
    if periscope_up or radio_up:
        return VisualExposure(True, MAST_DETECTABILITY, "periscope")
    return VisualExposure(False, 0.0, "none")
