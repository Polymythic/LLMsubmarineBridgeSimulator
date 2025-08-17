## Two-Tier Enemy AI Design

This document defines the Fleet Commander + Ship Commander AI architecture, strict information boundaries, data schemas, scheduling, engines, validation, and example prompts.

### Overview
- One global enemy (RED) Fleet Commander plans fleet strategy on a slow cadence and publishes a `FleetIntent`.
- Each enemy (RED) ship runs a Ship Commander that makes local decisions via tool calls at a faster cadence.
- All AI calls are asynchronous; the 20 Hz authoritative sim loop is never blocked.
- Strict information boundaries ensure agents only see what they “know” via sensors and comms—never debug truth.

### Roles and Cadence
- RED Fleet Commander
  - Cadence: every 20–30 s (configurable)
  - Input: own fleet full state, mission supplement (structured), aggregated RED belief of BLUE information (never ground truth)
  - Output: `FleetIntent`
- RED Ship Commander (per hostile ship)
  - Cadence: 5–15 s (configurable; alert cadence may be shorter)
  - Input: local ship summary + relevant slice of `FleetIntent`
  - Output: constrained tool calls: `set_nav`, `fire_torpedo`, `deploy_countermeasure`

### Strict Information Boundaries
- Never expose authoritative enemy truth to any AI agent.
- Fleet Commander receives:
  - Full state of RED fleet (ids, classes, kinematics, health, weapons readiness, detectability flags)
  - Mission supplement (structured only) + global context (time/weather)
  - Aggregated enemy fleet belief: contacts with uncertainty built from sensor reports
- Ship Commander receives:
  - Its own kinematics, constraints, health, maintenance aspects impacting availability, weapons readiness
  - Local contacts (bearing-only; noisy), active ping returns, local threat flags (e.g., torpedo inbound)
  - Last applied orders and relevant `FleetIntent` subset

### Data Schemas

#### FleetIntent (canonical schema)
```json
{
  "objectives": {
    "<ship_id>": {
      "destination": [x, y],
      "speed_kn": 12,
      "goal": "one sentence"
    }
  },
  "emcon": {
    "active_ping_allowed": false,
    "radio_discipline": "restricted"
  },
  "summary": "One short sentence describing the fleet plan",
  "notes": [
    {"ship_id": "<id>", "text": "<advisory>"}
  ]
}
```
Notes:
- `speed_kn` and `goal` are optional. Ship Commanders have latitude to adjust actual speed tactically.
- If an objective `destination` is omitted, the orchestrator may derive defaults from mission `target_wp`.

#### Mission supplement (structured fields)

Missions provide structured, side-specific context that supplements the core prompts. Typical fields:
```json
{
  "mission_summary": "One or two sentences summarizing the mission's high-level goal (passed to Fleet Commander)",
  "target_wp": [x, y],
  "side_objectives": {"RED": "escort_convoy_to_wp", "BLUE": "interdict_convoy"},
  "protected_assets": ["red-01", "red-02"],
  "emcon": {"RED": {"active_ping_allowed": false, "radio_discipline": "restricted"}},
  "formations": {"convoy": {"ships": ["red-01","red-02"], "formation": "column", "spacing_m": 300}},
  "speed_limits": {"convoy": {"min_kn": 4, "max_kn": 8}},
  "navigation_constraints": {"no_go_zones": [], "transit_lanes": []},
  "threat_hints": [{"type": "suspected_submarine", "center": [2500, -500], "radius_m": 1500, "confidence": 0.3}],
  "success_criteria": {"RED": {"reach_wp_within_m": 200, "min_survivors": 2, "timeout_s": 900}}
}
```
- `mission_summary` is passed as data to the Fleet Commander (not imperative text) so it understands the high-level intent.

#### Fleet Summary (for Fleet Commander)
```json
{
  "time": "2025-08-12T14:22:05Z",
  "own_fleet": [
    {
      "id": "red-01",
      "class": "Destroyer",
      "pos": [1000, 200],
      "depth": 0,
      "heading": 140,
      "speed": 10,
      "health": {"hull": 0.0},
      "weapons": {"tubes_ready": 1, "ammo": {"torpedo": 5}},
      "detectability": {"noise": 0.42, "emcon_risk": 0.2}
    }
  ],
  "enemy_belief": [
    {
      "id": "C1",
      "bearing": 95.0,
      "range_est": 3200,
      "class": "Unknown",
      "confidence": 0.35,
      "last_seen": "2025-08-12T14:22:05Z"
    }
  ],
  "mission": {"mission_summary": "...", "target_wp": [0,0], "side_objectives": {"RED": "..."}},
  "fleet_intent_last": {"hash": "abc123"}
}
```

#### Ship Summary (for Ship Commander)
```json
{
  "self": {
    "id": "red-01",
    "class": "Destroyer",
    "pos": [1000, 200],
    "depth": 0,
    "heading": 140,
    "speed": 10
  },
  "constraints": {"maxSpeed": 18, "maxDepth": 0, "turnRate": 7},
  "weapons": {
    "tubes": [{"idx": 1, "state": "DoorsOpen"}],
    "has_countermeasures": true
  },
  "contacts": [
    {"bearing": 95, "range_est": 3200, "class": "Unknown", "confidence": 0.35}
  ],
  "orders_last": {"heading": 135, "speed": 9, "depth": 0},
  "fleet_intent": {"objectives": {}, "summary": "..."},
  "detected_state": {"alert": false}
}
```

### Tool Call Schema (outputs from Ship Commander)
```json
{"tool":"set_nav","arguments":{"heading":255,"speed":10,"depth":150}}
```
```json
{"tool":"fire_torpedo","arguments":{"tube":1,"bearing":145,"run_depth":120,"enable_range":2000}}
```
```json
{"tool":"deploy_countermeasure","arguments":{"type":"noisemaker"}}
```

### Engine Abstraction
- Engines: `StubEngine` (deterministic/local), `OllamaEngine` (local HTTP), `OpenAIEngine` (remote HTTP)
- Interface:
  - `propose_fleet_intent(fleet_summary) -> FleetIntent`
  - `propose_orders(ship_summary, fleet_intent) -> ToolCall`
- Selection is configurable per agent via debug UI or config.

### Scheduling
- Each agent has a next-run timestamp (configurable cadences via `.env`).
- On each sim tick, agents that are due run in background tasks:
  1) Build summary JSON within the agent’s information boundary
  2) Call engine with timeout
  3) Parse → validate → clamp → enqueue orders for next tick
- Detection-aware cadence: switch ship cadence to a shorter alert cadence if threatened/detected.

### Validation and Clamping
- `set_nav`: clamp heading 0–359.9, speed ≥0 and ≤ ship max, depth ≥0 and ≤ ship max depth.
  - For surface vessels, depth is clamped to near-surface.
- `fire_torpedo`: a tube must be in `DoorsOpen`; geometry must be plausible (enable range, seeker cone).
- `deploy_countermeasure`: must be supported; rate-limited by platform.
- If validation fails, reject and fall back to conservative rule-based behavior; log the decision and reason.
- Ship tool calls must include a concise human-readable `summary` explaining intent/rationale; the UI surfaces it in the Fleet AI log.
- Policy: when the orchestrator is enabled, stub-generated ship actions are disabled; only LLM-produced ship actions are applied.

### Debugging and Observability
- Debug panel shows: enable/disable per-ship AI and Fleet AI, engine/model selection, last decisions, errors, and next-run ETA.
- Persistence: log hashed summaries, tool calls, and applied/clamped orders for replay.
- Fleet UI (`/fleet`) shows per-run provider/model, OK/FAIL status, decision source (llm/intent_fallback/disabled_stub/none), and whether the summary was auto-filled.

### Configuration
- `.env` keys (selected):
  - `USE_AI_ORCHESTRATOR`
  - `AI_FLEET_ENGINE`/`AI_SHIP_ENGINE` = `stub` | `ollama` | `openai`
  - `AI_FLEET_MODEL`/`AI_SHIP_MODEL` = engine-specific identifiers
  - `AI_FLEET_CADENCE_S`, `AI_SHIP_CADENCE_S`, `AI_SHIP_ALERT_CADENCE_S`
  - `OLLAMA_HOST`, `OPENAI_API_KEY`

### Example Prompt Templates

#### Fleet Commander (system prompt)
```
You are the RED Fleet Commander in a naval wargame.
Your role is to produce a `FleetIntent` JSON that strictly follows the provided schema.
Do not output anything except valid JSON conforming to schema.
You control all RED ships: destroyers, escorts, supply ships, and submarines.
You must translate high-level mission objectives into concrete ship tasks, formations, and tactical guidance.

### Duties
1. **Formation & Strategy (Summary field)**
   - Always describe the fleet-wide strategy in tactical terms, not just the mission restated.
   - Organize ships into task groups (e.g., Convoy A, Convoy B, Sub screen) and describe their roles.
   - Explicitly list key ship positions or offsets (e.g., “dd-01 escorts supply-01 1 km ahead”).
   - Capture EMCON posture and baseline speeds.
   - Repeat strategy across turns unless you are adapting — do not thrash.

2. **Ship Objectives**
   - Every RED ship must appear under `objectives`.
   - Include `destination` [x,y] and a one-sentence `goal`.
   - Add `speed_kn` only if a clear recommendation exists.

3. **EMCON**
   - Always set `active_ping_allowed` and `radio_discipline`.
   - If conditions for escalation exist (e.g., when to allow active sonar), place them in `notes`.

4. **Contact Picture**
   - If bearings or detections exist, perform a rough TDC-like analysis.
   - Fuse multiple bearings into an approximate location, course, and speed of the suspected contact.
   - Include this as a note, e.g., “Bearings converge: possible sub at [x,y], heading ~200, ~12 knots.”

5. **Notes**
   - Use `notes` to give conditional rules, task-group coordination, or advisories.
   - Link escorts to their convoys, give subs patrol doctrine, or note engagement rules.
   - Keep concise and actionable.

6. **Constraints**
   - Do not invent enemy truth beyond provided beliefs.
   - Do not omit RED ships.
   - Do not output extra fields outside the schema.

### Schema (reminder)
{
 "objectives": { ship_id: { "destination": [x,y], "goal": "string", "speed_kn": optional number }},
 "emcon": { "active_ping_allowed": bool, "radio_discipline": "string" },
 "summary": "string",
 "notes": [ { "ship_id": optional, "text": "string" } ]
}
```

#### Fleet Commander (user message with variables)
```
FLEET_SUMMARY_JSON:
{{fleet_summary_json}}

CONSTRAINTS:
- Include EVERY RED ship id under 'objectives'.
- Each ship MUST include a one-sentence 'goal'.
- Include 'speed_kn' only if clearly recommended.
- If bearings exist, attempt to produce a fused contact estimate (location, course, speed).
- Encode strategy in the 'summary' so that strategy is persistent across turns.
- Use 'notes' for conditional rules, escort logic, patrol instructions, and advisories.
- Do not infer unknown enemy truth beyond provided beliefs.
- Output ONLY the JSON object conforming to the schema
```

#### Ship Commander (system prompt)
```
You command a single RED ship. Make tactical decisions using only your Ship Summary and the FleetIntent. Prefer following FleetIntent; if deviating, prefix the summary with 'deviate:'. Coordinates: X east (m), Y north (m). Bearings: 0°=North, 90°=East. Output EXACTLY one JSON object with keys {tool, arguments, summary}. No markdown or extra keys.
```

#### Ship Commander (user message with variables)
```
SHIP_SUMMARY_JSON:
{{ship_summary_json}}

FORMAT & BEHAVIOR:
- Prefer the FleetIntent; if deviating, prefix summary with 'deviate:'.
- Use only allowed tools supported by capabilities. If no change is needed, return set_nav with current values and a brief summary.
- The 'summary' MUST be one short, human-readable sentence explaining intent and rationale (e.g., 'Heading to 3000,2000 to investigate passive sonar contact').
```


