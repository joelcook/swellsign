# Working notes for Swell Sign

## Where things live

- **[ocean-swell-buoy-reader-build-spec.md](ocean-swell-buoy-reader-build-spec.md)**
  is the contract. It describes what the product is and refuses to be. When code
  and spec disagree, one of them is a bug; decide which and fix that one.
- **[TODO.md](TODO.md)** is the backlog. Put open work there, not in an agent's
  session task list, which is invisible on GitHub and vanishes when the session
  ends. A session task list is fine for tracking steps within one sitting.
- **[README.md](README.md)** is for someone who just found the repo.

## The product rule that outranks the others

No score, just swell. The sign reports measurements and their provenance; the
owner supplies the judgment. Anything that scores, rates, ranks, recommends, or
implies quality does not belong on the default face or in `/now`, no matter how
objective the underlying math feels. Freshness, fallback state, and a numerical
trend are allowed because they describe the data, not the surf.

## Invariants worth knowing before changing anything

- **Observed, forecast, and predicted stay structurally separate.** Different
  models, tables, provider interfaces, and endpoints. `/now` is always
  `mode: observed`, forecast is `mode: forecast`, tide is `mode: prediction`.
  A forecast point or a tide extreme must never satisfy a current observation,
  and there are tests whose only job is to prove it.
- **Never turn missing data into zero.** Provider missing markers become `None`.
  A missing direction renders `--`; it does not render `0`.
- **Never stitch a wave triplet across timestamps or stations.** Height from one
  observation and period from another is a fabrication.
- **`measurement_basis` is never fudged.** `total_sea` and `separated_swell`
  describe genuinely different quantities, and storage, `/now`, and provenance
  must always report which one a number actually is.

  The *sign's* label is a separate question and is now overridden. As of
  2026-07-31 `config/spots.yaml` sets `display.wave_label: SWELL`, so the face
  reads `SWELL` regardless of basis. This was the product owner's call, made
  with the tradeoff stated: `41070` publishes no partition data and never will,
  so the honest label would have read `SEAS` permanently and distinguished
  nothing. Do not "fix" this back, and do not let it leak past the compact
  display payload into the data model.
- **Wave and wind age independently.** They come from different stations and
  keep their own timestamps, freshness, and provenance.

## Product decisions that look like bugs

These were made deliberately. Do not "fix" them back.

- **The face says `SWELL` for everything.** `display.wave_label`, set 2026-07-31.
  41070 publishes no partition data and never will, so a basis-derived label
  would read `SEAS` permanently and distinguish nothing. `measurement_basis` in
  `/now` and in storage stays honest.
- **The face shows modeled wind, unmarked.** `display.wind_source: beach`, set
  2026-08-01. The configured anemometer is 31.6 km offshore where wind runs far
  above beach values; no anemometer stands on the beach, so the model at the
  spot's coordinates is the only available answer. Wind at a beach is understood
  to be forecast, the way tide is, so the face carries no marker. The compact
  payload still carries `source: "buoy" | "model"`, and `/now` still reports the
  measured buoy with its distance.

## Display

- The renderer is the single source of truth for what the sign shows. The Pi
  output, the browser simulator, and the PNG previews are all output adapters
  that receive rendered pixels. Do not reimplement the layout in another
  language; a second implementation is a second thing to be wrong.
- **Gamma is applied once, on the hardware path.** A PNG or a browser canvas is
  viewed on an sRGB display that already applies the curve, so the renderer
  emits perceptual values and only the panel adapter corrects them.
- Brightness values are perceptual. Below roughly `0.25` every channel floors to
  1 and sea glass turns grey, so the sign loses its color before its brightness.
- The layout budget is 128 pixels wide and genuinely tight. Field boxes are
  named constants in `renderer.py`; check the widest value each field can hold
  before moving one. Both bugs found so far were collisions that only appeared
  for the longest value (`SWELL`, `14.8FT`).

## Verifying display work

Fixture tests assert determinism, not legibility, and will not catch a
collision. Look at the face:

```bash
swellsign simulate --api-url http://127.0.0.1:8000/v1/spots/new-smyrna/display
```

Every layout bug so far was found by eye in that browser panel.

## Conventions

- Timestamps are timezone-aware UTC everywhere. Round only at presentation.
- Canonical storage is SI: meters, m/s, Celsius, degrees true.
- Configuration over code. Stations, spots, thresholds, and freshness policy
  live in `config/spots.yaml`; adding a spot should never require an edit here.
- Station availability changes. Treat any station assignment as verified on a
  date, not as a permanent fact.
