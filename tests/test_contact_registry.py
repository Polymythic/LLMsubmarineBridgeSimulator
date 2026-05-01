"""Tests for sub-bridge/backend/sim/contact_registry.py"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from backend.sim.contact_registry import ContactRegistry


def test_new_contact_gets_sequential_designation():
    reg = ContactRegistry()
    d1 = reg.get_or_create_designation("ship_a", current_time=0.0)
    d2 = reg.get_or_create_designation("ship_b", current_time=0.0)
    assert d1 == "Contact-1"
    assert d2 == "Contact-2"


def test_same_ship_returns_same_designation():
    reg = ContactRegistry()
    d1 = reg.get_or_create_designation("ship_a", current_time=0.0)
    d2 = reg.get_or_create_designation("ship_a", current_time=5.0)
    assert d1 == d2


def test_identify_contact_sets_class():
    reg = ContactRegistry()
    d = reg.get_or_create_designation("ship_a")
    assert reg.identify_contact(d, "Destroyer") is True
    assert reg.get_identified_class(d) == "Destroyer"


def test_is_identified_before_and_after():
    reg = ContactRegistry()
    d = reg.get_or_create_designation("ship_a")
    assert reg.is_identified(d) is False
    reg.identify_contact(d, "SSN")
    assert reg.is_identified(d) is True


def test_get_actual_id_returns_real_ship_id():
    reg = ContactRegistry()
    d = reg.get_or_create_designation("destroyer_3")
    assert reg.get_actual_id(d) == "destroyer_3"


def test_get_designation_for_ship_reverse_lookup():
    reg = ContactRegistry()
    d = reg.get_or_create_designation("ship_x")
    assert reg.get_designation_for_ship("ship_x") == d
    assert reg.get_designation_for_ship("nonexistent") is None


def test_clear_stale_contacts_removes_old():
    reg = ContactRegistry()
    reg.get_or_create_designation("old_ship", current_time=0.0)
    reg.get_or_create_designation("new_ship", current_time=400.0)
    reg.clear_stale_contacts(timeout_s=300.0, current_time=400.0)
    assert reg.get_designation_for_ship("old_ship") is None
    assert reg.get_designation_for_ship("new_ship") is not None


def test_reset_clears_all():
    reg = ContactRegistry()
    reg.get_or_create_designation("ship_a")
    reg.get_or_create_designation("ship_b")
    reg.reset()
    assert len(reg.get_all_contacts()) == 0
    # Next designation starts at 1 again
    d = reg.get_or_create_designation("ship_c")
    assert d == "Contact-1"
