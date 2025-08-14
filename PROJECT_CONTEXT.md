## Project Context and Objectives

### Current Implementation Status
- Backend server: FastAPI with 20 Hz loop; WebSockets `/ws/{station}` live.
- Ownship kinematics: accel/turn/depth clamp; cavitation flag; reactor caps speed.
- Sonar: passive contacts; active ping with cooldown; passive waterfall graphic in Sonar UI.
- Weapons: tube state machine with timers (reload/flood/doors); fire torpedo with PN; proximity detonation applies damage/flooding; timers shown in UI.
- Engineering: reactor MW set, SCRAM, battery drain, pumps reduce flooding; telemetry shows reactor/battery/damage.
- Captain: consent toggle; periscope/radio raised flags.
- Static enemy supported for station testing (AI disabled by default).
- Persistence: SQLite run/snapshot/event logging.
- UIs: minimal dark HTML for all stations; incremental graphics (waterfall, timers).

### MVP Objectives
- **Fixed timestep server loop**: 20 Hz (50 ms).
- **Ownship kinematics**: heading, speed, depth; with cavitation/noise modeling.
- **Sonar**: passive bearing-only contacts with noise; active ping with cooldown and reveal effects.
- **Weapons**: tubes with states (Empty→Loaded→Flooded→DoorsOpen→Fired); fire a basic homing torpedo (PN guidance).
- **Damage/Engineering**: pumps, ballast, reactor output caps speed; SCRAM reduces power and drains battery.
- **Five UIs**: captain, helm, sonar, weapons, engineering; modern dark naval theme.
- **Local-only agentic enemies**: LLM tool-calling; server clamps orders to platform limits; rule-based fallback.
- **Single-host serving**: `http://192.168.1.100:8000/<station>` and `localhost`.
- **State management**: in-memory authoritative state, with periodic SQLite snapshots (optional Redis pub/sub interface).
- **Compact models/messages**: small, well-documented payloads.

### Tech Stack
- **Backend**: Python 3.11+, FastAPI, uvicorn, pydantic, SQLModel (SQLite), orjson, starlette.websockets.
- **Real-time**: In-process pub/sub via async queues; optional Redis (later) via `redis.asyncio` behind a thin adapter.
- **Frontend**: Vite + TypeScript (vanilla or React) with TailwindCSS; lightweight 2D SVG/Canvas graphics; dark naval UI.
- **Process**: Single process, single port; no external services required.

### Folder Structure (planned)
- `sub-bridge/backend/`
  - `app.py`
  - `sim/` → `ecs.py`, `physics.py`, `sonar.py`, `weapons.py`, `damage.py`, `ai_tools.py`, `loop.py`
  - `models.py`, `storage.py`, `bus.py`, `config.py`, `assets/`
- `sub-bridge/frontend/` → `common/`, station apps, `vite.config.ts`, `index.html`
- `static/` → built frontend assets served by FastAPI
- `.env.example`, `README.md`

### Networking & Routes
- **HTTP**: `GET /captain`, `/helm`, `/sonar`, `/weapons`, `/engineering` serve the SPA for each station; `/` is home.
- **WebSocket**: `/ws/<station>` for commands and telemetry.
  - Client→Server topics (examples):
    - `helm.order`, `sonar.ping`, `weapons.tube.load`, `weapons.tube.flood`, `weapons.tube.doors`, `weapons.fire`, `engineering.reactor.set`, `engineering.pump.toggle`, `engineering.power.allocate`, `captain.periscope.raise`, `captain.radio.raise`, `debug.restart`.
  - Server→Client: station-filtered telemetry at 20 Hz + discrete events.

### Authoritative Sim Loop (20 Hz)
- `tick(dt=0.05)` integrates:
  - **Kinematics**: clamp accel/decel (knots/s), turn rate (deg/s), depth rate (m/s). Cavitation if `speed > cavitationSpeed(depth)`.
  - **Sonar**:
    - Passive: bearing-only; Gaussian noise with σ increasing at quiet speeds; confidence rises with SNR; baffles sector.
    - Active: ping has cooldown; returns noisy range+bearing; reveals ownship to others.
  - **Weapons**: torpedo entities with PN guidance, seeker cone, enable range; tube state machine and timing.
  - **Damage/Power**: flooding and pumps; reactor output caps shaft power/speed; SCRAM reduces to battery/aux.
- Server is authoritative; clients are displays/controllers.

### Data Model (pydantic / SQLModel)
- Entities (core): `Kinematics`, `Hull`, `Acoustics`, `WeaponsSuite`, `Tube`, `TorpedoDef`, `Reactor`, `DamageState`, `Ship`, optional `AIProfile`.
- Persistence: tables for `runs`, `snapshots`, `events`, and optionally `ships`, `torpedoes`.

### Enemy Agent (LLM Tool-Calling)
- Runs locally (Ollama/llama.cpp/text-gen-server or stub).
- Calls are asynchronous and never block the 20 Hz loop. Timeouts/failures fall back to a rule-based stub.
- Tool schema:
  - `set_nav(heading: 0–359.9, speed: knots ≥0, depth: m ≥0)`
  - `fire_torpedo(tube: int, bearing: 0–359.9, run_depth: m)`
  - `deploy_countermeasure(type: "noisemaker"|"decoy")`
- Server validates and clamps results to Hull/Weapons limits, ROE, and EMCON posture, then enqueues for the next tick.

### Two-Tier Enemy AI: Fleet Commander + Ship Commanders

#### Roles and Cadence
- **Fleet Commander** (global): plans fleet-level objectives and formations on a slow cadence (30–60 s). Writes a shared `FleetIntent` into sim state.
- **Ship Commanders** (per hostile ship): execute local actions via tool calls on a normal cadence (20 s) or an alert cadence (10 s) if detected/threatened (e.g., actively pinged, torpedo inbound, or confirmed counter-detection).

#### Strict Information Boundaries
- No AI agent may access hidden truth about the player submarine or any entity beyond what sensors and communications would provide.
- Fleet Commander receives:
  - Full state of friendly fleet (types, kinematics, health, weapons readiness, detectability flags).
  - Mission/ROE and global context (time/weather) as appropriate.
  - Aggregated enemy belief: contacts/classifications with uncertainty derived from sensor reports; never ground-truth positions.
- Ship Commander receives:
  - Its own kinematics, constraints, health, weapons readiness, maintenance state relevant to availability.
  - Local contacts view (bearing-only, noisy; active ping returns as applicable) and local events (e.g., torpedo inbound).
  - Last applied orders and the current `FleetIntent` subset relevant to its group/role.

#### FleetIntent (shared strategy scaffold)
- Example fields: `objectives`, `waypoints`/formations per group, `target_priority`, `engagement_rules` (weapons_free, min_confidence), `emcon` posture (active ping allowed, radio discipline), and optional convoy lanes/patrol boxes.
- Ships are expected to bias decisions toward achieving `FleetIntent` while respecting local constraints and sensor reality.

#### Engine Abstraction and Safety
- Pluggable engine per agent: stub, local LLM (Ollama), remote LLM (OpenAI). Configurable via `config.py` and Debug UI.
- All agent outputs are parsed, validated, and clamped. Invalid outputs are rejected and replaced with a conservative rule-based fallback.
- Decisions, summaries (hashed), and applied orders are logged to persistence for observability and replay.

#### LLM State Summary (example JSON)
```json
{
  "time": "2025-08-12T14:22:05Z",
  "self": {"id":"kilo-01","pos":[1234,-567],"depth":120,"heading":270,"speed":8},
  "contacts": [{"bearing":95,"range_est":3000,"class":"Unknown-SSN","confidence":0.42}],
  "constraints": {"maxSpeed":18,"maxDepth":300,"turnRate":7},
  "orders_last": {"heading":255,"speed":10,"depth":150},
  "damage": {"hull":0.0,"sensors":0.0,"propulsion":0.0}
}
```

#### LLM Tool Call (example)
```json
{"tool":"set_nav","arguments":{"heading":255,"speed":10,"depth":150}}
```

### Physics Defaults
- `turn_rate_max`: 7 °/s
- `accel_max/decel_max`: 0.5/0.7 knots/s
- `depth_rate`: 3 m/s (ballast up to 6 m/s)
- `max_speed`: 30 kn (ownship), quiet ≤ 5 kn
- Cavitation onset from `min_required_depth(speed)` curve; warn Helm UI
- Thermocline attenuation multiplier: 0.6 across layer
- Active ping cooldown: 12 s per array

### Sonar Model (Lightweight)
- Passive: bearing-only; bearing noise ~ N(0, σ) with σ higher at quiet; contact confidence rises as SNR accumulates; baffles wedge behind ownship.
- Active: ping event returns noisy range+bearing; triggers counter-detected events by enemies.

### Weapons Model
- Tubes: 6; states and timing → reload 45 s; flood 8 s; doors 3 s.
- Torpedo: PN guidance; seeker cone 35°; enable at `enable_range_m`; re-acquire within cone; noisemakers spoof probability by SNR/geometry.

### Damage / Engineering
- Compartment flooding; pumps reduce flooding rate; ballast affects depth rate; reactor output caps achievable speed; SCRAM drops shaft power and increases battery drain.
- Shared power budget (Engineering allocates reactor output across Helm/Propulsion, Sonar/Sensors, Weapons, Engineering): allocations affect caps and timers.
- Maintenance and system reliability model:
  - Per-system maintenance levels `0.0..1.0` for `rudder`, `ballast`, `sonar`, `radio`, `periscope`, `tubes`.
  - Engineering power accelerates maintenance recovery; neglect causes decay. Below thresholds, systems can fail/degrade:
    - Rudder fail → no turning authority.
    - Ballast fail → severely limited depth rate.
    - Sonar fail → passive/active degraded or disabled.
    - Radio fail / Periscope fail → respective captain station controls disabled.
    - Tubes fail → load/flood/doors inhibited.
  - Telemetry surfaces `power`, `systems`, and `maintenance` to Engineering; other stations experience the effects.

### Messages (WebSocket JSON)
- Telemetry (server→client example):
```json
{
  "topic":"telemetry",
  "data":{
    "ownship":{"heading":266.2,"orderedHeading":270,"speed":11.2,"depth":98,"cavitation":false},
    "contacts":[{"id":"C1","bearing":143,"strength":0.62,"classifiedAs":"SSN?","confidence":0.41}],
    "events":[],
    "pingResponses":[{"id":"red-01","bearing":145.2,"range_est":3200.0,"strength":0.42,"at":"2025-08-12T14:22:05Z"}],
    "lastPingAt":"2025-08-12T14:22:05Z"
  }
}
```
- Commands (client→server examples):
```json
{"topic":"helm.order","data":{"heading":270,"speed":12,"depth":100}}
{"topic":"sonar.ping","data":{"array":"bow"}}
{"topic":"weapons.tube.load","data":{"tube":1,"weapon":"Mk48"}}
{"topic":"weapons.tube.flood","data":{"tube":1}}
{"topic":"weapons.tube.doors","data":{"tube":1,"open":true}}
{"topic":"weapons.fire","data":{"tube":1,"bearing":145,"run_depth":120}}
{"topic":"engineering.reactor.set","data":{"mw":65}}
{"topic":"engineering.pump.toggle","data":{"pump":"fwd","enabled":true}}
{"topic":"engineering.reactor.scram","data":{"scrammed":true}}
{"topic":"captain.periscope.raise","data":{"raised":true}}
{"topic":"captain.radio.raise","data":{"raised":true}}
```

### UI Requirements (Dark, Modern, Naval)
- Global: dark slate palette (#0B1020), mono headings, soft glows, red/amber status pips.
- Captain: periscope viewport (2D horizon/reticle/bearing ring), radio mast toggle (EMCON risk), contact list with ROE/consent prompt.
- Helm: heading bug dial, depth ladder, speed telegraph, cavitation & thermocline indicators, ordered vs actual.
- Sonar: passive waterfall strip (canvas), active ping button + cooldown ring, contact bearing dial, mark/classify controls.
- Weapons: tube panel with state lights (E/L/F/DO), load queue, solution form (bearing/run depth/enable), fire interlocks requiring Captain consent.
- Engineering: reactor slider, battery gauge, pumps/valves toggles, ballast controls, damage widgets, noise budget meter.

### Persistence
- SQLite tables: runs, snapshots, events, ships, torpedoes.
- Snapshot cadence: every 2 s (configurable).
- SQLModel for schema; Redis pub/sub optional (fallback).

### Config (.env)
- `HOST=0.0.0.0`
- `PORT=8000`
- `TICK_HZ=20`
- `USE_REDIS=false`
- `REDIS_URL=redis://localhost:6379`
- `AI_POLL_S=2.0`
- `SNAPSHOT_S=2.0`
- `REQUIRE_CAPTAIN_CONSENT=true`
- `SQLITE_PATH=./sub-bridge.db`
- `LOG_LEVEL=INFO`
- `USE_ENEMY_AI=false`
- `ENEMY_STATIC=true`

### Testing & Acceptance
- Start server: `uvicorn sub-bridge.backend.app:app --reload --host 0.0.0.0 --port 8000`
- Open five tabs: `/captain`, `/helm`, `/sonar`, `/weapons`, `/engineering`.
- Helm orders change ownship within limits; Sonar shows contacts waterfall; Weapons can load/flood/open/fire one torpedo with timers; Engineering can SCRAM/adjust power/pumps; Captain consent required before firing (toggleable).
- Static enemy mode for station testing; enemy AI can be enabled later; tool calls logged when enabled.
