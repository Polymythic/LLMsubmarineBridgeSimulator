"""Shared plotting board — collaborative tactical map state.

A plotting board the whole crew can edit: bearing lines stamped down at
ownship's position, contact markers in world coordinates with a heading,
free-text captain notes. Lives in memory on the Simulation, broadcast to
all subscribers each tick. Cleared on mission load.

Pure dataclasses + a thin manager. No I/O, no LLM, no asyncio.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# Allowed contact types — UI maps these to colors. Keep the literal narrow
# so a typo client-side gets coerced to "unknown" rather than rendering
# something bizarre.
CONTACT_TYPES = ("unknown", "enemy", "neutral", "friendly")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000) % 100_000_000:08d}"


@dataclass
class PlotBearing:
    """A true-bearing ray stamped at ownship's position-at-time-of-mark.

    The ray is anchored in WORLD coordinates — it does not slide with
    ownship. Operators drop bearings as they hear them; the ray remains
    where it was placed so the geometry of multiple bearings can be
    triangulated visually.
    """
    id: str
    anchor_x: float
    anchor_y: float
    bearing_deg: float
    label: str = ""
    color: str = "#FACC15"
    created_at_s: float = 0.0


@dataclass
class PlotContact:
    """A contact marker in world coordinates with an editable heading.

    `type` controls the rendered color. `heading_deg` is the direction the
    contact's bow points — used by the UI to orient the triangle marker.
    """
    id: str
    x: float
    y: float
    heading_deg: float = 0.0
    type: str = "unknown"
    label: str = ""
    created_at_s: float = 0.0


@dataclass
class PlotNote:
    """Free-text captain note, append-only."""
    id: str
    text: str
    at: str  # ISO timestamp


@dataclass
class PlotBoard:
    bearings: Dict[str, PlotBearing] = field(default_factory=dict)
    contacts: Dict[str, PlotContact] = field(default_factory=dict)
    notes: List[PlotNote] = field(default_factory=list)
    version: int = 0  # bumped on every mutation so clients can reconcile

    # ------------------------------------------------------------------
    # Mutators — each bumps version
    # ------------------------------------------------------------------
    def add_bearing(self, anchor_x: float, anchor_y: float, bearing_deg: float,
                    label: str = "", color: str = "#FACC15") -> PlotBearing:
        b = PlotBearing(
            id=_new_id("bearing"),
            anchor_x=float(anchor_x), anchor_y=float(anchor_y),
            bearing_deg=float(bearing_deg) % 360.0,
            label=label or "",
            color=color or "#FACC15",
            created_at_s=time.time(),
        )
        self.bearings[b.id] = b
        self.version += 1
        return b

    def remove_bearing(self, bearing_id: str) -> bool:
        if bearing_id in self.bearings:
            del self.bearings[bearing_id]
            self.version += 1
            return True
        return False

    def add_contact(self, x: float, y: float, type_: str = "unknown",
                    heading_deg: float = 0.0, label: str = "") -> PlotContact:
        if type_ not in CONTACT_TYPES:
            type_ = "unknown"
        c = PlotContact(
            id=_new_id("contact"),
            x=float(x), y=float(y),
            heading_deg=float(heading_deg) % 360.0,
            type=type_,
            label=label or "",
            created_at_s=time.time(),
        )
        self.contacts[c.id] = c
        self.version += 1
        return c

    def update_contact(self, contact_id: str, **fields) -> Optional[PlotContact]:
        c = self.contacts.get(contact_id)
        if c is None:
            return None
        if "x" in fields and fields["x"] is not None:
            c.x = float(fields["x"])
        if "y" in fields and fields["y"] is not None:
            c.y = float(fields["y"])
        if "heading_deg" in fields and fields["heading_deg"] is not None:
            c.heading_deg = float(fields["heading_deg"]) % 360.0
        if "type" in fields and fields["type"] in CONTACT_TYPES:
            c.type = fields["type"]
        if "label" in fields and fields["label"] is not None:
            c.label = str(fields["label"])
        self.version += 1
        return c

    def remove_contact(self, contact_id: str) -> bool:
        if contact_id in self.contacts:
            del self.contacts[contact_id]
            self.version += 1
            return True
        return False

    def append_note(self, text: str) -> PlotNote:
        n = PlotNote(
            id=_new_id("note"),
            text=str(text or "").strip(),
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self.notes.append(n)
        if len(self.notes) > 200:
            self.notes = self.notes[-200:]
        self.version += 1
        return n

    def clear(self) -> None:
        self.bearings.clear()
        self.contacts.clear()
        self.notes.clear()
        self.version += 1

    def to_telemetry(self) -> Dict[str, Any]:
        """Serialize for broadcast. Compact dicts; all values JSON-safe."""
        return {
            "version": self.version,
            "bearings": [asdict(b) for b in self.bearings.values()],
            "contacts": [asdict(c) for c in self.contacts.values()],
            "notes": [asdict(n) for n in self.notes[-100:]],
        }
