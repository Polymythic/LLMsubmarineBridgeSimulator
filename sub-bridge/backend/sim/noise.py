from __future__ import annotations

import math
from typing import Dict, List, Tuple, Any
import random


# Per-station loudness calibration: (quiet floor dB, loud ceiling dB).
# Each station's 5-band scale (minimal..extreme) and bar fill are normalized
# to ITS OWN range — i.e. "loud for me", not "loud compared to other stations".
# The ship total is calibrated absolutely: it is the real combined signature the
# captain reads, and the only readout where cross-station comparison is valid.
# These are tuning knobs — adjust against real play.
STATION_CALIB: Dict[str, Tuple[float, float]] = {
    "helm":        (50.0, 86.0),
    "sonar":       (45.0, 85.0),
    "weapons":     (45.0, 90.0),
    "engineering": (54.0, 90.0),
    "total":       (58.0, 95.0),
}

# Bottom-up so index 0 == quietest.
_BANDS = ("minimal", "low", "medium", "high", "extreme")


def _sum_db(levels: List[float]) -> float:
    if not levels:
        return 0.0
    lin = 0.0
    for l in levels:
        try:
            lin += 10.0 ** (float(l) / 10.0)
        except Exception:
            continue
    return 10.0 * math.log10(max(1e-12, lin))


def _fill_fraction(db: float, lo: float, hi: float) -> float:
    """Where `db` sits within a station's own [lo, hi] range, clamped to 0..1."""
    span = hi - lo
    if span <= 1e-6:
        return 0.0
    return max(0.0, min(1.0, (db - lo) / span))


def _band_for(db: float, lo: float, hi: float) -> str:
    """Bucket `db` into one of five equal bands across the station's own range."""
    f = _fill_fraction(db, lo, hi)
    idx = min(len(_BANDS) - 1, int(f * len(_BANDS)))
    return _BANDS[idx]


class NoiseEngine:
    """Aggregates station noise contributions (dB) from sustained sources and impulses.

    Stations: helm, sonar, weapons, engineering. Captain sees `total` only.

    tick() returns, per station (plus "total"), a dict:
        {
          "dB": <displayed level, jittered for a live bar>,
          "band": "minimal|low|medium|high|extreme",   # relative to this station
          "fill": <0..1 bar position within this station's own range>,
          "contributors": [{"label": str, "dB": float}, ...]  # loudest first
        }
    Per-station bands/fill are normalized to each station's own calibration so an
    operator sees "how loud am I, for me". `total` is calibrated absolutely.
    """

    def __init__(self) -> None:
        # impulses[station] = list of (level_db, ttl_s, label)
        self._impulses: Dict[str, List[Tuple[float, float, str]]] = {
            s: [] for s in ("helm", "sonar", "weapons", "engineering")
        }
        # track counts for world-based impulses (e.g., depth charges)
        self._last_depth_charge_count: int = 0

    def add_impulse(self, station: str, level_db: float, duration_s: float, label: str = "Transient") -> None:
        if station not in self._impulses:
            self._impulses[station] = []
        self._impulses[station].append((float(level_db), max(0.05, float(duration_s)), str(label)))

    def _tick_impulses(self, dt: float) -> Dict[str, List[Tuple[str, float]]]:
        """Decay impulses; return per-station list of (label, level) still active."""
        out: Dict[str, List[Tuple[str, float]]] = {s: [] for s in self._impulses.keys()}
        for st, lst in self._impulses.items():
            next_list: List[Tuple[float, float, str]] = []
            active: List[Tuple[str, float]] = []
            for (lvl, ttl, label) in lst:
                ttl2 = ttl - dt
                if ttl2 > 0:
                    next_list.append((lvl, ttl2, label))
                    active.append((label, lvl))
            self._impulses[st] = next_list
            out[st] = active
        return out

    def tick(self, own, world, dt: float, loop_state: Any) -> Dict[str, Any]:
        # Labeled sustained contributions per station: (label, level_db)
        contrib: Dict[str, List[Tuple[str, float]]] = {
            "helm": [], "sonar": [], "weapons": [], "engineering": []
        }

        # Helm: propulsion baseline from speed (map 0..max_speed to ~50..75 dB)
        try:
            max_spd = max(1.0, float(own.hull.max_speed))
            frac = max(0.0, min(1.0, float(own.kin.speed) / max_spd))
            contrib["helm"].append(("Propulsion", 50.0 + 25.0 * (frac ** 1.2)))
        except Exception:
            pass

        # Engineering: reactor baseline from MW (map 0..max_mw to ~55..78 dB)
        try:
            mw = float(getattr(own.reactor, "output_mw", 0.0))
            max_mw = float(getattr(own.reactor, "max_mw", 100.0)) or 100.0
            frac = max(0.0, min(1.0, mw / max_mw))
            contrib["engineering"].append(("Reactor", 55.0 + 23.0 * (frac ** 1.1)))
        except Exception:
            pass

        # Sonar: mast mechanics when raised
        try:
            if bool(getattr(loop_state, "_periscope_raised", False)):
                contrib["sonar"].append(("Periscope mast", 60.0))
            if bool(getattr(loop_state, "_radio_raised", False)):
                contrib["sonar"].append(("Radio mast", 60.0))
        except Exception:
            pass

        # Engineering: ballast/flood pumps from loop flags
        try:
            if bool(getattr(loop_state, "_pump_fwd", False)):
                contrib["engineering"].append(("Ballast pump (fwd)", 72.0))
            if bool(getattr(loop_state, "_pump_aft", False)):
                contrib["engineering"].append(("Ballast pump (aft)", 72.0))
        except Exception:
            pass

        # Weapons: tube operations noise during timed state transitions
        try:
            for t in own.weapons.tubes:
                ts = float(getattr(t, "timer_s", 0.0) or 0.0)
                if ts <= 0.0:
                    continue
                nx = getattr(t, "next_state", None)
                idx = getattr(t, "idx", "?")
                if nx == "Loaded":
                    contrib["weapons"].append((f"Tube {idx}: loading", 62.0))
                elif nx == "Flooded":
                    contrib["weapons"].append((f"Tube {idx}: flooding", 68.0))
                elif nx == "DoorsOpen":
                    contrib["weapons"].append((f"Tube {idx}: doors", 72.0))
        except Exception:
            pass

        # Maintenance tasks: base by station × stage multiplier
        try:
            tasks = getattr(loop_state, "_active_tasks", {}) or {}
            base_by_station = {"helm": 60.0, "sonar": 58.0, "weapons": 64.0, "engineering": 66.0}
            for station, task_list in (tasks.items() if isinstance(tasks, dict) else []):
                if station not in contrib:
                    continue
                for task in task_list:
                    stage = getattr(task, "stage", "task")
                    base = base_by_station.get(station, 60.0)
                    mult = 1.0 if stage == "task" else (1.25 if stage == "failing" else 1.5)
                    title = getattr(task, "title", None) or "Maintenance"
                    suffix = "" if stage == "task" else f" ({stage})"
                    contrib[station].append((f"{title}{suffix}", base * mult))
        except Exception:
            pass

        # New depth charges cause a labeled weapons impulse
        try:
            count_dc = len(getattr(world, "depth_charges", []) or [])
            if count_dc > self._last_depth_charge_count:
                for _ in range(count_dc - self._last_depth_charge_count):
                    self.add_impulse("weapons", 80.0, 0.5, "Depth charge")
            self._last_depth_charge_count = count_dc
        except Exception:
            pass

        impulse_contrib = self._tick_impulses(dt)

        out: Dict[str, Any] = {}
        clean_levels: Dict[str, float] = {}
        for st in ("helm", "sonar", "weapons", "engineering"):
            items: List[Tuple[str, float]] = list(contrib.get(st, []))
            items.extend(impulse_contrib.get(st, []))
            # Clean (un-jittered) level drives the band so the label doesn't flicker.
            clean = _sum_db([lvl for (_, lvl) in items])
            clean_levels[st] = clean
            # Jitter the *displayed* value so the bar flutters; band stays steady.
            display = (clean + random.uniform(-0.7, 0.7)) if clean > 0.0 else 0.0
            lo, hi = STATION_CALIB[st]
            ranked = sorted(items, key=lambda kv: kv[1], reverse=True)
            out[st] = {
                "dB": round(display, 1),
                "band": _band_for(clean, lo, hi),
                "fill": round(_fill_fraction(display, lo, hi), 3),
                "contributors": [{"label": lbl, "dB": round(lvl, 1)} for (lbl, lvl) in ranked],
            }

        # Ship total — absolute calibration (what the captain reads). Contributors
        # become per-station lines so the captain sees which station dominates.
        total_clean = _sum_db([clean_levels[s] for s in ("helm", "sonar", "weapons", "engineering")])
        lo, hi = STATION_CALIB["total"]
        station_summary = sorted(
            [(s.capitalize(), clean_levels[s]) for s in ("helm", "sonar", "weapons", "engineering") if clean_levels[s] > 0.0],
            key=lambda kv: kv[1], reverse=True,
        )
        out["total"] = {
            "dB": round(total_clean, 1),
            "band": _band_for(total_clean, lo, hi),
            "fill": round(_fill_fraction(total_clean, lo, hi), 3),
            "contributors": [{"label": lbl, "dB": round(lvl, 1)} for (lbl, lvl) in station_summary],
        }
        return out
