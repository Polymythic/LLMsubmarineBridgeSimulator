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
- Mission selector with built-in Patrol and asset-driven missions. Asset missions live under `assets/missions/*.json` and include captain summary, ROE, target waypoints, and structured mission supplements. Prompts are mission-agnostic; missions provide data, not instructions.
- Debug helpers:
  - Mission dropdown fetches available missions from `/api/missions`; select and “Restart Mission” to apply changes in `.env` (via `MISSION_ID`).
  - Surface Training mission spawns a single convoy-like surface ship at ~6 km with a target waypoint for the Fleet Commander.

## AI Tooling
- Two-tier AI (Fleet Commander + Ship Commanders):
  - Fleet (slower cadence, stronger model) produces `FleetIntent` with per-ship objectives and optional `speed_kn` and `goal` text, plus an overall one-line `summary` and optional `notes`.
  - Ships (faster cadence, smaller models) issue constrained tool calls: `set_nav`, `fire_torpedo`, `deploy_countermeasure`. They generally follow `FleetIntent` but may deviate, marking summaries with `deviate:`.
- Each `Ship` now carries `ship_class` (e.g., `SSN`, `Convoy`, `Destroyer`) and `capabilities` (navigation, sensors, weapons, countermeasures).
- A debug `ai.tool` command applies minimal tool calls for LLM control:
  - `set_nav`: `{"ship_id":"red-01","tool":"set_nav","arguments":{"heading":120,"speed":6,"depth":0}}`
- Fleet Commander prompt (high-level): emphasizes formations/strategy (in `summary`), per-ship objectives, EMCON settings, fused contact picture (from bearings), and concise, actionable `notes`. Strict JSON-only.
- Ship Commander outputs must include a short human-readable `summary` explaining intent; the `/fleet` log displays it.
- When the orchestrator is enabled, stub-generated ship actions are disabled — only LLM-produced ship actions are applied.
- The `/fleet` UI shows engines/models and enhanced AI run metadata: provider/model, OK/FAIL, decision source (llm/intent_fallback/disabled_stub/none), and autoSummary flag.

### Two-Tier AI Control (Fleet Commander + Ship Commanders)
- Design: One global Fleet Commander plans strategy; each hostile ship executes local orders via tool calls.
- Cadence (configurable in `.env`):
  - Fleet Commander: ~20–30 s
  - Ship Commander: 5–15 s (shorter when alert)
- Information boundaries: Fleet sees only mission supplement + enemy belief (no truth); Ships see only local state and FleetIntent subset.
- Engines: Pluggable AI engines (stub, local `ollama`, remote OpenAI). Calls are async so the 20 Hz loop remains authoritative.
- Debug/Control: `/fleet` shows engines/models, `FleetIntent`, and recent runs.

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
  - Agent cadences: `AI_FLEET_CADENCE_S`, `AI_SHIP_CADENCE_S`, `AI_SHIP_ALERT_CADENCE_S`.
  - Missions: `MISSION_ID` selects `assets/missions/<id>.json`.

### Fleet AI UI and health checks
- Open `/fleet` to view current FleetIntent, recent runs, engines/models, and a live call log.
- A health-check is issued via `GET /api/ai/health` shortly after page load to verify engine connectivity.
- Press “Restart Mission” in `/debug` after changing `.env`. The server reloads `.env` and re-initializes the orchestrator without full server restart.
