# Submarine Bridge Simulator (MVP)

A cooperative, local-multiplayer submarine bridge simulator. Five players occupy classic stations on a nuclear attack submarine and must coordinate under time pressure, conflicting objectives, and imperfect information. The backend runs an authoritative 20 Hz sim loop; the frontends are lightweight station UIs served from one host.

## What this is
- A real-time sim of ownship kinematics, acoustics, weapons, and damage/engineering.
- A five-station experience designed to create tension and immersion via power/noise budgets, maintenance/failure mechanics, and EMCON tradeoffs.
- An offline-friendly architecture with optional AI “enemy” control in the future.

## Roles and Stations

### Captain
- Grants firing consent (time-limited window), raises/lowers periscope and radio masts (EMCON risk).
- Receives the mission brief and Rules of Engagement (ROE).
- Monitors EMCON state: sees noise budget and detectability.

### Helm
- Steers and drives the ship: heading, speed, depth.
- Must respect cavitation and thermocline to manage acoustic signature.
- Responds to power caps; speed may be limited by Engineering’s allocations.

### Sonar
- Tracks passive contacts (bearing-only) with noisy bearings and confidence accumulation.
- Triggers active pings (cooldown). Active returns appear as bright dots on the DEMON waterfall and in a Ping Responses list (bearing, estimated range, strength, timestamp). Pings increase counter-detection risk.
- Manages classification/marking flow to support Weapons’ firing solution quality (future).

### Weapons
- Manages tubes: load → flood → doors → fire. Tube timers and interlocks apply.
- Requires Captain consent to fire (if enabled). Power allocation affects tube timing.
- Receives solution inputs (bearing/run depth) and monitors timers/door states.

### Engineering
- Allocates reactor power budget across Helm (propulsion), Sonar (sensors), Weapons, and Engineering (maintenance).
- Manages reactor output, SCRAM, pumps; tracks battery, flooding, and hull damage.
- Oversees maintenance levels per system (rudder, ballast, sonar, radio, periscope, tubes). Neglect causes failures (e.g., lost rudder authority, limited ballast rate, sensor outages). Allocating power to Engineering recovers maintenance over time.
- Balances acoustic noise budget: pumps, cavitation, and raised masts increase detectability.

## Core Mechanics
- Authoritative 20 Hz sim loop integrates kinematics, sonar (passive/active), weapons, and damage/engineering.
- Acoustic noise budget affects detectability; EMCON risk is surfaced to Captain and Sonar.
- Shared power budget forces tradeoffs across propulsion, sensors, weapons, and maintenance.
- Maintenance/failure model: low maintenance degrades or disables systems; recovery requires sustained Engineering allocation.
- Active sonar pings: cooldown-limited, with responses rendered and listed; trigger counter-detection events.

### Bearings
- The sim uses compass bearings: 0°=North, 90°=East, 180°=South, 270°=West. If ownship is at (0,0) heading 000 and another ship is at (x>0, y=0), its true bearing is ~090.

## Debug and Missions
- Debug view provides a live truth map of entities.
- Restart Mission button resets world state.
- Mission selector (scaffold) for presets with initial ship types/positions, captain brief text, and timed radio traffic (future expansion).
- Debug helpers:
  - Button “Mission 1” sets a simple surface contact.
  - Button “Surface Vessel Mission” resets and spawns a single convoy-like surface ship at ~6 km for torpedo testing.

## AI Tooling
- Each `Ship` now carries `ship_class` (e.g., `SSN`, `Convoy`, `Destroyer`) and `capabilities` (navigation, sensors, weapons, countermeasures).
- A debug `ai.tool` command applies minimal tool calls for LLM control:
  - `set_nav`: `{"ship_id":"red-01","tool":"set_nav","arguments":{"heading":120,"speed":6,"depth":0}}`
  - `fire_torpedo` and `deploy_countermeasure`: placeholders gated by capabilities.

### Two-Tier AI Control (Fleet Commander + Ship Commanders)
- **Design**: One global Fleet Commander plans strategy; each hostile ship has its own Ship Commander that executes local orders via tool calls.
- **Cadence**:
  - Fleet Commander: slow planning every 30–60 s.
  - Ship Commander: every 20 s normally; every 10 s if detected/threatened.
- **Strict Information Boundaries** (never leak hidden truth about the player sub):
  - Fleet Commander sees: full state of its own fleet; mission/ROE; global game context that would be known (e.g., time/weather). It does not see the authoritative truth of enemy positions—only the aggregated enemy belief (contacts/classifications with uncertainty) derived from sensors and shared reports.
  - Ship Commander sees: its own kinematics/health/weapons readiness, its local contacts (bearing-only, noisy), last orders, and the published `FleetIntent`. It never sees the debug truth map or hidden enemy data.
- **Outputs**:
  - Fleet Commander writes a shared `FleetIntent` (objectives, waypoints/formations, target priorities, engagement and EMCON posture).
  - Ship Commanders issue constrained tool calls: `set_nav`, `fire_torpedo`, `deploy_countermeasure`. Server validates/clamps to platform limits and ROE before applying.
- **Engines**: Pluggable AI engines (stub, local `ollama`, remote OpenAI). Calls run asynchronously so the 20 Hz loop remains authoritative and jitter-free.
- **Debug/Control**: Debug panel can enable/disable fleet and per-ship AI, choose engine/model, and view last decisions. Information shown in debug never expands what the AI actually receives.

See also: `docs/AI.md` for detailed schemas, prompts, scheduling, and agent-based handoff/tracing.


## Requirements
- Python 3.11+

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp example.env .env  # then edit .env as needed; do NOT commit your real .env
```

## Run
```bash
uvicorn sub-bridge.backend.app:app --reload --host 0.0.0.0 --port 8000
```

Open:
- http://localhost:8000/
- http://localhost:8000/captain
- http://localhost:8000/helm
- http://localhost:8000/sonar
- http://localhost:8000/weapons
- http://localhost:8000/engineering
 - http://localhost:8000/fleet

LAN access: http://192.168.1.100:8000/ (adjust to your host IP)

### Environment configuration
- Copy `example.env` to `.env` and edit values locally. Never commit `.env`.
- Relevant keys:
  - `USE_AI_ORCHESTRATOR` to enable Fleet/Ship agents orchestration.
  - `AI_FLEET_ENGINE` / `AI_SHIP_ENGINE` = `stub` | `ollama` | `openai` with corresponding `*_MODEL`.
  - `OPENAI_API_KEY` (only in your local `.env`) if using OpenAI; `OLLAMA_HOST` for local LLMs.
  - Legacy path: `USE_ENEMY_AI` for simple periodic stub behavior.

### Fleet AI UI and health checks
- Open `/fleet` to view:
  - Current FleetIntent (if any), recent agent runs/tool calls, engine/model banner, and a live call log.
  - A health-check is issued via `GET /api/ai/health` shortly after page load to verify engine connectivity.
- Press “Restart Mission” in `/debug` after changing `.env`. The server reloads `.env` and re-initializes the orchestrator without full server restart.
- Default Debug mission selection is set to “Surface Vessel (Training)” for quick testing.
