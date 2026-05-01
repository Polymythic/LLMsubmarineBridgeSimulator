"""
Contact Registry - Manages anonymous contact designations for sonar.

Contacts are assigned anonymous designations (Contact-1, Contact-2, etc.)
until the captain identifies them via periscope. This creates realistic
fog-of-war where sonar operators don't know ship types until visual confirmation.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Optional
from enum import Enum


class IdentificationLevel(Enum):
    UNKNOWN = "unknown"        # Only bearing/signal data
    IDENTIFIED = "identified"  # Fully identified via periscope


@dataclass
class RegisteredContact:
    """Represents a tracked contact in the registry."""
    designation: str              # Anonymous name: "Contact-1", "Contact-2"
    actual_ship_id: str           # Real ship ID in simulation
    identification_level: IdentificationLevel = IdentificationLevel.UNKNOWN
    identified_class: Optional[str] = None  # Only populated after identification
    first_detected_at: float = 0.0
    last_detected_at: float = 0.0


class ContactRegistry:
    """
    Manages mapping between anonymous contact designations and actual ship IDs.
    Tracks identification state for each contact.
    """

    def __init__(self):
        self._contacts: Dict[str, RegisteredContact] = {}  # designation -> contact
        self._ship_to_designation: Dict[str, str] = {}     # actual_id -> designation
        self._next_contact: int = 1

    def get_or_create_designation(self, actual_ship_id: str, current_time: float = 0.0) -> str:
        """
        Get existing designation or create new one for a ship.

        Args:
            actual_ship_id: The real ship ID from the simulation
            current_time: Current simulation time for tracking

        Returns:
            Anonymous designation like "Contact-1"
        """
        if actual_ship_id in self._ship_to_designation:
            # Update last detected time
            designation = self._ship_to_designation[actual_ship_id]
            if designation in self._contacts:
                self._contacts[designation].last_detected_at = current_time
            return designation

        # Create new designation
        designation = f"Contact-{self._next_contact}"
        self._next_contact += 1

        self._ship_to_designation[actual_ship_id] = designation
        self._contacts[designation] = RegisteredContact(
            designation=designation,
            actual_ship_id=actual_ship_id,
            first_detected_at=current_time,
            last_detected_at=current_time,
        )
        return designation

    def identify_contact(self, designation: str, ship_class: str) -> bool:
        """
        Mark a contact as identified (via periscope).

        Args:
            designation: The anonymous designation (e.g., "Contact-1")
            ship_class: The identified ship class (e.g., "Destroyer")

        Returns:
            True if identification succeeded, False if contact not found
        """
        if designation not in self._contacts:
            return False
        contact = self._contacts[designation]
        contact.identification_level = IdentificationLevel.IDENTIFIED
        contact.identified_class = ship_class
        return True

    def is_identified(self, designation: str) -> bool:
        """Check if a contact has been identified."""
        if designation not in self._contacts:
            return False
        return self._contacts[designation].identification_level == IdentificationLevel.IDENTIFIED

    def get_identified_class(self, designation: str) -> Optional[str]:
        """Get the identified class for a contact, or None if not identified."""
        if designation not in self._contacts:
            return None
        contact = self._contacts[designation]
        if contact.identification_level == IdentificationLevel.IDENTIFIED:
            return contact.identified_class
        return None

    def get_display_info(self, designation: str) -> dict:
        """
        Get display info for a contact based on identification state.

        Args:
            designation: The anonymous designation

        Returns:
            Dict with id, classifiedAs, and optionally actual_id
        """
        if designation not in self._contacts:
            return {"id": designation, "classifiedAs": "Unknown", "is_identified": False}

        contact = self._contacts[designation]
        if contact.identification_level == IdentificationLevel.IDENTIFIED:
            return {
                "id": designation,
                "classifiedAs": contact.identified_class,
                "actual_id": contact.actual_ship_id,
                "is_identified": True,
            }
        return {
            "id": designation,
            "classifiedAs": "Unknown",
            "is_identified": False,
        }

    def get_actual_id(self, designation: str) -> Optional[str]:
        """
        Get actual ship ID from designation (for fire control/targeting).

        Args:
            designation: The anonymous designation

        Returns:
            Actual ship ID or None if not found
        """
        if designation in self._contacts:
            return self._contacts[designation].actual_ship_id
        return None

    def get_designation_for_ship(self, actual_ship_id: str) -> Optional[str]:
        """
        Get the designation for a known ship ID.

        Args:
            actual_ship_id: The real ship ID

        Returns:
            The designation or None if ship not in registry
        """
        return self._ship_to_designation.get(actual_ship_id)

    def get_all_contacts(self) -> Dict[str, RegisteredContact]:
        """Get all registered contacts."""
        return self._contacts.copy()

    def clear_stale_contacts(self, timeout_s: float = 300.0, current_time: float = 0.0):
        """
        Remove contacts not detected for timeout period.

        Args:
            timeout_s: Seconds before a contact is considered stale
            current_time: Current simulation time
        """
        stale = [d for d, c in self._contacts.items()
                 if current_time - c.last_detected_at > timeout_s]
        for d in stale:
            actual_id = self._contacts[d].actual_ship_id
            del self._contacts[d]
            if actual_id in self._ship_to_designation:
                del self._ship_to_designation[actual_id]

    def reset(self):
        """Clear all contacts (mission restart)."""
        self._contacts.clear()
        self._ship_to_designation.clear()
        self._next_contact = 1
