from __future__ import annotations
from typing import Dict, Literal, Optional, List, Any
from pydantic import BaseModel, Field


class Kinematics(BaseModel):
    x: float = 0.0
    y: float = 0.0
    depth: float = 0.0
    heading: float = 0.0
    speed: float = 0.0  # knots
    turn_rate: float = 0.0
    accel: float = 0.0
    depth_rate: float = 0.0


class Hull(BaseModel):
    max_depth: float = 300.0
    crush_depth: float = 600.0
    max_speed: float = 30.0
    quiet_speed: float = 5.0
    turn_rate_max: float = 7.0
    accel_max: float = 0.5
    decel_max: float = 0.7


class Acoustics(BaseModel):
    source_level_by_speed: Dict[int, float] = Field(
        default_factory=lambda: {5: 110.0, 10: 118.0, 15: 130.0}
    )
    broadband_sig: float = 0.0
    thermocline_on: bool = True


class TorpedoDef(BaseModel):
    name: str = "Mk48"
    speed: float = 45.0
    seeker_cone_deg: float = 35.0
    enable_range_m: float = 800.0
    max_run_time_s: float = 600.0


class Tube(BaseModel):
    idx: int
    state: Literal["Empty", "Loaded", "Flooded", "DoorsOpen"] = "Empty"
    weapon: Optional[TorpedoDef] = None


class WeaponsSuite(BaseModel):
    tube_count: int = 6
    torpedoes_stored: int = 6
    reload_time_s: float = 45.0
    tubes: List[Tube] = Field(default_factory=lambda: [Tube(idx=i) for i in range(1, 7)])


class Reactor(BaseModel):
    output_mw: float = 60.0
    max_mw: float = 100.0
    scrammed: bool = False
    battery_pct: float = 100.0


class DamageState(BaseModel):
    hull: float = 0.0
    sensors: float = 0.0
    propulsion: float = 0.0
    flooding_rate: float = 0.0


class AIProfile(BaseModel):
    name: str = "stub"
    constraints: Dict[str, float] = Field(
        default_factory=lambda: {"maxSpeed": 18.0, "maxDepth": 300.0, "turnRate": 7.0}
    )


class Ship(BaseModel):
    id: str
    side: Literal["BLUE", "RED"]
    kin: Kinematics
    hull: Hull
    acoustics: Acoustics
    weapons: WeaponsSuite
    reactor: Reactor
    damage: DamageState
    ai_profile: Optional[AIProfile] = None


class TelemetryOwnship(BaseModel):
    heading: float
    orderedHeading: float
    speed: float
    depth: float
    cavitation: bool


class TelemetryContact(BaseModel):
    id: str
    bearing: float
    strength: float
    classifiedAs: str
    confidence: float


class TelemetryMessage(BaseModel):
    topic: Literal["telemetry"]
    data: Dict[str, Any]
