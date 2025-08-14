import os
import sys
import pytest
import math
from unittest.mock import Mock, patch

# Ensure sub-bridge backend is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sub-bridge')))

from backend.sim.sonar import passive_contacts, _classify_ship_passive
from backend.sim.ai_orchestrator import AgentsOrchestrator
from backend.models import Ship, Kinematics, Acoustics, Hull, WeaponsSuite, Reactor, DamageState, PowerAllocations, SystemsStatus, MaintenanceState


def make_ship(id_: str, side: str, ship_class: str, x: float = 0.0, y: float = 0.0, 
              depth: float = 0.0, heading: float = 0.0, speed: float = 0.0) -> Ship:
    """Helper to create test ships with realistic properties."""
    return Ship(
        id=id_,
        side=side,
        kin=Kinematics(x=x, y=y, depth=depth, heading=heading, speed=speed),
        hull=Hull(max_depth=300.0, max_speed=25.0, quiet_speed=5.0),
        acoustics=Acoustics(
            thermocline_on=False,
            source_level_by_speed={5: 110.0, 15: 125.0, 25: 135.0},
            last_snr_db=0.0,
            last_detectability=0.0,
            bearing_noise_extra=0.0,
            passive_snr_penalty_db=0.0
        ),
        weapons=WeaponsSuite(tube_count=4, torpedoes_stored=16, tubes=[]),
        reactor=Reactor(),
        damage=DamageState(),
        power=PowerAllocations(),
        systems=SystemsStatus(),
        maintenance=MaintenanceState(),
        ship_class=ship_class,
        capabilities=None
    )


class TestPassiveSonarClassification:
    """Test the enhanced passive sonar classification system."""
    
    def test_strong_signal_confident_classification(self):
        """Strong signals should provide confident classifications."""
        ship = make_ship("test-ssn", "BLUE", "SSN")
        
        # Strong signal: high detectability and SNR
        result = _classify_ship_passive(ship, detectability=0.9, snr_db=30.0, range_m=1000.0)
        assert result == "SSN"
        
        # Test other ship types
        convoy = make_ship("test-convoy", "BLUE", "Convoy")
        result = _classify_ship_passive(convoy, detectability=0.85, snr_db=28.0, range_m=1500.0)
        assert result == "Merchant/Convoy"
        
        destroyer = make_ship("test-destroyer", "BLUE", "Destroyer")
        result = _classify_ship_passive(destroyer, detectability=0.88, snr_db=32.0, range_m=800.0)
        assert result == "Warship"
    
    def test_medium_signal_probable_classification(self):
        """Medium signals should provide probable classifications."""
        ship = make_ship("test-ssn", "BLUE", "SSN")
        
        # Medium signal: moderate detectability and SNR
        result = _classify_ship_passive(ship, detectability=0.7, snr_db=22.0, range_m=2000.0)
        assert result == "SSN?"
        
        convoy = make_ship("test-convoy", "BLUE", "Convoy")
        result = _classify_ship_passive(convoy, detectability=0.65, snr_db=21.0, range_m=2500.0)
        assert result == "Merchant?"
    
    def test_weak_signal_possible_classification(self):
        """Weak signals should provide possible classifications."""
        ship = make_ship("test-ssn", "BLUE", "SSN")
        
        # Weak signal: low detectability and SNR
        result = _classify_ship_passive(ship, detectability=0.45, snr_db=16.0, range_m=4000.0)
        assert result == "Submarine?"
        
        convoy = make_ship("test-convoy", "BLUE", "Convoy")
        result = _classify_ship_passive(convoy, detectability=0.42, snr_db=15.5, range_m=4500.0)
        assert result == "Vessel?"
    
    def test_very_weak_signal_unknown_classification(self):
        """Very weak signals should return unknown."""
        ship = make_ship("test-ssn", "BLUE", "SSN")
        
        # Very weak signal: very low detectability and SNR
        result = _classify_ship_passive(ship, detectability=0.3, snr_db=12.0, range_m=6000.0)
        assert result == "Unknown"
        
        result = _classify_ship_passive(ship, detectability=0.35, snr_db=14.0, range_m=5000.0)
        assert result == "Unknown"
    
    def test_edge_cases(self):
        """Test edge cases and boundary conditions."""
        ship = make_ship("test-ssn", "BLUE", "SSN")
        
        # Boundary between strong and medium
        result = _classify_ship_passive(ship, detectability=0.8, snr_db=25.0, range_m=1000.0)
        assert result == "SSN"  # Should be confident
        
        result = _classify_ship_passive(ship, detectability=0.79, snr_db=24.9, range_m=1000.0)
        assert result == "SSN?"  # Should be probable
        
        # Boundary between medium and weak
        result = _classify_ship_passive(ship, detectability=0.6, snr_db=20.0, range_m=2000.0)
        assert result == "SSN?"  # Should be probable
        
        result = _classify_ship_passive(ship, detectability=0.59, snr_db=19.9, range_m=2000.0)
        assert result == "Submarine?"  # Should be possible
    
    def test_unknown_ship_class(self):
        """Test handling of ships with unknown class."""
        ship = make_ship("test-unknown", "BLUE", None)
        
        # Should handle gracefully
        result = _classify_ship_passive(ship, detectability=0.9, snr_db=30.0, range_m=1000.0)
        assert result == "Unknown"


class TestVisualDetectionSystem:
    """Test the visual detection system with ship identification."""
    
    def test_visual_detection_includes_side_information(self):
        """Visual detection should include ship side for friendly identification."""
        # Create test world with ships
        ownship = make_ship("ownship", "BLUE", "SSN", x=0.0, y=0.0, depth=0.0)
        red_01 = make_ship("red-01", "RED", "Convoy", x=6000.0, y=0.0, depth=0.0)
        red_02 = make_ship("red-02", "RED", "Convoy", x=5800.0, y=10.0, depth=0.0)
        
        # Mock world getter
        world_mock = Mock()
        world_mock.all_ships.return_value = [ownship, red_01, red_02]
        
        # Create AI orchestrator with mocked dependencies
        orchestrator = AgentsOrchestrator(
            world_getter=lambda: world_mock,
            storage_engine=Mock(),
            run_id="test-run"
        )
        
        # Build ship summary for red-01
        summary = orchestrator._build_ship_summary(red_01)
        
        # Check that contacts include side information
        contacts = summary["contacts"]
        assert len(contacts) > 0
        
        # Find ownship contact
        ownship_contact = next((c for c in contacts if c["id"] == "ownship"), None)
        assert ownship_contact is not None
        assert ownship_contact["side"] == "BLUE"
        assert ownship_contact["class"] == "SSN"
        assert "bearing" in ownship_contact
        assert "range_est" in ownship_contact
    
    def test_visual_detection_range_limits(self):
        """Visual detection should respect range and depth limits."""
        # Create test world with ships at various ranges and depths
        ownship = make_ship("ownship", "BLUE", "SSN", x=0.0, y=0.0, depth=0.0)
        
        # Ship within visual range and depth
        red_close = make_ship("red-close", "RED", "Convoy", x=1000.0, y=0.0, depth=2.0)
        
        # Ship too far for visual detection
        red_far = make_ship("red-far", "RED", "Convoy", x=20000.0, y=0.0, depth=0.0)
        
        # Ship too deep for visual detection
        red_deep = make_ship("red-deep", "RED", "Convoy", x=1000.0, y=0.0, depth=10.0)
        
        world_mock = Mock()
        world_mock.all_ships.return_value = [ownship, red_close, red_far, red_deep]
        
        orchestrator = AgentsOrchestrator(
            world_getter=lambda: world_mock,
            storage_engine=Mock(),
            run_id="test-run"
        )
        
        summary = orchestrator._build_ship_summary(ownship)
        contacts = summary["contacts"]
        
        # Should detect close ship
        close_contact = next((c for c in contacts if c["id"] == "red-close"), None)
        assert close_contact is not None
        
        # Should not detect far ship
        far_contact = next((c for c in contacts if c["id"] == "red-far"), None)
        assert far_contact is None
        
        # Should not detect deep ship
        deep_contact = next((c for c in contacts if c["id"] == "red-deep"), None)
        assert deep_contact is None
    
    def test_visual_detection_bearing_calculation(self):
        """Visual detection should calculate correct bearings."""
        ownship = make_ship("ownship", "BLUE", "SSN", x=0.0, y=0.0, depth=0.0)
        
        # Ship to the east (bearing 90°)
        red_east = make_ship("red-east", "RED", "Convoy", x=1000.0, y=0.0, depth=0.0)
        
        # Ship to the north (bearing 0°)
        red_north = make_ship("red-north", "RED", "Convoy", x=0.0, y=1000.0, depth=0.0)
        
        # Ship to the northeast (bearing 45°)
        red_northeast = make_ship("red-northeast", "RED", "Convoy", x=1000.0, y=1000.0, depth=0.0)
        
        world_mock = Mock()
        world_mock.all_ships.return_value = [ownship, red_east, red_north, red_northeast]
        
        orchestrator = AgentsOrchestrator(
            world_getter=lambda: world_mock,
            storage_engine=Mock(),
            run_id="test-run"
        )
        
        summary = orchestrator._build_ship_summary(ownship)
        contacts = summary["contacts"]
        
        # Check east ship bearing (should be ~90°)
        east_contact = next((c for c in contacts if c["id"] == "red-east"), None)
        assert east_contact is not None
        assert abs(east_contact["bearing"] - 90.0) < 1.0  # Allow small tolerance
        
        # Check north ship bearing (should be ~0°)
        north_contact = next((c for c in contacts if c["id"] == "red-north"), None)
        assert north_contact is not None
        assert abs(north_contact["bearing"] - 0.0) < 1.0  # Allow small tolerance
        
        # Check northeast ship bearing (should be ~45°)
        northeast_contact = next((c for c in contacts if c["id"] == "red-northeast"), None)
        assert northeast_contact is not None
        assert abs(northeast_contact["bearing"] - 45.0) < 1.0  # Allow small tolerance


class TestPassiveSonarContacts:
    """Test the passive sonar contact generation system."""
    
    def test_passive_contacts_include_classification(self):
        """Passive contacts should include realistic classification."""
        ownship = make_ship("ownship", "BLUE", "SSN", x=0.0, y=0.0, depth=50.0, heading=0.0)
        target = make_ship("target", "RED", "Convoy", x=500.0, y=0.0, depth=10.0, speed=5.0)
        
        # Ensure target has source level data with higher levels for better detection
        target.acoustics.source_level_by_speed = {5: 120.0, 15: 135.0, 25: 145.0}
        
        # Mock systems to ensure sonar is working
        ownship.systems.sonar_ok = True
        
        contacts = passive_contacts(ownship, [target])
        
        assert len(contacts) > 0
        contact = contacts[0]
        
        # Should have classification based on signal quality
        assert "classifiedAs" in contact.__dict__
        assert contact.classifiedAs != "SSN?"  # Should use new classification system
        
        # Should include other required fields
        assert contact.id == "target"
        assert contact.bearing is not None
        assert contact.strength > 0
        assert contact.confidence > 0
    
    def test_passive_contacts_signal_quality_effects(self):
        """Signal quality should affect classification confidence."""
        ownship = make_ship("ownship", "BLUE", "SSN", x=0.0, y=0.0, depth=50.0, heading=0.0)
        
        # Test different ranges (affects signal quality) - use closer ranges for better detection
        ranges = [200, 400, 600, 800]
        classifications = []
        
        for rng in ranges:
            target = make_ship(f"target-{rng}", "RED", "Convoy", x=rng, y=0.0, depth=10.0, speed=5.0)
            # Ensure target has source level data with much higher levels for better detection
            target.acoustics.source_level_by_speed = {5: 130.0, 15: 145.0, 25: 155.0}
            ownship.systems.sonar_ok = True
            
            contacts = passive_contacts(ownship, [target])
            if contacts:
                classifications.append(contacts[0].classifiedAs)
        
        # Should have different classifications based on range
        assert len(set(classifications)) > 1  # At least some variation
    
    def test_passive_contacts_sonar_failure(self):
        """Sonar failure should prevent contact generation."""
        ownship = make_ship("ownship", "BLUE", "SSN", x=0.0, y=0.0, depth=50.0)
        target = make_ship("target", "RED", "Convoy", x=1000.0, y=0.0, depth=10.0)
        
        # Mock sonar failure
        ownship.systems.sonar_ok = False
        
        contacts = passive_contacts(ownship, [target])
        
        # Should return no contacts when sonar is down
        assert len(contacts) == 0


if __name__ == "__main__":
    pytest.main([__file__])
