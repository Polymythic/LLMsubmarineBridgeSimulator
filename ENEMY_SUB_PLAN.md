# Assessment & Plan: Enemy Submarine Ship Type

## Context

The simulator currently has three ship types: SSN (player submarine), Convoy, and Destroyer. All RED ships are surface vessels controlled by a two-tier AI (Fleet Commander + Ship Commander). The user wants to add an enemy submarine — a fundamentally different opponent that operates independently, exploits depth and stealth, and needs a more capable AI model. This assessment evaluates the level of effort and proposes a phased implementation.

## What Already Works (No Changes Needed)

The codebase is more ready for this than it might seem:

- **Physics**: `integrate_kinematics()` handles depth changes for any ship — enemy subs can dive today
- **Passive sonar**: Full underwater acoustics model with thermocline shadow zones, speed-indexed source levels, and automatic "Submerged Contact" vs "Surface Contact" classification
- **Torpedoes**: `try_launch_torpedo_quick()` works for any ship with `has_torpedoes=True` — PN guidance, seeker acquisition, all generic
- **Countermeasures**: `try_deploy_countermeasure()` works for any ship with countermeasures in capabilities
- **Damage system**: Compartment-based flooding/hull damage is fully ship-agnostic
- **Ship spawning**: `apply_mission_to_world()` resolves class from catalog, deep-copies defaults — adding a catalog entry "just works"

## What's Broken / Missing

### Critical Bugs (Must Fix)

1. **Torpedo self-destruct hardcoded to "ownship"** — `weapons.py:114-147`
   - `step_torpedo()` references `world.ships["ownship"]` for arming distance AND self-destruct proximity
   - Enemy sub torpedoes currently arm based on distance from *the player* (wrong) and self-destruct near *the player* (also wrong — should protect the *firing* ship)
   - **Blast radius**: Affects every torpedo in every mission

2. **AI `deploy_countermeasure` not handled in orchestrator path** — `loop.py:999-1154`
   - The `_ship_job` AI tool handler covers `set_nav`, `fire_torpedo`, `drop_depth_charges`, `launch_torpedo_quick`, `active_ping` — but NOT `deploy_countermeasure`
   - Enemy sub AI can request countermeasures but nothing happens
   - The handler exists in the `handle_command`/`ai.tool` path (line 2436) but not in the orchestrator's automatic execution path

3. **Literal type constraints** — `models.py:130,227`
   - `ShipDef.ship_class: Literal["SSN", "Convoy", "Destroyer"]` and `Ship.ship_class` — Pydantic will reject any new class name until these are extended

### Architectural Gaps

4. **No submarine-specific AI prompt** — `ship_commander_system.md` is written entirely for destroyer escort/ASW doctrine. No mention of depth tactics, thermocline exploitation, evasion, or stealth
5. **No per-class AI engine selection** — one `_ship_engine` for all RED ships. User wants "more capable model" for enemy sub
6. **No incoming torpedo awareness in AI context** — ship commander summary doesn't include hostile torpedoes heading toward the ship. Enemy sub has no way to know it should evade
7. **Ship commander fully dependent on Fleet Commander** — receives fleet intent, follows objectives. No independent operation mode

---

## Phased Plan

### Tier 1: Minimum Viable Enemy Submarine (1-2 days)

**Goal**: An enemy sub exists in the world, moves in 3D, is detectable on sonar, and can fire torpedoes. Uses existing AI (stub or same ship commander prompt).

| # | Change | File(s) | Effort |
|---|--------|---------|--------|
| 1a | Add `EnemySSN` to ship catalog | `assets/ships/catalog.json` | 30 min |
| 1b | Extend `ship_class` Literal types | `models.py` (lines 130, 227) | 10 min |
| 1c | Fix torpedo self-destruct to use torpedo's `side` | `weapons.py` (lines 114-147) | 1 hr |
| 1d | Add `EnemySSN` to sonar classification | `sonar.py` (`_classify_ship_passive`) | 10 min |
| 1e | Add `EnemySSN` to captain identification | `commands.py` (`_captain_identify`) | 10 min |
| 1f | Add `deploy_countermeasure` to AI tool handler | `loop.py` (after line ~1090) | 30 min |
| 1g | Create test mission with enemy sub | `assets/missions/enemy_sub_training.json` | 30 min |

**Catalog entry sketch** (EnemySSN):
- Hull: max_depth=300m, crush_depth=600m, max_speed=28kn, quiet_speed=5kn
- Acoustics: {5kn: 112dB, 10kn: 120dB, 15kn: 132dB} — slightly louder than player SSN
- Weapons: 4 tubes, 8 torpedoes, 4 noisemakers, 2 decoys
- Capabilities: nav, active sonar, torpedoes, countermeasures (no depth charges)

**Torpedo fix approach**: The torpedo dict already carries `"side"` (e.g., "BLUE" or "RED"). Replace the hardcoded `world.ships["ownship"]` lookup with: find the nearest same-side ship to use for arming distance and self-destruct proximity. This protects the firing ship (and its allies) without breaking existing behavior for player torpedoes.

**What Tier 1 unlocks**: Enemy sub appears on sonar as "Submerged Contact", navigates at depth, fires torpedoes at the player, is killable. Thermocline gameplay works (both subs can exploit the layer). Basic AI behavior via stub engine or existing ship commander.

**What Tier 1 does NOT give you**: No submarine tactics, no evasion, no stealth awareness, not independent from fleet commander, same AI model as destroyers.

---

### Tier 2: Independent AI with Submarine Doctrine (5-8 days)

**Goal**: The enemy submarine operates independently with a purpose-built submarine commander AI, running on a more capable model, with tactical awareness of threats.

| # | Change | File(s) | Effort |
|---|--------|---------|--------|
| 2a | Write submarine commander system prompt | `ai/sub_commander_system.md` (new) | 2-3 days |
| 2b | Add per-class engine selection | `ai_orchestrator.py`, `config.py` | 1 day |
| 2c | Route prompt by ship class | `ai_orchestrator.py` (`run_ship`) | 30 min |
| 2d | Build submarine-specific context summary | `ai_orchestrator.py` (new `_build_sub_summary`) | 1 day |
| 2e | Omit fleet intent for independent subs | `ai_orchestrator.py` (`_build_ship_summary`) | 30 min |
| 2f | Add submarine-specific decision cadence | `loop.py`, `config.py` | 30 min |
| 2g | Inject incoming torpedo awareness | `loop.py` (before `run_ship`) | 1 day |
| 2h | Create 2-3 scenario missions | `assets/missions/` (new files) | 1 day |

**Submarine commander prompt** (the biggest effort — 2a):

This is where gameplay quality lives or dies. Key doctrine sections:
- **Patrol**: Quiet speed, random depth changes, maintain search pattern
- **Detection & Classification**: Close on bearing, use bearing rate for TMA, passive-only by default
- **Attack**: Set up at optimal range (2-4km), fire 2-torpedo salvo, then immediate evasion
- **Evasion**: On incoming torpedo or active ping -> deploy countermeasure, go deep below thermocline, go silent, change course 90-120 degrees, clear datum
- **Depth Management**: Exploit thermocline layer, never go above periscope depth unless necessary, respect crush depth
- **EMCON**: No active sonar unless terminal attack. Active pings reveal position.

**Per-class engine config** (2b):
```
AI_SUB_ENGINE=openai    AI_SUB_MODEL=gpt-4o
AI_SHIP_ENGINE=ollama   AI_SHIP_MODEL=mistral-7b
```
In `run_ship()`, check `ship.ship_class == "EnemySSN"` -> use sub engine/model.

**Submarine context summary** (2d) — extends ship summary with:
- Own depth relative to thermocline (above/below/at layer, distance to layer)
- Incoming torpedo threats (hostile torpedoes within 4000m heading toward ship)
- Recent active ping detections (was the sub pinged recently?)
- Noise state awareness (current speed vs quiet speed — "you are detectable")
- Countermeasure inventory remaining

**Independence from fleet commander** (2e):
- Omit `fleet_intent` from submarine's context -> prompt says "you operate independently"
- Fleet commander still knows the sub exists (for awareness) but does not issue it objectives
- Sub has its own decision cadence: 5s alert / 10s normal (vs 10s/20s for surface ships)

**Incoming torpedo awareness** (2g):
- Before each `run_ship()` for EnemySSN, scan `world.torpedoes` for hostile torpedoes within ~4000m heading roughly toward the ship
- Inject as `incoming_threats: [{bearing, range_est, closing_speed}]` in context
- This enables the AI to react with evasion doctrine

**What Tier 2 unlocks**: A genuine submarine opponent that stalks the player using passive sonar, attacks with torpedo salvos, evades counterattack by going deep and deploying countermeasures, and exploits the thermocline layer. Uses a more capable model than surface ships. Operates on its own without fleet commander micromanagement.

---

### Tier 3: Campaign-Ready Full Feature (8-15 days)

**Goal**: Full-depth enemy submarine with behavioral variety, silent running mechanics, and multiple scenario types.

| # | Change | File(s) | Effort |
|---|--------|---------|--------|
| 3a | Silent running noise mode | `models.py`, `sonar.py`, `noise.py` | 2 days |
| 3b | Multi-tool decisions per AI cycle | `ai_orchestrator.py`, `loop.py` | 2 days |
| 3c | Contact memory / TMA module | `ai_orchestrator.py` (new class) | 2 days |
| 3d | AI personality variants | `ai/sub_commander_*.md` (new files) | 2 days |
| 3e | Wolfpack coordination (multi-sub) | `ai_orchestrator.py` | 3 days |
| 3f | Mission variety (duel, wolfpack, hunter-killer, escape) | `assets/missions/` | 2 days |
| 3g | UI refinements for submerged threats | `static/sonar.html`, `static/weapons.html` | 2 days |

**Silent running** (3a): Add `noise_mode: "normal" | "silent" | "cavitating"` to Ship model. Silent = -6dB source level (much harder to detect), Cavitating = +10dB (high speed). New AI tool `set_noise_mode` or parameter on `set_nav`.

**Multi-tool** (3b): Let submarine AI return multiple actions per cycle (e.g., "fire torpedo AND deploy countermeasure AND change depth"). Parse array of tool calls, execute sequentially.

**AI personalities** (3d): Mission JSON selects personality:
- Aggressive: closes fast, attacks early, accepts risk
- Cautious: stalks longer, waits for perfect shot, prioritizes survival
- Wolfpack: coordinates with other enemy subs

**Can be safely deferred past Tier 3**:
- Per-ship mast state (periscope/ESM for enemy sub) — not needed for core gameplay
- Crush depth failure mode in damage.py — enemy subs won't exceed crush depth if AI prompt is correct
- Visual/periscope detection by enemy sub — sonar is sufficient

---

## Risk Assessment

### Highest Risk: Submarine Commander Prompt Quality (Tier 2)
The quality of the enemy sub AI depends almost entirely on the system prompt. A bad prompt produces a submarine that sprints at full speed, pings active sonar, and fires at max range — essentially a dumb destroyer underwater. The prompt requires iterative testing with real LLM calls. The `ship_behaviors` field in mission JSON provides a safety valve for overriding bad behavior per-mission.

### Highest Blast Radius: Torpedo Self-Destruct Fix (Tier 1)
`step_torpedo()` runs every tick for every torpedo in every mission. The fix must be correct and handle edge cases (torpedo with no matching same-side ship, etc.). Test with existing missions before and after to verify no regression.

### Medium Risk: Per-Class Engine Selection (Tier 2)
Introduces different API costs and latency profiles for different ship classes. If the "more capable" model (e.g., GPT-4o) has higher latency, the 5-second decision cadence may not work well. Fallback: increase cadence for that model.

### Low Risk: Everything Else
Catalog entry, Literal types, sonar classification, mission files — all straightforward and isolated changes.

---

## Recommended Implementation Order

1. **Tier 1 first** — gets an enemy sub in the world fast, validates all mechanical systems work
2. **Tier 2a (prompt) in parallel** — start writing and testing the submarine commander prompt while Tier 1 code is built
3. **Rest of Tier 2** — wire up the AI infrastructure once the prompt exists
4. **Tier 3** — only after Tier 2 gameplay is validated

## Verification

- **Tier 1**: Load enemy sub mission -> see "Submerged Contact" on sonar -> enemy fires torpedoes -> player can destroy enemy sub -> no torpedo self-destruct bugs
- **Tier 2**: Enemy sub patrols silently -> detects player -> closes for attack -> fires salvo -> evades counterattack by diving and deploying countermeasures -> uses thermocline
- **Tier 3**: Multiple enemy subs coordinate -> different personalities produce different behaviors -> silent running dramatically reduces detectability
