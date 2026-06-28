# Captain-Tier Plan — reasoning quality across the model ladder

**Created:** 2026-06-27. **Status:** ready to resume; eval harness not yet built.

This plan picks up after a session that (a) repaired the RED AI wiring, (b)
validated it in live play, and (c) isolated the next bottleneck: the ship
captain LLM won't make crisp engagement decisions, for reasons of **prompt
incoherence**, not model weakness or timeouts. Read `PROJECT_OVERVIEW.md §1.1`
first — it codifies the design intent this plan serves (**inform, don't
neuter**).

---

## 1. Where we are (committed this session)

Wiring repairs — all on `main`, suite green (251 passed / 6 xfailed):

- `02190eb` — repaired RED "brain" wiring: `CRITICAL_ORDERS` injection (was a
  double-replace no-op + read from the never-populated `world.mission_brief`),
  dead per-ship contact memory (`getattr` on a dict), permanent
  `last_action_failed`, and a fleet alert cadence that latched on forever.
- `fe18989` — **role doctrine never loaded in production** (same wrong-object
  read) → fixed; sanitized the fleet `MISSION_BRIEF` to a RED-only slice;
  aligned role-doc field names (`suggested_action`/`SENSORS_BRIEFING` →
  `doctrine_recommendation`/`tactical_briefing`); completed the doctrine enum.
- `e148839` — `TriggerManager.reset()` deleted triggers instead of unfiring.
- `ccc5041` — fixed/relabeled the detection xfail tests (test issues, not bugs).
- `a456c78` — **`PROJECT_OVERVIEW.md §1.1`**: codified captain-tier intent.
- Plus `4748fd7` (salvo discipline) and `c1f518a` (shared plot board).

Regression tests now lock the order-injection and role-loading paths
(`tests/test_ai_tool_execution.py`).

---

## 2. The live finding (run 17, `interdict_dual_convoys`)

- Wiring validated end-to-end: role doctrine + critical orders confirmed in
  every captain prompt; gemma3 produced **181 tool calls, 0 failures**; the
  cloud fleet commander stayed coherent and its "MUST engage >0.7" directive
  was confirmed reaching the local escort prompts (cloud→local chain working
  for the first time).
- Player sank escort `red-a-dd-01` with two stern torpedoes — **and the escort
  never fired back.** It held `set_nav` every cycle with `ENGAGE_TORPEDO`, a
  counter-fire bearing, 10 ready torpedoes, and the contact at confidence 1.0.

### Root cause (proven by replay, not guessed)

- gemma3, **original prompt**: 5/5 `set_nav`.
- gemma3, **fire spelled out explicitly**: 5/5 `fire_torpedo`.
- qwen2.5:14b, **original prompt (unchanged)**: 3/3 `fire_torpedo`.
- **Not a timeout**: `done_reason=stop`, full 224-char output; 12.9s wall
  (6.2s one-time cold-load + 5.3s prompt-eval of a ~5000-token prompt + 1.3s
  gen). It *chose* nav and explained why ("maintain escort").

The captain prompt (~4300 tokens) pulls the model four ways at once:
1. a **bolded** "Default behavior: follow `suggested_heading` → `set_nav`",
   while the `ENGAGE → fire` rule is one un-bolded line below;
2. **two contradictory `CRITICAL ORDERS`** — mission text "your primary mission
   is convoy protection, *not submarine hunting*" vs fleet "MUST engage";
3. a role doctrine saturated with "*not* kills / conserve weapons / only after
   passive localization";
4. dead `BEHAVIOR GUIDELINES` boilerplate ("use active_ping every 15-20s", "if
   no change, return `set_nav` holding current values").

It's also **bloated** (full JSON Schema, `contacts_history`, the entire fleet
intent with all six ships' objectives) — heavy prompt-eval every call, every
ship, near the 15s timeout cliff under 6-ship concurrency.

---

## 3. Design guardrails (do not violate — see §1.1)

- **Inform, don't neuter.** Precompute may answer fact/geometry questions; it
  must NOT pre-decide the action. The LLM owns tool choice, args, and
  `deviate:`. Do **not** "fix" the trigger problem by pre-filling
  `fire_torpedo` or making doctrine non-negotiable — that hollows out the
  experiment.
- **Success = a defensible decision serving intent, with sound rationale** —
  not compliance with the doctrine label. Evade-with-reasoning can be the
  right answer to a 420 m torpedo; hold-course-into-it is the failure.
- **Eval-first.** Validate every prompt/doctrine change against the frozen
  scenario set across the model ladder. No anecdotes, no single-case tuning.
- **De-noise ≠ neuter.** Removing contradiction/dead text *enables* reasoning
  (good). Removing the decision *destroys* it (bad). Stay on the right side.

---

## 4. The plan (phased)

### Phase 1 — Eval harness (DO FIRST; blocks everything else)
Goal: turn "gemma3 won't fire" into a measured table.

1. Extract a spread of **frozen real decision prompts** from the run-17 event
   log (`sub-bridge/sub-bridge.db`, `ai.run.ship` payloads → `api_call_debug`)
   — one per rung: `TRANSIT`, `INVESTIGATE`, a borderline ~0.65 contact,
   `ENGAGE_TORPEDO`, `torpedo_in_water` (armed), `friendly_hit`. Persist them
   as fixtures (e.g. `evals/captain_scenarios/*.json`) so they're stable even
   after the DB is overwritten.
2. Replay each against the **model ladder**: `gemma3:4b` → a mid local model
   (e.g. `qwen2.5:14b` or `llama3.1:8b`) → `gpt-4o-mini` at the captain seat.
   Match production call shape (Ollama sends **no** temperature/format — see
   `ai_engines.py:_chat` ~line 151).
3. Score each result on **decision soundness + rationale quality** (not "did it
   match the doctrine label"). Start with a rubric + a judge model; capture the
   chosen tool, args, and `summary`.
4. Output: a baseline table (scenario × model → tool / soundness / rationale).

**Acceptance:** a re-runnable script + committed baseline table. Ollama is
already configured (`OLLAMA_NUM_PARALLEL=4`, models pulled).

### Phase 2 — Make the captain's inputs coherent (de-noise)
Only after Phase 1 baseline exists. Each change A/B'd on the eval set; **must
not regress** `TRANSIT`/`INVESTIGATE`.

- Resolve the two contradictory `CRITICAL ORDERS` — fleet attack directives
  must not sit next to "not submarine hunting" mission text with equal "MUST
  follow" weight. Decide precedence explicitly.
- Delete the dead `BEHAVIOR GUIDELINES` boilerplate in `ai/ship_commander_user.md`.
- De-bury the doctrine→tool mapping: make `doctrine_recommendation` drive tool
  choice as a **weighed default the model owns**, with `set_nav`+`suggested_heading`
  scoped to the movement doctrines — without making it non-negotiable or
  pre-filling the call. Keep `deviate:` first-class.
- Keep the JSON Schema (0 malformed calls with it) and the task-group
  coordination context (peers/lead) — don't cut those for tokens.

### Phase 3 — Enrich the doctrine with *options* (not a dictated answer)
- `tactical.doctrine_for` / the threat path has no EVADE / countermeasure /
  depth dimension under `torpedo_in_water` — it only knows CLOSE/fire. Give the
  captain the option set as **reasoning inputs**, and let it choose + justify.
- Re-judge on the eval: a sound evade or countermeasure call should score as
  well as a sound counter-fire.

### Phase 4 — Engine + latency (measured)
- Set Ollama `temperature` low (~0.2) and consider `format:"json"` in
  `ai_engines.py:_chat` (currently neither — defaults to ~0.8).
- Measure prompt-eval reduction from any Phase-2 trimming; confirm calls clear
  the 15s timeout under 6-ship concurrency.

### Phase 5 — Map reasoning depth across the model ladder (the goal-#2 result)
- With a coherent prompt, run the full eval across the ladder and document
  where genuine tactical reasoning holds vs thins. That table *is* the
  multi-tier finding — not "we forced the weak one to comply."

---

## 5. Backlog (lower priority; from the AI-stack review)

- **Fleet-tier adaptation:** the cloud commander took ~2 min to re-task the
  surviving escort after `red-a-dd-01` was lost. Prompt nudge to react to ship
  losses / re-assign escorts.
- **Hallucinated note coords → 0.85 confidence:** `fleet_fused_contacts` are
  regex-scraped from fleet free-text notes and promoted to 0.85, can trigger
  ENGAGE. Brittle.
- **Prompt schema drifts:** `radio_discipline` defaults to `"restricted"`
  (outside its own `strict|normal|relaxed` schema); `journal_entry`
  contradiction (fleet system vs user prompt); `fleet_commander_user.md`
  BEHAVIOR section is copy-pasted ship-captain text.
- **Sensor info-boundary (goal #3):** visual contacts hand the AI exact
  range/side/class with zero error — removes cerebral uncertainty. Inject error.
- **3 visual-detection xfail tests** need rewriting to drive the real
  `loop.py` visual layer (currently mock the orchestrator's passive-only path).
- **Engine robustness:** no retries (Ollama/OpenAI); OpenAI has no client-side
  timeout + hardcoded temperature 0; `_extract_json` ignores string context;
  all event timestamps are wall-clock, not sim-time (hurts post-game analysis).

---

## 6. How to resume

**Servers** (Ollama may still be running from this session):
```sh
# Ollama (ship tier) — parallel handling so concurrent ship calls don't serialize
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=4 OLLAMA_KEEP_ALIVE=30m ollama serve &

# App server — config uses load_dotenv(no override), so shell env wins over .env
cd sub-bridge && AI_SHIP_MODEL=gemma3:latest MISSION_ID=interdict_dual_convoys \
  ../.venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000
# Browser → http://localhost:8000  (stations: /captain /helm /sonar /weapons /plot)
```

**Mission for validation:** `interdict_dual_convoys` (has `ship_roles`,
`task_groups`, AND `ship_behaviors` — exercises role doctrine + fleet
coordination + critical orders).

**Replay technique (the key tool):** every AI call's full prompt is logged to
SQLite. Extract `api_call_debug.system_prompt` / `user_prompt` from
`ai.run.ship` events in `sub-bridge/sub-bridge.db`, POST to Ollama
`/api/chat`. This gives deterministic before/after on a frozen scenario
without a full play session. Production sends NO temperature/format.

**Post-game log query:**
```sh
sqlite3 sub-bridge/sub-bridge.db \
 "SELECT created_at,type,substr(payload,1,160) FROM event \
  WHERE type LIKE 'ai.%' ORDER BY id DESC LIMIT 40;"
```
Note: tests write to the same DB — filter by `run_id`.

**Config:** `.env` has `AI_FLEET_ENGINE=openai` (gpt-4o-mini),
`AI_SHIP_ENGINE=ollama` (`ministral-3:3b` by default — we ran `gemma3:latest`
via env override). Models pulled locally include gemma3, ministral-3:3b/8b,
qwen2.5:14b, llama3.1:8b.
