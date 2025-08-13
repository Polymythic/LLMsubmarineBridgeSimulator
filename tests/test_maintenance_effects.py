import os
import sys

# Ensure sub-bridge backend is importable (align with other tests)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from backend.sim.loop import Simulation
from backend.models import MaintenanceTask


def test_helm_degraded_and_failed_effects():
    sim = Simulation()
    own = sim.world.get_ship("ownship")

    # Baseline
    assert own.hull.turn_rate_max == 7.0
    assert own.systems.rudder_ok is True

    # Degraded reduces turn authority
    sim._apply_stage_penalties(own, "helm", "degraded")
    assert own.hull.turn_rate_max < 7.0

    # Failed disables rudder
    sim._apply_stage_penalties(own, "helm", "failed")
    assert own.systems.rudder_ok is False


def test_sonar_degradation_penalties():
    sim = Simulation()
    own = sim.world.get_ship("ownship")

    # Baseline acoustics modifiers
    assert getattr(own.acoustics, "passive_snr_penalty_db", 0.0) == 0.0
    assert getattr(own.acoustics, "active_range_noise_add_m", 0.0) == 0.0
    assert getattr(own.acoustics, "active_bearing_noise_extra", 0.0) == 0.0
    assert own.systems.sonar_ok is True

    sim._apply_stage_penalties(own, "sonar", "degraded")
    assert own.acoustics.passive_snr_penalty_db > 0.0
    assert own.acoustics.active_range_noise_add_m > 0.0
    assert own.acoustics.active_bearing_noise_extra > 0.0

    sim._apply_stage_penalties(own, "sonar", "failed")
    assert own.systems.sonar_ok is False


def test_weapons_degradation_and_failure():
    sim = Simulation()
    own = sim.world.get_ship("ownship")

    base_mult = own.weapons.time_penalty_multiplier
    assert base_mult == 1.0
    assert own.systems.tubes_ok is True

    sim._apply_stage_penalties(own, "weapons", "degraded")
    assert own.weapons.time_penalty_multiplier > base_mult

    sim._apply_stage_penalties(own, "weapons", "failed")
    assert own.systems.tubes_ok is False


def test_task_escalation_applies_penalties():
    sim = Simulation()
    own = sim.world.get_ship("ownship")

    # Seed a manual HELM task at expired deadline to force escalation
    t = MaintenanceTask(
        id="t1",
        station="helm",
        system="rudder",
        key="helm.rudder.lube",
        title="Rudder Lubricate",
        stage="normal",
        progress=0.0,
        started=False,
        base_deadline_s=5.0,
        time_remaining_s=0.0,
        created_at=0.0,
    )
    sim._active_tasks["helm"] = [t]

    # Capture current turn rate, then step tasks to trigger escalation to degraded
    before_turn = own.hull.turn_rate_max
    sim._step_station_tasks(own, dt=0.1)
    after_turn = own.hull.turn_rate_max
    assert sim._active_tasks["helm"][0].stage in ("degraded", "damaged", "failed")
    assert after_turn <= before_turn


