## Two-Tier Enemy AI Design

This document defines the Fleet Commander + Ship Commander AI architecture, strict information boundaries, data schemas, scheduling, engines, validation, and example prompts.

### Overview
- One global enemy (RED) Fleet Commander plans fleet strategy on a slow cadence and publishes a `FleetIntent`.
- Each enemy (RED) ship runs a Ship Commander that makes local decisions via tool calls at a faster cadence.
- All AI calls are asynchronous; the 20 Hz authoritative sim loop is never blocked.
- Strict information boundaries ensure agents only see what they “know” via sensors and comms—never debug truth.

### Roles and Cadence
- RED Fleet Commander
  - Cadence: every 30–60 s (configurable)
  - Input: own fleet full state, mission/ROE, aggregated RED belief of BLUE information (never ground truth)
  - Output: `FleetIntent`
- RED Ship Commander (per hostile ship)
  - Cadence: 20 s normal; 10 s alert if detected/threatened
  - Input: local ship summary + relevant slice of `FleetIntent`
  - Output: constrained tool calls: `set_nav`, `fire_torpedo`, `deploy_countermeasure`

### Strict Information Boundaries
- Never expose authoritative enemy truth to any AI agent.
- Fleet Commander receives:
  - Full state of RED fleet (ids, classes, kinematics, health, weapons readiness, detectability flags)
  - Mission/ROE + global context (time/weather)
  - Aggregated enemy fleet belief: contacts with uncertainty built from sensor reports
- Ship Commander receives:
  - Its own kinematics, constraints, health, maintenance aspects impacting availability, weapons readiness
  - Local contacts (bearing-only; noisy), active ping returns, local threat flags (e.g., torpedo inbound)
  - Last applied orders and relevant `FleetIntent` subset

### Data Schemas

### LLM call composition (what each call contains)

All AI calls use the same composition:

1) CORE system prompt (role, objectives, constraints) – fixed in the orchestrator/engines for consistency
2) Mission supplement – structured mission fields and optional short hints
3) Current game realities – the sanitized Fleet/Ship summary JSON within information boundaries

This ensures agents always understand their role and output contract, while the mission adds scenario-specific guidance, and summaries add live state.

#### FleetIntent (canonical schema)
```json
{
  "objectives": {
    "<ship_id>": {"destination": [x, y]}
  },
  "engagement_rules": {
    "weapons_free": false,
    "min_confidence": 0.6,
    "hold_fire_in_emcon": true
  },
  "emcon": {
    "active_ping_allowed": false,
    "radio_discipline": "restricted"
  },
  "summary": "One short sentence explaining the plan",
  "notes": [
    {"ship_id": "<id>", "text": "<advisory>"}
  ]
}
```

#### Mission supplement (structured fields)

Missions provide structured, side-specific context that supplements the core prompts for better decisions. Typical fields:

```json
{
  "target_wp": [x, y],
  "side_objectives": {"RED": "escort_convoy_to_wp", "BLUE": "interdict_convoy"},
  "protected_assets": ["red-01", "red-02"],
  "emcon": {"RED": {"active_ping_allowed": false, "radio_discipline": "restricted"}},
  "formations": {"convoy": {"ships": ["red-01","red-02"], "formation": "column", "spacing_m": 300}},
  "speed_limits": {"convoy": {"min_kn": 4, "max_kn": 8}},
  "navigation_constraints": {"no_go_zones": [], "transit_lanes": []},
  "threat_hints": [{"type": "suspected_submarine", "center": [2500, -500], "radius_m": 1500, "confidence": 0.3}],
  "success_criteria": {"RED": {"reach_wp_within_m": 200, "min_survivors": 2, "timeout_s": 900}},
  "ai_fleet_prompt": "Concise hint for fleet",
  "ai_ship_prompts": {"red-01": "Concise hint for ship red-01"}
}
```

Notes:
- The orchestrator passes these fields through in `fleet_summary.mission` and injects optional short hints as `_prompt_hint`.
- Engines for OpenAI and Ollama use the same strict output contracts and constraints to minimize drift.

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
  "mission": {"roe": {"weapons_free": false}},
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
  "fleet_intent": {"objective": "escort_convoy", "next_wp": [1200, -300]},
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
```json
{"tool":"drop_depth_charges","arguments":{"spread_meters":20,"minDepth":30,"maxDepth":80,"spreadSize":5}}
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
- Detection-aware cadence: switch ship cadence to `AI_SHIP_ALERT_CADENCE_S` if threatened/detected (actively pinged, torpedo inbound, or counter-detected), otherwise `AI_SHIP_CADENCE_S`.

### Validation and Clamping
- `set_nav`: clamp heading 0–359.9, speed ≥0 and ≤ ship max, depth ≥0 and ≤ ship max depth.
  - For surface vessels (e.g., Convoy), hull.max_depth is near-surface; depth is clamped accordingly.
- `fire_torpedo`: a tube must be in `DoorsOpen`; ROE must allow (i.e., `weapons_free=true`); geometry must be plausible (enable range, seeker cone).
- `deploy_countermeasure`: must be supported; rate-limited by platform.
- If validation fails, reject and fall back to conservative rule-based behavior; log the decision and reason.

### Debugging and Observability
- Debug panel shows: enable/disable per-ship AI and Fleet AI, engine/model selection, per-ship prompt, last decisions, errors, and next-run ETA.
- Persistence: log hashed summaries, tool calls, and applied/clamped orders for replay.

### Configuration
- `.env` keys (selected):
  - `USE_ENEMY_AI`, `ENEMY_STATIC`
  - `AI_POLL_S` (base poll; per-agent cadence overrides apply)
  - `TICK_HZ`
  - Engine config: model names and endpoints (to be extended)

### Agent engines and configuration
- Orchestrator toggle: `USE_AI_ORCHESTRATOR=true` enables Fleet/Ship agents.
- Engine selection per tier:
  - `AI_FLEET_ENGINE` / `AI_SHIP_ENGINE` = `stub` | `ollama` | `openai`
  - `AI_FLEET_MODEL` / `AI_SHIP_MODEL` = engine-specific identifiers
- Ollama-first (local, no cloud calls):
  - `AI_*_ENGINE=ollama`, `AI_*_MODEL=llama3.1:8b` (or small ships model like `llama3.2:3b`), `OLLAMA_HOST=http://localhost:11434`
  - Expect higher latency than cloud; runs are async and won’t block the 20 Hz loop.
- OpenAI Agents SDK (cloud):
  - `AI_*_ENGINE=openai`, `AI_*_MODEL=gpt-4o-mini` (example), `OPENAI_API_KEY=...`
  - Uses Agents, Tools, and Handoffs; our orchestrator still validates/clamps outputs.

### Fleet UI and health checks
- `/fleet` displays engines/models, `FleetIntent`, recent runs, ships, and a call log.
- Health check: `GET /api/ai/health` is called on load and logs connectivity for fleet/ship engines.
- After changing `.env`, use “Restart Mission” in `/debug` to hot-reload config and re-init the orchestrator.

### Example Prompt Templates

#### Fleet Commander (system prompt)
```
You are the RED Fleet Commander. Plan strategy to achieve mission objectives while minimizing detectability and obeying ROE. You will receive a structured fleet summary and a mission supplement. Never assume ground-truth enemy positions; use only provided beliefs and hints. Coordinate system: X east (m), Y north (m). Output ONLY one JSON object with fields: objectives (per-ship destinations), engagement_rules (weapons_free,min_confidence,hold_fire_in_emcon), emcon (active_ping_allowed,radio_discipline), summary (one sentence), notes (optional). No markdown, no extra prose.
```

#### Fleet Commander (user message with variables)
```
FLEET_SUMMARY_JSON:
{{fleet_summary_json}}

CONSTRAINTS:
- Include EVERY RED ship id under 'objectives' with a 'destination' [x,y] in meters.
- If a mission target waypoint is provided, use it unless another destination is clearly safer/better.
- Respect formations, spacing, speed limits, and navigation constraints (lanes, no-go zones) if provided.
- Prefer convoy protection unless ROE authorizes engagement.
- Do not reveal or rely on unknown enemy truth.
```

#### Ship Commander (system prompt)
```
You command a single RED ship. Make conservative, doctrine-aligned decisions using only your local Ship Summary and the FleetIntent. Prefer following FleetIntent; if you must deviate, prefix the summary with 'deviate:'. Coordinate system: X east (m), Y north (m). Bearings: 0°=North, 90°=East. Output EXACTLY one JSON object with keys {tool, arguments, summary}. No markdown or extra keys.
```

#### Ship Commander (user message with variables)
```
SHIP_SUMMARY_JSON:
{{ship_summary_json}}

CONSTRAINTS:
- Strongly prefer the FleetIntent; if deviating, prefix summary with 'deviate:'.
- Respect EMCON posture and ROE (if weapons_free=false, do not fire).
- Only fire_torpedo if has_torpedoes=true AND a tube state is 'DoorsOpen'; set bearing from contacts; choose a realistic enable_range.
- Use only allowed tools and only if supported by capabilities.
- If no change is needed, return set_nav holding current values with a brief summary (e.g., 'holding course per FleetIntent').
- Output ONLY one JSON with keys {tool, arguments, summary}. No markdown, no extra keys.
```

### Testing
- Unit: cadence switching, validation/clamping, engine timeout fallback
- Integration: FleetIntent influences ships; weapons actions respect ROE and consent; no sim-loop jitter with AI enabled (stub)

### Agent Framework Integration, Handoff, and Tracing

We support an agent-based orchestration where the Fleet Commander and each Ship Commander are distinct agents with restricted tool exposure. The orchestrator mediates all calls and handoffs to enforce information boundaries.

- Agent definitions
  - Fleet Commander Agent: tools → `get_fleet_summary`, `get_enemy_belief`, `set_fleet_intent`, `handoff_to_ship(ship_id, intent_or_order_json)`
  - Ship Commander Agent: tools → `get_ship_summary(ship_id)`, `get_local_contacts(ship_id)`, `set_nav`, `fire_torpedo`, `deploy_countermeasure`

- Handoff semantics
  - Fleet emits `handoff_to_ship` for a `ship_id` with intent/order JSON.
  - Orchestrator starts a Ship Commander run with the ship’s summary + the passed intent/order, then applies the ship’s resulting tool call after validation/clamping on the next tick.

- Engines
  - Ollama-first: implement `OllamaAgentsEngine` that uses prompt templates to elicit tool-call JSON (including `handoff_to_ship`).
  - OpenAI Agents: implement `OpenAIAgentsEngine` using OpenAI’s agent framework (Assistants/Responses). The same tool contracts apply; handoff is realized by intercepting `handoff_to_ship` and launching a new ship-agent run.

- Tracing and observability
  - Internal traces persisted to `events` (SQLite):
    - `run_id`, `parent_run_id` (linking fleet handoff to ship run)
    - `agent_type` (fleet|ship), `engine` (stub|ollama|openai), `model`
    - `summary_hash`, `summary_size`, `prompt_tokens` (if available)
    - `tool_call_raw`, `tool_call_validated`, `clamp_actions`
    - `applied` (bool), `error` (if any), `duration_ms`
  - Debug UI trace view: collapsible tree by `run_id` → shows inputs (hashed), outputs, validation/clamping, and applied orders.
  - When using OpenAI Agents, optionally emit provider-native traces if available; otherwise rely on internal traces.

- Latency considerations
  - Ollama-first keeps costs down; expect higher latency. Mitigations: small JSON summaries, diffed summaries, lower cadence, and event-driven triggers.
  - Ship runs are short and localized; Fleet runs are infrequent. All runs are async with strict timeouts and graceful fallbacks.

### Tool Contracts (Schemas and Access Control)

All tools return JSON objects with `{ "ok": true, "data": ... }` or `{ "ok": false, "error": "..." }`.

- Fleet-only tools
  - `get_fleet_summary()` → data: Fleet Summary JSON (see above). No args.
  - `get_enemy_belief()` → data: list of contact beliefs. No args.
  - `set_fleet_intent(intent)`
    - args: `intent` (object) matching FleetIntent schema; extraneous fields ignored.
    - result: `{hash: string}` of accepted intent.
  - `handoff_to_ship(ship_id, order)`
    - args: `ship_id` (string), `order` (object; arbitrary intent/order payload for the ship agent)
    - result: `{child_run_id: string}`; orchestrator schedules ship-agent run.

- Ship-only tools
  - `get_ship_summary(ship_id)` → data: Ship Summary JSON for that ship. args: `ship_id` (string)
  - `get_local_contacts(ship_id)` → data: contacts for that ship. args: `ship_id` (string)
  - `set_nav(ship_id, heading, speed, depth)`
    - args: `ship_id` (string), `heading` (0–359.9), `speed` (≥0), `depth` (≥0)
    - result: `{applied: boolean}`; server clamps to constraints/ROE.
  - `fire_torpedo(ship_id, tube, bearing, run_depth, enable_range)`
    - args: `ship_id` (string), `tube` (int), `bearing` (float), `run_depth` (float), `enable_range` (float)
  - `deploy_countermeasure(ship_id, type)`
    - args: `ship_id` (string), `type` ("noisemaker" | "decoy")

Access control is enforced by the orchestrator: Fleet agent cannot call ship-only tools except `handoff_to_ship`. Ship agents cannot call fleet-only tools.

### Orchestrator API and Flow

High-level orchestrator responsibilities:
- Bind agents to engines (Ollama/OpenAI) and expose only their allowed tools
- Build summaries within info boundaries
- Run agents asynchronously with timeouts and fallbacks
- Validate/clamp tool outputs and enqueue to the sim loop
- Emit structured traces for all runs

Pseudo API (Python):
```python
class AgentsOrchestrator:
    async def run_fleet(self) -> RunResult: ...
    async def run_ship(self, ship_id: str, injected_order: dict | None = None) -> RunResult: ...

class RunResult(TypedDict):
    run_id: str
    parent_run_id: str | None
    agent_type: Literal["fleet", "ship"]
    engine: Literal["stub", "ollama", "openai"]
    model: str
    duration_ms: int
    error: str | None
    tool_calls: list[dict]  # raw
    tool_calls_validated: list[dict]  # after validation/clamping
    applied: bool
```

Scheduling flow:
1) On tick, if `now >= next_fleet_run_at`, call `run_fleet()` in background.
2) On tick, for each ship due (cadence based on detection state), call `run_ship(ship_id)` in background.
3) If fleet emits `handoff_to_ship`, orchestrator immediately schedules `run_ship(ship_id, injected_order=...)` as a child run.
4) Tool calls are validated/clamped; accepted actions are queued for the next sim tick.

### Trace Event Schema

Events persisted to `events` table (shape illustrative):
```json
{
  "event": "ai.run",
  "time": "2025-08-12T14:22:05Z",
  "run_id": "r_123",
  "parent_run_id": null,
  "agent_type": "fleet",
  "engine": "ollama",
  "model": "llama3.1:8b",
  "summary_hash": "s_abc",
  "summary_size": 1342,
  "prompt_tokens": 820,
  "duration_ms": 1180,
  "error": null
}
```
```json
{
  "event": "ai.tool_call",
  "run_id": "r_123",
  "raw": {"tool": "set_nav", "arguments": {"heading": 255, "speed": 10, "depth": 150}},
  "validated": {"tool": "set_nav", "arguments": {"heading": 255, "speed": 10, "depth": 150}},
  "clamp_actions": [],
  "applied": true
}
```


