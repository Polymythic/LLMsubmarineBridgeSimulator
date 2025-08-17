from __future__ import annotations

import math
from typing import Dict, List, Tuple, Any
import random


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


class NoiseEngine:
    """Aggregates station noise contributions (dB) from sustained sources and impulses.

    Stations: helm, sonar, weapons, engineering
    Captain sees total only.
    """

    def __init__(self) -> None:
        # impulses[(station)] = list of (level_db, ttl_s)
        self._impulses: Dict[str, List[Tuple[float, float]]] = {s: [] for s in ("helm", "sonar", "weapons", "engineering")}
        # track counts for world-based impulses (e.g., depth charges)
        self._last_depth_charge_count: int = 0

    def add_impulse(self, station: str, level_db: float, duration_s: float) -> None:
        if station not in self._impulses:
            self._impulses[station] = []
        self._impulses[station].append((float(level_db), max(0.05, float(duration_s))))

    def _tick_impulses(self, dt: float) -> Dict[str, float]:
        out: Dict[str, float] = {s: 0.0 for s in self._impulses.keys()}
        for st, lst in self._impulses.items():
            next_list: List[Tuple[float, float]] = []
            levels: List[float] = []
            for (lvl, ttl) in lst:
                ttl2 = ttl - dt
                if ttl2 > 0:
                    next_list.append((lvl, ttl2))
                    levels.append(lvl)
            self._impulses[st] = next_list
            out[st] = _sum_db(levels) if levels else 0.0
        return out

    def tick(self, own, world, dt: float, loop_state: Any) -> Dict[str, float]:
        # Sustained contributions per station
        sustained: Dict[str, List[float]] = {"helm": [], "sonar": [], "weapons": [], "engineering": []}

        # Helm: propulsion baseline from speed (map 0..max_speed to ~50..75 dB)
        try:
            max_spd = max(1.0, float(own.hull.max_speed))
            frac = max(0.0, min(1.0, float(own.kin.speed) / max_spd))
            helm_base = 50.0 + 25.0 * (frac ** 1.2)
            sustained["helm"].append(helm_base)
        except Exception:
            pass
        # Helm: cavitation spike → we treat as impulse; loop_state provides 'cav' in tick scope, but we cannot access it here reliably
        # Callers may add impulses externally when cavitation is detected.

        # Engineering: reactor baseline from MW (map 0..max_mw ~ 55..78 dB)
        try:
            mw = float(getattr(own.reactor, "output_mw", 0.0))
            max_mw = float(getattr(own.reactor, "max_mw", 100.0))
            frac = max(0.0, min(1.0, mw / max_mw))
            eng_base = 55.0 + 23.0 * (frac ** 1.1)
            sustained["engineering"].append(eng_base)
        except Exception:
            pass
        # Captain station: masts mechanics (periscope/radio) cause sustained small noise when raised
        try:
            if bool(getattr(loop_state, "_periscope_raised", False)):
                sustained["sonar"].append(60.0)
            if bool(getattr(loop_state, "_radio_raised", False)):
                sustained["sonar"].append(60.0)
        except Exception:
            pass

        # Engineering: ballast/flood pumps from loop flags
        try:
            if bool(getattr(loop_state, "_pump_fwd", False)):
                sustained["engineering"].append(72.0)
            if bool(getattr(loop_state, "_pump_aft", False)):
                sustained["engineering"].append(72.0)
        except Exception:
            pass

        # Weapons: tube operations noise during actions (timed states)
        try:
            for t in own.weapons.tubes:
                ts = float(getattr(t, "timer_s", 0.0) or 0.0)
                nx = getattr(t, "next_state", None)
                if ts > 0.0 and nx == "Loaded":
                    # loading clanks
                    sustained["weapons"].append(62.0)
                if ts > 0.0 and nx == "Flooded":
                    # pump noise while flooding
                    sustained["weapons"].append(68.0)
                if ts > 0.0 and nx == "DoorsOpen":
                    # door motor while opening/closing
                    sustained["weapons"].append(72.0)
        except Exception:
            pass

        # Maintenance tasks: base by station + stage multiplier
        try:
            tasks = getattr(loop_state, "_active_tasks", {}) or {}
            # Base defaults per station
            base_by_station = {"helm": 60.0, "sonar": 58.0, "weapons": 64.0, "engineering": 66.0}
            for station, task_list in (tasks.items() if isinstance(tasks, dict) else []):
                for task in task_list:
                    stage = getattr(task, "stage", "task")
                    base = base_by_station.get(station, 60.0)
                    mult = 1.0 if stage == "task" else (1.25 if stage == "failing" else 1.5)
                    sustained.get(station, []).append(base * mult / 1.0)
        except Exception:
            pass

        # New depth charges cause a weapons impulse
        try:
            count_dc = len(getattr(world, "depth_charges", []) or [])
            if count_dc > self._last_depth_charge_count:
                # Each new DC adds an impulse
                for _ in range(count_dc - self._last_depth_charge_count):
                    self.add_impulse("weapons", 80.0, 0.5)
            self._last_depth_charge_count = count_dc
        except Exception:
            pass

        # Sum sustained and impulses per station
        impulse_levels = self._tick_impulses(dt)
        station_levels: Dict[str, float] = {}
        for st in ("helm", "sonar", "weapons", "engineering"):
            sustained_db = _sum_db(sustained.get(st, []))
            if impulse_levels.get(st, 0.0) > 0.0:
                station_levels[st] = _sum_db([sustained_db, impulse_levels[st]])
            else:
                station_levels[st] = sustained_db
        # Small random jitter per station for UI liveliness
        for st in ("helm", "sonar", "weapons", "engineering"):
            lvl = station_levels.get(st, 0.0)
            if lvl > 0.0:
                station_levels[st] = max(0.0, lvl + random.uniform(-0.7, 0.7))
        # Total ship noise from jittered stations
        total = _sum_db([station_levels.get(s, 0.0) for s in ("helm", "sonar", "weapons", "engineering")])
        station_levels["total"] = total
        return station_levels


