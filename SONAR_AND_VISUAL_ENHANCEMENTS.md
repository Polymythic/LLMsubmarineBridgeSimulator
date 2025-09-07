# Sonar and Visual System Enhancements

## Current Issues Identified

### 1. Sonar System Problems
- **Enemy Active Pings Not Visible**: Enemy ships are not frequently using active sonar, or their pings are not being properly displayed
- **Enemy Torpedoes Infrequent**: Enemy ships are not firing torpedoes often enough during combat
- **Own Torpedoes Not Showing**: When the player fires torpedoes, they don't appear in the sonar contacts list

### 2. Visual Detection System Limitations
- **No Weather Effects**: Visual detection is not affected by weather conditions (fog, rain, storms)
- **No Day/Night Cycle**: Visual detection doesn't vary based on time of day
- **Static Detection Ranges**: Detection probabilities are fixed regardless of environmental conditions

## Proposed Enhancements

### Weather System
- **Weather States**: Clear, Fog, Rain, Storm, Heavy Storm
- **Visual Detection Modifiers**:
  - Clear: 100% detection range
  - Fog: 50% detection range, -20% detection probability
  - Rain: 75% detection range, -10% detection probability
  - Storm: 40% detection range, -30% detection probability
  - Heavy Storm: 25% detection range, -50% detection probability

### Day/Night Cycle
- **Time States**: Dawn, Day, Dusk, Night
- **Visual Detection Modifiers**:
  - Dawn/Dusk: 80% detection range, -15% detection probability
  - Day: 100% detection range and probability
  - Night: 30% detection range, -40% detection probability

### Sonar System Fixes
- **Torpedo Detection**: Ensure all torpedoes (friendly and enemy) appear in sonar contacts when fired
- **Enemy Active Sonar**: Increase frequency of enemy active sonar usage during combat
- **Contact Classification**: Improve torpedo contact classification and display

### Implementation Priority
1. **High Priority**: Fix torpedo visibility in sonar
2. **High Priority**: Increase enemy active sonar frequency
3. **Medium Priority**: Implement weather system
4. **Medium Priority**: Implement day/night cycle
5. **Low Priority**: Advanced weather effects (wind, sea state)

## Technical Implementation Notes

### Weather System
- Add `WeatherState` model with current weather and transition probabilities
- Modify visual detection calculations in `loop.py`
- Add weather display to debug interface
- Consider weather effects on sonar performance (surface noise)

### Day/Night Cycle
- Add `TimeOfDay` tracking with realistic day/night transitions
- Modify visual detection ranges and probabilities
- Add time display to captain's station
- Consider artificial lighting effects for night operations

### Sonar Enhancements
- Ensure torpedo contacts are properly created in `passive_projectiles()`
- Increase AI active sonar cooldown frequency during combat
- Add torpedo-specific contact classification
- Improve torpedo tracking and display

## Testing Requirements
- Verify torpedoes appear in sonar when fired
- Test enemy active sonar frequency during combat scenarios
- Validate weather effects on visual detection
- Confirm day/night cycle affects detection ranges
- Test combined weather and time-of-day effects
