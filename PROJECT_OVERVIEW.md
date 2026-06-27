# Submarine Bridge Simulator — Project Overview

A naval wargame where a human submarine crew (BLUE) faces an enemy fleet
(RED) commanded by a layered AI system: a strategic fleet commander (cloud
LLM) directing per-ship captains (small local LLMs) executing tactical
doctrine through a typed action surface.

This document is the durable reference for the design intent and the
architecture we've built. It supersedes the original `REFACTOR_PLAN.md`,
which described the initial seven-phase refactor; the project has since
extended through Phases 6–8.

---

## 1. Design intent

**A capable fleet commander coordinates ship captains; the captains execute
ship-level tactics within their role's doctrine.**

- **Fleet commander** (cloud LLM, e.g. OpenAI): consumes the broad picture
  — fleet positions, weapon loads, fused contacts, scenario context — and
  produces a `FleetIntent` of per-ship destinations, EMCON posture, and
  task-group coordination. Adapts pre-authored task groups; doesn't invent
  them.
- **Ship captains** (a local LLM via Ollama, or a capable cloud model — the
  model tier is a dial, not a fixed weakness): one inference per ship per
  cadence. Each captain receives a focused prompt: its role, its task group
  context, threat alerts, a tactical briefing with pre-computed geometry and
  a doctrine recommendation, plus its local sensor data. The recommendation
  is a **default to weigh, not a mandate** — the captain owns the decision.
  Deviate: prefix `summary` with `deviate:` and state the reason.
- **The architecture supports human-in-the-loop at any layer.** A human
  could replace the fleet commander, a single ship captain, or a specific
  role. The Hands/Controllers split was built specifically to make this
  swappable without code changes — only a different `ShipController`
  implementation.

The two original litmus tests:

1. **A single destroyer driven by either a human or an LLM via the same
   tool/command surface.** Structurally met by Phase 2's `ShipController`
   abstraction; demonstrated by the `ScriptedShipController` test that
   swaps in for `LLMShipController`.
2. **(Stretch) The submarine crewed by role-specialized LLMs vs. a human
   crew.** Architecture supports it (Phases 1–3 ready); the player-side
   role abstraction (Phase 5) is still pending.

### 1.1 Captain-tier intent: reason about tactics under strategic intent

The multi-tier test is specifically about **locally-running LLMs making
tactical decisions while understanding strategic intent.** The captain must
*comprehend* the fleet's intent and translate it into sound local action —
that reasoning is the capability under test. Four principles govern all
captain-tier work:

- **The model tier is a dial.** Captains can run on a small local model
  (Ollama) *or* a capable cloud model, over the same prompt and tool surface.
  The question is "how far down the capability ladder does genuine tactical
  reasoning survive?" — measured per model, not "compensate for a weak model."

- **The captain is a reasoner, not a transcriber.** The tactical briefing
  exists to remove the burden of *fact and geometry* (bearings, intercepts,
  envelopes) so the model spends its capacity on judgment. It must never
  pre-decide the action.

- **The cardinal rule — inform, don't neuter.** Pre-computation may hand the
  captain answers to questions of fact and geometry; it must never collapse
  the *decision*. The LLM owns the choice of tool, its arguments, and the
  `deviate:` path. Over-constraining (pre-filling the tool call, making
  doctrine "non-negotiable") strips the reasoning depth that is the entire
  point. When a captain underperforms, the first moves are to make its inputs
  **coherent** (remove contradictory orders, dead boilerplate, buried rules)
  and/or move up the model ladder — never to remove the decision.

- **Success = a defensible decision that serves intent, with a sound
  rationale** — *not* compliance with the doctrine recommendation. An escort
  that evades an inbound torpedo with good reasoning is a success; one that
  holds course into the torpedo is the failure, even though "hold course" is
  a nav action. Decisions are judged on reasoning quality, not action label.

**How we measure it.** Freeze real decision prompts across the doctrine ladder
(TRANSIT / INVESTIGATE / ENGAGE / torpedo-in-water / friendly-hit) and replay
them against the model ladder, scoring decision soundness + rationale. Prompt
and doctrine changes are validated against this set, not asserted.

---

## 2. Architecture

Four pure layers plus orchestration glue:

```
                ┌─────────────────────────────────────────────┐
                │  ai/roles/*.md       — captain doctrine     │
                │  ai/*_commander_*.md — system prompts       │
                │  assets/missions/*   — scenario JSON        │
                └────────────────────┬────────────────────────┘
                                     │ inputs
        ┌────────────────────────────▼─────────────────────────┐
        │  ai_orchestrator.py                                   │
        │  - Builds fleet/ship summaries                        │
        │  - Calls LLM engines (OpenAI / Ollama / stub)         │
        │  - Returns RunResult with validated tool calls        │
        │  - Maintains:                                         │
        │      _last_fleet_intent   _orders_last_by_ship        │
        │      _recent_runs         _recent_combat_events       │
        │      _fleet_intent_history                            │
        │  - Loads role doctrine per ship                       │
        └────────────────────────────┬─────────────────────────┘
                                     │ tool_calls_validated
        ┌────────────────────────────▼─────────────────────────┐
        │  control/controllers.py — ShipController             │
        │   • LLMShipController  : wraps orchestrator          │
        │   • ScriptedShipController : test/demo path          │
        │   • returns List[Action] (typed dataclasses)         │
        └────────────────────────────┬─────────────────────────┘
                                     │ Action[]
        ┌────────────────────────────▼─────────────────────────┐
        │  control/hands.py — ShipControls                     │
        │   capability-gated mutators:                         │
        │     set_nav, fire_torpedo, drop_depth_charges,       │
        │     deploy_countermeasure, active_ping               │
        │   single chokepoint for ship/world mutation          │
        └────────────────────────────┬─────────────────────────┘
                                     │ ship + world deltas
        ┌────────────────────────────▼─────────────────────────┐
        │  core.py — SimulationCore.step_physics(dt, …)        │
        │   pure step: kinematics, weapons, projectiles,       │
        │   damage, engineering. Returns CoreStepResult        │
        │   (events, system_failures, sonar explosions, ...)   │
        └────────────────────────────┬─────────────────────────┘
                                     │
        ┌────────────────────────────▼─────────────────────────┐
        │  tactical.py — pure compute (no I/O)                  │
        │   bearing_to / range_to / intercept_solution          │
        │   weapon_envelope / doctrine_for / scan_threats       │
        │   used by orchestrator to pre-compute the captain's   │
        │   tactical briefing                                    │
        └───────────────────────────────────────────────────────┘
```

`loop.py` is the orchestration glue: tick scheduling, AI cadence, telemetry
broadcast, command dispatch routing through `commands.py:CommandDispatcher`.

### Critical invariants

- **No layer below `control/` knows the LLM exists.** `core.py`,
  `tactical.py`, `damage.py`, `physics.py`, `weapons.py`, `sonar.py` are
  all callable from a plain pytest with no asyncio, no BUS, no engine.
- **All ship/world mutation goes through `ShipControls`.** Direct mutation
  is permitted only inside `core.step_physics` (the physics integration
  itself) and inside the legacy `commands.py` handlers (which Phase 4 will
  also route through `SubmarineControls`).
- **Tactical computation is pre-baked into the captain prompt.** The LLM
  consumes answers, not raw geometry. A 3B local model picks an action;
  it does not do trigonometry.

---

## 3. Phase-by-phase summary

| # | Name | What it delivered | Status |
|---|---|---|---|
| 0 | Safety net | Stub LLM engine + 3 integration test files locking the AI tool-call seam, command dispatch, and tick smoke | ✅ |
| 0.5 | CommandDispatcher extraction | Wired the abandoned `CommandDispatcher`; deleted ~590 duplicate handler lines from `loop.py:handle_command` | ✅ |
| 1 | ShipControls | New `sim/control/hands.py` with capability-gated mutators; AI tool execution moved out of `loop.py` inline switch | ✅ |
| 2 | ShipController | `LLMShipController` + `ScriptedShipController` + typed `Action` dataclasses; per-ship decision-maker is swappable | ✅ |
| 3 | SimulationCore | Pure `step_physics` extracted from `loop.py:tick`; physics callable from plain pytest | ✅ |
| 3.5 | Tactical compute layer | `bearing_to`, `range_to`, `intercept_solution`, `weapon_envelope`, `doctrine_for` as pure functions | ✅ |
| 4 | SubmarineControls | Extend Hands abstraction to player sub; route `commands.py` through it | ⏳ pending |
| 5 | Role abstraction (player side) | Crew-position roles (helm/sonar/weapons/engineering/captain) with role-specific controllers | ⏳ pending |
| 6 | Scenario system overhaul | Mission schema gains `scenario_context`, `task_groups`, `ship_roles`; orchestrator projects them; role library appended to captain prompts | ✅ |
| 7 | Lethality rebalance | Per-hit damage tuned for naval realism: 1 torpedo cripples, 2 destroy. Hull formula: max-loss-weighted with critical-compartment escalation. Slow breach healing | ✅ |
| 8 | Threat awareness | `ThreatAlert` + `scan_threats` for torpedoes-in-water, friendly hits, self hits. Doctrine override; new `INVESTIGATE` and `EVADE` actions; threat sections in role files | ✅ |

`loop.py` shrunk from **2565 → 1922 lines (-25%)** through Phases 0.5
through 3, with all reductions backed by tests.

---

## 4. Scenario authorship surface (mission schema)

A mission JSON now expresses each side's situation explicitly:

```json
{
  "id": "interdict_dual_convoys",
  "title": "...",
  "objective": "...",
  "blue_captain_summary": "...",
  "red_mission_summary": "...",
  "side_objectives": { "RED": "...", "BLUE": "..." },

  "scenario_context": {
    "RED": {
      "narrative": "Two cargo convoys in scheduled transit ...",
      "primary_objective": "Deliver all 4 cargo ships to [0, -25000]",
      "win_condition": "All cargo ships reach destination",
      "lose_condition": "More than 1 cargo lost OR all escorts destroyed",
      "intelligence": "Submarine activity reported sector DELTA-9 ...",
      "constraints": [
        "Maintain EMCON until contact confidence >= 0.7",
        "Escorts must remain within 5 km of their convoy unless prosecuting"
      ],
      "doctrine_emphasis": "convoy_protection"
    },
    "BLUE": { /* analogous BLUE-side narrative + objectives */ }
  },

  "task_groups": {
    "RED": {
      "CONVOY_A": {
        "doctrine": "convoy_escort",
        "lead": "red-a-dd-01",
        "members": ["red-a-dd-01", "red-a-cv-01", "red-a-cv-02"],
        "protected": ["red-a-cv-01", "red-a-cv-02"],
        "formation": "screen-ahead",
        "destination": [-2000, -25000]
      },
      "CONVOY_B": { /* analogous */ }
    }
  },

  "ship_roles": {
    "red-a-dd-01": { "role": "convoy_escort_destroyer", "task_group": "CONVOY_A" },
    "red-a-cv-01": { "role": "convoy_cargo",            "task_group": "CONVOY_A" }
  },

  "ships": [ /* spawn definitions */ ],
  "triggers": [ /* timed comms events */ ]
}
```

All five missions now use this schema. Legacy `ship_behaviors` is retained
as a fallback path.

### Field semantics

- **`scenario_context.RED`** is what the fleet commander reads. It includes
  what RED *believes* about the situation, RED's objective, win/lose
  conditions, intelligence, and any constraints that should temper its
  decisions. The fleet does **not** see `scenario_context.BLUE` — that's
  the human player's narrative.
- **`task_groups.RED`** is pre-authored coordination. The fleet commander
  *adapts* groups (re-tasking, detaching, reassigning escorts) instead of
  inventing groups every turn from a flat ship list.
- **`ship_roles[ship_id]`** binds a ship to a role file in `ai/roles/` and
  to its task group. The orchestrator loads `ai/roles/<role>.md` and
  appends it to the captain system prompt for that ship.

---

## 5. Role library

Each role is a doctrine prompt that captures *how a captain in that role
should think*. They are loaded by `_load_role_prompt()` and concatenated
to the ship system prompt. Layout: priorities → doctrine by situation
(TRANSIT / INVESTIGATE / ENGAGE / etc.) → hard constraints → threat
override → deviation guidance.

Current library:

| File | Used by | Doctrine emphasis |
|---|---|---|
| `ai/roles/convoy_escort_destroyer.md` | Convoy DD escorts | Protect-first, EMCON discipline, weapon conservation |
| `ai/roles/asw_hunter_destroyer.md` | ASW patrol DDs | Aggressive prosecution, expend ammo to kill |
| `ai/roles/convoy_cargo.md` | Unarmed transports | Survive, no weapons, evade rather than fight |

Adding a new role is a markdown file plus `ship_roles[id].role = "name"`
in mission JSON. No code change.

---

## 6. Tactical layer (pre-baked answers for the captain LLM)

`tactical.py` is a pure-function library. The captain LLM consults its
output through the **tactical_briefing** block in the ship summary instead
of doing geometry itself.

### Functions

- `bearing_to(from, to) → float` — compass bearing.
- `range_to(from, to) → float` — meters.
- `intercept_solution(hunter, target, speeds) → InterceptSolution` — full
  quadratic solver, falls back to direct bearing when hunter is too slow.
- `weapon_envelope(ship, target, kind) → EnvelopeReport` — torpedo / depth
  charge / active ping range checks.
- `scan_threats(ship, world, recent_events) → List[ThreatAlert]` — detect
  hostile torpedoes within passive range, friendly hits, self hits.
- `doctrine_for(ship, contacts, fleet_dest, threats) → DoctrineRecommendation`
  — pick action: `ENGAGE_TORPEDO` / `ENGAGE_DC` / `CLOSE` / `INVESTIGATE` /
  `TRANSIT` / `HOLD` / `EVADE`. Threats override the contact-confidence
  ladder.

### Doctrine ladder

```
0. Threats present (highest priority):
   self_hit         → engage best contact, or evade flank if no firing solution
   torpedo_in_water → counter-fire on bearing if armed; else perpendicular evasion
   friendly_hit     → CLOSE on hit ship's bearing at flank
1. High-confidence contact (≥ 0.7) in torpedo envelope → ENGAGE_TORPEDO
2. High-confidence contact in DC envelope            → ENGAGE_DC
3. High-confidence contact known position            → CLOSE
4. Moderate confidence (0.3 ≤ c < 0.7)               → INVESTIGATE
5. Fleet destination set                             → TRANSIT
6. Else                                              → HOLD
```

---

## 7. Lethality model (Phase 7)

A single torpedo hit must be combat-effective; two should destroy a ship.
The math reflects that:

- **Torpedo primary compartment**: -0.85 integrity, +0.6 breach rate
- **Torpedo adjacent compartments**: -0.30 integrity, +0.25 breach rate
- **Hull damage formula**: `0.4·destroyed_count + 0.5·max_loss + 0.1·avg_loss`,
  clamped to [0, 1].
- **Critical compartment rule**: engine room or reactor at 0 integrity AND
  ≥ 80% flooded → instant total loss.
- **Effective destruction threshold**: a compartment at ≤ 0.15 integrity
  counts as destroyed (catches saturated adjacents).
- **Breach healing**: 0.003/s. A torpedo-grade breach takes minutes to
  seal even with damage control.

Result, validated in live-play:
- 1 torpedo hit → ~0.45 hull damage (crippled, ruined systems, flooding).
- 2 torpedo hits same area → ~0.95 hull damage (mission-killed).
- 3 torpedo hits → 1.0 (destroyed).

Depth charges remain less individually lethal — they kill through
saturation. Single direct DC: ~0.40 hull damage. Far miss: minor.

---

## 8. Captain prompt structure (what the LLM reads)

The captain receives, per inference:

1. **System prompt**: `ai/ship_commander_system.md` (general protocol) +
   `ai/roles/<role>.md` (role-specific doctrine) appended.
2. **User prompt**: a JSON ship summary with these fields:
   - `self` — own position, kinematics
   - `constraints` / `weapons` / `capabilities` / `sensors`
   - **`role`** — the role string
   - **`task_group_context`** — name, lead, peers (with positions),
     protected ships, formation, doctrine
   - **`threats`** — list of `ThreatAlert` entries (overrides everything
     when non-empty)
   - **`tactical_briefing`** — `doctrine_recommendation`, `target_id`,
     `suggested_heading`, `suggested_speed_kn`, `fleet_destination_bearing`,
     `fleet_destination_range_m`
   - `contacts` / `fleet_fused_contacts` / `contacts_history`
   - `fleet_intent` / `orders_last` / `detected_state`

The LLM's job is *judgment*: trust the briefing or deviate with reason.
Geometry is already done.

---

## 9. Test surface

229 tests passing, 8 xfailed (pre-existing logic issues flagged for
future investigation, none introduced by the refactor):

| Suite | What it covers |
|---|---|
| `test_simulation_core.py` | Pure physics step (kinematics, projectiles, damage, destruction events) |
| `test_ship_controls.py` | Hands layer: each method, capability gating, world mutation |
| `test_ship_controllers.py` | LLMShipController, ScriptedShipController, tool-call → Action translation |
| `test_tactical.py` | Bearings, ranges, intercept solver, envelopes, doctrine, threats (46 tests) |
| `test_lethality.py` | Hull formula, single-hit cripples, two-hit destroys, breach progression |
| `test_scenario_roles.py` | Mission schema, role loader, summary projection |
| `test_ai_tool_execution.py` | Orchestrator → tool_calls under stub LLM |
| `test_command_dispatch.py` | WebSocket command surface |
| `test_tick_smoke.py` | Full tick under real Simulation + stub LLM |
| `test_damage_system.py` | Compartment flooding, pumps, system failures |
| (legacy) | Maintenance, sonar, victory, waypoints, triggers, bearings math |

---

## 10. Live-play validation (what we've proven)

From the Phase 6 + 7 + 8 play sessions on `interdict_dual_convoys` and
`weapons_validation_destroyers`:

- **Captains follow doctrine recommendations.** SetNav rate jumped from
  1% to 39% across all decisions; convoy ships stay in formation, escorts
  stay with their convoy.
- **Depth-charge destroyers actually use depth charges.** Previous
  session: 168 DC-ship decisions, 0 drops. Phase 6 session: drops occur
  when contacts cross threshold.
- **Torpedoes are lethal.** First confirmed kill in any session:
  `red-dd-torp-02` destroyed by 2 stern hits → engine room loss → instant
  total damage via the critical-compartment rule.
- **Fleet reads scenario_context.** Fleet journal explicitly cited *"the
  EMCON restrictions are tight, but I trust my destroyers"* — direct
  evidence of the constraint propagation.
- **(Open observation)** Convoy escorts under EMCON discipline correctly
  did NOT prosecute low-confidence passive contacts. This is doctrinally
  correct but exposed the gap that drove Phase 8: without a torpedo-in-water
  signal or friendly-hit signal, escorts had no path from "ambient threat"
  to "engage." Phase 8 closes that gap; not yet validated in play.

---

## 11. Open follow-ups (ranked by impact)

### High impact

1. **Validate Phase 8 in live play** — restart and play `interdict_dual_convoys`,
   confirm convoy escorts react when torpedoes enter their detection
   bubble.
2. **Destroyed-ship cleanup** — orchestrator still schedules AI runs and
   fleet still issues objectives for ships at hull ≥ 1.0. Filter destroyed
   ships from `_build_fleet_summary`'s `own_fleet`; skip AI scheduling in
   `loop.py`'s ship-AI loop. (~30 min)
3. **`report_to_fleet` tool** — captains emit a 2-3 line situation report
   each cadence; fleet sees them in next run. Closes the captain → fleet
   feedback loop. New `Action` type, orchestrator buffer, role-prompt
   instruction. (~half day)
4. **Phase 4 — `SubmarineControls`** — extend the Hands abstraction to the
   player sub. Route `commands.py` handlers through it. Single chokepoint
   for sub state mutation. Unblocks Phase 5. (~3-4 days)

### Medium impact

5. **`ai.tool.apply` payload enrichment** — current event records only
   action class name. Include action params via `dataclasses.asdict()` so
   the SQLite event log captures what was actually decided. Cosmetic but
   improves debugging.
6. **Failed-action signal back to LLM** — when `ShipControls` returns
   `ControlResult.fail(...)`, that error doesn't reach the next prompt.
   Captains repeat broken actions. Surface `last_action_failed: {tool,
   error}` in the ship summary.
7. **Time-based fleet escalation** — if a contact persists at moderate
   confidence for N fleet runs, fleet should detach an escort to
   investigate. Track `contact_age_s` on the orchestrator side.
8. **Phase 5 — `Role` abstraction (player side)** — crew positions
   (Helm / Sonar / Weapons / Engineering / Captain) become first-class.
   Each is a slice of `SubmarineControls` plus a controller (LLM agent or
   human panel). Meets litmus test 2.

### Low impact

9. **5 pre-existing xfail tests** — substantive logic bugs in visual
   detection, classification, and trigger reset. Not refactor regressions;
   investigate when convenient.
10. **`pytest_cache` pollution** — running tests writes `.pyc` and DB
    rows. Test isolation: a per-test SQLite path or `:memory:` engine.

---

## 12. How to run things

```sh
# Run the full test suite
python -m pytest tests/ -q

# Start the server (under .venv to get sqlmodel for event logging)
cd sub-bridge && ../.venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000

# Pick mission
# Browser → http://localhost:8000/missions  → click one
# OR API:  curl -X POST http://localhost:8000/api/missions/<id>/start

# Watch AI events live
sqlite3 sub-bridge/sub-bridge.db \
  "SELECT created_at, type, substr(payload, 1, 120) FROM event \
   WHERE type LIKE 'ai.%' OR type IN ('torpedo.detonated','depth_charge.detonated','ship.destroyed') \
   ORDER BY id DESC LIMIT 30;"

# Read fleet commander journal entries (per-day file)
cat logs/fleet_journal_$(date +%Y-%m-%d).md
```

`.env` controls AI engine selection (`AI_FLEET_ENGINE=openai`,
`AI_SHIP_ENGINE=ollama`, etc.) and cadences. The default sub-bridge
mission is `MISSION_ID=torpedo_training`.

---

## 13. What this project is *not*

- It is not trying to be a milsim. The goal is testing whether a layered
  LLM hierarchy can produce coherent fleet behavior with a small local
  model at the per-ship layer. Realism is a constraint; lethality and
  doctrine are tuned for *interesting decisions*, not historical accuracy.
- It is not a cloud-only application. The whole point of the captain
  layer using a small Ollama model is that captains run cheaply and
  locally. A 70B model at the captain level would defeat the cost
  argument the architecture is designed to test.
- It is not done. Phases 4 + 5 (player-side Hands and Roles) are
  outstanding. Several behavioral gaps remain (destroyed-ship cleanup,
  captain → fleet reports, failed-action signal). The architecture is
  the durable artifact; behavior is iterative.

---

*This document is the source of truth for design intent and current
state. When something here disagrees with the code, the code is the
authority — but if the disagreement reveals an architectural drift,
update this document.*
