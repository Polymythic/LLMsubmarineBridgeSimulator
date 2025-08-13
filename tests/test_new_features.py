import os
import sys
import asyncio
import pytest

# Add project sub-bridge to import path (same pattern as existing tests)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from backend.sim.loop import Simulation
from backend.sim.damage import step_engineering
from backend.sim.physics import integrate_kinematics
from backend.sim.sonar import passive_contacts, active_ping
from backend.models import Ship, Kinematics, Hull, Acoustics, WeaponsSuite, Reactor, DamageState


def test_power_allocation_rejects_and_accepts():
    sim = Simulation()
    # Reject over-budget
    err = asyncio.run(sim.handle_command(
        "engineering.power.allocate", {"helm": 0.5, "weapons": 0.5, "sonar": 0.3, "engineering": 0.0}
    ))
    assert isinstance(err, str) and "exceeds" in err.lower()

    # Accept exact budget and set fractions
    err2 = asyncio.run(sim.handle_command(
        "engineering.power.allocate", {"helm": 0.1, "weapons": 0.2, "sonar": 0.3, "engineering": 0.4}
    ))
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


def test_active_ping_cooldown_and_event():
    sim = Simulation()
    # Initial ping should start cooldown and emit event
    err = asyncio.run(sim.handle_command("sonar.ping", {"array": "bow"}))
    assert err is None
    assert sim.active_ping_state.timer > 0.0
    # Event queued for this tick
    assert any(e.get("type") == "counterDetected" for e in sim._transient_events)

    # Immediate second ping should be rejected by cooldown
    err2 = asyncio.run(sim.handle_command("sonar.ping", {"array": "bow"}))
    assert isinstance(err2, str) and "cooldown" in err2.lower()


def test_compass_bearings_convention_in_active_ping():
    # 0=N, 90=E, 180=S, 270=W
    import random
    random.seed(0)
    own = Ship(
        id="ownship",
        side="BLUE",
        kin=Kinematics(x=0.0, y=0.0, depth=100.0, heading=0.0, speed=0.0),
        hull=Hull(), acoustics=Acoustics(), weapons=WeaponsSuite(), reactor=Reactor(), damage=DamageState()
    )
    def bearing_of(other_xy):
        other = Ship(
            id="contact",
            side="RED",
            kin=Kinematics(x=other_xy[0], y=other_xy[1], depth=100.0, heading=180.0, speed=0.0),
            hull=Hull(), acoustics=Acoustics(), weapons=WeaponsSuite(), reactor=Reactor(), damage=DamageState()
        )
        res = active_ping(own, [other])
        assert len(res) == 1
        _, _, brg, _ = res[0]
        return brg
    # East -> ~90
    b_e = bearing_of((1000.0, 0.0))
    assert 80.0 <= b_e <= 100.0
    # South -> ~180
    b_s = bearing_of((0.0, -1000.0))
    assert 170.0 <= b_s <= 190.0
    # West -> ~270
    b_w = bearing_of((-1000.0, 0.0))
    # account for wraparound: allow [260, 280]
    assert 260.0 <= b_w <= 280.0
    # North -> ~0 (or 360); normalize to [0,360)
    b_n = bearing_of((0.0, 1000.0)) % 360.0
    assert b_n <= 10.0 or b_n >= 350.0


def test_debug_restart_resets_world_to_defaults():
    import asyncio
    sim = Simulation()
    # Change orders to non-default
    _ = asyncio.run(sim.handle_command("helm.order", {"heading": 45.0, "speed": 15.0, "depth": 50.0}))
    # Restart
    _ = asyncio.run(sim.handle_command("debug.restart", {}))
    own = sim.world.get_ship("ownship")
    assert own.kin.heading == 270.0
    assert own.kin.speed == 8.0
    assert own.kin.depth == 100.0
    red = [s for s in sim.world.all_ships() if s.id != own.id]
    assert len(red) == 1 and red[0].id == "red-01"
    assert red[0].kin.x == 3000.0 and red[0].kin.y == 0.0
    assert red[0].kin.heading == 90.0 and red[0].kin.speed == 8.0 and red[0].kin.depth == 120.0


def test_station_tasks_spawn_and_progress_with_power():
    import time as _time
    sim = Simulation()
    own = sim.world.get_ship("ownship")
    # Force spawn timers to immediate
    sim._task_spawn_timers = {k: 0.0 for k in sim._task_spawn_timers.keys()}
    # Tick enough to spawn
    for _ in range(5):
        _ = asyncio.run(sim.tick(0.05))
    # Expect some tasks present (randomized, but at least one station should have a task)
    active = [k for k, v in sim._active_tasks.items() if v]
    assert len(active) >= 1
    # Allocate high power to first station and tick to progress
    st = active[0]
    own.power.helm = 0.0; own.power.weapons = 0.0; own.power.sonar = 0.0; own.power.engineering = 0.0
    if st == "helm": own.power.helm = 1.0
    elif st == "weapons": own.power.weapons = 1.0
    elif st == "sonar": own.power.sonar = 1.0
    else: own.power.engineering = 1.0
    # Start/repair the task explicitly; also covers case where none existed
    _ = asyncio.run(sim.handle_command("station.task.start", {"station": st}))
    p0 = sim._active_tasks[st][0].progress
    for _ in range(30):
        _ = asyncio.run(sim.tick(0.1))
        if not sim._active_tasks.get(st):
            break
    # Either completed (cleared) or progressed
    if not sim._active_tasks.get(st):
        assert True
    else:
        assert sim._active_tasks[st][0].progress > p0

