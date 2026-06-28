"""Tonal-signature catalog + per-type torpedo tonal tests.

Guards the enriched vessel roster (late-1960s–early-1980s Soviet + European)
and the per-faction torpedo tonal cards added on top of the sonar narrowband
filter. See SONAR_TONAL_FILTER_PLAN.md §3/§7.
"""
import json
from pathlib import Path

import pytest

from conftest import make_ship, make_test_simulation
from backend import models
from backend.assets import load_ship_catalog, SHIPS_CATALOG_PATH
from backend.sim import sonar


# The four generic archetypes are deliberate catch-all references ("an unknown
# ship of this category") — they may be fully ambiguous (no unique tell).
GENERIC_ARCHETYPES = {"SSN", "Convoy", "Destroyer", "Neutral"}
# Intentional mimics: vessels designed to hide among another card. The Boris
# Chilikin AOR is built to read like a plain merchant (Convoy), so it owns no
# discriminator by design.
INTENTIONAL_MIMICS = {"BorisChilikin"}
VALID_CATEGORIES = {"SSN", "Convoy", "Destroyer", "Neutral"}


@pytest.fixture(scope="module")
def catalog_json():
    return json.loads(Path(SHIPS_CATALOG_PATH).read_text())


def _ship_cards(catalog_json):
    return {k: [round(f, 3) for f in v["acoustics"]["tonal_lines"]]
            for k, v in catalog_json.items()}


def test_roster_size_and_categories(catalog_json):
    """Roster is enriched (>=20 entries) and every entry uses a valid category."""
    assert len(catalog_json) >= 20, f"expected an enriched roster, got {len(catalog_json)}"
    for key, entry in catalog_json.items():
        assert entry["ship_class"] in VALID_CATEGORIES, f"{key}: bad category {entry['ship_class']}"


def test_every_card_has_five_inband_lines(catalog_json):
    for name, lines in _ship_cards(catalog_json).items():
        assert len(lines) == 5, f"{name}: expected 5 tonal lines, got {len(lines)}"
        assert all(0.0 <= f <= 15.0 for f in lines), f"{name}: line outside 0–15 kHz"


def test_no_card_is_perfectly_clean(catalog_json):
    """Challenge invariant: every card shares >=1 line with some other card, so
    no narrow band ever isolates a single vessel cleanly."""
    cards = _ship_cards(catalog_json)
    counts = {}
    for lines in cards.values():
        for f in lines:
            counts[f] = counts.get(f, 0) + 1
    for name, lines in cards.items():
        shared = [f for f in lines if counts[f] > 1]
        assert shared, f"{name}: shares no line with any other card (too clean)"


def test_identifiable_types_own_a_discriminator(catalog_json):
    """Identifiability invariant: every specific vessel type (excluding the
    generic archetypes and the deliberate merchant-mimic AOR) owns >=1 line that
    is unique across the whole library, so skilled acoustic ID stays possible."""
    cards = _ship_cards(catalog_json)
    counts = {}
    for lines in cards.values():
        for f in lines:
            counts[f] = counts.get(f, 0) + 1
    for name, lines in cards.items():
        if name in GENERIC_ARCHETYPES or name in INTENTIONAL_MIMICS:
            continue
        discriminators = [f for f in lines if counts[f] == 1]
        assert discriminators, f"{name}: has no discriminator line (not ID-able)"


def test_catalog_loads_torpedo_type():
    """torpedo_type in catalog weapons flows onto the loaded WeaponsSuite."""
    load_ship_catalog()  # load the real catalog into models.SHIP_CATALOG
    # Soviet hulls carry Soviet fish; RN carries Tigerfish; default stays Mk48.
    assert models.SHIP_CATALOG["Victor"].default_weapons.torpedo_type == "SET-65"
    assert models.SHIP_CATALOG["Kashin"].default_weapons.torpedo_type == "53-65"
    assert models.SHIP_CATALOG["Type42"].default_weapons.torpedo_type == "Tigerfish"
    assert models.SHIP_CATALOG["SSN"].default_weapons.torpedo_type == "Mk48"


# ---- per-type torpedo tonal emission ----

def _torp(side, name):
    return {"id": "t1", "x": 0.0, "y": 2000.0, "depth": 100.0, "speed": 40.0,
            "side": side, "name": name}


def test_enemy_torpedo_emits_its_model_card():
    own = make_ship("ownship", side="BLUE", depth=100.0, speed=5.0)
    own.acoustics.tonal_lines = list(sonar.SUB_TONAL_LINES)
    for model, expected in sonar.TORPEDO_TONAL_CARDS.items():
        out = sonar.passive_projectiles(own, [_torp("RED", model)], None)
        assert out and out[0].tonalLines == list(expected), f"{model} card mismatch"


def test_unknown_torpedo_name_falls_back_to_mk48():
    own = make_ship("ownship", side="BLUE", depth=100.0, speed=5.0)
    out = sonar.passive_projectiles(own, [_torp("RED", "MysteryFish")], None)
    assert out and out[0].tonalLines == list(sonar.TORPEDO_TONAL_LINES)


def test_own_torpedo_stays_all_pass_regardless_of_model():
    own = make_ship("ownship", side="BLUE", depth=100.0, speed=5.0)
    out = sonar.passive_projectiles(own, [_torp("BLUE", "53-65")], None)
    assert out and out[0].tonalLines is None


def test_npc_quick_launch_uses_platform_torpedo_type():
    from backend.sim.weapons import try_launch_torpedo_quick
    red = make_ship("red-victor", side="RED", depth=150.0, speed=8.0)
    red.capabilities = models.ShipCapabilities(has_torpedoes=True)
    red.weapons.torpedo_type = "SET-65"
    res = try_launch_torpedo_quick(red, bearing_deg=90.0, run_depth=100.0)
    assert res["ok"] is True
    assert res["data"]["name"] == "SET-65"


# ---- exact-type identification: foundation + captain ----

def test_apply_mission_persists_specific_ship_type():
    """A ship spawned from a specific catalog type remembers it (ship_type),
    while ship_class stays the broad category."""
    from backend.assets import apply_mission_to_world, MissionConfig
    from backend.sim.ecs import World
    load_ship_catalog()
    mission = MissionConfig(
        id="t", title="t", objective="t",
        ships=[
            {"id": "ownship", "side": "BLUE", "class": "SSN", "spawn": {"x": 0, "y": 0}},
            {"id": "red-1", "side": "RED", "class": "Krivak", "spawn": {"x": 1000, "y": 0}},
        ],
    )
    world = World()
    apply_mission_to_world(mission, lambda: world, lambda b: None)
    red = world.get_ship("red-1")
    assert red.ship_type == "Krivak"      # exact hull remembered
    assert red.ship_class == "Destroyer"  # broad category unchanged


def test_captain_visual_id_names_exact_type():
    """Captain periscope ID resolves a known catalog type to 'Category - Class'."""
    import asyncio
    load_ship_catalog()
    sim = make_test_simulation()
    own = sim.world.get_ship("ownship")
    own.kin.depth = 10.0  # shallow enough for visual ID
    red = make_ship("red-krivak", side="RED", x=800.0, y=0.0, depth=0.0)
    red.ship_type = "Krivak"
    red.ship_class = "Destroyer"
    sim.world.add_ship(red)
    designation = sim._contact_registry.get_or_create_designation("red-krivak", 0.0)
    sim._periscope_raised = True
    sim._periscope_contacts = [{"id": designation, "status": "visible"}]
    err = asyncio.run(sim.handle_command("captain.identify_contact", {"designation": designation}))
    assert err is None, f"identify failed: {err}"
    assert sim._contact_registry.get_identified_class(designation) == "Destroyer - Krivak-class"


def test_captain_visual_id_falls_back_to_category_without_type():
    """A ship with no catalog type still IDs to a generic category label."""
    import asyncio
    sim = make_test_simulation()
    own = sim.world.get_ship("ownship")
    own.kin.depth = 10.0
    red = make_ship("red-x", side="RED", x=800.0, y=0.0, depth=0.0)
    red.ship_type = None
    red.ship_class = "Destroyer"
    sim.world.add_ship(red)
    designation = sim._contact_registry.get_or_create_designation("red-x", 0.0)
    sim._periscope_raised = True
    sim._periscope_contacts = [{"id": designation, "status": "visible"}]
    err = asyncio.run(sim.handle_command("captain.identify_contact", {"designation": designation}))
    assert err is None, f"identify failed: {err}"
    assert sim._contact_registry.get_identified_class(designation) == "Destroyer"
