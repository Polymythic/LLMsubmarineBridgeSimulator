# Refactor Plan — Submarine Bridge Simulator

Goal: refactor for stability, testability, and separation of concerns.
Two structural targets:

1. **Physics and game mechanics** isolated and strongly unit-testable.
2. **LLM logic** isolated behind a clean interface so any unit can be driven by an LLM, a human, or a scripted controller.

**Litmus tests:**

- A single destroyer can be driven by either a human or an LLM via the same tool/command surface.
- Stretch: the submarine can be crewed by a team of role-specialized LLMs against a human crew (or vice versa).

---

## Pre-flight assumptions to verify in Phase 0

These come from the initial architecture survey and are re-verified at the start of Phase 0:

- `loop.py` is ~2565 lines and contains inline LLM tool dispatch around lines 808–1159 (fleet) and 975–1154 (per-ship `set_nav` / `fire_torpedo`).
- Physics integration in `loop.py` lives roughly at lines 1175–1305.
- `commands.py` is the WebSocket command dispatcher for the submarine UI and currently mutates `Ship` state directly.
- `physics.py`, `weapons.py`, `damage.py`, `sonar.py`, `noise.py`, `contact_registry.py` are mostly pure and stay where they are.
- `ai_orchestrator.py` returns parsed-but-unexecuted tool calls; `loop.py` executes them.

---

## Pre-flight findings (verified)

- `loop.py` is 2565 lines.
- AI scheduling lives at `loop.py:808–1159`; per-ship tool dispatch at `loop.py:975–1154`.
- `ai_orchestrator.run_fleet()` and `run_ship()` return `RunResult` (TypedDict) with `tool_calls_validated`.
- Engine interface is `BaseEngine.propose_fleet_intent()` / `propose_ship_tool()` in `ai_engines.py` — the seam for the stub.
- **Discovered:** `sim/commands.py` defines `CommandDispatcher` but nothing instantiates it. `app.py:159` calls `sim.handle_command()` (the inline duplicate at `loop.py:2032`). It's an abandoned in-progress extraction. Decision: **complete the extraction** in Phase 0.5 below, not delete it.

---

## Phase 0 — Safety net (target: 0.5 day)

**Goal:** Lock current behavior with tests so structural changes can't silently regress it.

**New files:**

- `tests/test_ai_tool_execution.py` — drive canned LLM tool calls (`set_nav`, `fire_torpedo`, `set_fleet_intent`) through the existing path and assert resulting world state.
- `tests/test_command_dispatch.py` — invoke `commands.py` handlers directly with WebSocket-shaped payloads (helm, weapons, sonar, engineering, captain) and assert ship/world state changes.
- `tests/test_tick_smoke.py` — load one mission, stub the LLM engine to return a fixed sequence of tool calls, run N ticks, assert no exceptions and a small set of invariants.

**Modified files:**

- `tests/conftest.py` — add a `StubLLMEngine` fixture returning canned tool-call sequences.

**Acceptance criteria:**

- All three new test files green.
- Existing `tests/` suite unchanged and still green.
- Stub LLM is reusable across phases.

**Out of scope:** any production-code changes.

Tests target the **live public surface** (`sim.handle_command(topic, data)`), not the abandoned `CommandDispatcher`. They survive Phase 0.5 unchanged.

---

## Phase 0.5 — Complete the abandoned `CommandDispatcher` extraction (target: 0.5 day)

**Goal:** Eliminate the duplicate command-handling path so there's one obvious place to do Phase 4.

**Modified files:**

- `sub-bridge/backend/sim/commands.py` — diff against `loop.py:handle_command()` and resolve any drift; treat the loop.py copy as authoritative for any divergent behavior.
- `sub-bridge/backend/sim/loop.py` — `Simulation.__init__` instantiates a `CommandDispatcher`; `handle_command` becomes a thin delegate (or is removed and `app.py` calls the dispatcher directly).
- `sub-bridge/backend/app.py` — optionally call `dispatcher.dispatch()` directly instead of going through `sim.handle_command`.

**Acceptance criteria:**

- All Phase 0 tests still green (the public `sim.handle_command` surface still works, even if internally it delegates).
- `loop.py` shrinks by ~600 lines (the duplicated handlers).
- One implementation of each command handler.

**Out of scope:** introducing `SubmarineControls`. Phase 4 still does that. This phase is purely about deduplication.

---

## Phase 1 — `ShipControls` for the destroyer (target: 2 days)

**Goal:** Single chokepoint for all mutations to enemy ships. LLM tool execution moves out of `loop.py`.

**New files:**

- `sub-bridge/backend/sim/control/__init__.py`
- `sub-bridge/backend/sim/control/hands.py` — abstract `ShipControls` and concrete `DestroyerControls` with: `set_nav(heading, speed, depth)`, `fire_torpedo(...)`, `drop_depth_charge(...)`, `deploy_countermeasure(...)`, `active_ping()`. Each method validates inputs and mutates the underlying `Ship`.
- `tests/test_ship_controls.py` — direct unit tests on `DestroyerControls`. No LLM, no WebSocket.

**Modified files:**

- `sub-bridge/backend/sim/loop.py` — replace inlined `set_nav` / `fire_torpedo` blocks (~975–1154) with `controls.method(...)` calls. No behavior change.
- `sub-bridge/backend/sim/ai_tools.py` — keep schema as-is; document 1:1 mapping to `ShipControls` methods.

**Acceptance criteria:**

- `tests/test_ship_controls.py` green.
- Phase 0 tests still green.
- `loop.py` shrinks by the expected amount.
- `grep` shows no direct mutation of enemy `Ship.kin` from `loop.py`'s ship-AI section.

**Out of scope:** human UI for the destroyer; submarine; controller abstraction.

---

## Phase 2 — `ShipController` interface (target: 1.5 days)

**Goal:** Decouple "who decides" from "what gets done." Driver-swappability for the destroyer.

**New files:**

- `sub-bridge/backend/sim/control/controllers.py` — abstract `ShipController` with `step(snapshot, dt) -> List[Action]`. Concrete: `LLMShipController`, `ScriptedShipController`.
- `sub-bridge/backend/sim/control/actions.py` — `Action` dataclasses mirroring `ShipControls` methods.
- `tests/test_ship_controllers.py`.

**Modified files:**

- `sub-bridge/backend/sim/loop.py` — replace AI scheduling/dispatch (~808–1159) with: `actions = controller.step(snapshot, dt); for a in actions: a.apply(controls)`.
- `sub-bridge/backend/sim/ai_orchestrator.py` — `run_ship` returns typed `Action` objects.

**Acceptance criteria:**

- All Phase 0/1 tests still green.
- A flag/config can swap any destroyer to `ScriptedShipController` and the mission still completes.
- **Litmus test 1 structurally met.**

---

## Phase 3 — Extract `SimulationCore.step_physics()` (target: 2 days)

**Goal:** A pure physics step callable from a plain pytest, no asyncio, no BUS, no AI.

**New files:**

- `sub-bridge/backend/sim/core.py` — `SimulationCore.step(world, ordered, dt) -> (new_state, events)`. Internally calls existing physics/weapons/damage primitives. No side effects beyond returned state.
- `tests/test_simulation_core.py` — multi-tick scenarios with no mocking.

**Modified files:**

- `sub-bridge/backend/sim/loop.py` — `tick()` becomes: gather orders → `core.step()` → mission rules → broadcast.

**Acceptance criteria:**

- `tests/test_simulation_core.py` green with no mocks/stubs.
- `loop.py` shrinks by another ~150 lines.
- All earlier tests still green.

---

## Phase 4 — `SubmarineControls` + route `commands.py` through it (target: 3–4 days)

**Goal:** Single chokepoint for submarine state mutation. Human commands and LLM tool calls converge.

**New files:**

- `sub-bridge/backend/sim/control/submarine.py` — `SubmarineControls` covering helm, ballast, periscope, sonar, tubes, pumps, comms, captain overrides.
- `tests/test_submarine_controls.py`.

**Modified files:**

- `sub-bridge/backend/sim/commands.py` — every handler becomes a thin translator that calls `SubmarineControls`.

**Acceptance criteria:**

- `tests/test_submarine_controls.py` green.
- Phase 0 `test_command_dispatch.py` still green (behavior unchanged).
- `grep` shows no direct submarine `Ship` mutation outside `SubmarineControls`.

**Out of scope:** any frontend changes.

---

## Phase 5 — `Role` abstraction (target: 2–3 days)

**Goal:** Crew positions are first-class. LLM-vs-human is configurable per role.

**New files:**

- `sub-bridge/backend/sim/control/roles.py` — `Role` = (subset of `SubmarineControls` + `Controller`). Concrete: `HelmRole`, `SonarRole`, `WeaponsRole`, `EngineeringRole`, `CaptainRole`.
- `sub-bridge/backend/sim/control/role_controllers.py` — per-role LLM and human controllers.
- `tests/test_roles.py`.

**Modified files:**

- `sub-bridge/backend/sim/ai_orchestrator.py` — gains role-specific prompt builders/tool schemas.
- `sub-bridge/backend/sim/loop.py` — orchestrates roles via `SubmarineControls`.

**Acceptance criteria:**

- A mission can run with any mix of human/LLM roles.
- **Litmus test 2 structurally met.**

**Out of scope:** LLM prompt-engineering for compelling per-role behavior.

---

## Explicitly out of scope across all phases

- Replacing the 135-attribute `Simulation` state bag with a `GameState` dataclass.
- Refactoring sensor/telemetry aggregation in the broadcast path.
- Frontend UI changes.
- Rewriting physics/sonar/weapons math.
- `ENEMY_SUB_PLAN.md` work (this refactor unblocks it).
- LLM behavior tuning.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Hidden state-mutation paths missed in initial survey | Phase 0 tests + grep audits at each phase boundary |
| Phase 4 (submarine) is bigger than estimated | Don't start until 1–3 are stable; reassess scope |
| LLM tool-call shape changes break missions | `Action` dataclasses give a stable internal contract |
| `ai_orchestrator.py` is too tangled to refactor incrementally | Phase 2 only changes return type of `run_ship`; deeper work deferred |

---

## Branching / commit strategy

- One phase = one branch, one PR.
- Each PR must end with all tests green and the game still playable.
- Each phase is self-contained enough to work from a fresh context: `Read REFACTOR_PLAN.md, we're starting Phase N`.
