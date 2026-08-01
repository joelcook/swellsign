# Swell Sign backlog

Open work, roughly in the order it matters. Anything requiring the physical
panel is grouped at the bottom, since none of it can be settled from software.

## Display

- **`S` and `5` are the same glyph at a glance.** They differ by two pixels out
  of thirty-five, so `13.2S` reads as `13.25` from across the room. This is
  worse than a collision: nothing looks broken, it just shows a plausible wrong
  number, and period is precisely the reading that distinguishes a groundswell
  from windslop. Options are a redesigned `S` with an open top-left, a space
  before the unit at a cost of 6px from a tight budget, or moving the unit out
  of the numeric run entirely. `S` after digits is the only place this bites;
  `FT`, `MPH`, and `M` are all clearly distinct from numerals.

- **Replace the single crest with a slow swell train.** The lone dot crossing
  the divider reads as a cursor, not water: it is a single object, and at a 6s
  period it moves about 21px/sec, fast enough that the eye tracks it. Anything
  you track has failed at being ambient. Use two or three dim crests drifting
  together so there is no single object to follow, and so each crest only
  travels the gap to the next one per cycle rather than the whole face.
  Spacing and drift speed then become a felt analogue of period: a 15s
  groundswell crawls, a 6s windswell chops. Keep it abstract. Spec 12.5 bans
  implying phase-accurate ocean motion, so no undulating surface or seascape.
  Consider a brighter one-off sweep when a new observation lands, since that is
  rare (hourly) and actually means something.

- **Tighten period and direction spacing.** `17.5S` and `SSW` sit 3px apart,
  the tightest pair on the face. Legible but cramped. One option is dropping
  the period to integer form above a width threshold so a long period never
  crowds a three-letter direction. Needs a judgment call at physical scale.

- **Golden images.** `tests/golden/` does not exist, so the renderer is only
  tested for determinism, not appearance. Every layout bug found so far was
  found by eye in the browser, which is not a repeatable test.
  `swellsign write-state-fixtures` already dumps all fourteen states as JSON,
  which is the groundwork.

## Data

- **Derive measured swell partitions from NDBC spectra.** This is the one
  feature that would make the sign strictly better than Surfline rather than
  merely honest. Verified 2026-07-31: Surfline's buoy reading for Ponce de Leon
  Inlet (PNCWAVE / 41070) is `1.3ft 6s E 84deg`, identical to ours to the
  decimal, with an equally empty partition list. Their prominent three-train
  swell breakdown is labeled *LOTUS Forecast* on their own page - it is model
  output, not measurement. NDBC publishes full directional spectra for 41070
  (`.data_spec`, `.swdir`, `.swdir2`, `.swr1`, `.swr2`), which is enough to
  compute partitions locally. Spec 9.1 has the math: `m0` and `Hm0 = 4*sqrt(m0)`,
  `Tp = 1/fpeak`, circular statistics for direction. Label the result `PART`,
  carry algorithm version and confidence, and validate against 41113, which
  publishes provider partitions we can check against. Measured partitions where
  a paid product shows modeled ones is a real advantage.

- **Consider surfacing 41113's measured partitions as disclosed context.** A
  cheaper interim step than a local partitioner. 41113 already publishes
  separated swell (Surfline lists three trains for it), but we only consult it
  as a fallback when 41070 goes stale. It is a Cape Canaveral proxy 45 miles
  south, so it can never be presented as a New Smyrna measurement, but a
  clearly labeled secondary reading may still be worth more than nothing.

- **Never add surf height.** Recorded here because it will keep coming up.
  Surfline's headline `1-2ft` is breaking wave face at the beach, derived from
  a model plus a cam. It is a different physical quantity from buoy significant
  wave height and it requires bathymetry and refraction modeling. Spec 1.2 and
  5.1 rule it out, and doing it badly would be worse than not doing it.

- **Run the collector continuously and measure NDBC publication lag.** The
  freshness multipliers (2.5x / 4x / 7x) are a defensible guess, not a
  measurement. Observation timestamps are exactly hourly, but the delay between
  a measurement time and its appearance on NOAA's server has never been
  measured here, because the collector has only run in manual bursts. Once it
  runs for a day or two that lag is measurable and the multipliers can be tuned
  against it. If `DLY` ever appears while the NDBC station page shows a current
  reading, the multiplier is still too tight.

- **Re-evaluate 41070 separated swell.** `SwH`, `SwP`, and `SwD` were all `MM`
  as of 2026-07-30, which is why the sign says `SEAS`. The code already
  upgrades to `SWELL` automatically if the provider starts publishing valid
  fields; this is a reminder to confirm it happens rather than assume.

- **Fort Pierce stays out.** CDIP 134 / NDBC 41114 has been inactive since the
  buoy was recovered on 2026-04-27. Do not configure it as a live source until
  deployment and reporting are independently verified.

## Hardware, blocked on buying panels

- **Validate the brightness constants.** Day 0.55 / evening 0.40 / night 0.28
  are perceptual levels chosen to sit above the knee where every channel floors
  to 1 and sea glass turns grey. Whether night is actually dim enough for a
  dark room is not answerable from a monitor.

- **Validate MatrixPortal bit depth.** Raised from 4 to 6 because a dim,
  gamma-corrected face lives at the bottom of the range where 16 levels
  posterize. The tradeoff is refresh rate, and only the real panel batch can
  settle whether 6 flickers.

- **Confirm panel power before trusting PoE.** Two 64x32 P4 panels can pull
  roughly 8A at 5V worst case, and PoE+ tops out near 25W at the device. Real
  draw at these brightness levels is likely a small fraction of worst case, but
  measure it rather than assume, or run PoE to the Pi and a separate 5V supply
  to the panels.

- **Panel color order and font legibility** at reading distance, per spec
  phase 7.

## Someday

- Physical control for switching spots: a rotary switch or single button
  cycling a few configured spots. No screen, no app, no network config.
- Optional IP geolocation to preselect a default spot. Never to pick stations,
  which stays a curatorial decision rather than a distance calculation.
- The tide clock: a circular, fully offline instrument. Tides are astronomical
  and computable years ahead on-device, so it needs no network at all.
