"""BLUE Fleet Commander intel buffer + prompt builder.

Pure module. No I/O, no LLM, no asyncio. Same shape as `tactical.py` —
callable from a plain pytest. The orchestrator owns one `BlueIntelBuffer`,
calls `record_sample` on a slow sim-time cadence, and asks for releasable
snapshots when it's time to brief the player sub via radio.

Design intent:
    - The BLUE fleet commander LLM should NEVER see live RED positions.
      It sees only snapshots that are at least `min_age_s` old, simulating
      the lag of decoded intercepts and other-unit reports.
    - Snapshots are intentionally lossy: positions rounded to ~1 km,
      headings to 30°, classes downgraded to generic descriptors. This
      mirrors what a fleet command center would actually have, not what
      the simulator knows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class BlueIntelSnapshot:
    """One observation of RED state taken at a specific sim time.

    `age_s_at` is filled in when the snapshot is read out — it's the
    delta between snapshot time and the current sim time at brief time.
    """

    sim_time_s: float
    """Sim time (seconds since mission start) when the snapshot was taken."""

    contacts: List[Dict[str, Any]]
    """List of {id, class, pos_grid, heading_bin_deg, speed_kn_round, side}."""

    source: str
    """Synthetic source label: 'intercept' | 'patrol_report' | 'shore_observation'."""


def _round_to(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def _grid_pos(x: float, y: float, step_m: float = 1000.0) -> List[float]:
    """Round position to a coarse grid (default 1 km). Loses precision on
    purpose so the LLM can't extrapolate exact intercept courses."""
    return [_round_to(x, step_m), _round_to(y, step_m)]


def _heading_bin(deg: float, bin_deg: float = 30.0) -> float:
    """Snap heading to coarse bins (default 30°)."""
    return _round_to(deg % 360.0, bin_deg) % 360.0


def _speed_round(kn: float) -> float:
    return float(int(round(kn)))


def sample_red_state(world, sim_time_s: float) -> BlueIntelSnapshot:
    """Produce a single BlueIntelSnapshot from a `world` at `sim_time_s`.

    Captures only RED-side ships. Does not capture submarines below the
    surface threshold (depth > 30m) — those would not be visible to other
    units or interceptable. Lossy on purpose.
    """
    contacts: List[Dict[str, Any]] = []
    try:
        ships = world.all_ships()
    except Exception:
        ships = []
    for ship in ships:
        if getattr(ship, "side", None) != "RED":
            continue
        # Surface units only — submarines aren't observed by other ships
        # at any useful range, so they shouldn't appear in fleet intel.
        depth = getattr(getattr(ship, "kin", None), "depth", 0.0)
        if depth > 30.0:
            continue
        contacts.append({
            "id": getattr(ship, "id", "unknown"),
            "class": getattr(ship, "ship_class", "Unknown") or "Unknown",
            "pos_grid": _grid_pos(ship.kin.x, ship.kin.y),
            "heading_bin_deg": _heading_bin(ship.kin.heading),
            "speed_kn_round": _speed_round(ship.kin.speed),
        })
    return BlueIntelSnapshot(
        sim_time_s=sim_time_s,
        contacts=contacts,
        source="intercept",
    )


@dataclass
class BlueIntelBuffer:
    """Rolling buffer of dated RED intel snapshots.

    `record_sample(world, sim_time_s)` adds a snapshot. `releasable(now_s,
    min_age_s)` returns the snapshots old enough to share with the LLM.
    """

    snapshots: List[BlueIntelSnapshot] = field(default_factory=list)
    max_snapshots: int = 24

    def record_sample(self, world, sim_time_s: float) -> BlueIntelSnapshot:
        snap = sample_red_state(world, sim_time_s)
        self.snapshots.append(snap)
        if len(self.snapshots) > self.max_snapshots:
            self.snapshots = self.snapshots[-self.max_snapshots:]
        return snap

    def releasable(self, now_s: float, min_age_s: float) -> List[BlueIntelSnapshot]:
        """Return snapshots whose age >= min_age_s, oldest first."""
        out = [s for s in self.snapshots if (now_s - s.sim_time_s) >= min_age_s]
        out.sort(key=lambda s: s.sim_time_s)
        return out

    def reset(self) -> None:
        self.snapshots = []


def build_prompt_payload(
    *,
    snapshots: Sequence[BlueIntelSnapshot],
    now_s: float,
    mission_brief: Optional[Dict[str, Any]],
    ownship_summary: Dict[str, Any],
    last_brief_age_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Assemble the payload the LLM sees — only fleet-side knowable data.

    The LLM never gets a live world reference; it only gets dated snapshots
    plus the mission narrative and the ownship's reported state.
    """
    intel_entries: List[Dict[str, Any]] = []
    for snap in snapshots:
        age_s = max(0.0, now_s - snap.sim_time_s)
        intel_entries.append({
            "as_of_s_ago": int(age_s),
            "source": snap.source,
            "contacts": snap.contacts,
        })
    blue_context = None
    objective = None
    if isinstance(mission_brief, dict):
        sc = mission_brief.get("scenario_context")
        if isinstance(sc, dict):
            blue_context = sc.get("BLUE")
        objective = (
            mission_brief.get("blue_captain_summary")
            or (mission_brief.get("side_objectives") or {}).get("BLUE")
            or mission_brief.get("objective")
        )
    return {
        "elapsed_mission_s": int(now_s),
        "mission": {
            "id": (mission_brief or {}).get("id"),
            "title": (mission_brief or {}).get("title"),
            "objective": objective,
            "blue_context": blue_context,
        },
        "ownship_reported": ownship_summary,
        "intel_snapshots": intel_entries,
        "since_last_brief_s": last_brief_age_s,
    }
