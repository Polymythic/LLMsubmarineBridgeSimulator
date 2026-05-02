"""Tests for Phase 7 — torpedo and depth-charge lethality.

Locks in the rebalanced damage model:
- A single torpedo hit cripples the target.
- Two torpedo hits in the same area destroy the target.
- A direct depth-charge hit is painful but rarely fatal alone.
- Critical compartment loss + flooding = catastrophic.
- Breach healing is slow enough that flooding actually progresses.
"""
import pytest

from backend.models import CompartmentState
from backend.sim.damage import (
    BREACH_HEALING_RATE,
    apply_compartment_damage,
    compute_hull_damage,
    step_damage,
)
from backend.sim.weapons import (
    DEPTH_CHARGE_DIRECT_PRIMARY_INTEGRITY_LOSS,
    DEPTH_CHARGE_FAR_PRIMARY_INTEGRITY_LOSS,
    TORPEDO_ADJACENT_INTEGRITY_LOSS,
    TORPEDO_PRIMARY_BREACH_RATE,
    TORPEDO_PRIMARY_INTEGRITY_LOSS,
)

from conftest import make_ship


# --------------------------------------------------------------------------- #
# compute_hull_damage formula behavior
# --------------------------------------------------------------------------- #

def _comps(losses):
    """Build compartments with given integrity_loss list (length 6)."""
    out = []
    for loss in losses:
        c = CompartmentState()
        c.hull_integrity = max(0.0, 1.0 - loss)
        out.append(c)
    return out


def test_hull_damage_zero_when_undamaged():
    assert compute_hull_damage(_comps([0.0] * 6)) == pytest.approx(0.0)


def test_hull_damage_dominated_by_worst_compartment():
    """Even when the average is small, a single nearly-destroyed compartment
    drives hull damage to a critical level."""
    # One comp at 0.9 loss, rest pristine. Avg = 0.15. Old formula = 0.15;
    # new formula should be substantially higher.
    losses = [0.9, 0.0, 0.0, 0.0, 0.0, 0.0]
    h = compute_hull_damage(_comps(losses))
    assert h > 0.4, f"single near-total compartment loss should be critical, got {h}"


def test_hull_damage_two_destroyed_compartments_is_total_loss():
    """Two fully-destroyed compartments triggers keel-break / mission kill."""
    losses = [1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    assert compute_hull_damage(_comps(losses)) == pytest.approx(1.0)


def test_hull_damage_critical_compartment_lost_and_flooded_is_fatal():
    """Loss of engine room (compartment 4) + full flood = catastrophic."""
    comps = [CompartmentState() for _ in range(6)]
    comps[4].hull_integrity = 0.0
    comps[4].flooding_level = 1.0
    assert compute_hull_damage(comps) == pytest.approx(1.0)


def test_hull_damage_critical_compartment_lost_but_not_flooded_is_severe_not_total():
    """Engine room destroyed but not yet flooded — still severe, not 1.0."""
    comps = [CompartmentState() for _ in range(6)]
    comps[4].hull_integrity = 0.0
    # No flooding
    h = compute_hull_damage(comps)
    assert 0.4 < h < 1.0


# --------------------------------------------------------------------------- #
# Torpedo damage: single hit cripples; two hits destroy
# --------------------------------------------------------------------------- #

def test_single_torpedo_hit_cripples_ship():
    """Apply one hit's worth of damage manually (primary + 2 adjacents) and
    confirm hull damage exceeds 0.4 (heavily damaged)."""
    ship = make_ship()
    # Simulate a midship hit: primary comp 3, adjacents 2 and 4.
    apply_compartment_damage(ship, 3,
                              breach_rate_add=TORPEDO_PRIMARY_BREACH_RATE,
                              integrity_loss=TORPEDO_PRIMARY_INTEGRITY_LOSS)
    apply_compartment_damage(ship, 2,
                              breach_rate_add=0.25,
                              integrity_loss=TORPEDO_ADJACENT_INTEGRITY_LOSS)
    apply_compartment_damage(ship, 4,
                              breach_rate_add=0.25,
                              integrity_loss=TORPEDO_ADJACENT_INTEGRITY_LOSS)
    ship.damage.hull = compute_hull_damage(ship.damage.compartments)
    assert ship.damage.hull > 0.4, f"single torpedo hit should cripple ship, got hull={ship.damage.hull}"
    # Heavy breach should be present on the primary compartment
    assert ship.damage.compartments[3].breach_rate >= 0.6


def test_two_torpedo_hits_destroy_or_near_destroy_ship():
    """Two hits clustered in the same area should bring hull >= ~0.9."""
    ship = make_ship()
    # Two midship hits, both primary on comp 3.
    for _ in range(2):
        apply_compartment_damage(ship, 3,
                                  breach_rate_add=TORPEDO_PRIMARY_BREACH_RATE,
                                  integrity_loss=TORPEDO_PRIMARY_INTEGRITY_LOSS)
        apply_compartment_damage(ship, 2,
                                  breach_rate_add=0.25,
                                  integrity_loss=TORPEDO_ADJACENT_INTEGRITY_LOSS)
        apply_compartment_damage(ship, 4,
                                  breach_rate_add=0.25,
                                  integrity_loss=TORPEDO_ADJACENT_INTEGRITY_LOSS)
    ship.damage.hull = compute_hull_damage(ship.damage.compartments)
    # Two midship hits → comp 3 destroyed (=0), and adjacents heavily damaged.
    # Even though comp 4 is the engine room (critical), without flooding yet
    # the hull should still be > 0.85.
    assert ship.damage.hull > 0.85, (
        f"two clustered torpedo hits should be near-fatal, got hull={ship.damage.hull}"
    )


def test_three_torpedo_hits_destroy_ship():
    """Three hits on the same area should destroy the ship (hull >= 1.0)."""
    ship = make_ship()
    for _ in range(3):
        apply_compartment_damage(ship, 3,
                                  breach_rate_add=TORPEDO_PRIMARY_BREACH_RATE,
                                  integrity_loss=TORPEDO_PRIMARY_INTEGRITY_LOSS)
        apply_compartment_damage(ship, 2,
                                  breach_rate_add=0.25,
                                  integrity_loss=TORPEDO_ADJACENT_INTEGRITY_LOSS)
        apply_compartment_damage(ship, 4,
                                  breach_rate_add=0.25,
                                  integrity_loss=TORPEDO_ADJACENT_INTEGRITY_LOSS)
    # After three midship hits, comps 2, 3, 4 are likely all at 0 integrity
    ship.damage.hull = compute_hull_damage(ship.damage.compartments)
    assert ship.damage.hull >= 1.0, f"three midship hits should destroy ship, got hull={ship.damage.hull}"


# --------------------------------------------------------------------------- #
# Flooding & system failure progression after a hit
# --------------------------------------------------------------------------- #

def test_torpedo_hit_creates_flooding_over_time():
    """A torpedo breach must keep flooding on subsequent ticks (the breach
    rate exceeds the slow healing)."""
    ship = make_ship()
    apply_compartment_damage(ship, 0,
                              breach_rate_add=TORPEDO_PRIMARY_BREACH_RATE,
                              integrity_loss=TORPEDO_PRIMARY_INTEGRITY_LOSS)
    # Step damage for 5 seconds: flooding should rise meaningfully.
    for _ in range(5):
        step_damage(ship, dt=1.0)
    assert ship.damage.compartments[0].flooding_level > 0.2, (
        f"breach should drive significant flooding in 5s, got {ship.damage.compartments[0].flooding_level}"
    )


def test_torpedo_hit_triggers_system_failure_for_hit_compartment():
    """A bow hit (compartment 0) flooding should disable forward sonar and
    slow torpedo loading once the compartment fills."""
    ship = make_ship()
    apply_compartment_damage(ship, 0,
                              breach_rate_add=TORPEDO_PRIMARY_BREACH_RATE,
                              integrity_loss=TORPEDO_PRIMARY_INTEGRITY_LOSS)
    # Run damage steps long enough to flood compartment 0 past 75%.
    failures = {}
    for _ in range(120):
        failures = step_damage(ship, dt=1.0)
    assert ship.damage.compartments[0].flooding_level >= 0.75
    assert failures.get("torpedo_loading_factor", 1.0) <= 0.5
    assert failures.get("forward_sonar_factor", 1.0) <= 0.5


def test_breach_healing_is_slow_enough_to_progress_flooding():
    """A torpedo-grade breach (0.6) minus current healing rate must remain
    positive — otherwise damage just heals away."""
    assert TORPEDO_PRIMARY_BREACH_RATE > BREACH_HEALING_RATE * 10, (
        "torpedo breach rate must dwarf healing for flooding to actually progress"
    )


# --------------------------------------------------------------------------- #
# Depth charges remain less individually lethal
# --------------------------------------------------------------------------- #

def test_depth_charge_direct_hit_is_painful_not_fatal_alone():
    ship = make_ship()
    apply_compartment_damage(ship, 3,
                              breach_rate_add=0.30,
                              integrity_loss=DEPTH_CHARGE_DIRECT_PRIMARY_INTEGRITY_LOSS)
    apply_compartment_damage(ship, 2, breach_rate_add=0.08, integrity_loss=0.15)
    apply_compartment_damage(ship, 4, breach_rate_add=0.08, integrity_loss=0.15)
    ship.damage.hull = compute_hull_damage(ship.damage.compartments)
    # A single direct-hit DC should leave the ship hurt but functional.
    assert 0.2 < ship.damage.hull < 0.7, (
        f"single direct DC should be painful-but-survivable, got hull={ship.damage.hull}"
    )


def test_depth_charge_far_hit_is_minor():
    ship = make_ship()
    apply_compartment_damage(ship, 2,
                              breach_rate_add=0.10,
                              integrity_loss=DEPTH_CHARGE_FAR_PRIMARY_INTEGRITY_LOSS)
    ship.damage.hull = compute_hull_damage(ship.damage.compartments)
    assert ship.damage.hull < 0.20, f"far DC should be minor, got hull={ship.damage.hull}"


def test_torpedo_is_more_lethal_than_depth_charge():
    """Sanity: a torpedo hit must produce a higher hull-damage value than a
    direct depth-charge hit on equivalent ships."""
    ship_t = make_ship()
    apply_compartment_damage(ship_t, 3,
                              breach_rate_add=TORPEDO_PRIMARY_BREACH_RATE,
                              integrity_loss=TORPEDO_PRIMARY_INTEGRITY_LOSS)
    ship_t.damage.hull = compute_hull_damage(ship_t.damage.compartments)

    ship_dc = make_ship()
    apply_compartment_damage(ship_dc, 3,
                              breach_rate_add=0.30,
                              integrity_loss=DEPTH_CHARGE_DIRECT_PRIMARY_INTEGRITY_LOSS)
    ship_dc.damage.hull = compute_hull_damage(ship_dc.damage.compartments)

    assert ship_t.damage.hull > ship_dc.damage.hull
