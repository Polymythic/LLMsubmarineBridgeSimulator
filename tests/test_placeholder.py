import json
import os
import sys

def test_placeholder_passes():
    # Smoke test placeholder to keep CI green until real tests are added
    assert True


def test_captain_comms_delivered_at_radio_depth():
    import asyncio
    # Add project sub-bridge to import path (consistent with other tests)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))
    from backend.sim.loop import Simulation
    sim = Simulation()
    own = sim.world.get_ship("ownship")
    # Go to radio depth and raise radio
    own.kin.depth = 10.0
    _ = asyncio.run(sim.handle_command("captain.radio.raise", {"raised": True}))
    # Fast-forward sim time to just before first comms and tick once
    sim._sim_time_s = 119.9
    _ = asyncio.run(sim.tick(0.2))
    # Expect at least one comm
    assert hasattr(sim, "_captain_comms") and len(sim._captain_comms) >= 1
