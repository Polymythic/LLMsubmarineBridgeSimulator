## Passive Sonar Enhancements

### Goals
- Make passive detection feel dynamic and readable: louder ships and higher speeds are easier to detect; baffles and layers matter; DEMON shows signal intensity.

### Model (lightweight)
- Source Level (SL): per ship class vs speed curve (dB). Cavitation adds a step increase.
- Transmission Loss (TL): 20·log10(range) dB. Optional layer attenuation (+~4 dB when thermocline in effect).
- Ambient: fixed base plus station penalties.
- SNR = SL − TL − Ambient − penalties. Map to [0..1] detectability with a soft knee.
- Bearing noise sigma decreases with SNR; confidence increases with SNR/time.

### Telemetry
- `contacts[]`: add `detectability`, `snrDb`, `bearingSigmaDeg`.
- Debug ships: `passiveDetect`, `slDb`, `tlDb`, `ambientDb`, `inBaffles`, `layerAttenDb`.

### UI
- Debug page: display per-ship `passiveDetect` (%) with colored pip and tooltip breakdown.
- Sonar DEMON: intensity of each contact stripe uses `detectability` (fallback to strength).

### Acceptance
- Faster target → higher detectability at same range.
- Farther target → lower detectability; beyond threshold disappears.
- In baffles → no contact.
- Cavitation (later) clearly increases detectability.

### Notes
- Keep computations cheap per tick; derived metrics only.
- Default curves are approximate and tuned for fun, not physics accuracy.


