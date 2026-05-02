from __future__ import annotations
import asyncio
import json
import time
import math
from typing import Dict, Optional
import os
import random
from pathlib import Path
from ..bus import BUS
from ..config import CONFIG, reload_from_env
from ..models import Ship, Kinematics, Hull, Acoustics, WeaponsSuite, Reactor, DamageState, MaintenanceTask, SHIP_CATALOG, TelemetryContact
from ..assets import load_ship_catalog, load_mission_by_id, apply_mission_to_world
from ..storage import init_engine, create_run, insert_snapshot, insert_event
from .ecs import World
from .physics import integrate_kinematics
from .sonar import passive_contacts, ActivePingState, active_ping, passive_projectiles, explosion_contacts, countermeasure_contacts, normalize_angle_deg, clear_contact_memory
from .contact_registry import ContactRegistry
from .weapons import step_torpedo, step_tubes, try_drop_depth_charges, step_depth_charge, try_launch_torpedo_quick, step_countermeasure
from .ai_tools import LocalAIStub
from .ai_orchestrator import AgentsOrchestrator
from .damage import step_damage, step_engineering
from .noise import NoiseEngine
from .waypoints import WaypointTracker
from .conditions import ConditionEvaluator
from .triggers import TriggerManager
from .victory import VictoryEvaluator
from .intercepts import InterceptSystem
from .commands import CommandDispatcher
from .control import (
    ActivePingAction,
    LLMShipController,
    SetNavAction,
    ShipController,
    ShipControls,
)
from .core import SimulationCore
from ..models import MissionOutcome


def _unwrap_scalar(v):
    """LLM tool calls occasionally wrap scalars in single-element lists.

    Normalize list-of-one to its sole element; pass through everything else.
    """
    if isinstance(v, list):
        return v[0] if v else None
    return v


class Simulation:
    def __init__(self) -> None:
        self.dt = 1.0 / CONFIG.tick_hz
        self.world = World()
        self.active_ping_state = ActivePingState(cooldown_s=12.0)
        self.engine = init_engine(CONFIG.sqlite_path)
        self.run_id = create_run(self.engine)
        self.ai = LocalAIStub()
        self._loading = False
        self._captain_consent = False
        self._periscope_raised = False
        self._radio_raised = False
        # Pump assignments: maps pump number (1 or 2) to compartment index (0-5)
        # e.g., {1: 0, 2: 5} means pump 1 on fore (comp 0), pump 2 on stern (comp 5)
        self._pump_assignments: Dict[int, int] = {}
        # Debug toggles for visual detection (disabled by default)
        self._debug_player_visual_100 = False
        self._debug_enemy_visual_100 = False
        # Visual contact tracking for persistence and improved re-detection
        self._visual_contacts = {}  # {observer_id: {target_id: {"last_seen": timestamp, "detection_count": int, "last_confidence": float}}}
        # Periscope contacts list for UI display (persists between ticks)
        self._periscope_contacts = []
        # Contact registry for anonymous sonar designations
        self._contact_registry = ContactRegistry()
        # Lazily initialize asyncio.Event to avoid requiring an event loop during tests
        self._stop: Optional[asyncio.Event] = None
        self._last_snapshot = 0.0
        self._last_ai = 0.0
        self._transient_events = []  # cleared every tick
        self._destroyed_ships = set()  # track ships already destroyed to avoid duplicate events
        self._last_ping_responses = []
        self._last_ping_at = None
        # Mission state tracking for startup sequence
        self._mission_active = False      # True when a mission is running
        self._mission_version = 0         # Increments on each mission load (for client refresh detection)
        self._mission_id: Optional[str] = None  # Current mission ID
        # Load external assets
        try:
            # Import models here to avoid circular import issues
            from .. import models
            load_ship_catalog()
            # Ensure we have at least the basic ships if catalog loading failed
            if not models.SHIP_CATALOG:
                print("WARNING: Ship catalog is empty, using fallback definitions")
                # Add minimal fallback ships
                models.SHIP_CATALOG.update({
                    "SSN": models.ShipDef(
                        name="Nuclear Attack Submarine",
                        ship_class="SSN",
                        capabilities=models.ShipCapabilities(
                            can_set_nav=True, has_active_sonar=True, has_torpedoes=True,
                            has_guns=False, has_depth_charges=False, countermeasures=["noisemaker", "decoy"]
                        ),
                        default_hull=models.Hull(max_depth=300.0, max_speed=30.0, quiet_speed=5.0),
                        default_weapons=models.WeaponsSuite(),
                        default_acoustics=models.Acoustics(),
                    ),
                    "Destroyer": models.ShipDef(
                        name="Destroyer (ASW)",
                        ship_class="Destroyer", 
                        capabilities=models.ShipCapabilities(
                            can_set_nav=True, has_active_sonar=True, has_torpedoes=True,
                            has_guns=True, has_depth_charges=True, countermeasures=[]
                        ),
                        default_hull=models.Hull(max_depth=0.0, max_speed=32.0, quiet_speed=8.0),
                        default_weapons=models.WeaponsSuite(tube_count=2, torpedoes_stored=10, depth_charges_stored=30),
                        default_acoustics=models.Acoustics(thermocline_on=False, source_level_by_speed={5: 125.0, 15: 140.0, 25: 150.0}),
                    )
                })
        except Exception as e:
            print(f"ERROR: Failed to load ship catalog: {e}")
            pass
        self._init_default_world()
        # Station task state
        self._active_tasks: Dict[str, list[MaintenanceTask]] = {s: [] for s in ["helm", "sonar", "weapons", "engineering"]}
        self._task_spawn_timers: Dict[str, float] = {s: CONFIG.first_task_delay_s for s in ["helm", "sonar", "weapons", "engineering"]}
        # Noise aggregation engine
        self._noise = NoiseEngine()
        # Mission briefing and ROE (only set defaults if not set by mission assets)
        if not hasattr(self, "mission_brief"):
            self.mission_brief = {
                "title": "Patrol Box KILO-7",
                "objective": "Shadow contact RED-01, maintain undetected posture, do not fire unless fired upon.",
                "roe": [
                    "Weapons free upon hostile engagement or direct order.",
                    "Avoid active sonar unless necessary for navigation or identification.",
                    "Maintain EMCON; minimize mast raises."
                ],
                "comms_schedule": [
                    {"at_s": 120.0, "msg": "FLASH: New tasking window opens at 18:00Z. Await further instructions."},
                    {"at_s": 300.0, "msg": "INFO: Intel suggests RED-01 may alter course east within 10 minutes."}
                ],
            }
        self._delivered_comms_idx = -1
        # EMCON and storms
        self._emcon_high_timer = 0.0
        self._storm_timer = 0.0
        # Debug control: suppress spawning new maintenance tasks
        self._suppress_maintenance_spawns = False
        # AI orchestrator (optional)
        if CONFIG.use_ai_orchestrator:
            self._ai_orch = AgentsOrchestrator(lambda: self.world, self.engine, self.run_id)
            try:
                self._ai_orch.set_fleet_engine(getattr(CONFIG, "ai_fleet_engine", "stub"), getattr(CONFIG, "ai_fleet_model", "stub"))
                self._ai_orch.set_ship_engine(getattr(CONFIG, "ai_ship_engine", "stub"), getattr(CONFIG, "ai_ship_model", "stub"))
                # Provide mission brief for Fleet Commander inputs
                try:
                    setattr(self._ai_orch, "_mission_brief", self.mission_brief)
                except Exception:
                    pass
            except Exception:
                pass
            # Timers (sim-time based)
            # Prime timers so first runs occur quickly after startup
            self._ai_fleet_timer = getattr(CONFIG, "ai_fleet_cadence_s", 45.0)
            self._ai_ship_timers: Dict[str, float] = {}
            self._ai_pending: set[asyncio.Task] = set()

        # ========== Scenario and Game Rules Subsystems ==========
        # Simulation time counter
        self._sim_time_s = 0.0
        # Waypoint tracker (monitors ship positions, emits waypoint_reached events)
        self._waypoint_tracker = WaypointTracker(lambda: self.world)
        # Condition evaluator (checks trigger conditions)
        self._condition_evaluator = ConditionEvaluator(
            lambda: self.world,
            lambda: self,
            self._waypoint_tracker
        )
        # Trigger manager (fires actions when conditions are met)
        self._trigger_manager = TriggerManager(
            self._condition_evaluator,
            lambda: self.world,
            lambda: self
        )
        # Victory evaluator (checks success_criteria)
        self._victory_evaluator = VictoryEvaluator(
            lambda: self.world,
            self._waypoint_tracker
        )
        # Intercept system (handles intercepted enemy communications)
        self._intercept_system = InterceptSystem(lambda: self.world, lambda: self)
        # Captain intercepts list (accumulated for UI)
        self._captain_intercepts = []
        # Mission outcome
        self._mission_outcome = MissionOutcome()
        # Command dispatcher (single chokepoint for WebSocket commands)
        self._cmd_dispatcher = CommandDispatcher(self)
        # Per-ship decision-maker. Default is LLM-driven; tests/scenarios may
        # swap in `ScriptedShipController` or any other `ShipController`.
        self._ship_controller: ShipController = LLMShipController(
            lambda: getattr(self, "_ai_orch", None)
        )

    def _reset_all_state(self) -> None:
        """Reset ALL mutable sim state consistently for mission transitions."""
        # Deactivate mission while rebuilding
        self._mission_active = False
        self._mission_version += 1

        # Simulation clock
        self._sim_time_s = 0.0

        # Captain / comms state
        self._captain_comms = []
        self._captain_intercepts = []
        self._mission_outcome = MissionOutcome()
        self._delivered_comms_idx = -1

        # Equipment toggles
        self._periscope_raised = False
        self._radio_raised = False
        self._captain_consent = False
        self._pump_assignments = {}

        # Sonar / ping state
        self._last_ping_responses = []
        self._last_ping_at = None
        self.active_ping_state = ActivePingState(cooldown_s=12.0)

        # Visual / periscope contact tracking
        self._visual_contacts = {}
        self._enemy_search_timers = {}
        self._periscope_contacts = []
        self._destroyed_ships = set()
        self._transient_events = []

        # Contact registry
        if hasattr(self, "_contact_registry"):
            self._contact_registry.reset()

        # Clear module-level sonar contact memory
        clear_contact_memory()

        # Maintenance tasks and timers
        if hasattr(self, "_active_tasks"):
            self._active_tasks = {s: [] for s in ["helm", "sonar", "weapons", "engineering"]}
        if hasattr(self, "_task_spawn_timers"):
            from ..config import CONFIG as _cfg
            self._task_spawn_timers = {s: _cfg.first_task_delay_s for s in ["helm", "sonar", "weapons", "engineering"]}

        # Storm / EMCON timers
        if hasattr(self, "_emcon_high_timer"):
            self._emcon_high_timer = 0.0
        if hasattr(self, "_storm_timer"):
            self._storm_timer = 0.0

        # AI orchestrator state
        if hasattr(self, "_ai_fleet_timer"):
            self._ai_fleet_timer = getattr(CONFIG, "ai_fleet_cadence_s", 45.0)
        if hasattr(self, "_ai_ship_timers"):
            self._ai_ship_timers = {}
        if hasattr(self, "_ai_orch") and self._ai_orch is not None:
            try:
                self._ai_orch.reset_state()
            except Exception:
                pass

        # Scenario subsystems
        if hasattr(self, "_condition_evaluator"):
            try:
                self._condition_evaluator.reset()
            except Exception:
                pass
        if hasattr(self, "_waypoint_tracker"):
            try:
                self._waypoint_tracker.reset()
            except Exception:
                pass
        if hasattr(self, "_trigger_manager"):
            try:
                self._trigger_manager.reset()
            except Exception:
                pass
        if hasattr(self, "_victory_evaluator"):
            try:
                self._victory_evaluator.reset()
            except Exception:
                pass
        if hasattr(self, "_intercept_system"):
            try:
                self._intercept_system.reset()
            except Exception:
                pass

    def _init_default_world(self) -> None:
        # Set loading flag to prevent tick() from seeing partial state
        self._loading = True
        try:
            # Deactivate and reset all state while rebuilding
            self._reset_all_state()

            # Clear existing world and set to original game state
            self.world = World()
            # Try to load mission from assets unless a forced default reset is requested
            mission = None
            if not getattr(self, "_force_default_reset", False):
                # During tests, ignore external mission selection to ensure deterministic defaults
                if os.getenv("PYTEST_CURRENT_TEST"):
                    mission = None
                else:
                    # Fresh import so reload_from_env() changes take effect
                    from ..config import CONFIG as _cfg
                    mid = getattr(_cfg, "mission_id", "")
                    mission = load_mission_by_id(mid) if mid else None
            if mission:
                def _set_mission(brief: dict) -> None:
                    self.mission_brief = brief
                    # Initialize scenario subsystems with mission data
                    self._init_scenario_subsystems(brief)
                apply_mission_to_world(mission, lambda: self.world, _set_mission)
                self._mission_active = True
                self._mission_id = getattr(CONFIG, "mission_id", None)
            else:
                # No mission specified - stay in idle state (no ships spawned)
                # Don't spawn default ships - wait for explicit mission selection
                self._mission_active = False
                self._mission_id = None
            # Always clear one-shot forced reset flag
            if getattr(self, "_force_default_reset", False):
                self._force_default_reset = False
            # Set ordered state from ownship (from mission) or defaults for idle state
            ships = self.world.all_ships()
            if ships:
                try:
                    own_ref = self.world.get_ship("ownship")
                except Exception:
                    # Fallback: first BLUE ship or first ship
                    own_ref = next((s for s in ships if getattr(s, "side", "") == "BLUE"), ships[0])
                self.ordered = {"heading": own_ref.kin.heading, "speed": own_ref.kin.speed, "depth": own_ref.kin.depth}
            else:
                # No ships in world (idle state) - use default ordered values
                self.ordered = {"heading": 0.0, "speed": 0.0, "depth": 150.0}
        finally:
            self._loading = False

    def _init_scenario_subsystems(self, mission_brief: dict) -> None:
        """Initialize scenario subsystems with mission data."""
        # Initialize waypoint tracker
        if hasattr(self, "_waypoint_tracker"):
            self._waypoint_tracker.initialize(mission_brief)
        # Initialize trigger manager
        if hasattr(self, "_trigger_manager"):
            self._trigger_manager.initialize(mission_brief)
        # Initialize victory evaluator
        if hasattr(self, "_victory_evaluator"):
            self._victory_evaluator.initialize(mission_brief)
        # Initialize intercept system
        if hasattr(self, "_intercept_system"):
            self._intercept_system.initialize(mission_brief)
        # Reset captain intercepts
        self._captain_intercepts = []
        # Reset mission outcome
        self._mission_outcome = MissionOutcome()

    def _handle_trigger_event(self, event: dict) -> None:
        """Handle events from the trigger manager."""
        event_type = event.get("type")

        if event_type == "send_comms":
            # Send message to captain comms
            if not hasattr(self, "_captain_comms"):
                self._captain_comms = []
            self._captain_comms.append({
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "text": event.get("text", "")
            })
            insert_event(self.engine, self.run_id, "comms.sent", json.dumps(event))

        elif event_type == "broadcast_intercept":
            # Queue an interceptable message
            if hasattr(self, "_intercept_system"):
                self._intercept_system.add_scripted_intercept(
                    text=event.get("text", ""),
                    partial=event.get("partial", False),
                    source_ship=event.get("source_ship")
                )

        elif event_type == "change_behavior":
            # Update ship behavior in mission brief
            ship_id = event.get("ship_id")
            behavior = event.get("behavior", "")
            if ship_id and hasattr(self, "mission_brief"):
                if "ship_behaviors" not in self.mission_brief:
                    self.mission_brief["ship_behaviors"] = {}
                self.mission_brief["ship_behaviors"][ship_id] = behavior

        elif event_type == "end_scenario":
            # Force end the mission
            if hasattr(self, "_victory_evaluator"):
                outcome = event.get("outcome", "draw")
                reason = event.get("reason", "Scenario trigger")
                self._mission_outcome = self._victory_evaluator.force_end(outcome, reason)

        elif event_type == "set_ai_mode":
            # Change AI behavior mode (for future use)
            pass

        # Log the trigger event
        insert_event(self.engine, self.run_id, f"trigger.{event_type}", json.dumps(event))

    def _log_action(self, station: str, message: str, raw_data: dict = None) -> None:
        """Log an action to both the event system and the action log file.

        Args:
            station: Station name (HELM, SONAR, WEAPONS, ENGINEERING)
            message: Human-readable action description
            raw_data: Optional raw command data for debugging
        """
        from datetime import datetime, timezone

        timestamp = time.strftime("%H:%M:%S", time.localtime())
        iso_time = datetime.now(timezone.utc).isoformat()

        # Add to transient events for real-time display
        self._transient_events.append({
            "type": "action.log",
            "at": iso_time,
            "station": station,
            "message": message
        })

        # Write to database
        insert_event(self.engine, self.run_id, "action.log", json.dumps({
            "station": station, "message": message, "raw": raw_data
        }))

        # Append to action log file
        try:
            log_dir = Path(__file__).parent.parent.parent.parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"action_log_{self.run_id}.md"

            if not log_path.exists():
                today = time.strftime("%Y-%m-%d", time.localtime())
                log_path.write_text(f"# Action Log - Run {self.run_id} - {today}\n\n", encoding="utf-8")

            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"{timestamp} {station}: {message}\n")
        except Exception as e:
            print(f"Warning: Failed to write action log: {e}")

    async def _cancel_ai_tasks(self) -> None:
        """Cancel all pending AI tasks before touching world state."""
        if hasattr(self, '_ai_pending') and self._ai_pending:
            for task in list(self._ai_pending):
                task.cancel()
            await asyncio.gather(*self._ai_pending, return_exceptions=True)
            self._ai_pending.clear()

    async def load_mission(self, mission_id: str) -> bool:
        """Reset simulation and load a new mission.

        Args:
            mission_id: ID of the mission to load

        Returns:
            True if mission loaded successfully, False otherwise
        """
        mission = load_mission_by_id(mission_id)
        if not mission:
            print(f"ERROR: Mission '{mission_id}' not found")
            return False

        # Set loading flag to prevent tick() from seeing partial state
        self._loading = True
        try:
            # Cancel all pending AI tasks before touching world
            await self._cancel_ai_tasks()

            # Reset all mutable state (also sets _mission_active = False)
            self._reset_all_state()

            # Reset world state
            self.world = World()

            # Apply new mission
            def _set_mission(brief: dict) -> None:
                self.mission_brief = brief
                self._init_scenario_subsystems(brief)

            apply_mission_to_world(mission, lambda: self.world, _set_mission)

            # Update AI orchestrator mission brief
            if hasattr(self, "_ai_orch") and self._ai_orch is not None:
                try:
                    setattr(self._ai_orch, "_mission_brief", self.mission_brief)
                except Exception:
                    pass

            # Reset ordered states to match ownship spawn
            ownship = self.world.ships.get("ownship")
            if ownship:
                self.ordered = {
                    "heading": ownship.kin.heading,
                    "speed": ownship.kin.speed,
                    "depth": ownship.kin.depth
                }
            else:
                self.ordered = {"heading": 0.0, "speed": 5.0, "depth": 100.0}

            # Log mission load
            insert_event(self.engine, self.run_id, "mission.loaded", json.dumps({"mission_id": mission_id}))

            # Activate simulation
            self._mission_id = mission_id
            self._mission_active = True

            return True
        finally:
            self._loading = False

    def stop(self) -> None:
        if self._stop is None:
            try:
                self._stop = asyncio.Event()
            except Exception:
                # As a last resort, set a simple attribute; run loop will honor if already stopping
                self._stop = asyncio.Event()
        self._stop.set()

    def set_captain_consent(self, consent: bool) -> None:
        self._captain_consent = consent

    def _spawn_task_for(self, station: str, now_s: float) -> None:
        # Richer task catalog per station
        catalog = {
            "helm": [
                ("rudder", "helm.rudder.lube", "Rudder Lubricate"),
                ("rudder", "helm.rudder.linkage", "Rudder Linkage Adjust"),
                ("ballast", "helm.depth.sensor", "Depth/Pressure Sensor Recal"),
                ("ballast", "helm.pressure.sensor", "Hull Pressure Transducer Test"),
                ("ballast", "helm.salinity.sensor", "Salinity Sensor Clean"),
                ("ballast", "helm.temp.sensor", "Thermocline Temp Probe Cal"),
                ("rudder", "helm.gyro.align", "Gyro Alignment Check"),
                ("rudder", "helm.gps.sync", "GPS Time/Almanac Sync"),
                ("rudder", "helm.heading.encoder", "Heading Encoder Verify"),
                ("rudder", "helm.hydraulics.filter", "Hydraulics Filter Replace"),
            ],
            "sonar": [
                ("sonar", "sonar.hydro.cal", "Hydrophone Calibration"),
                ("sonar", "sonar.hydro.servo", "Hydrophone Servo Grease"),
                ("sonar", "sonar.passive.dsp", "Passive DSP Self-Test"),
                ("sonar", "sonar.ping.tx", "Ping Transmit Chain Test"),
                ("sonar", "sonar.ping.rx", "Ping Response Chain Test"),
                ("sonar", "sonar.preamp", "Preamp Gain Trim"),
                ("sonar", "sonar.array.cable", "Array Cable Continuity"),
                ("sonar", "sonar.cooling.loop", "Cooling Loop Flush"),
                ("sonar", "sonar.beamformer", "Beamformer Rebalance"),
                ("sonar", "sonar.clock", "ADC Clock Discipline"),
            ],
            "weapons": [
                ("tubes", "weap.tube.seal", "Tube Seal Inspection"),
                ("tubes", "weap.tube.purge", "Tube Purge Cycle"),
                ("tubes", "weap.tube.door", "Door Actuator Lube"),
                ("tubes", "weap.tube.bore", "Bore Clean & Inspect"),
                ("tubes", "weap.fire.ctrl", "Fire Control Align"),
                ("tubes", "weap.wire.handler", "Wire Guide Service"),
                ("tubes", "weap.gyros.spinup", "Gyro Spinup Test"),
                ("tubes", "weap.seeker.bench", "Seeker Bench Check"),
                ("tubes", "weap.power.bus", "Weapons Bus Check"),
                ("tubes", "weap.cooling.pump", "Cooling Pump Service"),
            ],
            "engineering": [
                ("ballast", "eng.ballast.valve", "Ballast Valve Service"),
                ("ballast", "eng.pump.impeller", "Pump Impeller Inspect"),
                ("ballast", "eng.scrubber", "Air Scrubber Replace"),
                ("ballast", "eng.heat.xchg", "Heat Exchanger Clean"),
                ("ballast", "eng.reactor.coolant", "Coolant Chemistry Check"),
                ("ballast", "eng.generator", "Generator Bearing Lube"),
                ("ballast", "eng.battery.cell", "Battery Cell Test"),
                ("ballast", "eng.hvac.filter", "HVAC Filter Replace"),
                ("ballast", "eng.busbars", "Busbar Tightening"),
                ("ballast", "eng.pipe.leak", "Pipe Leak Inspection"),
            ],
        }
        choices = catalog.get(station, [("rudder", "misc.task", "Maintenance")])
        system, key, title = random.choice(choices)
        base_deadline = random.uniform(25.0, 45.0)
        tid = f"{station}-{int(now_s*1000)%100000}-{random.randint(100,999)}"
        self._active_tasks[station].append(MaintenanceTask(
            id=tid, station=station, system=system, key=key, title=title,
            stage="task", progress=0.0, base_deadline_s=base_deadline, time_remaining_s=base_deadline, created_at=now_s
        ))

    def _station_power_fraction(self, ship: Ship, station: str) -> float:
        p = ship.power
        return max(0.0, min(1.0, getattr(p, station if station != "engineering" else "engineering")))

    def _apply_stage_penalties(self, ship: Ship, station: str, stage: str) -> None:
        # Apply degradation effects per station and stage
        if station == "helm":
            # Map new stages: task (no penalty), failing (moderate), failed (worst)
            factor = {"task": 1.0, "failing": 0.7, "failed": 0.0}.get(stage, 1.0)
            ship.hull.turn_rate_max = 7.0 * factor
            if stage == "failed":
                ship.systems.rudder_ok = False
            # Additional helm-related effects keyed to degraded states
            if stage in ("failing", "failed"):
                ship.acoustics.thermocline_on = True
        elif station == "sonar":
            extra = {"task": 0.0, "failing": 3.0, "failed": 12.0}.get(stage, 0.0)
            ship.acoustics.bearing_noise_extra = extra
            if stage == "failed":
                ship.systems.sonar_ok = False
            if stage in ("failing", "failed"):
                ship.acoustics.passive_snr_penalty_db = 3.0 if stage == "failing" else 10.0
                ship.acoustics.active_range_noise_add_m = 50.0 if stage == "failing" else 250.0
                ship.acoustics.active_bearing_noise_extra = 0.5 if stage == "failing" else 3.0
        elif station == "weapons":
            mult = {"task": 1.0, "failing": 1.4, "failed": 2.5}.get(stage, 1.0)
            ship.weapons.time_penalty_multiplier = mult
            if stage == "failed":
                ship.systems.tubes_ok = False
        elif station == "engineering":
            # Engineering failed: ballast system effectively unavailable
            if stage == "failed":
                ship.systems.ballast_ok = False

    def _recompute_penalties_from_tasks(self, ship: Ship) -> None:
        """Aggregate active task stages per station and apply the worst effect.

        This prevents a completed task from resetting penalties while other degraded/failed
        tasks remain for the same station.
        """
        # Determine worst stage order for new model
        stage_rank = {"task": 0, "failing": 1, "failed": 2}
        # Default to normal for all stations
        worst_by_station = {s: "task" for s in ["helm", "sonar", "weapons", "engineering"]}
        for station, tasks in self._active_tasks.items():
            if not tasks:
                continue
            worst = "task"
            for t in tasks:
                if stage_rank[t.stage] > stage_rank[worst]:
                    worst = t.stage
            worst_by_station[station] = worst
        # Apply aggregated penalties
        for station, stage in worst_by_station.items():
            self._apply_stage_penalties(ship, station, stage)

    def _get_recent_enemy_pings(self, own: Ship) -> list:
        """Get recent enemy pings with distance for audio playback."""
        pings = []
        if not hasattr(self, "_enemy_ping_contacts"):
            return pings
        now = time.time()
        for epc in self._enemy_ping_contacts:
            contact = epc.get("contact")
            ping_time = epc.get("timestamp_epoch", 0)
            source_id = epc.get("source", "")
            # Only include pings from last 3 seconds
            if now - ping_time > 3.0:
                continue
            # Calculate distance from ping source to ownship
            bearing = getattr(contact, "bearing", 0) if contact else 0
            distance = 5000  # default
            # Find the source ship by ID
            source_ship = self.world.get_ship(source_id)
            if source_ship:
                dx = source_ship.kin.x - own.kin.x
                dy = source_ship.kin.y - own.kin.y
                distance = math.hypot(dx, dy)
            pings.append({
                "id": f"ping_{int(ping_time * 1000)}",
                "at": ping_time,
                "bearing": bearing,
                "distance": distance,
            })
        return pings[-5:]  # Keep only last 5

    def _step_station_tasks(self, ship: Ship, dt: float) -> None:
        now_s = time.perf_counter()
        # Spawn logic per station (allow multiple concurrent tasks)
        for station in self._active_tasks.keys():
            self._task_spawn_timers[station] -= dt
            if self._task_spawn_timers[station] <= 0.0:
                if not self._suppress_maintenance_spawns:
                    self._spawn_task_for(station, now_s)
                base = random.uniform(60.0, 120.0)
                self._task_spawn_timers[station] = base / max(0.2, CONFIG.maint_spawn_scale)

        # Progress all active tasks based on power allocation for that station
        for station, tasks in self._active_tasks.items():
            if not tasks:
                continue
            power_frac = self._station_power_fraction(ship, station)
            for task in list(tasks):
                task.time_remaining_s = max(0.0, task.time_remaining_s - dt)
                if task.started:
                    task.progress = min(1.0, task.progress + (0.2 * power_frac) * dt)
                if task.progress >= 1.0:
                    ship.maintenance.levels[task.system] = min(1.0, ship.maintenance.levels.get(task.system, 1.0) + 0.1)
                    tasks.remove(task)
                    continue
                if task.time_remaining_s <= 0.0:
                    # New escalation: task -> failing -> failed
                    if task.stage == "task":
                        task.stage = "failing"
                        task.base_deadline_s *= 1.25
                        task.time_remaining_s = task.base_deadline_s
                        ship.maintenance.levels[task.system] = max(0.0, ship.maintenance.levels.get(task.system, 1.0) - 0.05)
                    elif task.stage == "failing":
                        task.stage = "failed"
                        # No further timeout; keep at zero
                        ship.maintenance.levels[task.system] = max(0.0, ship.maintenance.levels.get(task.system, 1.0) - 0.1)
        # After all updates, recompute aggregated penalties for accuracy
        self._recompute_penalties_from_tasks(ship)

    async def run(self) -> None:
        if self._stop is None:
            self._stop = asyncio.Event()
        last = time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            elapsed = now - last
            if elapsed < self.dt:
                await asyncio.sleep(self.dt - elapsed)
                continue
            last = now
            try:
                await self.tick(self.dt)
            except Exception as e:
                import traceback
                print(f"ERROR in tick(): {e}")
                traceback.print_exc()
                # Continue running despite errors
                await asyncio.sleep(1.0)

    def get_current_status(self) -> dict:
        """Get the current simulation status for initial WebSocket sync.

        Returns:
            A status message dict with topic and data fields
        """
        if self._loading:
            status = "loading"
            message = "Loading mission..."
        elif not self._mission_active:
            status = "idle"
            message = "Awaiting mission selection"
        elif hasattr(self, "_mission_outcome") and self._mission_outcome.status != "ongoing":
            status = "ended"
            message = None
        else:
            status = "active"
            message = None

        payload = {
            "missionActive": self._mission_active and not self._loading,
            "missionVersion": self._mission_version,
            "missionId": self._mission_id,
            "status": status,
        }

        if message:
            payload["message"] = message

        if status == "ended" and hasattr(self, "_mission_outcome"):
            payload["outcome"] = self._mission_outcome.dict()

        return {"topic": "status", "data": payload}

    async def _broadcast_idle_telemetry(self) -> None:
        """Broadcast minimal status when no mission is active (idle state)."""
        payload = {
            "missionActive": False,
            "missionVersion": self._mission_version,
            "missionId": self._mission_id,
            "status": "idle",
            "message": "Awaiting mission selection"
        }
        for topic in ["tick:captain", "tick:helm", "tick:sonar", "tick:weapons",
                      "tick:engineering", "tick:debug", "tick:fleet", "tick:logs"]:
            await BUS.publish(topic, {"topic": "status", "data": payload})

    async def _broadcast_ended_telemetry(self) -> None:
        """Broadcast status when mission has ended (victory/defeat)."""
        payload = {
            "missionActive": False,
            "missionVersion": self._mission_version,
            "missionId": self._mission_id,
            "status": "ended",
            "outcome": self._mission_outcome.dict() if hasattr(self, "_mission_outcome") else {"status": "unknown"}
        }
        for topic in ["tick:captain", "tick:helm", "tick:sonar", "tick:weapons",
                      "tick:engineering", "tick:debug", "tick:fleet", "tick:logs"]:
            await BUS.publish(topic, {"topic": "status", "data": payload})

    async def tick(self, dt: float) -> None:
        # Loading state - mission transition in progress, skip tick entirely
        if self._loading:
            return

        # Idle state - no active mission, broadcast status and return
        if not self._mission_active:
            await self._broadcast_idle_telemetry()
            await asyncio.sleep(self.dt)  # Throttle idle broadcasts to tick rate
            return

        # Check if mission has ended (victory/defeat) - auto-end
        if hasattr(self, "_mission_outcome") and self._mission_outcome.status != "ongoing":
            await self._broadcast_ended_telemetry()
            await asyncio.sleep(self.dt)
            return

        own = self.world.get_ship("ownship")

        # Guard: if no ownship, skip tick (mission not loaded properly)
        if own is None:
            # Debug: print world state
            if not hasattr(self, "_debug_no_ownship_warned"):
                print(f"DEBUG: No ownship found. World has ships: {list(self.world.ships.keys())}")
                self._debug_no_ownship_warned = True
            return

        # DEBUG: Log that we're running a full tick
        if not hasattr(self, "_debug_tick_count"):
            self._debug_tick_count = 0
        self._debug_tick_count += 1
        if self._debug_tick_count % 100 == 1:  # Log every 100 ticks (every 5 seconds at 20Hz)
            print(f"DEBUG: Running tick #{self._debug_tick_count}, mission_active={self._mission_active}, ownship={own.id}")

        if CONFIG.use_ai_orchestrator:
            # Advance orchestrator timers
            self._ai_fleet_timer += dt
            # Default cadences (configurable)
            fleet_cadence = getattr(CONFIG, "ai_fleet_cadence_s", 45.0)
            fleet_alert_cadence = getattr(CONFIG, "ai_fleet_alert_cadence_s", 20.0)
            fleet_trigger_conf = getattr(CONFIG, "ai_fleet_trigger_conf_threshold", 0.7)
            ship_normal_cadence = getattr(CONFIG, "ai_ship_cadence_s", 20.0)
            ship_alert_cadence = getattr(CONFIG, "ai_ship_alert_cadence_s", 10.0)
            # Contact-confidence trigger: if any RED ship first crosses confidence >= threshold on any contact, trigger immediate fleet run and switch to alert cadence
            try:
                if not hasattr(self, "_fleet_conf_tripped"):
                    self._fleet_conf_tripped = False  # type: ignore[attr-defined]
                if not hasattr(self, "_fleet_last_alert_time"):
                    self._fleet_last_alert_time = 0.0  # type: ignore[attr-defined]
                trigger_now = False
                if isinstance(fleet_trigger_conf, (int, float)) and fleet_trigger_conf > 0.0:
                    for s in self.world.all_ships():
                        if s.side != "RED":
                            continue
                        # Build local contacts as orchestrator does; use passive_contacts for bearing-only confidence
                        contacts = passive_contacts(s, [x for x in self.world.all_ships() if x.side != s.side and x.id != s.id])
                        for c in contacts:
                            conf = float(getattr(c, "confidence", 0.0))
                            # Consider a first-time crossing per ship to avoid repeated triggers
                            key = f"_{s.id}_conf_crossed"
                            if conf >= float(fleet_trigger_conf) and not getattr(self, key, False):
                                setattr(self, key, True)
                                trigger_now = True
                                break
                        if trigger_now:
                            break
                # Fleet cadence selection: alert cadence while in tripped state
                chosen_fleet_cadence = fleet_alert_cadence if trigger_now or getattr(self, "_fleet_conf_tripped", False) else fleet_cadence
                if trigger_now:
                    self._fleet_conf_tripped = True  # type: ignore[attr-defined]
                # Schedule fleet run
                do_run_fleet = (self._ai_fleet_timer >= chosen_fleet_cadence) or trigger_now
            except Exception:
                do_run_fleet = (self._ai_fleet_timer >= fleet_cadence)
            if do_run_fleet:
                self._ai_fleet_timer = 0.0
                # Mirror current mission brief to orchestrator before the run
                try:
                    if hasattr(self, "_ai_orch") and getattr(self, "_ai_orch", None) is not None:
                        setattr(self._ai_orch, "_mission_brief", self.mission_brief)
                except Exception:
                    pass
                async def _fleet_job():
                    res = await self._ai_orch.run_fleet()
                    # Persist tool calls for trace
                    for tc in res.get("tool_calls_validated", []):
                        insert_event(self.engine, self.run_id, "ai.tool.fleet", json.dumps(tc))
                        # Update world-level FleetIntent for UI and ship guidance
                        if tc.get("tool") == "set_fleet_intent":
                            self._fleet_intent = tc.get("arguments", {})
                            # Also attach to orchestrator so ship summaries can see guidance slice
                            try:
                                setattr(self._ai_orch, "_last_fleet_intent", self._fleet_intent)
                            except Exception:
                                pass
                            # Record concise history entry for Fleet Commander context
                            try:
                                if not hasattr(self, "_fleet_intent_history"):
                                    self._fleet_intent_history = []  # type: ignore[attr-defined]
                                intent = dict(self._fleet_intent or {})
                                # Compute short hash of intent body
                                import hashlib as _hashlib, json as _json
                                ihash = ""
                                try:
                                    ihash = _hashlib.sha1(_json.dumps(intent, sort_keys=True).encode()).hexdigest()[:8]
                                except Exception:
                                    ihash = ""
                                # Brief per-ship destinations
                                obj = intent.get("objectives", {}) or {}
                                brief = {}
                                if isinstance(obj, dict):
                                    for sid, val in obj.items():
                                        if isinstance(val, dict) and "destination" in val:
                                            brief[sid] = {"destination": val.get("destination")}
                                hist_entry = {
                                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "hash": ihash,
                                    "summary": intent.get("summary", ""),
                                    "objectives_brief": brief,
                                    "engagement_rules": intent.get("engagement_rules", {}),
                                }
                                self._fleet_intent_history.append(hist_entry)  # type: ignore[attr-defined]
                                # Keep last 8
                                self._fleet_intent_history = self._fleet_intent_history[-8:]  # type: ignore[attr-defined]
                                # Mirror to orchestrator for Fleet Commander prompt
                                try:
                                    setattr(self._ai_orch, "_fleet_intent_history", list(self._fleet_intent_history))
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        # Handle Fleet Commander journal entries
                        elif tc.get("tool") == "write_journal":
                            try:
                                args = tc.get("arguments", {})
                                text = args.get("text", "").strip() if isinstance(args, dict) else ""
                                if text:
                                    # Write to logs/fleet_journal_YYYY-MM-DD.md
                                    today = time.strftime("%Y-%m-%d", time.gmtime())
                                    journal_dir = Path(__file__).parent.parent.parent.parent / "logs"
                                    journal_dir.mkdir(parents=True, exist_ok=True)
                                    journal_path = journal_dir / f"fleet_journal_{today}.md"
                                    timestamp = time.strftime("%H:%M:%S", time.gmtime())
                                    # Create header if file is new
                                    if not journal_path.exists():
                                        header = f"# Fleet Commander's Log - {today}\n\n"
                                        journal_path.write_text(header, encoding="utf-8")
                                    # Append entry
                                    entry = f"## {timestamp}\n{text}\n\n"
                                    with journal_path.open("a", encoding="utf-8") as f:
                                        f.write(entry)
                                    insert_event(self.engine, self.run_id, "ai.tool.journal", json.dumps({"timestamp": timestamp, "length": len(text)}))
                            except Exception as je:
                                print(f"Warning: Failed to write journal: {je}")
                    # Mirror recent runs into sim for Fleet UI; drop alert after one alert cadence window with no additional triggers
                    self._ai_recent_runs = getattr(self._ai_orch, "_recent_runs", [])
                    try:
                        if getattr(self, "_fleet_conf_tripped", False):
                            # If no trigger occurs for one alert cadence window, reset to normal cadence
                            self._fleet_last_alert_time = 0.0
                    except Exception:
                        pass
                t = asyncio.create_task(_fleet_job())
                self._ai_pending.add(t)
                t.add_done_callback(lambda _t: self._ai_pending.discard(_t))
            # Schedule per-ship runs (RED side only)
            red_ships = [s for s in self.world.all_ships() if s.side == "RED"]
            if not hasattr(self, "_debug_ship_ai_logged"):
                print(f"DEBUG: Found {len(red_ships)} RED ships for AI control: {[s.id for s in red_ships]}")
                self._debug_ship_ai_logged = True
            for ship in red_ships:
                sid = ship.id
                if sid not in self._ai_ship_timers:
                    # Trigger initial run quickly after startup for each ship
                    self._ai_ship_timers[sid] = ship_normal_cadence
                self._ai_ship_timers[sid] += dt
                # Detection-aware cadence (non-leaking heuristic):
                # - If ownship recently active pinged (cooldown active), enemies go alert
                # - If close to noisy ownship while EMCON alert is high
                dx = ship.kin.x - own.kin.x
                dy = ship.kin.y - own.kin.y
                dist_m = (dx * dx + dy * dy) ** 0.5
                recent_active_ping = self.active_ping_state.timer > 0.0
                close_and_noisy = (dist_m <= 7000.0) and (self._emcon_high_timer >= 10.0)
                is_alert = recent_active_ping or close_and_noisy
                cadence = ship_alert_cadence if is_alert else ship_normal_cadence
                if self._ai_ship_timers[sid] >= cadence:
                    self._ai_ship_timers[sid] = 0.0
                    print(f"DEBUG: Triggering ship AI run for {sid} (alert={is_alert}, cadence={cadence})")
                    async def _ship_job(_sid: str = sid):
                        # Phase 2: decision via ShipController, application via ShipControls.
                        try:
                            actions = await self._ship_controller.step(_sid)
                        except Exception as e:
                            print(f"DEBUG: Ship {_sid} controller EXCEPTION: {e}")
                            return
                        try:
                            tgt = self.world.get_ship(_sid)
                        except Exception:
                            return
                        controls = ShipControls(tgt, self.world)
                        # Apply only the first action — preserves prior "first tool only" behavior.
                        for action in actions:
                            print(f"DEBUG: Ship {_sid} applying action={action.name}")
                            result = action.apply(controls)
                            if result.ok:
                                insert_event(
                                    self.engine, self.run_id, "ai.tool.apply",
                                    json.dumps({"ship_id": _sid, "tool": action.name}),
                                )
                                self._post_apply_ship_action(_sid, tgt, action, result)
                            break
                        # Mirror recent runs into sim for Fleet UI (LLM-only state)
                        try:
                            self._ai_recent_runs = getattr(self._ai_orch, "_recent_runs", [])
                        except Exception:
                            pass
                    t = asyncio.create_task(_ship_job())
                    self._ai_pending.add(t)
                    t.add_done_callback(lambda _t: self._ai_pending.discard(_t))
        elif CONFIG.use_enemy_ai:
            self._last_ai += dt
            if self._last_ai >= CONFIG.ai_poll_s:
                self._last_ai = 0.0
                for ship in self.world.all_ships():
                    if ship.side == "RED":
                        tool = self.ai.propose_orders(ship)
                        insert_event(self.engine, self.run_id, "ai_tool", json.dumps(tool))
                        args = tool.get("arguments", {})
                        ship.kin.heading = args.get("heading", ship.kin.heading)
                        ship.kin.speed = args.get("speed", ship.kin.speed)
                        ship.kin.depth = args.get("depth", ship.kin.depth)

        # Pure physics step (kinematics, weapons, projectiles, damage,
        # engineering). All world-mutation primitives are encapsulated in
        # `SimulationCore`; we stitch the result back to BUS / events here.
        core = SimulationCore(self.world)
        core_result = core.step_physics(
            dt=dt,
            ordered=self.ordered,
            pump_assignments=self._pump_assignments,
            enemy_static=CONFIG.enemy_static and not CONFIG.use_ai_orchestrator,
        )
        if core_result.cavitation:
            self._noise.add_impulse("helm", 82.0, 0.5)
        self._dispatch_core_events(core_result)
        self._apply_system_failures(own, core_result.system_failures)
        # Local aliases used by downstream telemetry / snapshot code below.
        heading = own.kin.heading
        speed = own.kin.speed
        depth = own.kin.depth
        cav = core_result.cavitation

        # Station maintenance/repair tasks lifecycle
        self._step_station_tasks(own, dt)

        # ========== Scenario and Game Rules Step ==========
        # Increment simulation time
        self._sim_time_s += dt

        # Step waypoint tracker (monitors ship positions, emits events)
        if hasattr(self, "_waypoint_tracker"):
            wp_events = self._waypoint_tracker.step(dt)
            for ev in wp_events:
                self._transient_events.append(ev)
                insert_event(self.engine, self.run_id, "waypoint.reached", json.dumps(ev))

        # Step trigger manager (evaluates conditions, executes actions)
        if hasattr(self, "_trigger_manager"):
            trigger_events = self._trigger_manager.step(dt, self._sim_time_s)
            for ev in trigger_events:
                self._handle_trigger_event(ev)

        # Step intercept system (delivers intercepted communications)
        if hasattr(self, "_intercept_system"):
            intercepts = self._intercept_system.step(dt, self._sim_time_s)
            for ic in intercepts:
                self._captain_intercepts.append(ic.dict())
                insert_event(self.engine, self.run_id, "intercept.received", json.dumps(ic.dict()))

        # Step victory evaluator (checks success criteria)
        if hasattr(self, "_victory_evaluator"):
            outcome = self._victory_evaluator.step(self._sim_time_s)
            if outcome and outcome.status != "ongoing":
                self._mission_outcome = outcome
                self._transient_events.append({
                    "type": "mission_end",
                    "outcome": outcome.status,
                    "reason": outcome.reason
                })
                insert_event(self.engine, self.run_id, "mission.end", json.dumps(outcome.dict()))

        self.active_ping_state.tick(dt)
        
        # Tick AI ship active sonar cooldowns
        for ship in self.world.all_ships():
            if ship.side != "ownship" and ship.active_sonar_cooldown > 0.0:
                ship.active_sonar_cooldown = max(0.0, ship.active_sonar_cooldown - dt)
        
        # Acoustic noise budget and detectability
        noise_from_speed = min(100.0, (speed / max(1.0, own.hull.max_speed)) * 70.0)
        noise_cav = 30.0 if cav else 0.0
        # Each active pump adds noise
        noise_pumps = 10.0 * len(self._pump_assignments)
        noise_masts = (10.0 if self._periscope_raised else 0.0) + (10.0 if self._radio_raised else 0.0)
        noise_budget = max(0.0, min(100.0, noise_from_speed + noise_cav + noise_pumps + noise_masts))
        
        # Compute per-station noise dB for UI and dynamic source level
        noise_levels = self._noise.tick(own, self.world, dt, self)
        
        # Update submarine's dynamic source level based on comprehensive noise budget
        # Convert noise budget (0-100) to dB source level (110-140 dB range)
        # Base source level from speed, then add noise contributions
        base_src_lvl = 110.0 + (speed / max(1.0, own.hull.max_speed)) * 20.0  # 110-130 dB range
        noise_contributions = noise_cav + noise_pumps + noise_masts  # Additional noise from operations
        
        # Add per-station noise contributions to source level
        # Convert dB noise levels to source level contributions (scale factor 0.1)
        station_noise_contrib = 0.0
        for station, db_level in noise_levels.items():
            if station != "total" and db_level > 0:
                station_noise_contrib += db_level * 0.1  # Scale station noise to source level
        
        dynamic_src_lvl = base_src_lvl + (noise_contributions * 0.3) + station_noise_contrib
        
        # Update submarine's acoustics with dynamic source level
        # This makes the submarine more detectable to enemies based on its noise
        own.acoustics.source_level_by_speed = {
            int(speed): dynamic_src_lvl
        }
        
        # EMCON pressure: sustained high noise raises alert
        if noise_budget >= 60.0:
            self._emcon_high_timer = min(30.0, self._emcon_high_timer + dt)
        else:
            self._emcon_high_timer = max(0.0, self._emcon_high_timer - dt)
        emcon_alert = self._emcon_high_timer >= 10.0
        detectability = noise_budget / 100.0
        contacts = passive_contacts(own, [s for s in self.world.all_ships() if s.id != own.id], self._contact_registry)
        # Build simple alert map for RED ships for orchestrator visibility
        try:
            if hasattr(self, "_ai_orch") and getattr(self, "_ai_orch", None) is not None:
                alert_map = {}
                for ship in self.world.all_ships():
                    if ship.side != "RED":
                        continue
                    dx = ship.kin.x - own.kin.x
                    dy = ship.kin.y - own.kin.y
                    dist_m = (dx * dx + dy * dy) ** 0.5
                    recent_active_ping = self.active_ping_state.timer > 0.0
                    close_and_noisy = (dist_m <= 7000.0) and (self._emcon_high_timer >= 10.0)
                    alert_map[ship.id] = bool(recent_active_ping or close_and_noisy)
                setattr(self._ai_orch, "_ship_alert_map", alert_map)
        except Exception:
            pass

        # Build visual detection map for enemy ships to detect surface vessels
        try:
            if hasattr(self, "_ai_orch") and getattr(self, "_ai_orch", None) is not None:
                visual_detection_map = {}
                
                # Initialize enemy search timers if not exists
                if not hasattr(self, "_enemy_search_timers"):
                    self._enemy_search_timers = {}
                
                # For each enemy ship, check if they can visually detect other ships
                for observer in self.world.all_ships():
                    if observer.side != "RED":  # Only enemy ships can visually detect
                        continue
                    
                    # Initialize search timer for this observer
                    if observer.id not in self._enemy_search_timers:
                        self._enemy_search_timers[observer.id] = 0.0
                    
                    # Update search timer (re-roll detection every 5 seconds, same as periscope)
                    self._enemy_search_timers[observer.id] += dt
                    search_interval = 5.0  # 5 seconds between search attempts
                    
                    # Only perform detection check every 5 seconds to simulate realistic visual scanning
                    if self._enemy_search_timers[observer.id] >= search_interval:
                        self._enemy_search_timers[observer.id] = 0.0  # Reset timer
                        
                        observer_detections = {}
                        
                        # Check detection of all other ships
                        for target in self.world.all_ships():
                            if target.id == observer.id:
                                continue
                            
                            # Calculate distance and check if target is within visual range
                            dx = target.kin.x - observer.kin.x
                            dy = target.kin.y - observer.kin.y
                            dist_m = math.hypot(dx, dy)
                            
                            # Visual detection range: 15km (same as periscope)
                            if dist_m > 15000.0:
                                continue
                            
                            # Visual detection conditions:
                            # 1. Target must be at or near surface (≤5m depth, same as periscope detection)
                            # 2. Observer must be at surface or shallow depth (≤10m depth for surface ships)
                            
                            target_surface = target.kin.depth <= 5.0
                            observer_surface = observer.kin.depth <= 10.0
                            
                            if target_surface and observer_surface:
                                # Initialize visual contact tracking for this observer
                                if observer.id not in self._visual_contacts:
                                    self._visual_contacts[observer.id] = {}
                                
                                # Check if we have previous contact history for this target
                                contact_history = self._visual_contacts[observer.id].get(target.id, {})
                                last_seen = contact_history.get("last_seen", 0.0)
                                detection_count = contact_history.get("detection_count", 0)
                                last_confidence = contact_history.get("last_confidence", 0.0)
                                
                                # Calculate detection probability based on distance and conditions
                                if self._debug_enemy_visual_100:
                                    # Debug mode: 100% detection
                                    detection_prob = 1.0
                                    detection_roll = 0.0  # Always pass
                                else:
                                    # Normal mode: probabilistic detection
                                    # Base detection probability decreases with distance
                                    base_prob = max(0.0, 1.0 - (dist_m / 15000.0))
                                    
                                    # Surface vessels are easier to detect than submarines
                                    if target.ship_class == "Convoy":
                                        detection_prob = base_prob * 1.3  # 30% easier to detect
                                    elif target.ship_class == "Destroyer":
                                        detection_prob = base_prob * 1.1  # 10% easier to detect
                                    else:
                                        detection_prob = base_prob
                                    
                                    # Improve detection probability for previously spotted targets
                                    if detection_count > 0:
                                        # Calculate time since last seen for decay
                                        current_time_check = time.time()
                                        time_since = current_time_check - last_seen
                                        # Memory bonus capped at 25%, decays over 60 seconds
                                        time_factor = max(0.0, 1.0 - time_since / 60.0)
                                        memory_bonus = min(0.25, detection_count * 0.10) * time_factor
                                        detection_prob = min(0.95, detection_prob + memory_bonus)
                                    
                                    # Cap at 95% maximum detection probability (never perfect, but very high for known targets)
                                    detection_prob = min(0.95, detection_prob)
                                    
                                    # Roll for detection
                                    detection_roll = random.random()
                                
                                # Check for detection
                                detected_this_cycle = detection_roll < detection_prob
                                
                                # If we detect the target OR we have recent contact history, include it
                                current_time = time.time()
                                time_since_last_seen = current_time - last_seen
                                
                                # Include contact if:
                                # 1. We detected it this cycle, OR
                                # 2. We saw it recently (within 30 seconds) and it's still in range
                                if detected_this_cycle or (detection_count > 0 and time_since_last_seen <= 30.0 and dist_m <= 15000.0):
                                    # Determine detection mode based on target depth
                                    if target.kin.depth <= 1.0:
                                        mode = "surface"  # Fully surfaced
                                    else:
                                        mode = "periscope"  # Periscope depth
                                    
                                    # Update contact history if we detected it this cycle
                                    if detected_this_cycle:
                                        self._visual_contacts[observer.id][target.id] = {
                                            "last_seen": current_time,
                                            "detection_count": detection_count + 1,
                                            "last_confidence": detection_prob
                                        }
                                    
                                    # Use current detection confidence or last known confidence
                                    current_confidence = detection_prob if detected_this_cycle else last_confidence
                                    
                                    observer_detections[target.id] = {
                                        "detected": detected_this_cycle,
                                        "mode": mode,
                                        "distance": dist_m,
                                        "confidence": current_confidence,
                                        "last_seen": last_seen if not detected_this_cycle else current_time,
                                        "detection_count": detection_count + (1 if detected_this_cycle else 0)
                                    }
                        
                        if observer_detections:
                            visual_detection_map[observer.id] = observer_detections
                
                setattr(self._ai_orch, "_visual_detection_map", visual_detection_map)
                
                # Clean up old visual contacts (older than 2 minutes)
                current_time = time.time()
                for observer_id in list(self._visual_contacts.keys()):
                    for target_id in list(self._visual_contacts[observer_id].keys()):
                        contact = self._visual_contacts[observer_id][target_id]
                        if current_time - contact.get("last_seen", 0.0) > 120.0:  # 2 minutes
                            del self._visual_contacts[observer_id][target_id]
                    # Remove empty observer entries
                    if not self._visual_contacts[observer_id]:
                        del self._visual_contacts[observer_id]
        except Exception:
            pass


        base = {
            "ownship": {
                "x": own.kin.x,
                "y": own.kin.y,
                "heading": heading,
                "orderedHeading": self.ordered["heading"],
                "orderedSpeed": self.ordered["speed"],
                "orderedDepth": self.ordered["depth"],
                "speed": speed,
                "depth": depth,
                "cavitation": cav,
                "damage": {
                    "hull": own.damage.hull,
                    "sensors": own.damage.sensors,
                    "propulsion": own.damage.propulsion,
                    "flooding_rate": own.damage.flooding_rate
                },
            },
            "acoustics": {
                "noiseBudget": noise_budget, 
                "detectability": detectability, 
                "emconRisk": ("high" if noise_budget >= 75 else "med" if noise_budget >= 40 else "low"), 
                "emconAlert": emcon_alert,
                "dynamicSourceLevel": dynamic_src_lvl,
                "baseSourceLevel": base_src_lvl,
                "noiseContributions": noise_contributions,
                "stationNoiseContrib": station_noise_contrib
            },
            "events": list(self._transient_events),
            "noise": {
                "helm_dB": noise_levels.get("helm", 0.0),
                "sonar_dB": noise_levels.get("sonar", 0.0),
                "weapons_dB": noise_levels.get("weapons", 0.0),
                "engineering_dB": noise_levels.get("engineering", 0.0),
                "total_dB": noise_levels.get("total", 0.0),
            },
            # Audio events - for client-side sound playback
            "lastPingAt": getattr(self, "_last_ping_at", None),
            "enemyPings": self._get_recent_enemy_pings(own),
            "explosions": list(getattr(self, "_sonar_explosions", [])),
            # System status for alarms
            "systems": own.systems.dict() if hasattr(own.systems, 'dict') else {},
            "maintenance": {"levels": own.maintenance.levels},
            # Mission state for startup sequence tracking
            "missionActive": self._mission_active,
            "missionVersion": self._mission_version,
            "missionId": self._mission_id,
        }

        # Broadcast class/capabilities in the general telemetry for downstream consumers/AI
        tel_all = {**base, "ships": [
            {
                "id": s.id,
                "side": s.side,
                "class": getattr(s, "ship_class", None),
                "capabilities": (getattr(s, "capabilities", None).dict() if getattr(s, "capabilities", None) else None),
                "x": s.kin.x, "y": s.kin.y, "depth": s.kin.depth,
                "heading": s.kin.heading, "speed": s.kin.speed,
            }
            for s in self.world.all_ships()
        ]}
        # Station status aggregation for captain dashboard
        def station_status(station: str, ok_flag: bool) -> str:
            if not ok_flag:
                return "Failed"
            tasks = self._active_tasks.get(station, [])
            if not tasks:
                return "OK"
            return "Degraded" if any(t.stage in ("failing",) for t in tasks) else "OK"

        station_statuses = {
            "helm": station_status("helm", own.systems.rudder_ok),
            "sonar": station_status("sonar", own.systems.sonar_ok),
            "weapons": station_status("weapons", own.systems.tubes_ok),
            "engineering": station_status("engineering", own.systems.ballast_ok),
        }

        # Periscope spotting: probabilistic detection with realistic search behavior and contact persistence
        # Initialize periscope contacts list if not exists
        if not hasattr(self, "_periscope_contacts"):
            self._periscope_contacts = []
        
        # Check if periscope is raised, at proper depth, and system is functional
        periscope_ok = getattr(own.systems, "periscope_ok", True)  # Default to True if systems not initialized
        if self._periscope_raised and own.kin.depth <= 20.0 and periscope_ok:
            # Initialize periscope search timer if not exists
            if not hasattr(self, "_periscope_search_timer"):
                self._periscope_search_timer = 0.0
            
            # Initialize player visual contact tracking if not exists
            if "ownship" not in self._visual_contacts:
                self._visual_contacts["ownship"] = {}
            
            # Update search timer (re-roll detection every 5 seconds)
            self._periscope_search_timer += dt
            search_interval = 5.0  # 5 seconds between search attempts
            
            # Clear and rebuild contacts list every tick
            self._periscope_contacts = []
            
            # Always check for contacts (both new detections and existing ones)
            for s in self.world.all_ships():
                if s.id == own.id:
                    continue
                if s.kin.depth <= 5.0:  # Target must be at or near surface
                    dx = s.kin.x - own.kin.x
                    dy = s.kin.y - own.kin.y
                    rng = (dx*dx + dy*dy) ** 0.5
                    if rng <= 15000.0:  # Within 15km range
                        # Check if we have previous contact history for this target
                        contact_history = self._visual_contacts["ownship"].get(s.id, {})
                        last_seen = contact_history.get("last_seen", 0.0)
                        detection_count = contact_history.get("detection_count", 0)
                        last_confidence = contact_history.get("last_confidence", 0.0)
                        
                        current_time = time.time()
                        time_since_last_seen = current_time - last_seen
                        
                        # Only perform new detection checks every 5 seconds
                        detected_this_cycle = False
                        if self._periscope_search_timer >= search_interval:
                            # Calculate detection probability based on range and conditions
                            if self._debug_player_visual_100:
                                # Debug mode: 100% detection
                                detection_prob = 1.0
                                detection_roll = 0.0  # Always pass
                            else:
                                # Normal mode: probabilistic detection
                                # Base probability decreases with distance
                                base_prob = max(0.0, 1.0 - (rng / 15000.0))
                                
                                # Apply maintenance penalty if periscope system is degraded
                                maintenance_factor = getattr(own.maintenance.levels, "periscope", 1.0) if hasattr(own, "maintenance") else 1.0
                                detection_prob = base_prob * maintenance_factor
                                
                                # Surface vessels are easier to detect than submarines
                                if s.ship_class == "Convoy":
                                    detection_prob *= 1.3  # 30% easier to detect
                                elif s.ship_class == "Destroyer":
                                    detection_prob *= 1.1  # 10% easier to detect
                                
                                # Improve detection probability for previously spotted targets
                                if detection_count > 0:
                                    # Memory bonus capped at 25%, decays over 60 seconds
                                    time_factor = max(0.0, 1.0 - time_since_last_seen / 60.0)
                                    memory_bonus = min(0.25, detection_count * 0.10) * time_factor
                                    detection_prob = min(0.95, detection_prob + memory_bonus)

                                # Cap at 95% maximum detection probability (never perfect, but very high for known targets)
                                detection_prob = min(0.95, detection_prob)

                                # Roll for detection
                                detection_roll = random.random()

                            # Check for detection
                            detected_this_cycle = detection_roll < detection_prob

                            # Update contact history if we detected it this cycle
                            if detected_this_cycle:
                                self._visual_contacts["ownship"][s.id] = {
                                    "last_seen": current_time,
                                    "detection_count": detection_count + 1,
                                    "last_confidence": detection_prob
                                }
                        
                        # Include contact if:
                        # 1. We detected it this cycle, OR
                        # 2. We saw it recently (within 2 minutes) and it's still in range
                        if detected_this_cycle or (detection_count > 0 and time_since_last_seen <= 120.0 and rng <= 15000.0):
                            # Use current detection confidence or last known confidence
                            current_confidence = last_confidence if not detected_this_cycle else last_confidence
                            
                            brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
                            
                            # Calculate time since last seen for UI display
                            time_since_last_seen = current_time - (last_seen if not detected_this_cycle else current_time)
                            
                            # Get/create anonymous designation for this contact
                            designation = self._contact_registry.get_or_create_designation(s.id, current_time)
                            is_identified = self._contact_registry.is_identified(designation)

                            # Show ship type only if identified, otherwise show generic descriptor
                            if is_identified:
                                ship_type = self._contact_registry.get_identified_class(designation) or "Unknown"
                            else:
                                ship_type = "Unidentified Vessel"

                            self._periscope_contacts.append({
                                "id": designation,  # Anonymous designation
                                "actual_id": s.id,  # Hidden from UI, used for identification
                                "bearing": brg_true,
                                "range_m": rng,
                                "speed_kn": s.kin.speed,
                                "type": ship_type,
                                "is_identified": is_identified,
                                "confidence": current_confidence,
                                "last_seen": last_seen if not detected_this_cycle else current_time,
                                "detection_count": detection_count + (1 if detected_this_cycle else 0),
                                "detected_this_cycle": detected_this_cycle,
                                "time_since_last_seen": time_since_last_seen,
                                "status": "visible" if detected_this_cycle else "last_seen"
                            })
            
            # Reset timer after detection cycle
            if self._periscope_search_timer >= search_interval:
                self._periscope_search_timer = 0.0
        # Build waypoint progress for captain display
        waypoint_progress = {}
        if hasattr(self, "_waypoint_tracker"):
            waypoint_progress = self._waypoint_tracker.get_all_progress()
        # Build mission status
        mission_status = {"status": "ongoing"}
        if hasattr(self, "_mission_outcome"):
            mission_status = self._mission_outcome.dict()
        tel_captain = {
            **base,
            "periscopeRaised": self._periscope_raised,
            "radioRaised": self._radio_raised,
            "mission": {
                "title": self.mission_brief["title"],
                "objective": self.mission_brief["objective"],
                "roe": self.mission_brief["roe"]
            },
            "comms": getattr(self, "_captain_comms", []),
            "stationStatus": station_statuses,
            "periscopeContacts": self._periscope_contacts,
            # New scenario system fields
            "intercepts": getattr(self, "_captain_intercepts", []),
            "missionStatus": mission_status,
            "waypointProgress": waypoint_progress,
            "simTime": getattr(self, "_sim_time_s", 0.0),
        }
        tel_helm = {**base, "cavitationSpeedWarn": speed > 25.0, "thermocline": own.acoustics.thermocline_on, "tasks": [t.__dict__ for t in self._active_tasks['helm']]}
        # Prepare recent active ping responses list (bearing, range_est, strength, time)
        # For now, only generate on demand when 'sonar.ping' happens; UI will render as DEMON dots
        if not hasattr(self, "_last_ping_responses"):
            self._last_ping_responses = []
        # Include passive projectiles (torpedoes only - depth charges are silent while sinking)
        proj_contacts = passive_projectiles(own, self.world.torpedoes, getattr(self.world, "depth_charges", []))
        # Initialize explosion overlays list if absent
        if not hasattr(self, "_sonar_explosions"):
            self._sonar_explosions = []
        # Create explosion contacts from recent explosions
        explosion_contacts_list = explosion_contacts(own, getattr(self, "_sonar_explosions", []))
        # Create countermeasure contacts (noisemakers and decoys)
        cm_contacts = countermeasure_contacts(own, self.world.countermeasures)
        # Collect all AI ping responses
        # Include enemy ping contacts in the contacts list
        enemy_ping_contacts = []
        if hasattr(self, "_enemy_ping_contacts"):
            enemy_ping_contacts = [epc["contact"] for epc in self._enemy_ping_contacts]

        # Note: all_ai_ping_responses removed from pingResponses - enemy pings should appear as contacts, not ping responses
        all_contacts = contacts + proj_contacts + explosion_contacts_list + cm_contacts + enemy_ping_contacts
        tel_sonar = {**base, "contacts": [c.dict() for c in all_contacts], "pingCooldown": max(0.0, self.active_ping_state.timer), "pingResponses": list(self._last_ping_responses), "lastPingAt": getattr(self, "_last_ping_at", None), "explosions": list(self._sonar_explosions), "tasks": [t.__dict__ for t in self._active_tasks['sonar']]}
        tel_weapons = {
            **base,
            "tubes": [t.dict() for t in own.weapons.tubes],
            "consentRequired": CONFIG.require_captain_consent,
            "captainConsent": self._captain_consent,
            "tasks": [t.__dict__ for t in self._active_tasks['weapons']],
            "countermeasures": {
                "noisemakers": getattr(own.weapons, "noisemakers_stored", 0),
                "decoys": getattr(own.weapons, "decoys_stored", 0),
                "deployed": [
                    {"id": cm["id"], "type": cm["type"], "age_s": cm.get("age_s", 0.0)}
                    for cm in self.world.countermeasures if cm.get("active", False)
                ]
            }
        }
        # Build pump status with compartment assignments
        pump_status = {
            "assignments": self._pump_assignments,  # {pump_num: compartment_idx}
            "pump1": self._pump_assignments.get(1),  # None or compartment index
            "pump2": self._pump_assignments.get(2),  # None or compartment index
        }
        # Build compartment data for telemetry
        compartment_data = [
            {
                "index": i,
                "name": ["FORE", "FORWARD", "CONTROL", "REACTOR", "ENGINE", "STERN"][i],
                "flooding_level": c.flooding_level,
                "hull_integrity": c.hull_integrity,
                "breach_rate": c.breach_rate,
                "pump_active": c.pump_active,
            }
            for i, c in enumerate(own.damage.compartments)
        ]
        tel_engineering = {
            **base,
            "reactor": own.reactor.dict(),
            "pumps": pump_status,
            "compartments": compartment_data,
            "damage": own.damage.dict(),
            "power": own.power.dict(),
            "systems": own.systems.dict(),
            "maintenance": own.maintenance.levels,
            "tasks": [t.__dict__ for t in self._active_tasks['engineering']]
        }

        def bearings_to(sx: float, sy: float) -> Dict[str, float]:
            # Compass bearing: 0=N, 90=E, 180=S, 270=W
            dx = sx - own.kin.x
            dy = sy - own.kin.y
            # atan2 returns angle from +X; to get compass bearing from +Y (north), swap args as atan2(dx, dy)
            brg_true = (math.degrees(math.atan2(dx, dy)) % 360.0)
            # Validate south-of-ownship case to reduce confusion: if target.y < own.y and dx≈0, brg_true≈180
            brg_rel = (brg_true - own.kin.heading + 360.0) % 360.0
            return {"bearing_true": brg_true, "bearing_rel": brg_rel, "heading_to_face": brg_true}

        debug_payload = {
            "ownship": {
                "x": own.kin.x, "y": own.kin.y, "depth": own.kin.depth,
                "heading": own.kin.heading, "speed": own.kin.speed,
            },
            "missionId": getattr(CONFIG, "mission_id", "patrol") or "patrol",
            "maintenance": {"spawnsEnabled": (not self._suppress_maintenance_spawns)},
            "debugToggles": {
                "playerVisual100": self._debug_player_visual_100,
                "enemyVisual100": self._debug_enemy_visual_100
            },
            # AI orchestrator quick status for debug view
            "ai": {
                "enabled": bool(getattr(CONFIG, "use_ai_orchestrator", False)),
                "engines": {
                    "fleet": {"engine": getattr(CONFIG, "ai_fleet_engine", "stub"), "model": getattr(CONFIG, "ai_fleet_model", "stub")},
                    "ship": {"engine": getattr(CONFIG, "ai_ship_engine", "stub"), "model": getattr(CONFIG, "ai_ship_model", "stub")},
                },
                # Last provider call metadata (if any)
                "providerMeta": {
                    "fleet": (getattr(getattr(self, "_ai_orch", None), "_fleet_engine", None)._last_call_meta if getattr(getattr(self, "_ai_orch", None), "_fleet_engine", None) is not None and hasattr(getattr(self, "_ai_orch", None)._fleet_engine, "_last_call_meta") else None),
                    "ship": (getattr(getattr(self, "_ai_orch", None), "_ship_engine", None)._last_call_meta if getattr(getattr(self, "_ai_orch", None), "_ship_engine", None) is not None and hasattr(getattr(self, "_ai_orch", None)._ship_engine, "_last_call_meta") else None),
                },
            },
            "ships": [
                {
                    "id": s.id, "side": s.side,
                    "class": getattr(s, "ship_class", None),
                    "capabilities": (getattr(s, "capabilities", None).dict() if getattr(s, "capabilities", None) else None),
                    "weapons": (getattr(s, "weapons", None).dict() if getattr(s, "weapons", None) else None),
                    "x": s.kin.x, "y": s.kin.y, "depth": s.kin.depth,
                    "heading": s.kin.heading, "speed": s.kin.speed,
                    # Damage information
                    "damage": {
                        "hull": s.damage.hull,
                        "sensors": s.damage.sensors,
                        "propulsion": s.damage.propulsion,
                        "flooding_rate": s.damage.flooding_rate
                    },
                    # Passive detectability breakdown for debug
                    "slDb": getattr(s.acoustics, "last_snr_db", 0.0) + (20.0 * 0),
                    "snrDb": getattr(s.acoustics, "last_snr_db", 0.0),
                    "passiveDetect": getattr(s.acoustics, "last_detectability", 0.0),
                    **bearings_to(s.kin.x, s.kin.y),
                    "range_from_own": (( ( (s.kin.x - own.kin.x)**2 + (s.kin.y - own.kin.y)**2 ) ** 0.5 )),
                    # Include contacts this ship has detected (e.g., from player's active ping)
                    "contacts": [c.dict() for c in getattr(self, "_enemy_ship_contacts", {}).get(s.id, [])]
                }
                for s in self.world.all_ships() if s.id != own.id
            ],
            "torpedoes": list(self.world.torpedoes),
            "depth_charges": list(getattr(self.world, "depth_charges", [])),
        }
        # Include Fleet Commander contact history if orchestrator is present
        try:
            if hasattr(self, "_ai_orch") and getattr(self, "_ai_orch", None) is not None:
                debug_payload["contactHistory"] = list(getattr(self._ai_orch, "_fleet_contact_history", []))[-100:]
        except Exception:
            pass
        
        # Clean up old enemy ship contacts (older than 30 seconds)
        if hasattr(self, "_enemy_ship_contacts") and hasattr(self, "_enemy_ship_contacts_timestamps"):
            current_time = time.time()
            for ship_id in list(self._enemy_ship_contacts.keys()):
                if ship_id in self._enemy_ship_contacts_timestamps:
                    # Keep contacts and timestamps that are less than 30 seconds old
                    valid_indices = [
                        i for i, timestamp in enumerate(self._enemy_ship_contacts_timestamps[ship_id])
                        if (current_time - timestamp) < 30.0
                    ]
                    self._enemy_ship_contacts[ship_id] = [
                        self._enemy_ship_contacts[ship_id][i] for i in valid_indices
                    ]
                    self._enemy_ship_contacts_timestamps[ship_id] = [
                        self._enemy_ship_contacts_timestamps[ship_id][i] for i in valid_indices
                    ]
                    
                    # Remove empty entries
                    if not self._enemy_ship_contacts[ship_id]:
                        del self._enemy_ship_contacts[ship_id]
                        del self._enemy_ship_contacts_timestamps[ship_id]

        # DEBUG: Log telemetry broadcast
        if self._debug_tick_count % 100 == 1:
            print(f"DEBUG: Broadcasting telemetry to all stations")
        await BUS.publish("tick:all", {"topic": "telemetry", "data": tel_all})
        await BUS.publish("tick:captain", {"topic": "telemetry", "data": tel_captain})
        # Store for tests/inspection
        self._last_captain_tel = tel_captain
        await BUS.publish("tick:helm", {"topic": "telemetry", "data": tel_helm})
        await BUS.publish("tick:sonar", {"topic": "telemetry", "data": tel_sonar})
        await BUS.publish("tick:weapons", {"topic": "telemetry", "data": tel_weapons})
        await BUS.publish("tick:engineering", {"topic": "telemetry", "data": tel_engineering})
        await BUS.publish("tick:debug", {"topic": "telemetry", "data": debug_payload})
        # Fleet AI telemetry: intent and recent runs/tool calls
        from ..config import CONFIG as _CFG
        fleet_payload = {
            **base,
            "fleetIntent": getattr(self, "_fleet_intent", {}),
            "aiRuns": getattr(self, "_ai_recent_runs", [])[-50:],
            "engines": {
                "fleet": {"engine": getattr(_CFG, "ai_fleet_engine", "stub"), "model": getattr(_CFG, "ai_fleet_model", "stub")},
                "ship": {"engine": getattr(_CFG, "ai_ship_engine", "stub"), "model": getattr(_CFG, "ai_ship_model", "stub")},
            },
            "ships": [
                {
                    "id": s.id, "side": s.side, "class": getattr(s, "ship_class", None),
                    "x": s.kin.x, "y": s.kin.y, "depth": s.kin.depth,
                    "heading": s.kin.heading, "speed": s.kin.speed,
                }
                for s in self.world.all_ships()
            ],
        }
        await BUS.publish("tick:fleet", {"topic": "telemetry", "data": fleet_payload})

        # Action log telemetry - only sends action events
        action_events = [e for e in self._transient_events if e.get("type") == "action.log"]
        logs_payload = {
            **base,
            "actions": action_events,
        }
        await BUS.publish("tick:logs", {"topic": "telemetry", "data": logs_payload})

        # Clear transient events after publishing
        self._transient_events.clear()
        
        # Clear old enemy ping contacts (keep them for 5 seconds)
        if hasattr(self, "_enemy_ping_contacts"):
            current_time = time.time()
            self._enemy_ping_contacts = [
                epc for epc in self._enemy_ping_contacts 
                if current_time - epc.get("timestamp_epoch", 0) < 5.0
            ]

        self._last_snapshot += dt
        if self._last_snapshot >= CONFIG.snapshot_s:
            self._last_snapshot = 0.0
            insert_snapshot(self.engine, self.run_id, heading, speed, depth)
        # Handle timed comms after core tick; uses sim time
        self._handle_captain_comms(dt)

    def _dispatch_core_events(self, core_result) -> None:
        """Wire physics-core events to BUS, transient_events, and event store.

        The core itself does no I/O. Each tick it returns a list of events
        (torpedo detonations, depth-charge detonations, ship destructions);
        this method attaches the standard simulation reactions so the core
        stays free of asyncio/BUS.
        """
        for ev in core_result.events:
            if ev.kind in ("torpedo.detonated", "depth_charge.detonated"):
                insert_event(self.engine, self.run_id, ev.kind, json.dumps(ev.payload))
                self._transient_events.append({
                    "type": ev.kind,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    **ev.payload,
                })
                # Phase 8 — feed combat events into the orchestrator's
                # rolling buffer so threat scans can react to them.
                if getattr(self, "_ai_orch", None) is not None:
                    try:
                        self._ai_orch.record_combat_event(ev.kind, ev.payload)
                    except Exception:
                        pass
            elif ev.kind == "ship.destroyed":
                ship_id = ev.payload.get("ship_id")
                if ship_id and ship_id not in self._destroyed_ships:
                    self._destroyed_ships.add(ship_id)
                    print(f"SHIP DESTROYED: {ship_id}")
                    self._transient_events.append({
                        "type": "ship.destroyed",
                        "target": ship_id,
                        "x": ev.payload.get("x", 0.0),
                        "y": ev.payload.get("y", 0.0),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    })
                    insert_event(self.engine, self.run_id, "ship.destroyed", json.dumps({"ship_id": ship_id}))

        if core_result.sonar_explosions:
            if not hasattr(self, "_sonar_explosions"):
                self._sonar_explosions = []
            for sx in core_result.sonar_explosions:
                self._sonar_explosions.append({
                    **sx,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
            self._sonar_explosions = self._sonar_explosions[-12:]

    def _apply_system_failures(self, own, system_failures) -> None:
        """Apply compartment-flooding consequences to ownship limits.

        Mirrors the existing per-tick application: each affected limit is
        scaled by its factor when the factor < 1.0. (This compounds tick
        over tick; preserved as-is from prior behavior.)
        """
        if not system_failures:
            return
        loading = system_failures.get("torpedo_loading_factor", 1.0)
        if loading < 1.0:
            own.weapons.time_penalty_multiplier = max(
                own.weapons.time_penalty_multiplier,
                1.0 / max(0.1, loading),
            )
        reactor_factor = system_failures.get("reactor_factor", 1.0)
        if reactor_factor < 1.0:
            own.reactor.output_mw = min(own.reactor.output_mw, own.reactor.max_mw * reactor_factor)
        propulsion_factor = system_failures.get("propulsion_factor", 1.0)
        if propulsion_factor < 1.0:
            own.hull.max_speed = own.hull.max_speed * propulsion_factor
        rudder_factor = system_failures.get("rudder_factor", 1.0)
        if rudder_factor < 1.0:
            own.hull.turn_rate_max = own.hull.turn_rate_max * rudder_factor

    def _post_apply_ship_action(self, ship_id, ship, action, result) -> None:
        """Side effects keyed off the action type, after a successful apply.

        - `SetNavAction`: record last orders so the orchestrator can preserve
          inter-tick continuity in its prompts.
        - `ActivePingAction`: trigger simulation-wide reactions (player
          counter-detection contact + transient UI event).
        Other action types currently have no post-apply needs.
        """
        if isinstance(action, SetNavAction):
            try:
                if getattr(self, "_ai_orch", None) is not None:
                    if not hasattr(self._ai_orch, "_orders_last_by_ship"):
                        self._ai_orch._orders_last_by_ship = {}
                    self._ai_orch._orders_last_by_ship[ship_id] = {
                        "heading": float(ship.kin.heading),
                        "speed": float(ship.kin.speed),
                        "depth": float(ship.kin.depth),
                    }
            except Exception:
                pass
        elif isinstance(action, ActivePingAction):
            self._handle_enemy_ping_side_effects(ship, result.data or [])

    def _handle_enemy_ping_side_effects(self, src_ship, ping_responses) -> None:
        """Apply simulation-level reactions to an enemy active sonar ping.

        `ShipControls.active_ping` handles the source ship's cooldown and
        returns the responses; this method handles everything that's
        simulation-wide: storing responses for AI continuity, creating a
        counter-detection contact for the player, and emitting the transient
        `counterDetected` event.
        """
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Store responses for the source ship (used by AI continuity)
        ping_records = [
            {"id": rid, "bearing": brg, "range_est": rng, "strength": st, "at": now_iso, "source": src_ship.id}
            for (rid, rng, brg, st) in ping_responses
        ]
        if not hasattr(self, "_ai_ping_responses"):
            self._ai_ping_responses = {}
        self._ai_ping_responses[src_ship.id] = ping_records

        # Counter-detection contact: player hears the enemy's ping
        own = self.world.get_ship("ownship")
        if own and own.id != src_ship.id:
            dx = src_ship.kin.x - own.kin.x
            dy = src_ship.kin.y - own.kin.y
            rng = math.hypot(dx, dy)
            brg = normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
            brg_noise = normalize_angle_deg(brg + random.gauss(0, 2.0))
            strength = max(0.0, min(1.0, 1.0 / (1.0 + (rng / 3000.0))))
            enemy_ping_contact = TelemetryContact(
                id="ENEMY_ACTIVE_SONAR",
                bearing=brg_noise,
                strength=strength,
                classifiedAs="ENEMY_ACTIVE_SONAR",
                confidence=0.9,
                bearingKnown=True,
                rangeKnown=False,
            )
            if not hasattr(self, "_enemy_ping_contacts"):
                self._enemy_ping_contacts = []
            self._enemy_ping_contacts.append({
                "contact": enemy_ping_contact,
                "timestamp": now_iso,
                "timestamp_epoch": time.time(),
                "source": src_ship.id,
            })

        self._transient_events.append({"type": "counterDetected", "at": now_iso, "source": src_ship.id})

    def _handle_captain_comms(self, dt: float) -> None:
        own = self.world.get_ship("ownship")
        if not hasattr(self, "_sim_time_s"):
            self._sim_time_s = 0.0
        self._sim_time_s += dt
        # Require shallow enough depth and radio raised
        at_radio_depth = own.kin.depth <= 20.0 and self._radio_raised
        if not at_radio_depth:
            return
        # Deliver next scheduled message if time passed
        sched = self.mission_brief.get("comms_schedule", [])
        next_idx = self._delivered_comms_idx + 1
        if 0 <= next_idx < len(sched):
            if self._sim_time_s >= sched[next_idx]["at_s"]:
                # Append to captain comms list
                if not hasattr(self, "_captain_comms"):
                    self._captain_comms = []
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._captain_comms.append({"at": ts, "text": sched[next_idx]["msg"]})
                self._delivered_comms_idx = next_idx

    async def handle_command(self, topic: str, data: Dict) -> Optional[str]:
        # Guard: allow only debug commands when no mission is active or world not ready
        if not self._mission_active and not topic.startswith("debug."):
            return "No active mission"
        own = self.world.get_ship("ownship")
        if own is None and not topic.startswith("debug."):
            return "Simulation not ready"
        return await self._cmd_dispatcher.dispatch(topic, data)
