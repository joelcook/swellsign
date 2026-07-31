# Swell Sign

Swell Sign is a quiet `128×32` ocean instrument for the wall above a surfboard
rack. Two chained `64×32` P4 HUB75 panels show the latest measured New Smyrna
conditions:

```text
NEW SMYRNA                 12M
SEAS  2.6FT  ↑   8.1S       NE
WIND  W                    8MPH
```

There is no surf score, quality color, board recommendation, or model value
masquerading as a measurement. `SEAS` means provider-reported total sea state;
`SWELL` appears only for a coherent provider-reported swell partition. The
small arrow is a robust six-hour measured-height trend, not a rating.

The complete product and engineering contract is in
[ocean-swell-buoy-reader-build-spec.md](ocean-swell-buoy-reader-build-spec.md).

## What is included

- NOAA NDBC collection for local waves (`41070`), local wind (`41069`), and a
  disclosed Cape Canaveral fallback (`41113`)
- Raw-response preservation and normalized observation history in SQLite/WAL
- Independent wave/wind timestamps, freshness, provenance, and fallback state
- A robust Theil–Sen six-hour wave-height trend
- A separate immutable seven-day Open-Meteo forecast archive
- A versioned FastAPI observation and forecast API
- An exact `128×32` Pillow simulator with a bundled pixel font
- A Raspberry Pi `rpi-rgb-led-matrix` output adapter
- A CircuitPython 10.2.1 MatrixPortal S3 thin client with offline cache
- Fixture-based parser, selection, API, persistence, forecast-separation, and
  renderer tests

Forecast data has separate provider interfaces, models, tables, and endpoints.
It is collected because it will be useful later, but it can never fill the
current-observation object or the default sign face.

## Quick start

Python 3.12 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env

swellsign init-db
swellsign collect-once --forecast
swellsign snapshot new-smyrna
swellsign api
```

In a second shell:

```bash
curl http://127.0.0.1:8000/v1/spots/new-smyrna/now
curl http://127.0.0.1:8000/v1/spots/new-smyrna/display
```

Run the long-lived collector separately from Uvicorn:

```bash
swellsign collector
```

That separation prevents multiple API workers from accidentally duplicating
upstream collection jobs.

## Simulator

Render a fixture at native LED resolution and at an enlarged nearest-neighbor
preview:

```bash
swellsign render-preview \
  examples/display-fresh.json \
  previews/fresh-6x.png \
  --scale 6
```

The same renderer feeds the optional Raspberry Pi matrix adapter. Colors are
deliberately low saturation: sea-glass cyan for water, warm white for labels,
muted amber for wind/data delay, and restrained red only for unavailable data.
Color never means “good” or “bad” surf.

### Browser panel

```bash
swellsign simulate
swellsign simulate --api-url http://127.0.0.1:8000/v1/spots/new-smyrna/display
```

This serves a page at `http://127.0.0.1:8100/` that shows the sign as an object:
individual emitters, bloom, smoked-acrylic sheen, and a dark enclosure, with
live controls for all fourteen data states, the brightness schedule across a
24-hour scrub, motion, and scale up to approximate physical size.

The browser is an output adapter, not a second renderer. Python renders every
frame with the real `DisplayRenderer` and ships raw pixels, so the page is
byte-identical to what the Pi draws. A JavaScript reimplementation of the
layout would be a second source of truth and would quietly break the simulator
contract. Passing `--api-url` adds a live state driven by real buoy data.

To dump every state as JSON for golden-image work:

```bash
swellsign write-state-fixtures examples/states
```

## Brightness, gamma, and motion

The sign is meant to be barely present at night, so the display client runs
three independent clocks: the API poll, the frame rate, and the crest cycle.

```bash
swellsign display --frames-per-second 20 --night-brightness 0.28
swellsign display --no-motion --brightness 0.4   # fixed level, no schedule
```

Brightness values are perceptual. Gamma correction is applied once, on the way
to the panel: a PNG is viewed on an sRGB display that already applies that
curve, so the simulator keeps perceptual values and correcting in both places
would darken the preview twice. Below roughly `0.25` every channel floors to
`1` and sea glass turns grey, so the sign loses its color before it loses its
brightness; the scheduled night level sits above that knee on purpose.

Motion is decorative and nonsemantic. A one-pixel crest completes one cycle
per reported dominant period and is suppressed when the period is missing, the
wave component is not fresh, or `--no-motion` is passed.

## API

Observation:

```text
GET /v1/spots
GET /v1/spots/{spot_id}
GET /v1/spots/{spot_id}/now
GET /v1/spots/{spot_id}/display
GET /v1/spots/{spot_id}/history?hours=24
GET /v1/stations
GET /v1/stations/{station_id}/latest
GET /v1/stations/{station_id}/observations?hours=24
```

Forecast archive:

```text
GET /v1/spots/{spot_id}/forecast?hours=168
GET /v1/spots/{spot_id}/forecast/runs
GET /v1/spots/{spot_id}/forecast/runs/{run_id}
```

Tide context (predictions, not measurements):

```text
GET /v1/spots/{spot_id}/tide?hours=48
GET /v1/spots/{spot_id}/sources
```

Operations:

```text
GET /v1/health
GET /v1/ready
```

`/now` is always `mode: "observed"`, forecast responses are always
`mode: "forecast"`, and tide responses are always `mode: "prediction"`. Wave
and wind each retain their own `observed_at`, `age_minutes`, freshness, and
station source.

## Tide context

New Smyrna uses NOAA CO-OPS subordinate station `8721147`, Ponce de Leon Inlet
South. It publishes high/low predictions rather than a live observed water
level, so the only derivation offered is the phase between two adjacent
predicted extremes:

```bash
swellsign collect-once --tide
swellsign tide new-smyrna
```

A phase is reported only when a predicted extreme brackets the moment on both
sides; the service will not span a gap in the archive or extrapolate past the
last known extreme. Station `8721164` is deliberately unused: it sits inside
Mosquito Lagoon and its timing differs materially from the inlet.

Tide has its own model, table, and endpoint. It never enters a
`CurrentSnapshot` and never appears on the default sign face.

## Data behavior

The New Smyrna source policy is intentionally conservative:

1. Use a coherent separated-swell height/period/direction triplet from local
   `41070` only if all three fields are present.
2. Otherwise use local `41070` `WVHT`/`DPD`/`MWD` and label it `SEAS`.
3. Use `41113` only when the local source is unusable or past its hard age
   limit, mark it as fallback, and show `ALT`.
4. Select wind independently from `41069`.
5. Keep stored observations through provider outages and let their displayed
   ages continue increasing.
6. Never combine fields across stations, timestamps, or measurement bases.
7. Never substitute a forecast point.

The default freshness windows are tuned for the hourly local observations:
fresh through 90 minutes, delayed through 180, stale through 360, then
unavailable. The age remains visible so the viewer can judge it directly.

Optional tide work uses CO-OPS subordinate prediction station `8721147`, Ponce
de Leon Inlet South. It must be labeled as a prediction and must not be
represented as a live observed tide height.

## Hardware

Reference build:

- 2 × Adafruit-style `64×32` P4 indoor HUB75 RGB panels
- Raspberry Pi 4 plus an Adafruit RGB Matrix Bonnet, or MatrixPortal S3
- regulated `5 V / 10 A` panel supply
- fused low-voltage split and short appropriately sized panel wiring
- continuous black diffusion/smoked acrylic face, roughly `21×6` inches
- matte dark or wood enclosure, hidden fasteners, ventilation, and wall mount

Each panel is specified for as much as 4 A at 5 V. Power the panels directly;
never through a Pi. With MatrixPortal, power the controller over USB-C, power
the panels from the external supply, and share ground. Keep mains conversion in
an enclosed listed brick outside the wooden sign where practical.

The MatrixPortal installation, exact chain direction, required CircuitPython
libraries, caching behavior, and power warnings are in
[firmware/matrixportal/README.md](firmware/matrixportal/README.md).

For Raspberry Pi output after installing `rpi-rgb-led-matrix`:

```bash
sudo -E swellsign display \
  --api-url http://127.0.0.1:8000/v1/spots/new-smyrna/display
```

Start around 25–35% effective indoor brightness behind the diffuser. On
MatrixPortal, brightness is implemented by scaling RGB colors because the
RGBMatrix display brightness property is not proportionally dimmable.

## Configuration and storage

Product configuration lives in [config/spots.yaml](config/spots.yaml).
Environment overrides are listed in [.env.example](.env.example). Runtime data
defaults to:

```text
data/swellsign.db
data/snapshots/
```

SQLite uses foreign keys, WAL journal mode, a busy timeout, short
transactions, and an explicit schema version. Raw fetch bodies remain linked
to normalized rows. Forecast runs are immutable instead of being overwritten,
which makes later model verification possible without look-ahead.

## Tests

```bash
PYTHONPATH=src pytest
ruff check .
```

Network access is not required for the test suite. Frozen fixtures cover real
NDBC column shapes, missing `MM` partitions, cardinal directions, Open-Meteo
array joins, source fallback, staleness, trend robustness, API schemas, and
display states.

## Deployment

For a single Pi appliance, use the service templates in `deploy/systemd/`:

```text
swellsign-api.service
swellsign-collector.service
swellsign-display.service
```

Docker Compose is also included for backend development. The physical HUB75
display should normally run natively so it can access the Pi GPIO hardware.

## Attribution

Observed buoy and wind data: NOAA National Data Buoy Center and, for the
fallback buoy, CDIP/Scripps where applicable. Tide predictions: NOAA CO-OPS.
Archived marine/weather forecasts: Open-Meteo, backed by the explicitly stored
model identifiers. Full responses retain source URLs and station attribution.

