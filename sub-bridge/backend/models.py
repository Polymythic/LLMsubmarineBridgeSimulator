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
    thermocline_depth_m: float = 50.0  # Depth of thermocline layer (configurable per mission)
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
    # AI quick torpedo launch (bypasses tubes for NPCs)
    torpedo_quick_cooldown_s: float = 10.0
    torpedo_quick_cooldown_timer_s: float = 0.0
    # Countermeasure inventory
    noisemakers_stored: int = 6
    decoys_stored: int = 4


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
    ship_class: Literal["SSN", "Convoy", "Destroyer", "Neutral"]
    capabilities: ShipCapabilities
    # Defaults that tune ships of this class; applied on spawn/assignment
    default_hull: Hull = Hull()
    default_weapons: WeaponsSuite = WeaponsSuite()
    default_acoustics: Acoustics = Acoustics()


# Ship catalog is loaded from assets/ships/catalog.json at runtime
# This provides a fallback for tests and ensures the catalog is always available
SHIP_CATALOG: Dict[str, ShipDef] = {}


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


# Compartment names for the 6 submarine sections
COMPARTMENT_NAMES = [
    "Fore (Torpedo Room)",
    "Forward (Crew)",
    "Control Room",
    "Aft (Reactor)",
    "Engine Room",
    "Stern (Steering)",
]


class CompartmentState(BaseModel):
    """State of a single submarine compartment"""
    flooding_level: float = 0.0      # 0.0-1.0 (0%=dry, 100%=flooded)
    hull_integrity: float = 1.0      # 0.0-1.0 (structural integrity)
    pump_active: bool = False        # Is pump assigned to this compartment?
    breach_rate: float = 0.0         # Water ingress rate per second


class DamageState(BaseModel):
    """Ship-wide damage state with compartments"""
    hull: float = 0.0                # Overall hull damage (average of compartment damage)
    sensors: float = 0.0
    propulsion: float = 0.0
    compartments: List[CompartmentState] = Field(
        default_factory=lambda: [CompartmentState() for _ in range(6)]
    )

    @property
    def flooding_rate(self) -> float:
        """Legacy field for backwards compatibility - average flooding across compartments"""
        if not self.compartments:
            return 0.0
        return sum(c.flooding_level for c in self.compartments) / len(self.compartments)

    def dict(self, **kwargs):
        """Override dict to include computed flooding_rate property"""
        d = super().dict(**kwargs)
        d["flooding_rate"] = self.flooding_rate
        return d


class AIProfile(BaseModel):
    name: str = "stub"
    constraints: Dict[str, float] = Field(
        default_factory=lambda: {"maxSpeed": 18.0, "maxDepth": 300.0, "turnRate": 7.0}
    )


class Ship(BaseModel):
    id: str
    side: Literal["BLUE", "RED", "NEUTRAL"]
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
    ship_class: Optional[Literal["SSN", "Convoy", "Destroyer", "Neutral"]] = None
    capabilities: Optional[ShipCapabilities] = None
    # Contact tracking for intercept calculations
    contact_tracks: List[ContactTrack] = Field(default_factory=list)
    # Active sonar cooldown for AI ships
    active_sonar_cooldown: float = 0.0
    # Waypoint route for game rules tracking (fleet commander navigates, game rules verify)
    route: Optional["WaypointRoute"] = None


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


class ContactTrack(BaseModel):
    """Simple contact tracking for intercept calculations"""
    contact_id: str
    last_known_x: float
    last_known_y: float
    last_known_depth: float
    last_known_heading: float
    last_known_speed: float
    last_seen_time: float
    track_confidence: float = 0.0


# ============================================================================
# Scenario and Game Rules Models
# ============================================================================


class Waypoint(BaseModel):
    """Single waypoint in a route"""
    x: float
    y: float
    speed_kn: Optional[float] = None      # Override speed for this leg
    hold_s: Optional[float] = None        # Time to hold at waypoint
    name: Optional[str] = None            # e.g., "Alpha", "Rally Point 1"


class WaypointRoute(BaseModel):
    """Multi-waypoint route for a ship - used by game rules to track progress"""
    waypoints: List[Waypoint] = Field(default_factory=list)
    current_idx: int = 0                  # Index of next waypoint to reach
    loop: bool = False                    # Patrol loop back to start
    arrival_threshold_m: float = 100.0    # Distance to consider waypoint reached


class ScenarioCondition(BaseModel):
    """A condition that can trigger scenario events"""
    type: Literal[
        "waypoint_reached",   # Ship reached a waypoint
        "ship_destroyed",     # Ship hull damage >= threshold
        "contact_detected",   # Observer detected target with confidence
        "damage_threshold",   # Ship damage exceeds value
        "time_elapsed",       # Simulation time >= value
        "distance_to",        # Distance between entities
        "all_of",             # All sub-conditions true
        "any_of"              # Any sub-condition true
    ]
    params: Dict[str, Any] = Field(default_factory=dict)


class ScenarioAction(BaseModel):
    """Action to take when a trigger condition is met"""
    type: Literal[
        "send_comms",         # Send message to captain comms
        "broadcast_intercept", # Broadcast interceptable enemy message
        "change_behavior",    # Update ship_behaviors for a ship
        "end_scenario",       # End the mission with outcome
        "set_ai_mode"         # Change AI behavior mode
    ]
    params: Dict[str, Any] = Field(default_factory=dict)


class ScenarioTrigger(BaseModel):
    """A trigger combines conditions with actions"""
    id: str
    condition: ScenarioCondition
    actions: List[ScenarioAction] = Field(default_factory=list)
    once: bool = True                     # Fire only once
    fired: bool = False                   # Has this trigger fired?


class InterceptedComm(BaseModel):
    """An intercepted enemy communication"""
    source: Literal["scripted", "fleet_commander"]
    original_text: str
    intercepted_text: str                 # Possibly partial/degraded
    bearing: Optional[float] = None       # Direction finding bearing
    timestamp: str
    confidence: float = 1.0               # Interception quality


class MissionOutcome(BaseModel):
    """Tracks mission victory/defeat state"""
    status: Literal["ongoing", "victory", "defeat", "draw"] = "ongoing"
    reason: Optional[str] = None
    ended_at: Optional[str] = None


class TelemetryMessage(BaseModel):
    topic: Literal["telemetry"]
    data: Dict[str, Any]
