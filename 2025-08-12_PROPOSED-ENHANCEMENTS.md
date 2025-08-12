## Proposed Enhancements — 2025-08-12

Focus: Intensify cooperative tension and immersion for five local stations (Captain, Helm, Sonar, Weapons, Engineering) while staying within the existing 20 Hz authoritative loop and WebSocket telemetry model.

### Summary (High Impact, Incremental)
- Noise Budget and EMCON meter that ties speed, pumps, cavitation, and masts to detectability.
- Captain Consent window with timeout and visible TTL for Weapons (timing pressure and comms friction).
- Active Ping consequences: counter-detection event, cooldown ring, and alarm SFX (tradeoffs under pressure).
- Shared Power Budget across propulsion, pumps, and sensors (conflicting objectives across stations).
- Tube Interlocks and Solution Quality dependent on Sonar classification (cross-station prep).
- Randomized minor faults and alarms requiring acknowledgements (background tension).
- Thermocline layer affecting acoustics and tactics (depth management gameplay).
- SCRAM load shedding with UI update impacts (emergency feel and tradeoffs).
- Ambient audio loops and distinct alarm tones (cheap immersion boost).
- Mission brief and ROE constraints visible to Captain (hidden info driving comms).

---

### 1) Noise Budget and EMCON
**Rationale:** Create constant stealth tradeoffs across Helm/Sonar/Engineering; make cavitation and pumps matter beyond numbers.

**Backend:**
- Compute `acoustics.noiseBudget` (0–100) derived from speed, cavitation flag, pumps, raised masts, ballast flow; expose `acoustics.detectability` derived from noise and environment.
- Emit events when thresholds are crossed (e.g., `noise_threshold_exceeded`).

**UI:**
- Helm: cavitation and thermocline warnings tied to noise; display a small noise indicator.
- Sonar: EMCON meter with green/amber/red zones; passive “quiet” hint when below target.
- Captain: consolidated EMCON/Noise panel (read-only) with status pips.
- Engineering: show noise cost on pump toggles and ballast operations.

**Telemetry additions:**
```json
{
  "acoustics": {
    "noiseBudget": 0,
    "detectability": 0,
    "cavitation": false,
    "emconRisk": "low|med|high"
  }
}
```

**Acceptance:** Speeding up or enabling pumps increases noise; crossing red threshold triggers audible alarm and red pip.

---

### 2) Captain Consent Window (Weapons-Hot TTL)
**Rationale:** Add timing pressure; encourage communication between Weapons and Captain.

**Backend:**
- Add `weapons.consentActiveUntil` (epoch ms). Fire attempts are rejected without valid consent.

**UI:**
- Weapons: show countdown chip; disabled Fire button when expired.
- Captain: “Weapons Free” toggle shows TTL and quick re-arm button.

**Telemetry additions:**
```json
{
  "weapons": {
    "consentActiveUntil": 0
  }
}
```

**Acceptance:** Consent expires after configured window (e.g., 60 s). Weapons view clearly reflects enabled/disabled state with TTL.

---

### 3) Active Ping Consequences + Cooldown Ring
**Rationale:** Make the decision to ping consequential; surface time pressure on repeated pings.

**Backend:**
- On ping, emit `events: [{type: "counterDetected"}]`. Optionally boost enemy aggressiveness internally (stub for now).
- Track and expose `sonar.pingCooldownMs`.

**UI:**
- Sonar: radial cooldown ring on Ping button; disable until cooldown completes; small EMCON warning flash.
- Captain: EMCON warning banner on active ping for a brief interval.

**Telemetry additions:**
```json
{
  "sonar": {
    "pingCooldownMs": 0
  },
  "events": [
    {"type": "counterDetected", "at": "ISO-8601"}
  ]
}
```

**Acceptance:** Ping triggers event; cooldown visually locks the button; audible tone plays.

---

### 4) Shared Power Budget
**Rationale:** Force explicit tradeoffs between propulsion, pumps, and sensors.

**Backend:**
- Track `engineering.powerBudgetMW` and consumers: `propulsion`, `pumps`, `sensors`.
- Clamp achievable shaft power/speed based on allocation.

**UI:**
- Engineering: budget slider and consumers list with live MW draw; quick toggles for shedding.
- Helm: show speed cap indicator when power constrained.

**Telemetry additions:**
```json
{
  "engineering": {
    "powerBudgetMW": 0,
    "consumers": [
      {"name": "propulsion", "mw": 0},
      {"name": "pumps", "mw": 0},
      {"name": "sensors", "mw": 0}
    ]
  }
}
```

**Acceptance:** Increasing pumps reduces max speed; UI reflects caps immediately.

---

### 5) Tube Interlocks and Solution Quality
**Rationale:** Require Sonar→Weapons coordination before firing; build a chain of prep steps.

**Backend:**
- Add `solutionQuality` (0–1) per tube; require ≥ threshold to fire.
- Allow Sonar classifications/marks to increase quality; degrade over time.

**UI:**
- Weapons: show per-tube quality bar; block fire under threshold with tooltip.
- Sonar: classify/mark actions that boost the active tube’s solution quality.
- Captain: small “Weapons Ready” pip indicating sufficient quality.

**Telemetry additions:**
```json
{
  "weapons": {
    "tubes": [{"id": 1, "state": "Loaded", "solutionQuality": 0.0}]
  }
}
```

**Acceptance:** Attempting to fire below threshold is rejected and explained; improving classification raises the bar.

---

### 6) Randomized Minor Faults and Alarms
**Rationale:** Introduce low-level background tension and split attention without overwhelming gameplay.

**Backend:**
- Periodically enqueue low-severity faults (e.g., “Cooling pump cavitation”, “Ballast valve sticky”). Minor penalties accrue if ignored.
- Some faults require acknowledgements from specific stations.

**UI:**
- Station-specific banner with "Acknowledge" action; global alarm until all required acks complete.

**Telemetry additions:**
```json
{
  "events": [
    {"id": "evt-1", "type": "fault", "severity": "low", "station": "engineering", "ackRequired": true}
  ]
}
```

**Acceptance:** Fault triggers klaxon and banners; silences only after required acknowledgements.

---

### 7) Thermocline Layer and Hide/Seek
**Rationale:** Reward tactical depth changes; provide a stealth tool.

**Backend:**
- Randomize a layer depth band each run; attenuate passive/active signals across the layer.
- Increase Sonar `layerConfidence` with dwell time and ping evidence.

**UI:**
- Sonar: “Layer suspected” indicator with confidence; hint based on bearings/fades.
- Helm: prompts like “Go Above/Below Layer” when beneficial.
- Captain: may see the actual band in a mission brief.

**Telemetry additions:**
```json
{
  "environment": {"layerDepthMin": 0, "layerDepthMax": 0},
  "sonar": {"layerConfidence": 0.0}
}
```

**Acceptance:** Crossing the layer reduces detectability and changes contact behavior; Sonar confidence evolves over time.

---

### 8) SCRAM Load Shedding Effects
**Rationale:** Make emergencies feel urgent and consequential without heavy backend complexity.

**Backend:**
- On SCRAM, reduce available power; optionally reduce sensor update rate and passive waterfall resolution; increase battery drain, especially with pumps.
- Expose `engineering.shedding` toggles.

**UI:**
- Engineering: shedding controls (sensors, lighting) with clear tradeoffs.
- Sonar: visibly slower waterfall or lower resolution under shedding.
- Captain: “Restricted Ops” banner when shedding is active.

**Telemetry additions:**
```json
{
  "engineering": {
    "scrammed": true,
    "batteryPct": 0.0,
    "shedding": {"sensors": false, "lighting": false}
  }
}
```

**Acceptance:** Toggling shedding changes UI behavior immediately; battery drains faster under load.

---

### 9) Ambient Audio and Alarms
**Rationale:** High immersion payoff with low development cost.

**Backend:** None beyond event hooks.

**UI:**
- Loop subtle ambient hull creaks and machinery hum per station.
- Distinct alert tones: cavitation, counter-detect, SCRAM, flooding.
- Master mute per station.

**Acceptance:** Correct SFX play on relevant events; mute works per station.

---

### 10) Mission Brief and ROE (Captain-only)
**Rationale:** Hidden constraints drive communication and friction under pressure.

**Backend:**
- Persist a mission/ROE blob with constraints (e.g., “No active ping unless fired upon”).
- Emit penalty or warning events on violations.

**UI:**
- Captain: ROE panel; other stations see minimal restriction hints (e.g., EMCON Restricted).

**Telemetry additions:**
```json
{
  "mission": {"roe": {"emcon": "restricted"}}
}
```

**Acceptance:** Violations trigger warnings/penalties; Captain can share or keep constraints private.

---

## Suggested Next Sprint (1–2 days)
Target four items that are tightly scoped and visibly impactful:

1. Noise Budget + EMCON meter (backend metrics + UI surfaces across stations).
2. Captain Consent window with TTL and Weapons countdown.
3. Active Ping consequences (counter-detect event) + cooldown ring + alarm tones.
4. Ambient audio loops + per-event alarm tones with per-station mute.

### Notes
- Maintain authoritative 20 Hz loop; keep new metrics lightweight and derived.
- Keep messages compact; only add fields noted above.
- Ensure unit tests cover consent TTL behavior, noise threshold events, and ping cooldown states.


