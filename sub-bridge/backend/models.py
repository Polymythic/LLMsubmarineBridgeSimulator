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
    # Additional noise injected into bearing measurement sigma (degradation effects)
    bearing_noise_extra: float = 0.0
    # Task-driven modifiers
    passive_snr_penalty_db: float = 0.0
    hydro_bearing_bias_deg: float = 0.0
    active_range_noise_add_m: float = 0.0
    active_bearing_noise_extra: float = 0.0
    thermocline_bias: float = 0.0  # >0 worsens propagation model
    # Derived detectability for debug/telemetry
    last_snr_db: float = 0.0
    last_detectability: float = 0.0


class PowerAllocations(BaseModel):
    helm: float = 0.25
    weapons: float = 0.25
    sonar: float = 0.25
    engineering: float = 0.25


class SystemsStatus(BaseModel):
    rudder_ok: bool = True
    ballast_ok: bool = True
    sonar_ok: bool = True
    radio_ok: bool = True
    periscope_ok: bool = True
    tubes_ok: bool = True


class MaintenanceState(BaseModel):
    # Levels from 0.0 (failed) to 1.0 (fully maintained)
    levels: Dict[str, float] = Field(
        default_factory=lambda: {
            "rudder": 1.0,
            "ballast": 1.0,
            "sonar": 1.0,
            "radio": 1.0,
            "periscope": 1.0,
            "tubes": 1.0,
        }
    )


class TorpedoDef(BaseModel):
    name: str = "Mk48"
    speed: float = 45.0
    seeker_cone_deg: float = 35.0
    seeker_range_m: float = 4000.0
    enable_range_m: float = 800.0
    max_run_time_s: float = 600.0


class Tube(BaseModel):
    idx: int
    state: Literal["Empty", "Loaded", "Flooded", "DoorsOpen"] = "Empty"
    weapon: Optional[TorpedoDef] = None
    timer_s: float = 0.0
    next_state: Optional[Literal["Loaded", "Flooded", "DoorsOpen"]] = None


class WeaponsSuite(BaseModel):
    tube_count: int = 6
    torpedoes_stored: int = 6
    reload_time_s: float = 45.0
    flood_time_s: float = 8.0
    doors_time_s: float = 3.0
    tubes: List[Tube] = Field(default_factory=lambda: [Tube(idx=i) for i in range(1, 7)])
    # Multiplier > 1.0 slows weapon timers (degradation effects)
    time_penalty_multiplier: float = 1.0
    # Depth charges (for surface combatants like Destroyers)
    depth_charges_stored: int = 0
    depth_charge_cooldown_s: float = 2.0
    depth_charge_cooldown_timer_s: float = 0.0


class ShipCapabilities(BaseModel):
    # Navigation control
    can_set_nav: bool = True
    # Sensors
    has_active_sonar: bool = True
    # Weapons
    has_torpedoes: bool = True
    has_guns: bool = False
    has_depth_charges: bool = False
    # Countermeasures available to deploy via tool calls
    countermeasures: List[Literal["noisemaker", "decoy"]] = Field(default_factory=list)


class ShipDef(BaseModel):
    name: str
    ship_class: Literal["SSN", "Convoy", "Destroyer"]
    capabilities: ShipCapabilities
    # Defaults that tune ships of this class; applied on spawn/assignment
    default_hull: Hull = Hull()
    default_weapons: WeaponsSuite = WeaponsSuite()
    default_acoustics: Acoustics = Acoustics()


SHIP_CATALOG: Dict[str, ShipDef] = {
    "SSN": ShipDef(
        name="Nuclear Attack Submarine",
        ship_class="SSN",
        capabilities=ShipCapabilities(
            can_set_nav=True,
            has_active_sonar=True,
            has_torpedoes=True,
            has_guns=False,
            has_depth_charges=False,
            countermeasures=["noisemaker", "decoy"],
        ),
        default_hull=Hull(max_depth=300.0, max_speed=30.0, quiet_speed=5.0),
        default_weapons=WeaponsSuite(),
        default_acoustics=Acoustics(),
    ),
    "Convoy": ShipDef(
        name="Convoy Cargo Vessel",
        ship_class="Convoy",
        capabilities=ShipCapabilities(
            can_set_nav=True,
            has_active_sonar=False,
            has_torpedoes=False,
            has_guns=False,
            has_depth_charges=False,
            countermeasures=[],
        ),
        default_hull=Hull(max_depth=20.0, max_speed=20.0, quiet_speed=5.0),
        default_weapons=WeaponsSuite(tube_count=0, torpedoes_stored=0, tubes=[]),
        default_acoustics=Acoustics(
            thermocline_on=False,
            source_level_by_speed={5: 120.0, 10: 130.0, 15: 140.0},
        ),
    ),
    "Destroyer": ShipDef(
        name="Destroyer (ASW)",
        ship_class="Destroyer",
        capabilities=ShipCapabilities(
            can_set_nav=True,
            has_active_sonar=True,
            has_torpedoes=False,  # placeholder until depth charges/ASROC modeled
            has_guns=True,
            has_depth_charges=True,
            countermeasures=[],
        ),
        default_hull=Hull(max_depth=50.0, max_speed=32.0, quiet_speed=8.0),
        default_weapons=WeaponsSuite(tube_count=0, torpedoes_stored=0, tubes=[], depth_charges_stored=30),
        default_acoustics=Acoustics(
            thermocline_on=False,
            source_level_by_speed={5: 125.0, 15: 140.0, 25: 150.0},
        ),
    ),
}


class MaintenanceTask(BaseModel):
    id: str
    station: Literal["helm", "sonar", "weapons", "engineering"]
    system: Literal["rudder", "sonar", "tubes", "ballast"]
    key: str
    title: str
    stage: Literal["task", "failing", "failed"] = "task"
    progress: float = 0.0  # 0..1
    started: bool = False
    base_deadline_s: float = 30.0
    time_remaining_s: float = 30.0
    created_at: float = 0.0


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
    power: PowerAllocations = PowerAllocations()
    systems: SystemsStatus = SystemsStatus()
    maintenance: MaintenanceState = MaintenanceState()
    ai_profile: Optional[AIProfile] = None
    # Classification & capabilities for AI and UI
    ship_class: Optional[Literal["SSN", "Convoy", "Destroyer"]] = None
    capabilities: Optional[ShipCapabilities] = None


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
    bearingKnown: bool = True
    rangeKnown: bool = False
    # Passive sonar enhancements (optional fields)
    detectability: Optional[float] = None
    snrDb: Optional[float] = None
    bearingSigmaDeg: Optional[float] = None


class TelemetryMessage(BaseModel):
    topic: Literal["telemetry"]
    data: Dict[str, Any]
