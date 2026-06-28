"""Reactor power-routing model — the Engineering station's core decision surface.

The reactor produces ``output_mw`` of power. Engineering routes that power across
four consumers as fractions (``helm``/``sonar``/``weapons``/``engineering``) which
sum to <= 1.0. Each route's MW slice drives a concrete, legible effect:

    helm        -> propulsion: sets the achievable top-speed ceiling.
    sonar       -> processing gain: +/- dB on passive SNR (hear farther / go deaf).
    weapons     -> tube machinery: scales reload / flood / door timers.
    engineering -> repair: maintenance progression rate (+ pump headroom).

Generating power is not free: ``reactor_noise_points`` couples ``output_mw`` to the
ship's acoustic signature, so cranking the reactor to give the crew capability also
makes the boat louder. That capability-vs-detectability trade is the station's job.

INVARIANT — every formula here reduces *exactly* to the legacy behavior at the
default 25% split. ``speed_cap_fraction`` at ``helm=0.25`` equals ``output_mw/max_mw``;
``sonar_snr_bonus_db`` / ``reload_multiplier`` / ``maintenance_rate`` all sit at their
neutral value when a route holds 25% of a 60 MW reactor (15 MW). AI ships never
re-route power, so this change leaves them — and existing mission balance — untouched.
"""
from __future__ import annotations

# --- Nominal operating points -------------------------------------------------
# A route "at nominal" produces the legacy effect. Nominal MW values are pinned to
# the historical default operating point: a 60 MW reactor split four ways = 15 MW.
HELM_NOMINAL_FRAC = 0.25   # helm fraction at which propulsion matches output_mw/max_mw
SONAR_NOMINAL_MW = 15.0    # sonar MW at which passive SNR gain is 0 dB
WEAP_NOMINAL_MW = 15.0     # weapons MW at which tube timers equal their base values
ENG_NOMINAL_MW = 15.0      # engineering MW at which repair runs at its nominal rate

# --- Sonar processing gain ----------------------------------------------------
# Passive SNR gains/loses dB as routed sonar power deviates from nominal. Receive-
# side processing adds no self-noise, so this is a pure "spend MW to hear" lever.
SONAR_SNR_PER_MW = 8.0 / 15.0   # ~+8 dB by double-nominal (30 MW); see clamps below
SONAR_SNR_MAX_DB = 8.0          # boost ceiling
SONAR_SNR_MIN_DB = -10.0        # starve floor (near-deaf, not fully blind)

# --- Weapons reload multiplier ------------------------------------------------
# Tube timers scale by 1 / (weapons_mw / nominal), clamped. Surge power for fast
# reloads before an engagement; starve the tubes when not fighting.
RELOAD_MULT_MIN = 0.5   # fastest: surge power roughly halves timers
RELOAD_MULT_MAX = 3.0   # slowest: starved tubes up to triple timers

# --- Engineering / maintenance ------------------------------------------------
MAINT_RATE_AT_NOMINAL = 0.1 * (ENG_NOMINAL_MW / 100.0)  # legacy: (eng_mw/max_mw)*0.1 @60MW/25%

# --- Reactor acoustic signature ----------------------------------------------
# MW above the quiet floor adds points to the 0..100 noise budget that drives
# detectability + EMCON. Below the floor the plant is acoustically quiet (rig for
# ultra-silent), so dropping the reactor is a real way to shrink the signature.
REACTOR_QUIET_MW = 25.0    # at/below this the plant adds no signature points
REACTOR_NOISE_MAX = 20.0   # signature points added at full reactor output


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _alloc(ship, route: str, default: float = 0.25) -> float:
    p = getattr(ship, "power", None)
    if p is None:
        return default
    return _clamp(float(getattr(p, route, default)), 0.0, 1.0)


def _total_mw(ship) -> float:
    r = ship.reactor
    return max(0.0, min(float(r.max_mw), float(r.output_mw)))


def route_mw(ship) -> dict:
    """Absolute MW routed to each consumer this tick."""
    total = _total_mw(ship)
    return {
        "helm": total * _alloc(ship, "helm"),
        "sonar": total * _alloc(ship, "sonar"),
        "weapons": total * _alloc(ship, "weapons"),
        "engineering": total * _alloc(ship, "engineering"),
    }


def speed_cap_fraction(ship) -> float:
    """Fraction of ``hull.max_speed`` the routed propulsion power permits (0..1).

    At ``helm=0.25`` this equals ``output_mw/max_mw`` exactly (legacy). Below 25%
    the boat cannot reach flank even with a hot reactor; above 25% it can hit full
    speed on a lower reactor setting.
    """
    max_mw = max(1.0, float(ship.reactor.max_mw))
    prop_full_mw = max_mw * HELM_NOMINAL_FRAC
    prop_mw = _total_mw(ship) * _alloc(ship, "helm")
    return _clamp(prop_mw / max(1e-6, prop_full_mw), 0.0, 1.0)


def sonar_snr_bonus_db(ship) -> float:
    """Signed dB applied to passive SNR from routed sonar power (0 at nominal)."""
    sonar_mw = route_mw(ship)["sonar"]
    db = SONAR_SNR_PER_MW * (sonar_mw - SONAR_NOMINAL_MW)
    return _clamp(db, SONAR_SNR_MIN_DB, SONAR_SNR_MAX_DB)


def reload_multiplier(ship) -> float:
    """Multiplier on tube timers from routed weapons power (1.0 at nominal)."""
    weap_mw = route_mw(ship)["weapons"]
    ratio = weap_mw / max(1e-6, WEAP_NOMINAL_MW)
    return _clamp(1.0 / max(1e-6, ratio), RELOAD_MULT_MIN, RELOAD_MULT_MAX)


def maintenance_rate(ship) -> float:
    """Per-second maintenance progression from routed engineering power.

    Preserves the legacy ``(eng_mw/max_mw)*0.1`` curve while routing through the
    shared helper so the Engineering UI can surface the same number it acts on.
    """
    eng_mw = route_mw(ship)["engineering"]
    return (eng_mw / max(1.0, float(ship.reactor.max_mw))) * 0.1


def reactor_noise_points(ship) -> float:
    """Signature points (0..REACTOR_NOISE_MAX) the running reactor contributes."""
    mw = _total_mw(ship)
    span = max(1.0, float(ship.reactor.max_mw) - REACTOR_QUIET_MW)
    frac = max(0.0, (mw - REACTOR_QUIET_MW) / span)
    return _clamp(frac * REACTOR_NOISE_MAX, 0.0, REACTOR_NOISE_MAX)
