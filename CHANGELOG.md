## Changelog

### 0.1.1 (Latest)
- **Ship-Specific Behavior Instructions**: Added `ship_behaviors` field to mission JSON files to specify custom behavior instructions for each RED ship. This allows destroyers to behave aggressively when detecting submarines, convoy ships to prioritize evasion, etc.
- **Mission Updates**: Updated all missions (`interdict_dual_convoys`, `evade_destroyers`, `weapons_validation_destroyers`, `surface_training`) with appropriate ship-specific behaviors.
- **AI Enhancement**: Modified AI orchestrator to incorporate ship-specific behavior instructions into prompts, improving realism and mission-specific behavior.

### 0.1.0 (MVP)
- FastAPI backend with 20 Hz simulation loop
- Ownship kinematics, passive/active sonar, basic weapons tubes + torpedo
- WebSocket endpoints per station; minimal dark-themed station UIs
- SQLite snapshots; local AI stub with tool-calls logged
- Docs: README, PROJECT_CONTEXT, PROJECT_GUIDELINES
