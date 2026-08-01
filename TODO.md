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
