import os
import sys
import pytest

# Add project sub-bridge to import path (same pattern as existing tests)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from backend.sim.loop import Simulation
from backend.sim.damage import step_engineering
from backend.sim.physics import integrate_kinematics
from backend.sim.sonar import passive_contacts, active_ping


@pytest.mark.asyncio
async def test_power_allocation_rejects_and_accepts():
    sim = Simulation()
    # Reject over-budget
    err = await sim.handle_command(
        "engineering.power.allocate", {"helm": 0.5, "weapons": 0.5, "sonar": 0.3, "engineering": 0.0}
    )
    assert isinstance(err, str) and "exceeds" in err.lower()

    # Accept exact budget and set fractions
    err2 = await sim.handle_command(
        "engineering.power.allocate", {"helm": 0.1, "weapons": 0.2, "sonar": 0.3, "engineering": 0.4}
    )
    assert err2 is None
    own = sim.world.get_ship("ownship")
    assert own.power.helm == pytest.approx(0.1)
    assert own.power.weapons == pytest.approx(0.2)
    assert own.power.sonar == pytest.approx(0.3)
    assert own.power.engineering == pytest.approx(0.4)


def test_maintenance_failure_flags_and_recovery():
    sim = Simulation()
    own = sim.world.get_ship("ownship")

    # Force low maintenance -> failures
    for k in own.maintenance.levels.keys():
        own.maintenance.levels[k] = 0.1
    step_engineering(own, dt=0.05)
    assert not own.systems.rudder_ok
    assert not own.systems.ballast_ok
    assert not own.systems.sonar_ok
    assert not own.systems.tubes_ok

    # Recover with high engineering allocation over a few seconds
    own.power.helm = 0.0
    own.power.weapons = 0.0
    own.power.sonar = 0.0
    own.power.engineering = 1.0
    for _ in range(100):  # 100 * 0.05s = 5s
        step_engineering(own, dt=0.05)
    # Levels should climb above the 0.2 failure threshold
    assert own.maintenance.levels["rudder"] > 0.2
    step_engineering(own, dt=0.05)
    assert own.systems.rudder_ok


def test_physics_respects_rudder_and_ballast_failures():
    sim = Simulation()
    own = sim.world.get_ship("ownship")

    # Rudder failure -> no heading change
    own.maintenance.levels["rudder"] = 0.0
    step_engineering(own, dt=0.05)
    h0 = own.kin.heading
    cav, h1, *_ = integrate_kinematics(own, ordered_heading=(h0 + 90) % 360, ordered_speed=own.kin.speed, ordered_depth=own.kin.depth, dt=1.0)
    assert h1 == pytest.approx(h0)

    # Ballast failure -> very limited depth rate
    own.maintenance.levels["ballast"] = 0.0
    step_engineering(own, dt=0.05)
    d0 = own.kin.depth
    cav, _, _, d1 = integrate_kinematics(own, ordered_heading=own.kin.heading, ordered_speed=own.kin.speed, ordered_depth=d0 + 100, dt=1.0)
    # With failure, max rate ~0.5 m/s -> < 1.0 m change over 1 second
    assert d1 - d0 < 1.0


def test_sonar_gating_on_failure():
    sim = Simulation()
    own = sim.world.get_ship("ownship")
    red = [s for s in sim.world.all_ships() if s.id != own.id]

    # Healthy sonar should produce contacts
    contacts = passive_contacts(own, red)
    assert isinstance(contacts, list)

    # Fail sonar -> no contacts and no active returns
    own.maintenance.levels["sonar"] = 0.0
    step_engineering(own, dt=0.05)
    assert passive_contacts(own, red) == []
    assert active_ping(own, red) == []


@pytest.mark.asyncio
async def test_active_ping_cooldown_and_event():
    sim = Simulation()
    # Initial ping should start cooldown and emit event
    err = await sim.handle_command("sonar.ping", {"array": "bow"})
    assert err is None
    assert sim.active_ping_state.timer > 0.0
    # Event queued for this tick
    assert any(e.get("type") == "counterDetected" for e in sim._transient_events)

    # Immediate second ping should be rejected by cooldown
    err2 = await sim.handle_command("sonar.ping", {"array": "bow"})
    assert isinstance(err2, str) and "cooldown" in err2.lower()


