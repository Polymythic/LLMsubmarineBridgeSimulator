## Torpedo System Proposal — 2025-08-12

### Goals
- Provide a clear, fun, and teachable torpedo workflow suitable for Mission 1 (slow surface contact), while laying a path to deeper mechanics.
- Increase inter-station tension via consent, EMCON, maintenance states, and limited power.

### MVP Scope (Start Simply)
- Tube FSM: Empty → Loaded → Flooded → DoorsOpen → Fired → Empty.
- Fire command: tube, bearing (true), run_depth (m); enable_range_m; doctrine: "passive_then_active"; wire_guided: false.
- Kinematics: fixed speed, turn limit, seeker cone; arming distance; simple PN guidance when seeker active.
- Safety: no detonation pre‑arm; simple self‑preserve logic (avoid ownship within short range).
- Passive→Active guidance: passive home by bearing strength; switch to active in terminal or when lost.
- Consent interlock: captain grants 3–5 s window; events logged.
- Periscope spotting (Mission 1 help): precise bearing/range/class when at periscope depth with scope raised.

### Tube Lifecycle & Interlocks
- Preconditions: cannot open doors unless Flooded; cannot Fire unless DoorsOpen and consent valid.
- Timers: reload/flood/doors scale with Weapons power and maintenance; jams possible when degraded (later).
- UI feedback: per‑tube readiness flags (Flood/Doors/Fire) and timers; disabled operations with reasons.

### Fire Command Schema (MVP → expandable)
- Fields (MVP):
  - tube: int
  - bearing_true: deg (0=N/90=E)
  - run_depth_m: float
  - enable_range_m: float (arming/enable)
  - doctrine: "passive_then_active" | "BOL" (initially only passive_then_active)
  - wire_guided: bool (MVP false; later true)
- Future fields: gyro_angle, salvo_id/spacing_s, terminal_mode, search_pattern.

### Torpedo Kinematics & Safety
- Speed: fixed (e.g., 45 kn); battery runtime cap (max_run_time_s).
- Turn: capped deg/s; seeker cone (e.g., 35° half‑angle).
- Arming: distance/time before fuze live; proximity fuze radius.
- Self‑preservation: if ownship within X m at Y° sector pre‑arm → inhibit; post‑arm → self‑destruct.

### Guidance Doctrines (Roadmap)
- MVP: passive_then_active
  - Launch bearing; passive home on strongest source in cone; if lost or within terminal gate, switch to active.
- BOL: straight to enable range then seeker; surface mode later.
- Passive‑only / Active‑only for diagnostics/training.

### Wire Guidance (Next)
- Until wire cut/timeout; player can steer heading bias or reseed enable range.
- Wire cuts on sharp ownship turn, timeout, or manual cut; command dropouts when Weapons degraded.

### Countermeasures (Next)
- Noisemaker/decoy effects: false locks by SNR/geometry; periodic spoof window; re‑acquire logic.

### Environment & EMCON
- Thermocline: reduces passive SNR; shortens active range; accounted in seeker signal model (later deepen).
- EMCON hits: flood/doors/launch/active seeker; surfaced on Captain to pressure tactics.

### Mission 1: Slow Surface Contact
- Setup: single surface ship at shallow depth, slow speed.
- Player flow:
  1) Periscope depth, scope raised → confirm class, steady bearing/range.
  2) Weapons: Load → Flood → DoorsOpen; set bearing_true = scope; run_depth_m ≈ 8; enable_range_m ≈ 1500; doctrine = passive_then_active.
  3) Captain grants consent → Weapons fires.
  4) Observe guidance; impact via proximity fuze.

### UI/UX
- Weapons: per‑tube readiness, doctrine selector (locked to passive_then_active for MVP), enable_range input, clear errors.
- Captain: consent button with countdown; EMCON alert; optical contact list.
- Sonar: passive waterfall, bearing list with gating (weak lines hidden); active dots; (later) bearing‑rate and mark/lock.

### Testing & Acceptance
- Tube FSM: valid transitions and guards; timers scale with power; invalid ops rejected with reasons.
- Consent gating: fire rejected without window; accepted within window.
- Guidance: enable at range; passive then active homes to correct target; turn/arming respected; no pre‑arm detonation.
- Periscope: contacts only at periscope depth; precise bearing/range/class surfaced.

### Phased Implementation Plan
1) Tighten tube FSM + consent UX and errors (MVP polish).
2) Fire schema finalize; implement passive_then_active with arming/enable and PN guidance.
3) Periscope spotting and Captain UI (done in core, expand visuals later).
4) Wire guidance (steer + cut) and doctrine UI.
5) Countermeasures and seeker susceptibility knobs.
6) Environmental deepening (thermocline, sea state), safety, and jams/misfires.


