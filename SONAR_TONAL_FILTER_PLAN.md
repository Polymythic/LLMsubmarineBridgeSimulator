# Sonar Tonal Filter — narrowband ID cards + analog Min/Max band

**Created:** 2026-06-27. **Status:** design agreed; not yet built.

Make the sonar station a skill seat. Today every contact is bearing-only,
SNR-based — there is **no spectral model anywhere** (`broadband_sig` exists and
is unused). This adds a frequency dimension the operator *works*: each class has
a tonal signature ("ID card"), and a tunable Min/Max passband decides how
brightly each contact paints on the DEMON waterfall. Serves `PROJECT_OVERVIEW
§1.1` — **inform, don't neuter**: the filter is an operator *display* tool, it
never deletes a contact from the world, other stations, or tracking.

---

## 1. Player mechanic

- Each ship/torpedo class has **N tonal lines** (default N=5) at fixed
  frequencies — its acoustic fingerprint.
- A reference **ID-card browser** sits under the waterfall: arrows cycle the
  *class* cards (Destroyer → Torpedo → Convoy → SSN → …) so the operator can see
  where each class's lines live. **The card library is anonymous of live
  contacts** — it tells you what a Destroyer *looks like*, not that contact-3
  *is* one. Matching observed glow to a card is how you infer class; the game
  never hands you the answer.
- One **Min/Max passband** (a single contiguous window) gates the whole scope.
- A contact's brightness = its distance-limited strength **×** the fraction of
  its own lines that fall inside the band. Capture all lines → full (distance)
  brightness. Capture some → proportionally dimmer. Capture none → it vanishes.
- **The trade-off is the game.** Narrow the band to declutter and you dim your
  target and may lose its weaker lines; widen it to brighten your target and you
  also satisfy *other* classes' lines, so neighbors fade in. Wide-open band =
  today's full waterfall.

**Classification — two independent IDs:**

- **Captain visual ID** (periscope, already in `contact_registry`): authoritative
  and correct → the contacts table shows the true type. Nothing to build beyond
  surfacing it clearly in the table.
- **Sonar operator lock:** the operator can tag a contact with a class *they*
  judge from the tonals — by assigning the currently-shown reference card to the
  selected contact. It **persists and may be wrong**; the sim never corrects or
  blocks it. Shown as e.g. `Destroyer (sonar)` to mark an unconfirmed acoustic
  call vs a confirmed visual one. This is the operator's job made consequential:
  their belief — right or wrong — is what the crew acts on.

---

## 2. The math

Per contact, per frame (computed client-side for instant drag response):

```
brightness = base_detectability(distance, SNR, ambient)  ×  ( Σ_i w_i / N )

w_i = analog weight of line i against the band [Min, Max]
```

`base_detectability` is the existing 0..1 `detectability` from `sonar.py` — it
already encodes distance/SNR/ambient, so "max brightness based on distance"
needs no new code.

**Rolloff — no quantized step.** Each line's weight ramps smoothly 1→0 across a
soft skirt of width `S` Hz at each band edge, so a line sitting on the Max edge
contributes ~0.5 and fades as you drag past it. With smoothstep at each edge:

```
w_i = smoothstep((f_i - (Min - S/2)) / S)  ×  (1 - smoothstep((f_i - (Max - S/2)) / S))
```

→ `w_i ≈ 1` comfortably inside, `0.5` exactly on an edge, `0` comfortably
outside. `S` is the single "analog softness" knob — bigger `S` = mushier,
more forgiving tuning. (Lines are equal-weight for now per "each line is 1/N";
a future `level_db` per line could make loud lines count more.)

---

## 3. Content design — the cards (FIRST DRAFT, needs playtest)

Difficulty lives entirely here: deliberately cluster some lines across classes
so no narrow band is ever perfectly clean, while leaving each class one or two
**discriminator** lines so skilled ID stays *possible*. Ruler: **0–15 kHz**.

| Class      | Lines (kHz)                  | Discriminator(s) | Shared / decoy clusters |
|------------|------------------------------|------------------|-------------------------|
| SSN        | 0.9, 2.1, 4.0, 6.3, 8.5      | 6.3              | ~1k, ~2k, ~4k, ~8.5k    |
| Destroyer  | 2.0, 3.8, 5.0, 9.2, 12.0     | 9.2              | ~2k, ~4k, 5k↔torp, 12k↔torp |
| Convoy     | 1.0, 2.2, 3.9, 7.5, 11.0     | 7.5, 11.0        | ~1k, ~2k, ~4k           |
| Neutral    | 1.1, 4.1, 6.0, 9.0, 13.5     | 13.5             | ~1k, ~4k, ~6k, ~9k      |
| Torpedo    | 2.0, 5.0, 8.8, 12.0, 14.5    | 14.5 (seeker)    | ~2k, 5k↔dd, 8.8k, 12k↔dd |

Deliberate collisions: a band around **2 kHz** lights up almost everyone (the
classic ambiguous low band); **5.0** and **12.0 kHz** are exact Destroyer↔Torpedo
ties — terrifying when you're trying to tell an inbound fish from its launching
escort. The clean tells: Convoy's **7.5/11.0**, the torpedo's **14.5** seeker
line, Neutral's lone **13.5**.

**Broadband emitters** (noisemakers, explosions) carry energy across the *whole*
ruler rather than discrete lines → fraction ≈ 1 in any band → they cannot be
filtered out. **Confirmed all-pass.** Realistic (broadband jamming defeats
narrowband tuning), and they're short-lived — noisemaker/explosion lifetimes are
seconds — so they clutter briefly and clear on their own. No notch escape.

---

## 4. Implementation surface

Small and contained — sits on top of the existing SNR model, touches no
detection logic.

1. **`models.py`** — add to `Acoustics` (~line 31, replacing dead `broadband_sig`
   intent): `tonal_lines: List[float] = []` (kHz). Add the same passthrough to
   `TelemetryContact` (~line 247): `tonalLines: Optional[List[float]] = None`.
2. **`assets/ships/catalog.json`** — author `acoustics.tonal_lines` per class
   (the §3 table). Torpedo lines: torpedoes aren't catalog ships, so seed their
   card in `passive_projectiles` (`sonar.py:211`).
3. **`sonar.py`** — in `passive_contacts` / `passive_projectiles` /
   `countermeasure_contacts`, copy the source's `tonal_lines` onto the emitted
   `TelemetryContact`. Broadband sources emit a sentinel (e.g. `["broadband"]`
   or an explicit wide set) so the client treats them as all-pass. No filtering
   happens server-side — we ship the lines and let the UI gate brightness.
4. **`loop.py`** — none beyond `c.dict()` already carrying the new field through
   `tel_sonar` (~line 1674).
5. **`sonar.html`** — the UI (§5) + multiply each contact's draw alpha/intensity
   by the computed fraction in `drawWaterfall()` (~line 219). Min/Max + selected
   card are pure client state (no round-trip); the **classify lock** does need a
   command (item 6).
6. **`commands.py` + `contact_registry.py`** — new `sonar.classify` command
   `{contactId, shipClass}` → store an `operator_class` on the registry entry,
   **separate from** `identified_class`. `passive_contacts` surfaces it in
   `classifiedAs` as `<class> (sonar)` when set and the contact isn't
   captain-identified; captain visual ID supersedes it. Occasional action, not
   per-frame — a round-trip is fine.

**No server round-trip** for the band/brightness — it lives in the browser.
*(Caveat: an LLM sonar operator (goal #4, lowest priority) would need the
fraction computed server-side to "see" the filtered scope. Deferred.)*

---

## 5. UI (per the mockup)

- **Frequency strip** beneath the waterfall, **its own X-axis** — frequency, with
  explicit ticks (0 / 5k / 10k / 15k Hz), a visibly different background and a
  gap from the bearing waterfall above, so nobody reads a tonal at "bearing 270."
- **ID-card browser:** ◀ [Class name] ▶ cycles reference cards; the strip draws
  that card's lines.
- **Min/Max band:** draggable edge handles on the strip with a live Hz readout.
  Rotary Min/Max knobs optional/secondary (kept in sync if we add them).
- Waterfall contacts dim/brighten live as the band moves.

---

## 6. Decisions — defaults chosen, veto any

- **N = 5, equal-weight lines.** Per "each line is 1/5."
- **Ruler 0–15 kHz.** Fits the draft cards (covers the 12k DD/torpedo tie and
  14.5k seeker).
- **Skirt `S`** — start by feel (~a few hundred Hz); tune in playtest.
- **Display-only, client-side.** Filter scales waterfall brightness only; the
  contacts table and all other stations see every contact unchanged.
- **Selector cycles class cards, not live contacts.** Preserves no-leak ID.
- **RESOLVED — broadband all-pass.** Noisemakers/explosions can't be
  narrowbanded out; short-lived, they clear on their own.
- **RESOLVED — own units & decoys aren't auto-filtered (acoustic, not special-
  case).** Driven entirely by what `sonar.py` emits in `tonalLines`, so the
  client needs no per-type carve-out: **own torpedoes** emit `None` (all-pass —
  never dim your own fish off your own scope; *enemy* torpedoes keep the card so
  the 14.5 kHz seeker line still IDs an inbound). **Noisemakers** emit `None`
  (full-spectrum broadband). **Decoys** emit the **submarine card**
  (`SUB_TONAL_LINES`, mirror of the SSN catalog entry; falls back from the
  observing sub's own `acoustics.tonal_lines`) so a decoy reads like a sub and
  survives a sub-hunt passband instead of vanishing — the same deception it
  plays on a torpedo seeker. No schema change: existing `tonalLines`/`None`
  all-pass path carries all of it.
- **Contacts table shows class when known:** the true type once captain-ID'd,
  the operator's `(sonar)` lock when set, plus in-band line count as a soft ID aid.
- **Operator lock is unvalidated.** A wrong tag is allowed and persists — that's
  the point.
- **RESOLVED — captain ID overrides.** Captain visual ID supersedes the operator
  lock outright (he can *see* it) — show only the corrected truth, no side-by-side.
- **RESOLVED — plot board stays manual.** The `(sonar)` lock never auto-flows to
  the shared plot; the crew must relay and mark it by hand. Manual relay *is* the
  inter-player drama, by design — a miscommunicated or wrong call is on the crew.

---

## 7. Tests

- Unit: `fraction()` — all-in → 1.0; none-in → 0.0; one-of-five → ~0.2; a line
  exactly on an edge → ~0.5 (rolloff); broadband sentinel → 1.0 in any band.
- Unit: brightness = base × fraction; wide-open band reproduces today's
  detectability exactly (backward-compat guard).
- Content: every class shares ≥1 line with another (challenge invariant) AND
  owns ≥1 discriminator (identifiability invariant) — assert over the catalog so
  card edits can't accidentally make a class trivially or impossibly ID-able.
- Lock: `sonar.classify` sets `operator_class`; `classifiedAs` becomes
  `<class> (sonar)`; a wrong lock is accepted (no validation against truth); a
  later captain ID overrides it.

---

## 8. Build order

1. ~~`Acoustics.tonal_lines` + `TelemetryContact.tonalLines` + catalog cards +
   passthrough in `sonar.py`. Verify lines arrive in `/sonar` telemetry.~~
   **DONE** — verified Destroyer/torpedo cards in telemetry, broadband = None
   (all-pass), suite green.
2. ~~Frequency strip + card browser + Min/Max handles in `sonar.html` (read-only:
   draw cards, no brightness effect yet).~~ **DONE** — reference card library
   served via `tel_sonar.tonalCards` (from catalog + torpedo); strip draws the
   selected card's lines + a draggable Min/Max passband (full-open default).
   No brightness effect yet. Suite green (255/6xf).
3. ~~Wire the fraction into `drawWaterfall()` brightness.~~ **DONE** —
   `bandFraction()`/`lineWeight()` in `sonar.html` scale each contact's glow by
   the smoothstep-weighted fraction of its `tonalLines` inside the band (skirt
   `TONAL_SKIRT_KHZ = 0.4`). Display-only (local copy, never mutates the
   contact); broadband/None = all-pass; wide-open band reproduces today's
   waterfall. Still TODO: playtest the card set; tune frequencies and `S` for
   feel.
4. **Operator lock** (independent of 1–3): `sonar.classify` command +
   `operator_class` on the registry + `(sonar)` surfacing in `classifiedAs` +
   "assign card to selected contact" button in the UI. Confirm the table shows
   captain-ID truth, sonar locks, and in-band line count.
5. Tests (§7). Then revisit the two OPEN items (visual-conflict, plot
   propagation) with real play data.

---

## 9. Roster expansion — many more signatures (DONE 2026-06-28)

Enriched `assets/ships/catalog.json` from 4 generic entries to **22**: the four
generic archetypes (kept for back-compat — existing scenarios reference them, and
they serve as "unknown <category>" reference cards) plus **18 specific
late-1960s–early-1980s types**, Soviet + European. Each carries its own 5-line
tonal card. Categories are unchanged — every entry's `ship_class` is still one of
`SSN / Convoy / Destroyer / Neutral`; the *type* (catalog key + `name`) is what
specifies the tonals (no new category, no Literal change).

- **Subs (SSN category):** Victor, Charlie, Alfa, November, Foxtrot (USSR);
  Oberon (RN), Daphne (FR). Diesel boats live under the SSN category since it's
  the only sub category — acceptable per "don't mess with categories."
- **Surface (Destroyer category):** Kashin, Kresta II, Krivak, Kotlin, Petya
  (USSR); Leander, Type 42 (RN), Georges Leygues (FR), Köln (FRG), Lupo (IT).
- **Auxiliary (Convoy category):** Boris Chilikin AOR — a deliberate
  merchant-mimic (no discriminator; hides among the Convoy card).

Tonal content honors §3: every card shares ≥1 line (no perfectly-clean band) and
every *identifiable* type owns ≥1 discriminator. The generic archetypes and the
AOR mimic are intentionally non-discriminable. Asserted in
`tests/test_ship_tonals.py`.

**Per-type torpedoes.** Torpedoes now carry distinct cards by model
(`sonar.TORPEDO_TONAL_CARDS`: Mk48, 53-65, SET-65, Tigerfish). `WeaponsSuite`
gained `torpedo_type` (loaded from catalog `weapons.torpedo_type`); NPC
quick-launch fires the platform's own model (Soviet hulls → 53-65/SET-65, RN →
Tigerfish), so an inbound fish reads its type on the filter. Own fish stay
all-pass (§6 own-units decision); enemy fish look up their card by name (unknown
→ Mk48 fallback). The reference library serves one card per torpedo model.

**Merchant/neutral variety (for the scenario refresh).** Added Poltava + Krym
(Convoy) and Trawler + Coaster (Neutral) so convoys/neutrals diversify too —
roster now **26** entries.

## 10. Exact-type identification — foundation + captain (DONE 2026-06-28)

The richer roster only pays off if stations can resolve a contact to the exact
hull, not just the category. Done so far:

- **Foundation:** `Ship.ship_type` (the catalog key, e.g. `"Krivak"`) is now
  persisted at spawn (`apply_mission_to_world`), alongside the broad
  `ship_class` category. Previously the specific type was discarded at spawn, so
  nothing in the world knew a hull "was a Krivak."
- **Captain:** periscope visual ID (`captain.identify_contact`) is authoritative
  and now names the exact hull as **"Category - Class"** (e.g.
  "Destroyer - Krivak-class", "SSN - Victor-class"). Catalog `name` fields are the
  short class label only (no hull-code/project-number/nation cruft). A generic
  archetype (catalog key == category) or an ad-hoc ship with no catalog type
  shows just the category. Flows to all stations via the existing
  `classifiedAs`/registry path.
- **Plot:** already supports it — `PlotContact.label` is free text, so the
  plotter writes whatever type is relayed; `type` stays the color enum.
- **Scenarios refreshed:** all 7 missions remapped from generic classes to
  specific Soviet (RED) / civilian (NEUTRAL) types, with per-scenario rotation
  for variety. Only `class` values changed (ids/routes/triggers untouched).
  No RED submarines exist in current scenarios, so the sub cards enrich the
  reference library but don't yet appear in play.

**Still open — sonar operator exact-type lock (plan step 4, deferred):** the
sonar operator can't yet *select* an exact type from the tonals (a fallible
`operator_class`). The registry already stores free-string classes, so it's
ready; it needs the `sonar.classify` command + an "assign card" affordance in the
tonal browser.
