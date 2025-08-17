# Player Guide

Welcome aboard. This is a cooperative submarine bridge simulator. Five stations must coordinate under time pressure, imperfect information, and EMCON constraints.

## Captain
- Responsibilities:
  - Overall mission intent and ROE adherence
  - Grant firing consent (time-limited window)
  - Raise/lower periscope and radio masts (EMCON and noise)
  - Monitor aggregate ship noise (dB) and EMCON risk; keep the team under thresholds
  - Coordinate courses, speeds, and tactics via voice comms
- UI Elements:
  - Aggregate Noise (dB) bar with peak-hold (Captain cannot see per-station noise)
  - EMCON indicator (low/med/high)
  - Periscope contacts list (precise bearing/range/type when shallow and scope up)
  - Mission brief and ROE; comms panel for scheduled radio messages when radio mast up
- Tips:
  - Keep masts down when not needed; each raised mast adds noise and EMCON risk
  - Enforce quiet periods during sensitive phases (intercepts, evasion)

## Helm
- Responsibilities:
  - Drive the ship: ordered heading, speed, depth
  - Manage cavitation (spikes noise and detectability), thermocline tactics, and ballast
- UI Elements:
  - Heading dial with ordered vs actual markers
  - Depth ladder and speed telegraph
  - Cavitation and thermocline indicators
  - Helm Noise (dB) bar with peak-hold (station-only)
  - Maintenance panel (e.g., rudder lubrication, linkage adjustments)
- Tips:
  - Avoid cavitation by keeping speeds under the cav threshold at current depth
  - Small course corrections can be quieter than aggressive maneuvers

## Sonar
- Responsibilities:
  - Track passive contacts; initiate active ping when authorized
  - Manage classification and bearing confidence; report fused bearings
- UI Elements:
  - Passive contacts list (bearing-only, noisy)
  - Active ping responses (bearing, range-est, strength)
  - Waterfall (bearing vs time) with overlays:
    - Own ping (thin cyan)
    - Explosions (bold orange) for depth charge detonations
  - Sonar Noise (dB) bar with peak-hold (station-only)
  - Maintenance panel (DSP self-test, hydrophone calibration/servo)
- Tips:
  - Multiple bearings over time improve fusion; avoid unnecessary active pings
  - Use voice comms to coordinate bearings with Weapons and Helm

## Weapons
- Responsibilities:
  - Tube operations: load → flood → doors → fire
  - Maintain interlocks; coordinate consent with Captain
  - Employ torpedoes and depth charges as appropriate
- UI Elements:
  - Tube states and timers; permissives (Flood/Doors/Fire)
  - Weapons Noise (dB) bar with peak-hold (station-only)
  - Quick test-fire (dev/testing)
  - Maintenance panel (tube hydraulics, etc.)
- Tips:
  - Loading/flooding/doors generate sustained noise; plan tube prep during higher ambient noise
  - Confirm bearing solutions with Sonar and Fleet guidance

## Engineering
- Responsibilities:
  - Manage reactor output, SCRAM, pumps, and damage control
  - Allocate power across Helm/Weapons/Sonar/Engineering
  - Oversee maintenance tasks; prevent system degradation
- UI Elements:
  - Reactor MW, SCRAM toggle, pump controls
  - Power allocation sliders and health pips for systems
  - Engineering Noise (dB) bar with peak-hold (station-only)
  - Maintenance panel with task stages (task/failing/failed)
- Tips:
  - High reactor MW raises baseline ship noise; balance propulsion needs vs EMCON
  - Prioritize loud maintenance tasks when stealth is less critical

## Noise and EMCON
- Each station’s activities add to a station-only dB meter; the Captain sees only the aggregate ship dB.
- Sources include reactor MW, pumps, tube operations, cavitation, maintenance tasks, and raised masts.
- Noise meters jitter slightly to reflect live updates; red peak-hold shows recent spikes.
- Coordinate to keep aggregate noise within acceptable ranges; EMCON risk rises with sustained high noise.

## Maintenance
- Tasks appear by station; each has a stage (task/failing/failed) and raises noise accordingly.
- Repairing tasks may reduce station capabilities and timers temporarily; plan operations accordingly.

## Communication
- No single station has the full picture. Voice comms are essential.
- Captain sets priorities; Helm and Weapons coordinate geometry; Sonar reports bearings; Engineering balances noise and power.

Fair winds and following seas.
