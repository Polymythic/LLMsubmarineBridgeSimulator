"""
Simple intercept calculation utilities for destroyer AI
"""
import math
from typing import Tuple, Optional
from ..models import Ship, ContactTrack


def calculate_intercept_course(
    hunter: Ship, 
    target: Ship, 
    hunter_speed_kts: float
) -> Tuple[float, float]:
    """
    Calculate intercept course and time for a hunter to reach a target.
    
    Args:
        hunter: The ship doing the hunting
        target: The target ship
        hunter_speed_kts: Speed of hunter in knots
        
    Returns:
        Tuple of (intercept_heading_deg, intercept_time_seconds)
    """
    # Convert knots to m/s (1 knot = 0.514 m/s)
    hunter_speed_ms = hunter_speed_kts * 0.514
    target_speed_ms = target.kin.speed * 0.514
    
    # Current positions
    hx, hy = hunter.kin.x, hunter.kin.y
    tx, ty = target.kin.x, target.kin.y
    
    # Target velocity vector
    target_heading_rad = math.radians(target.kin.heading)
    tvx = target_speed_ms * math.sin(target_heading_rad)
    tvy = target_speed_ms * math.cos(target_heading_rad)
    
    # Relative position
    dx = tx - hx
    dy = ty - hy
    
    # Solve intercept equation: |hunter_pos + hunter_vel * t| = |target_pos + target_vel * t|
    # This is a quadratic equation: at² + bt + c = 0
    a = hunter_speed_ms**2 - target_speed_ms**2
    b = 2 * (dx * tvx + dy * tvy)
    c = dx**2 + dy**2
    
    # Find positive time solution
    if abs(a) < 1e-6:  # Linear case
        if abs(b) > 1e-6:
            t = -c / b
        else:
            t = 0
    else:
        discriminant = b**2 - 4 * a * c
        if discriminant < 0:
            # No real solution, just head directly toward target
            t = math.sqrt(dx**2 + dy**2) / hunter_speed_ms
        else:
            t1 = (-b + math.sqrt(discriminant)) / (2 * a)
            t2 = (-b - math.sqrt(discriminant)) / (2 * a)
            t = min(t1, t2) if t1 > 0 and t2 > 0 else max(t1, t2)
    
    # Calculate intercept position
    intercept_x = tx + tvx * t
    intercept_y = ty + tvy * t
    
    # Calculate heading to intercept point
    dx_intercept = intercept_x - hx
    dy_intercept = intercept_y - hy
    
    if abs(dx_intercept) < 1e-6 and abs(dy_intercept) < 1e-6:
        # Already at intercept point
        intercept_heading = hunter.kin.heading
    else:
        intercept_heading = math.degrees(math.atan2(dx_intercept, dy_intercept))
        intercept_heading = (intercept_heading + 360) % 360
    
    return intercept_heading, max(0, t)


def update_contact_track(
    hunter: Ship, 
    contact_id: str, 
    contact_x: float, 
    contact_y: float, 
    contact_depth: float,
    contact_heading: float,
    contact_speed: float,
    current_time: float
) -> None:
    """
    Update or create a contact track for intercept calculations.
    """
    # Find existing track
    existing_track = None
    for track in hunter.contact_tracks:
        if track.contact_id == contact_id:
            existing_track = track
            break
    
    if existing_track:
        # Update existing track
        existing_track.last_known_x = contact_x
        existing_track.last_known_y = contact_y
        existing_track.last_known_depth = contact_depth
        existing_track.last_known_heading = contact_heading
        existing_track.last_known_speed = contact_speed
        existing_track.last_seen_time = current_time
        # Increase confidence with each update
        existing_track.track_confidence = min(1.0, existing_track.track_confidence + 0.1)
    else:
        # Create new track
        new_track = ContactTrack(
            contact_id=contact_id,
            last_known_x=contact_x,
            last_known_y=contact_y,
            last_known_depth=contact_depth,
            last_known_heading=contact_heading,
            last_known_speed=contact_speed,
            last_seen_time=current_time,
            track_confidence=0.5
        )
        hunter.contact_tracks.append(new_track)


def get_best_contact_track(hunter: Ship, current_time: float) -> Optional[ContactTrack]:
    """
    Get the most recent and confident contact track for intercept.
    """
    if not hunter.contact_tracks:
        return None
    
    # Filter out old tracks (older than 60 seconds)
    recent_tracks = [
        track for track in hunter.contact_tracks 
        if current_time - track.last_seen_time < 60.0
    ]
    
    if not recent_tracks:
        return None
    
    # Return the track with highest confidence
    return max(recent_tracks, key=lambda t: t.track_confidence)


def calculate_distance_to_contact(hunter: Ship, contact: ContactTrack) -> float:
    """
    Calculate current distance to a contact based on its last known position and movement.
    """
    # Simple prediction based on last known position and velocity
    time_since_seen = 0  # Assume current time for simplicity
    predicted_x = contact.last_known_x + contact.last_known_speed * 0.514 * math.sin(math.radians(contact.last_known_heading)) * time_since_seen
    predicted_y = contact.last_known_y + contact.last_known_speed * 0.514 * math.cos(math.radians(contact.last_known_heading)) * time_since_seen
    
    dx = predicted_x - hunter.kin.x
    dy = predicted_y - hunter.kin.y
    
    return math.sqrt(dx**2 + dy**2)
