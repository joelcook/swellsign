# Swell Sign: Ocean Observation Service + 128×32 LED Instrument

## Codex Build Specification

**Project name:** Swell Sign  
**Initial spot:** New Smyrna Beach, Florida  
**Primary object:** A wall-mounted, two-panel HUB75 LED sign above a surfboard rack  
**Primary question:** “What is the ocean doing right now?”  
**Prepared:** July 30, 2026

> Station availability and reporting capabilities change. Station assignments below are the verified starting configuration, not timeless facts. The collector must detect missing fields, preserve source metadata, and make configuration changes possible without code changes.

---

## 1. Product Definition

Swell Sign is a quiet, glanceable ocean instrument. It shows the latest measured wave and wind observations so an experienced surfer can decide whether the conditions are interesting enough to investigate further.

It does **not** decide whether the surf is good. It does not recommend a board, manufacture a surf-height forecast from offshore buoy height, or imitate a forecast app.

The product has five cooperating parts:

1. Collect current public wave and wind observations.
2. Normalize, quality-check, and store those observations.
3. Expose current observations and history through a versioned API.
4. Render a beautiful, restrained `128×32` LED sign.
5. Collect and archive a seven-day model forecast on a separate data path for future use.

All “now” outputs consume one canonical `CurrentSnapshot`. Forecast values use separate models, tables, endpoints, and visual states.

```text
                       OBSERVED / NOW PATH

NDBC / CDIP / CO-OPS ──> raw fetches ──> normalized observations
                                                |
                                                v
                                      source selection + QC
                                                |
                                                v
                                        CurrentSnapshot
                                          /           \
                                         v             v
                                    REST API      Swell Sign
                                                  + simulator


                       FORECAST / FUTURE PATH

Marine model provider ──> forecast run ──> forecast points ──> forecast API
                              archive           (0–168 h)
```

The two paths may be compared in analytics, but forecast values must never silently fill a missing current observation.

### 1.1 Product promise

At a normal walk-by glance, the owner should be able to read:

- Where the observation applies.
- Whether the wave number is total sea state or separated swell.
- Wave height, dominant or swell period, and direction.
- Wind direction and speed.
- How old the data is.
- Whether a fallback source is in use.
- Whether measured wave height is rising, steady, or falling.

### 1.2 Explicit non-goals

The default sign and `/now` API do not provide:

- Surf ratings, scores, stars, percentages, or color-coded quality.
- Condition-quality recommendations of any kind.
- Board selection.
- Surfline-style corrected breaking-wave height.
- Camera analysis.
- Crowd estimates.
- Forecast data presented as a current observation.
- An automatically rotating forecast screen.

Those exclusions are part of the product identity: **no score, just swell**.

---

## 2. Core Principles

1. **Measurement before interpretation.** Preserve what the instrument reported.
2. **Name the measurement honestly.** A total significant wave height is `SEAS`; a genuine separated-swell value is `SWELL`.
3. **Do not imply beach wave-face height.** Offshore or nearshore buoy height is not labeled as surf height.
4. **Keep observed and forecast data structurally separate.**
5. **Expose provenance.** Station, provider, role, distance, timestamps, QC, and fallback state remain available.
6. **Age each component independently.** Wave and wind often have different observation times.
7. **Never turn missing data into zero.**
8. **Prefer a clear partial reading over an invented complete one.**
9. **Use objective derived values only.** Freshness, fallback, and numerical trend are allowed; a quality judgment is not.
10. **Keep the default face stable.** A glance at any moment should show current observations.
11. **Store history from day one.** It enables trends, source validation, and later forecast verification.
12. **Build the simulator before buying or driving hardware.**
13. **Favor a durable local appliance.** The sign should survive short internet and upstream-provider outages.

---

## 3. Initial Station Strategy

### 3.1 New Smyrna Beach

New Smyrna Beach is the only required live spot for V1.

| Function | Station | Role | Important behavior |
|---|---|---|---|
| Waves | NDBC `41070` | Primary | Use valid standard/spectral summary fields; currently treat its available wave summary as total sea state |
| Wind | NDBC `41069` | Primary | Pair meteorological observation with the wave observation, retaining its own timestamp |
| Waves | NDBC `41113` / CDIP `143` | Fallback | Cape Canaveral Nearshore directional buoy; disclose that it is an alternate, more remote source |
| Tide context | NOAA CO-OPS `8721147` | Optional context | Ponce de Leon Inlet South high/low predictions; not required on the default face |

As verified July 30, 2026, the separated-swell fields in the `41070.spec` realtime product are missing. Therefore:

- Do not treat `SwH`, `SwP`, or `SwD` from `41070` as usable while they are `MM`.
- Use valid `WVHT`, `DPD`, and `MWD` values.
- Set `measurement_basis: "total_sea"`.
- Render the label `SEAS`, not `SWELL`.
- Re-evaluate automatically if the provider later begins reporting valid separated-swell fields.

`41113` may provide richer separated-swell fields, but it is a Cape Canaveral proxy. If selected, the snapshot must say `fallback_used: true`, identify the station, and allow the sign to show `ALT`.

Initial source policy:

```yaml
id: new-smyrna
name: New Smyrna Beach
display_name: NEW SMYRNA
timezone: America/New_York
latitude: 29.0258
longitude: -80.9270

sources:
  wave:
    - station: ndbc:41070
      role: primary
      maximum_usable_age_minutes: 360
      preferred_basis:
        - separated_swell
        - total_sea
    - station: ndbc:41113
      role: fallback
      maximum_usable_age_minutes: 360
      preferred_basis:
        - separated_swell
        - total_sea

  wind:
    - station: ndbc:41069
      role: primary
      maximum_usable_age_minutes: 360
```

The order under `preferred_basis` does not authorize a substitution with missing values. It means “use the first valid basis actually reported by this station.”

### 3.2 Direction convention

Wave and wind directions are “from” directions in degrees true:

- `45°` means waves arriving **from the northeast**.
- `270°` means wind arriving **from the west**.

Store degrees true. Generate a 16-point cardinal label only at presentation boundaries.

### 3.3 Fort Pierce status

Fort Pierce is not a live V1 spot.

CDIP `134` / NDBC `41114`, the dedicated Fort Pierce directional wave buoy, is inactive as of July 30, 2026; the buoy was recovered on April 27, 2026. Its historical observations remain useful, but it must not be configured as a live primary source until deployment and reporting are independently verified again.

Potential future context sources:

| Station | Potential role | Caveat |
|---|---|---|
| NDBC `41068` | Local oceanographic context | May not provide a complete live wave observation |
| NDBC `41113` | Remote nearshore fallback | Cape Canaveral proxy; disclose distance and role |
| NDBC `41009` | Offshore context | Not a Fort Pierce beach reading |
| NDBC `41010` | Deep-water upstream context | Not a local current condition |

No remote source may be presented as a direct Fort Pierce measurement.

---

## 4. Data Providers

### 4.1 NOAA National Data Buoy Center

NDBC is the V1 observation provider.

```text
# Station inventory
https://www.ndbc.noaa.gov/data/stations/station_table.txt

# Standard realtime observations
https://www.ndbc.noaa.gov/data/realtime2/{station}.txt

# Separated swell / wind-wave summary when populated
https://www.ndbc.noaa.gov/data/realtime2/{station}.spec

# Nondirectional spectral energy
https://www.ndbc.noaa.gov/data/realtime2/{station}.data_spec

# Directional spectral coefficients
https://www.ndbc.noaa.gov/data/realtime2/{station}.swdir
https://www.ndbc.noaa.gov/data/realtime2/{station}.swdir2
https://www.ndbc.noaa.gov/data/realtime2/{station}.swr1
https://www.ndbc.noaa.gov/data/realtime2/{station}.swr2
```

Important fields:

```text
WVHT  Total significant wave height
DPD   Dominant wave period
APD   Average wave period
MWD   Mean wave direction at the dominant period
SwH   Separated swell height
SwP   Separated swell period
SwD   Separated swell direction
WWH   Wind-wave height
WWP   Wind-wave period
WWD   Wind-wave direction
WSPD  Wind speed
WDIR  Wind direction
GST   Wind gust
WTMP  Water temperature
```

Selection within one observation:

```text
1. If SwH + SwP + SwD are all present, valid, and QC-accepted:
   basis = separated_swell, label = SWELL.

2. Otherwise, if WVHT + DPD + MWD are present, valid, and QC-accepted:
   basis = total_sea, label = SEAS.

3. Otherwise return a partial wave observation or no usable wave component.
   Do not combine fields from different timestamps to fabricate a triplet.
```

Keep provider missing markers such as `MM` out of numeric fields and record the missing-field names in normalization metadata.

### 4.2 CDIP / Scripps Institution of Oceanography

Add CDIP after the NDBC path works. It can provide richer Waverider metadata, spectra, and QC.

```text
https://erddap.cdip.ucsd.edu/erddap/tabledap/wave_agg.json
```

Relevant fields include:

```text
station_id
time
latitude
longitude
metaStationName
waveHs
waveTp
waveTa
waveDp
waveFlagPrimary
waveFlagSecondary
```

Known physical-station aliases must be explicit to prevent duplicate records:

```text
CDIP 134p1 <-> NDBC 41114
CDIP 143p1 <-> NDBC 41113
```

An alias is not permission to merge observations with different timestamps or processing methods.

### 4.3 NOAA CO-OPS

CO-OPS can supply objective tide and water-level context:

- Observed water level.
- Predicted high and low tides.
- Water temperature.
- Currents where available.

Observed water level and tide predictions must use distinct models and fields. Tide predictions are forecasts and therefore do not become measured `/now` wave data.

For New Smyrna, use subordinate prediction station `8721147`, Ponce de Leon
Inlet South. It supports high/low predictions rather than a live observed water
level. Derive only the phase between adjacent predicted extrema and always label
that context as a prediction. Do not use `8721164` as the default surf proxy;
that station is inside Mosquito Lagoon and its timing can differ materially.

### 4.4 Seven-day marine forecast provider

The V1 adapter uses Open-Meteo Marine plus Open-Meteo Weather:

```text
Marine endpoint    https://marine-api.open-meteo.com/v1/marine
Marine model       ncep_gfswave016
Wind endpoint      https://api.open-meteo.com/v1/forecast
Wind model         gfs_seamless
Timezone           GMT
Forecast horizon   168 hours
```

Outer-join marine and wind arrays by `valid_at`; never zip parallel arrays
blindly. Record the returned grid coordinates because the selected marine grid
may be offshore from the configured spot. These live responses do not expose a
trustworthy model initialization timestamp, so leave `issued_at` null instead
of substituting retrieval time. A direct NOAA wave-model adapter can be added
later.

Forecast collection requirements:

- Fetch up to 168 hours of hourly wave, swell, wind-wave, and wind fields.
- Store the provider/model identifier.
- Store `issued_at` or model initialization time when supplied.
- Store `fetched_at` and every point’s `valid_at`.
- Store latitude, longitude, units, and raw payload.
- Archive each run; never overwrite an older run with a newer one.
- Keep forecast QC and availability independent from observation freshness.

Forecast points may be exposed and graphed later. They are not candidates in the current-observation source selector.

### 4.5 Future providers

A generic ERDDAP adapter may later support SECOORA, GCOOS, and other IOOS regions. Global expansion may add Copernicus Marine, EMODnet, WaveNet, and regional Australian systems. None are required for the New Smyrna V1.

---

## 5. Measurement Semantics

### 5.1 Measurement basis

Every displayed wave triplet has one explicit basis:

```text
separated_swell    Provider-reported swell partition
total_sea          Provider-reported combined significant wave field
spectral_partition Locally derived, validated spectral partition
```

Presentation labels:

| Basis | Default sign label | Meaning |
|---|---|---|
| `separated_swell` | `SWELL` | Provider or validated processor separated swell from wind sea |
| `total_sea` | `SEAS` | Combined significant wave conditions |
| `spectral_partition` | `PART` | Derived partition; provenance remains visible in full API |

Do not label `WVHT` as `SWELL`. Do not label any of these values as breaking surf height.

### 5.2 Time

- Store every timestamp as timezone-aware UTC.
- Keep `observed_at`, `received_at`, and `fetched_at` distinct.
- Render local wall-clock time only when a layout explicitly needs it.
- Calculate age from `observed_at`, never from API response time.
- The display client recalculates age while offline so a cached `12M` reading does not remain frozen.

### 5.3 Units

Canonical storage:

```text
Wave height: meters
Wind speed: meters per second
Temperature: degrees Celsius
Direction: degrees true
Distance: meters
Time: UTC
```

Presentation:

```python
feet = meters * 3.280839895
mph = meters_per_second * 2.236936292
fahrenheit = celsius * 9 / 5 + 32
```

Round only at presentation. Preserve source precision in storage.

---

## 6. Canonical Models

The implementation uses Pydantic models and explicit `sqlite3` repositories.
The semantic fields below are required.

### 6.1 Station

```python
class Station:
    id: str
    provider: str
    provider_station_id: str
    canonical_physical_station_id: str
    name: str
    latitude: float
    longitude: float
    water_depth_m: float | None
    platform_type: str | None
    active_status: str
    expected_interval_minutes: int | None
    capabilities: list[str]
    attribution: str | None
    metadata: dict
```

### 6.2 Wave observation

```python
class WaveObservation:
    id: str
    station_id: str
    observed_at: datetime
    received_at: datetime | None
    fetched_at: datetime

    significant_height_m: float | None
    dominant_period_s: float | None
    average_period_s: float | None
    mean_direction_deg_true: float | None

    swell_height_m: float | None
    swell_period_s: float | None
    swell_direction_deg_true: float | None

    wind_wave_height_m: float | None
    wind_wave_period_s: float | None
    wind_wave_direction_deg_true: float | None

    water_temperature_c: float | None
    qc_status: str
    qc_detail: dict
    source_url: str
    raw_fetch_id: str
```

The normalized record preserves all valid provider fields. A later snapshot selects one coherent display triplet and records its basis.

### 6.3 Wind observation

```python
class WindObservation:
    id: str
    station_id: str
    observed_at: datetime
    received_at: datetime | None
    fetched_at: datetime
    speed_mps: float | None
    gust_mps: float | None
    direction_deg_true: float | None
    qc_status: str
    qc_detail: dict
    source_url: str
    raw_fetch_id: str
```

### 6.4 Forecast run and point

```python
class ForecastRun:
    id: str
    provider: str
    model: str
    location_id: str
    issued_at: datetime | None
    fetched_at: datetime
    horizon_hours: int
    raw_fetch_id: str


class ForecastPoint:
    run_id: str
    valid_at: datetime
    wave_height_m: float | None
    wave_period_s: float | None
    wave_direction_deg_true: float | None
    swell_height_m: float | None
    swell_period_s: float | None
    swell_direction_deg_true: float | None
    wind_wave_height_m: float | None
    wind_wave_period_s: float | None
    wind_wave_direction_deg_true: float | None
    wind_speed_mps: float | None
    wind_direction_deg_true: float | None
```

No `ForecastPoint` can satisfy a `CurrentSnapshot` wave or wind field.

### 6.5 Current snapshot

This is the single object shared by `/now`, the compact display endpoint, the simulator, and the physical sign.

```json
{
  "schema_version": 1,
  "mode": "observed",
  "spot": {
    "id": "new-smyrna",
    "name": "New Smyrna Beach",
    "display_name": "NEW SMYRNA",
    "timezone": "America/New_York"
  },
  "generated_at": "2026-07-30T17:22:00Z",
  "wave": {
    "height_m": 0.792,
    "height_ft": 2.6,
    "period_s": 8.1,
    "direction_deg_true": 43,
    "direction_cardinal": "NE",
    "measurement_basis": "total_sea",
    "display_label": "SEAS",
    "observed_at": "2026-07-30T17:10:00Z",
    "age_minutes": 12,
    "freshness": "fresh",
    "source": {
      "station_id": "ndbc:41070",
      "role": "primary",
      "fallback_used": false,
      "distance_to_spot_m": null,
      "qc_status": "accepted"
    }
  },
  "wind": {
    "speed_mps": 3.49,
    "speed_mph": 7.8,
    "gust_mps": 4.11,
    "gust_mph": 9.2,
    "direction_deg_true": 268,
    "direction_cardinal": "W",
    "observed_at": "2026-07-30T17:00:00Z",
    "age_minutes": 22,
    "freshness": "fresh",
    "source": {
      "station_id": "ndbc:41069",
      "role": "primary",
      "fallback_used": false,
      "distance_to_spot_m": null,
      "qc_status": "accepted"
    }
  },
  "trend": {
    "wave_height": {
      "state": "rising",
      "window_hours": 6,
      "estimated_change_m": 0.18,
      "estimated_change_ft": 0.6,
      "sample_count": 7,
      "station_id": "ndbc:41070",
      "measurement_basis": "total_sea"
    }
  },
  "data_state": "fresh",
  "fallback_used": false,
  "warnings": []
}
```

Important invariants:

- `mode` is always `observed` for this model.
- Wave and wind have independent `observed_at`, age, freshness, source, and QC.
- `data_state` summarizes required components but never replaces component detail.
- Source distance is calculated from stored station/spot coordinates and remains `null` when either coordinate is unknown; it is never guessed.
- `warnings` are objective data-health messages.
- There is no score, quality judgment, recommendation, or preferred-condition comparison.

---

## 7. SQLite-First Storage

SQLite is the correct V1 database. It keeps the appliance easy to install, back up, inspect, and run on one Raspberry Pi while comfortably handling the expected observation volume.

Configuration:

```text
SQLite 3
WAL journal mode
foreign_keys = ON
busy_timeout configured
UTC ISO-8601 text or a consistently documented integer epoch representation
schema migrations from the first release
```

Core tables:

```text
raw_fetches
-----------
id
provider
resource_type
source_url
requested_at_utc
received_at_utc
http_status
content_type
body_blob
body_sha256
error_json

stations
--------
id
provider
provider_station_id
canonical_physical_station_id
name
latitude
longitude
water_depth_m
platform_type
active_status
expected_interval_minutes
capabilities_json
attribution
metadata_json
created_at_utc
updated_at_utc

station_aliases
---------------
provider
provider_station_id
canonical_physical_station_id
valid_from_utc
valid_to_utc
metadata_json

wave_observations
-----------------
id
station_id
observed_at_utc
received_at_utc
fetched_at_utc
significant_height_m
dominant_period_s
average_period_s
mean_direction_deg_true
swell_height_m
swell_period_s
swell_direction_deg_true
wind_wave_height_m
wind_wave_period_s
wind_wave_direction_deg_true
water_temperature_c
qc_status
qc_detail_json
source_url
raw_fetch_id

wind_observations
-----------------
id
station_id
observed_at_utc
received_at_utc
fetched_at_utc
speed_mps
gust_mps
direction_deg_true
qc_status
qc_detail_json
source_url
raw_fetch_id

forecast_runs
-------------
id
provider
model
location_id
issued_at_utc
fetched_at_utc
horizon_hours
raw_fetch_id
metadata_json

forecast_points
---------------
run_id
valid_at_utc
wave_height_m
wave_period_s
wave_direction_deg_true
swell_height_m
swell_period_s
swell_direction_deg_true
wind_wave_height_m
wind_wave_period_s
wind_wave_direction_deg_true
wind_speed_mps
wind_direction_deg_true
qc_status
```

Required constraints and indexes:

- Unique normalized observation per physical station, timestamp, and provider processing product.
- Unique forecast point per run and `valid_at`.
- Index observation tables by station and descending observation time.
- Index forecast runs by location and descending issue/fetch time.
- Index forecast points by run and valid time.
- Keep raw fetches addressable from every normalized row.

PostgreSQL, PostGIS, TimescaleDB, and Redis are unnecessary for V1. Add them only after measured load or multi-device deployment creates a real need. Repository interfaces should prevent SQLite assumptions from leaking into provider and API code.

---

## 8. Collection and Snapshot Pipeline

### 8.1 Observation collector

For each configured source:

1. Fetch with a bounded timeout and identifiable user agent.
2. Persist the raw response or error metadata.
3. Parse all returned rows, not only the first line.
4. Convert missing markers to `None`.
5. Normalize units and timestamps.
6. Retain provider QC and parsing warnings.
7. Upsert idempotently.
8. Rebuild the spot’s `CurrentSnapshot`.
9. Atomically replace a local last-good snapshot file after validation.

Use a station-aware schedule. A sensible initial NDBC poll interval is 20 minutes; configuration may adjust it to the source’s reporting cadence. Polling the local API can be much faster because it creates no upstream load.

### 8.2 Forecast collector

On its own schedule:

1. Fetch a complete model run or the provider’s latest 168-hour series.
2. Persist the raw response.
3. Create a new immutable `forecast_run`.
4. Insert its points.
5. Mark completeness and missing fields.
6. Retain previous runs for verification.

Do not call the observation snapshot builder from forecast ingestion.

### 8.3 Snapshot source selection

Wave and wind selection happen independently:

```text
1. Load source candidates in configured priority order.
2. Load each candidate’s newest normalized observation.
3. Reject fields that are missing, nonphysical, or QC-rejected.
4. Require a coherent wave triplet from one timestamp and one basis.
5. Prefer a primary candidate while it remains within the usable-age limit.
6. Otherwise select the first usable fallback.
7. Record station, role, distance, basis, QC, and fallback state.
8. If no usable source exists, expose the last known component only within
   the stale-cache limit and label it stale.
9. Otherwise expose that component as unavailable.
```

Never stitch height from one station to period or direction from another. Never replace a missing observation with a forecast.

### 8.4 Partial data

- Valid wave plus missing wind: show wave and `WIND --`.
- Valid wind plus missing wave: the API returns a partial snapshot; the sign shows `NO WAVE DATA`.
- Missing direction does not make valid height and period equal zero; render `--`.
- A provider outage does not erase the most recent stored observation.

---

## 9. Objective Trend Math

The sign may show whether measured height is rising, steady, or falling. That is a description of recent measurements, not a surf rating.

For the six-hour wave-height trend:

1. Use accepted observations from the same physical station.
2. Use the same measurement basis throughout the window.
3. Deduplicate timestamps.
4. Require at least four observations and at least three hours of time coverage.
5. Estimate slope with the Theil–Sen median of pairwise slopes. This is more resistant to one bad spike than ordinary least squares.
6. Convert the fitted slope into estimated change over six hours.
7. Classify:

```text
estimated six-hour change >= +0.30 ft  -> rising
estimated six-hour change <= -0.30 ft  -> falling
otherwise                              -> steady
```

Thresholds belong in configuration. If coverage or consistency requirements fail, return `state: "unknown"` and omit the arrow.

Do not compute a trend across a primary-to-fallback station switch. A fallback observation can be shown, but its trend starts with its own source history.

### 9.1 Optional spectral derivations

Full spectra can support more accurate objective summaries later:

```text
m0  = Σ S(fᵢ) Δfᵢ
Hm0 = 4 √m0
Tp  = 1 / fpeak
```

Directional means require circular statistics, not arithmetic averaging:

```text
θmean = atan2(Σ wᵢ sin θᵢ, Σ wᵢ cos θᵢ)
```

A future spectral partitioner may identify separate energy peaks and distinguish wind sea from swell. It must:

- Preserve the source spectrum and algorithm version.
- Validate results against known provider partitions and fixtures.
- Report confidence and input completeness.
- Use `measurement_basis: "spectral_partition"`.
- Render `PART`, not silently upgrade the result to `SWELL`.

Provider-reported valid summary fields remain the V1 source of truth.

---

## 10. Freshness, Fallback, and Reliability

Initial component freshness thresholds:

```yaml
freshness:
  fresh_max_age_minutes: 90
  delayed_max_age_minutes: 180
  stale_max_age_minutes: 360
```

States:

```text
0–90 minutes      fresh
91–180 minutes    delayed
181–360 minutes   stale
over 360 minutes  unavailable
```

Allow station-specific overrides because reporting cadence varies.

These global numbers are a fallback, not the operating values. A provider
stamps an observation with its measurement time and publishes it later, so for
a station reporting every 60 minutes the newest available observation sweeps
across a full hour of age. Verified against `41070`: 200 consecutive
observations exactly 60 minutes apart, with the newest routinely older than 90
minutes. A fixed 90-minute limit therefore reports `DELAYED` during completely
normal operation, which trains the owner to ignore the one word that is
supposed to mean something.

Effective thresholds are resolved per station in this order:

```text
1. An explicit per-station override.
2. Derivation from the station's expected_interval_minutes:
   fresh   = 2.5 x interval
   delayed = 4.0 x interval
   stale   = 7.0 x interval
3. The global defaults above.
```

`DELAYED` then means a report was genuinely missed. A faster station gets a
tighter window rather than inheriting a slow station's patience: hourly `41070`
resolves to `150/240/420`, while the half-hourly `41113` fallback resolves to
`75/120/210`.

Because thresholds vary by station and the compact display payload carries no
station identity, each component in that payload states the limits it was
classified against. Without them an offline sign would have to guess which
limits applied while recalculating its own age.

Top-level `data_state` is the least-fresh required available component:

```text
wave fresh + wind delayed -> delayed
wave fresh + wind absent  -> partial
wave stale + wind fresh   -> stale
wave absent               -> partial or unavailable
```

Fallback and freshness are orthogonal:

- A fresh fallback is `fallback_used: true`, `freshness: fresh`.
- A stale primary is `fallback_used: false`, `freshness: stale`.
- The sign may show both `ALT` and an age.

Reliability requirements:

- Bounded HTTP timeouts.
- Retries with jittered exponential backoff.
- Conditional requests where supported.
- Provider-specific rate limits.
- WAL-backed local history.
- Atomic last-good `CurrentSnapshot` JSON.
- Structured logs with source and request identifiers.
- Health and readiness endpoints.
- Graceful parsing of provider column additions.
- NTP-synchronized system time.
- Display-side cache and independent age calculation.

---

## 11. REST API

Use FastAPI and version public endpoints.

### 11.1 Observation endpoints

```text
GET /v1/spots
GET /v1/spots/{spot_id}
GET /v1/spots/{spot_id}/now
GET /v1/spots/{spot_id}/history?hours=24
GET /v1/spots/{spot_id}/display
GET /v1/spots/{spot_id}/sources

GET /v1/stations
GET /v1/stations/{station_id}
GET /v1/stations/{station_id}/latest
GET /v1/stations/{station_id}/observations?hours=24

GET /v1/health
GET /v1/ready
```

`/now` returns the full `CurrentSnapshot`. `/display` returns a stable compact projection of that exact snapshot.

### 11.2 Forecast endpoints

```text
GET /v1/spots/{spot_id}/forecast?hours=168
GET /v1/spots/{spot_id}/forecast/runs
GET /v1/spots/{spot_id}/forecast/runs/{run_id}
```

### 11.2.1 Tide context endpoint

```text
GET /v1/spots/{spot_id}/tide?hours=48
```

Tide predictions are model output on a third path, separate from both observed
and forecast wave data. Every response carries `mode: "prediction"`, and the
derived phase is reported only when two adjacent predicted extremes bracket the
requested moment. The endpoint returns `404` for a spot with no configured tide
source, and a tide outage never degrades `/now`.

Forecast responses include:

```json
{
  "mode": "forecast",
  "provider": "example-provider",
  "model": "example-model",
  "issued_at": "2026-07-30T12:00:00Z",
  "fetched_at": "2026-07-30T12:18:00Z",
  "points": [
    {
      "valid_at": "2026-07-30T13:00:00Z",
      "lead_hours": 1,
      "wave_height_m": 0.9,
      "wave_period_s": 8.4,
      "wave_direction_deg_true": 48
    }
  ]
}
```

The `mode` discriminator is required. Observation endpoints must never return `mode: "forecast"`.

### 11.3 Compact display endpoint

```json
{
  "schema_version": 1,
  "mode": "observed",
  "spot": "NEW SMYRNA",
  "generated_at": "2026-07-30T17:22:00Z",
  "wave": {
    "label": "SEAS",
    "height_ft": 2.6,
    "period_s": 8.1,
    "direction": "NE",
    "observed_at": "2026-07-30T17:10:00Z",
    "trend": "rising"
  },
  "wind": {
    "direction": "W",
    "speed_mph": 7.8,
    "observed_at": "2026-07-30T17:00:00Z"
  },
  "data_state": "fresh",
  "fallback_used": false
}
```

Keep numeric values numeric. The display client owns rounding and typography. Include observation timestamps so it can recalculate age during an API outage.

### 11.4 Error behavior

- Return stored observations with truthful freshness when upstream providers fail.
- Return a partial snapshot when one component is missing.
- Return `503 Service Unavailable` only when no meaningful current or permitted cached observation can be produced.
- Return forecast errors only from forecast endpoints; a forecast outage must not degrade `/now`.
- Never serialize nonfinite values or replace missing numbers with zero.

---

## 12. The 128×32 Swell Sign

### 12.1 Physical format

Use two horizontally chained `64×32` P4 HUB75 RGB panels for one logical `128×32` canvas. Before enclosure, the combined active area is approximately 20×5 inches. The finished object should read as a single, slim sign rather than two development panels.

Design intent:

- Visible in peripheral vision from across a room.
- Restrained enough to live above a surfboard rack.
- More like a small transit or marine instrument than a television.
- No scrolling on the default face.
- No dashboard chrome.

### 12.2 Default face

```text
+--------------------------------+
| NEW SMYRNA                 12M |
| SEAS  2.6FT  ↑   8.1S      NE |
| WIND  W                  8MPH |
+--------------------------------+
```

The actual renderer uses pixel coordinates rather than a character grid. At minimum:

- Header: configured display name, left; current wave age, right.
- Wave row: measurement label, height, optional trend arrow, period, direction.
- Wind row: wind label, direction, speed.
- `ALT` is visible whenever the selected wave source is a fallback.
- `DELAYED`, `STALE`, or missing-data text replaces decoration before it replaces a measurement.

The age in the header is wave age because wave is the primary observation. Full wave and wind ages remain available through `/now`. A compact wind-age warning appears when wind is materially older than wave.

### 12.3 Objective display states

| Data condition | Text behavior |
|---|---|
| Fresh primary | Normal face plus age |
| Delayed | Show `DELAYED` and age without hiding values |
| Stale cached | Show `STALE` and age; retain last values |
| Fresh fallback | Show `ALT` plus age |
| Partial wind | Show valid wave and `WIND --` |
| No usable wave | Show `NO WAVE DATA` and last successful fetch time if available |
| API unreachable | Continue cached payload, calculate increasing age, show offline indicator |

There are no condition-quality display states.

### 12.4 Visual language

Default palette:

```text
Background            black
Primary numbers       low-saturation sea-glass cyan
Labels and place      warm white
Wind                  muted amber or warm white
Delayed / stale       amber
Unavailable / error   restrained red
```

Use color as atmosphere and redundancy, never as a surf-quality judgment.

Physical finish:

- One continuous smoked or diffusion-acrylic face over both panels.
- Black-painted or dark wood enclosure.
- Hidden fasteners from the viewing angle.
- Shallow shadow gap from the wall.
- Matte surfaces to avoid a plastic development-kit appearance.
- A concealed brightness sensor when practical.

Brightness:

- Apply gamma correction.
- Cap indoor brightness far below panel maximum.
- Support day/evening/night brightness schedules.
- Fade brightness changes over several frames.
- Never flash stale/error states in a living space.

### 12.5 Subtle motion

Motion is optional and nonsemantic. A small one- or two-pixel wave mark may complete one gentle cycle at the reported period—for example, every `8.1` seconds. Measurements remain fixed. Disable motion when period is missing or the data is stale.

The animation must not imply phase-accurate ocean motion or condition quality.

### 12.6 Forecast access

The sign always returns to `NOW`.

A future physical button may show an explicitly labeled `FORECAST` face for 20 seconds. It must never appear through automatic rotation, and forecast values must use visually distinct labeling. This is outside the default V1 face.

---

## 13. Hardware

### 13.1 Recommended V1: Raspberry Pi

```text
Raspberry Pi 4
Adafruit RGB Matrix Bonnet or compatible level-shifting HUB75 HAT
2 × 64×32 indoor P4 HUB75 RGB panels
Dedicated regulated 5 V panel power supply
Fused low-voltage distribution
Appropriate-gauge short power wiring
Smoked/diffusing acrylic
Ventilated enclosure
Secure wall mount
Optional ambient-light sensor
```

The Pi runs the local API/storage stack and the display client. This is the fastest route to a complete first instrument and supports high-quality fonts, simulator parity, and offline operation.

Electrical requirements:

- Never power the panels through the Raspberry Pi.
- Size the supply from the panel manufacturer’s worst-case current, plus margin.
- Fuse the low-voltage panel branch.
- Use level shifting appropriate for HUB75.
- Enclose mains terminals and provide strain relief.
- Protect wiring from sharp frame edges.
- Provide ventilation and test enclosure temperature.
- Configure a conservative current/brightness limit in software.

### 13.2 Split client: MatrixPortal S3

The repository includes a MatrixPortal S3 thin Wi-Fi display client:

```text
Swell Sign API ──Wi-Fi JSON──> MatrixPortal S3 ──HUB75──> 128×32
```

CircuitPython initializes the horizontal chain as one canvas:

```python
matrix = rgbmatrix.RGBMatrix(
    width=128,
    height=32,
    bit_depth=4,
    addr_pins=board.MTX_ADDRESS[:4],
    tile=1,
    serpentine=False,
    doublebuffer=True,
    **board.MTX_COMMON,
)
```

`tile=1` is intentional; it means one vertical panel row. Use stable
CircuitPython 10.2.1 and the matching 10.x MPY bundle. The client must:

- Parse the versioned compact payload.
- Cache the last valid payload in nonvolatile storage.
- Recalculate age from `observed_at`.
- Render stale/offline states without server help.
- Fail closed if `mode` is not `observed`.

Connect the MatrixPortal to the right-hand panel `IN`, then that panel `OUT` to
the left-hand panel `IN`, with both panels upright. Size panel power for 5 V at
8 A worst case and use a regulated 5 V / 10 A supply. Power the MatrixPortal
separately over USB-C, power both panels directly through a fused split, and
share ground. Implement actual dimming by scaling RGB values; the CircuitPython
RGBMatrix brightness property does not provide proportional nonzero dimming.

The Pi renderer remains the visual reference, while the MatrixPortal is the
small production-object client.

### 13.3 E-ink alternative

E-ink remains a possible later edition, but it is not the target build:

- It is quiet and furniture-like.
- It needs ambient light.
- Slow refresh is acceptable for buoy cadence.
- It moves the object closer to existing framed forecast products.

The distinctive V1 is the dim, panoramic LED instrument.

---

## 14. Software Architecture

### 14.1 Stack

```text
Language           Python 3.12+
API                FastAPI
Validation         Pydantic
HTTP               httpx
Database           SQLite 3, WAL mode
Persistence        Explicit sqlite3 schema and repositories
Scheduling         Separate bounded collector loop
Rendering          Pillow for simulator; rpi-rgb-led-matrix on Raspberry Pi
Testing            pytest, FastAPI TestClient, fixture fetchers
Configuration      YAML plus environment overrides
Deployment         native virtualenv + systemd on Pi; containers optional for dev
```

Large data libraries such as pandas, xarray, and netCDF4 should be optional and added only when spectral or gridded provider adapters require them.

### 14.2 Repository layout

```text
swellsign/
|-- README.md
|-- pyproject.toml
|-- .env.example
|-- Dockerfile
|-- compose.yaml
|-- config/
|   `-- spots.yaml
|-- src/swellsign/
|   |-- __init__.py
|   |-- __main__.py
|   |-- api.py
|   |-- cli.py
|   |-- config.py
|   |-- directions.py
|   |-- freshness.py
|   |-- models.py
|   |-- storage.py
|   |-- units.py
|   |-- providers/
|   |   |-- __init__.py
|   |   |-- base.py
|   |   |-- ndbc.py
|   |   |-- coops.py
|   |   `-- open_meteo.py
|   |-- services/
|   |   |-- collector.py
|   |   |-- snapshot.py
|   |   |-- tide.py
|   |   `-- trend.py
|   `-- display/
|       |-- client.py
|       |-- font.py
|       |-- hub75.py
|       |-- palette.py
|       |-- renderer.py
|       |-- simulator.py
|       |-- simulator.html
|       `-- web.py
|-- firmware/
|   `-- matrixportal/
|       |-- code.py
|       |-- settings.example.toml
|       `-- README.md
|-- deploy/systemd/
|   |-- swellsign-api.service
|   |-- swellsign-collector.service
|   `-- swellsign-display.service
|-- examples/
|   `-- display-*.json
`-- tests/
    |-- fixtures/provider/
    |-- test_api.py
    |-- test_cli.py
    |-- test_coops.py
    |-- test_display.py
    |-- test_ndbc.py
    |-- test_open_meteo.py
    |-- test_snapshot.py
    |-- test_storage.py
    `-- test_trend.py
```

### 14.3 Processes

Reference Pi deployment uses three deliberately separate processes:

```text
swellsign-api.service
- serves FastAPI on localhost/LAN
- composes snapshots from stored observations

swellsign-collector.service
- polls NDBC independently from API worker count
- gates each station by its own reporting cadence
- normalizes and stores observations
- archives independent seven-day forecast runs
- archives CO-OPS tide predictions on a third schedule
- atomically refreshes last-good snapshot files

swellsign-display.service
- polls /v1/spots/new-smyrna/display every 15 seconds
- renders 128×32 output
- persists the last valid observed payload
- recalculates age and handles offline state
```

The display poll interval does not control upstream polling.

---

## 15. Provider Interfaces

Observation and forecast providers deliberately implement different interfaces.

```python
from datetime import datetime
from typing import Protocol


class ObservationProvider(Protocol):
    name: str

    async def fetch_latest(self, station_id: str) -> "RawFetch":
        ...

    async def fetch_range(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
    ) -> "RawFetch":
        ...

    def normalize_observations(
        self,
        raw: "RawFetch",
    ) -> list["WaveObservation | WindObservation"]:
        ...


class ForecastProvider(Protocol):
    name: str

    async def fetch_run(
        self,
        location: "ForecastLocation",
        horizon_hours: int,
    ) -> "RawFetch":
        ...

    def normalize_forecast(
        self,
        raw: "RawFetch",
    ) -> tuple["ForecastRun", list["ForecastPoint"]]:
        ...
```

This separation makes accidental forecast-to-observation substitution difficult and testable.

Provider requirements:

- Preserve raw responses and source URLs.
- Use timezone-aware UTC.
- Normalize to SI units.
- Preserve source precision.
- Convert missing markers to `None`.
- Retain QC fields and parser warnings.
- Be idempotent for duplicate fetches.
- Tolerate extra source columns.
- Reject impossible values without turning them into zeros.

---

## 16. Direction Utilities

Cardinal mapping supports 16 points:

```text
N, NNE, NE, ENE, E, ESE, SE, SSE,
S, SSW, SW, WSW, W, WNW, NW, NNW
```

Normalize degrees into `[0, 360)`. Test exact sector boundaries and values close to north:

```text
0
11.24
11.25
22.5
348.74
348.75
359.99
360
-1
```

Use circular differences for directional change. For two directions `a` and `b`:

```python
delta = ((b - a + 180) % 360) - 180
```

Do not use ordinary arithmetic means for circular direction series.

---

## 17. Simulator and Display Contract

The simulator is a first-class client, not throwaway debug code.

It must:

- Consume the same compact payload as the physical sign.
- Render an exact `128×32` PNG.
- Optionally render a nearest-neighbor enlarged preview.
- Offer a browser panel that receives rendered pixels rather than
  reimplementing the layout, so no client can become a second source of truth
  about what the sign shows.
- Load only bundled bitmap fonts.
- Support fresh, delayed, stale, fallback, partial, offline, and no-data fixtures.
- Match Pi output for the same payload.
- Allow palette and brightness approximation.
- Produce deterministic golden images when animation is disabled.

Display contract rules:

- Unknown `schema_version`: retain cache and show a compatibility warning.
- `mode != "observed"`: reject the payload.
- Missing optional fields: show `--`.
- Missing required spot or timestamps: reject the payload as malformed.
- Values outside renderer limits: clamp typography, not data; log the issue.
- No text marquee on the default face.

---

## 18. Testing Requirements

### 18.1 Parser and normalization

Frozen fixtures cover:

- NDBC standard `.txt` headers and rows.
- NDBC `.spec` with valid separated-swell fields.
- Realistic `41070.spec` rows with `MM` separated-swell fields.
- Missing values, duplicate timestamps, extra columns, partial rows, and out-of-order rows.
- Unit conversions without early rounding.
- CDIP alias behavior when that adapter is added.
- Forecast run and point parsing independently from observation parsing.

### 18.2 Snapshot and source selection

Required cases:

```text
41070 WVHT/DPD/MWD valid, Sw* missing
  -> SEAS, total_sea, primary

41070 coherent separated-swell triplet valid in a future fixture
  -> SWELL, separated_swell, primary

41070 too old, 41113 valid
  -> selected 41113, fallback_used true, ALT-capable payload

Height at time A, period at time B
  -> never combined

Wave valid, wind absent
  -> partial snapshot with valid wave

Only forecast point available
  -> no current wave observation

Provider missing marker
  -> None, never 0
```

### 18.3 Trend

- Rising synthetic series.
- Falling synthetic series.
- Steady/noisy series.
- Single outlier does not dominate Theil–Sen.
- Insufficient samples returns unknown.
- Insufficient time coverage returns unknown.
- Primary/fallback history is never combined.
- Separated-swell and total-sea history is never combined.

### 18.4 API

- Full and compact schemas.
- Numeric display values remain numeric.
- Independent wave and wind age.
- Correct source and measurement basis.
- Forecast endpoint `mode: forecast`.
- Observation endpoint `mode: observed`.
- No forecast model can enter a current snapshot.
- Partial and stale behavior.
- `503` only when no meaningful permitted snapshot exists.

### 18.5 Display

Golden `128×32` images cover:

- Fresh primary `SEAS`.
- Fresh primary `SWELL`.
- Rising, falling, steady, and unknown trend.
- Fresh fallback with `ALT`.
- Delayed and three-digit stale age.
- Missing wind.
- Missing direction.
- No wave data.
- Offline cached data.
- Long values and configured spot abbreviations.
- Reduced-brightness readability.

Inspect previews at both native pixel size and expected physical viewing distance before hardware purchase.

---

## 19. Implementation Plan

### Phase 1: Foundation and observation CLI

- Create package, configuration, SQLite schema, and migrations.
- Implement NDBC standard and spectral-summary parsers.
- Configure `41070`, `41069`, and fallback `41113`.
- Preserve raw fetches and normalized history.
- Implement units, directions, freshness, basis selection, and QC.
- Print one full `CurrentSnapshot`.

Completion:

```text
swellsign collect-once
swellsign snapshot new-smyrna
```

prints a source-attributed, recommendation-free observed snapshot.

### Phase 2: API and scheduled collection

- Implement versioned observation endpoints.
- Add station-aware collection schedules.
- Add source selection, fallback, and atomic last-good snapshot.
- Add health/readiness and structured logging.
- Add exact no-forecast-mixing tests.

Completion:

```text
curl http://localhost:8000/v1/spots/new-smyrna/now
curl http://localhost:8000/v1/spots/new-smyrna/display
```

return coherent views of the same current snapshot.

### Phase 3: History and objective trend

- Add history queries.
- Implement six-hour Theil–Sen wave-height trend.
- Add sample/coverage safeguards.
- Expose the objective trend through current and display payloads.

### Phase 4: Forecast archive

- Implement one marine forecast adapter.
- Archive immutable seven-day runs and points.
- Expose forecast-only endpoints.
- Add run-to-observation verification primitives.
- Keep forecast collection failures isolated from current conditions.

### Phase 5: Simulator and art direction

- Implement exact `128×32` renderer.
- Create bundled fonts, palette, and golden fixtures.
- Render physical-size mockups.
- Tune typography, diffuser choice, and night brightness.
- Add optional period-paced micro-animation.

### Phase 6: Physical Raspberry Pi sign

- Assemble power, level shifting, panels, and enclosure.
- Run the API and display as separate systemd services.
- Validate restart, Wi-Fi outage, provider outage, and stale behavior.
- Compare the sign to source pages over several reporting cycles.

### Phase 7: Thin-controller client

- Install the included MatrixPortal S3 CircuitPython client.
- Validate refresh, fonts, reconnect behavior, and panel color order on the
  purchased panel/controller batch.
- Preserve the Pi renderer as reference behavior.

### Phase 8: Expansion

- Add CO-OPS context.
- Add CDIP and generic ERDDAP adapters.
- Add spectral diagnostics and validated partitions.
- Add an explicitly requested forecast face.
- Add Fort Pierce only after a trustworthy live-source policy exists.

---

## 20. V1 Acceptance Criteria

V1 is complete when:

- Two `64×32` panels behave as one `128×32` sign.
- New Smyrna wave observations are collected from `41070`.
- New Smyrna wind observations are collected from `41069`.
- `41113` can be selected as a clearly labeled fallback.
- Missing separated-swell fields at `41070` result in `SEAS`, not `SWELL`.
- Wave height, period, direction, wind direction, wind speed, and age are readable without scrolling.
- The display contains no surf-quality score, recommendation, or equipment advice.
- Wave and wind timestamps, freshness, and provenance remain independent.
- Stale and fallback readings remain legible and unmistakable.
- SQLite stores raw fetches and normalized observation history.
- The six-hour trend is objective, robust, and omitted when evidence is insufficient.
- `/now` and `/display` are projections of the same `CurrentSnapshot`.
- Seven-day forecasts are archived as immutable runs.
- Forecast and observed values use separate models, tables, provider interfaces, and endpoints.
- Automated tests prove forecast data cannot fill a current observation.
- The simulator and physical sign render the same stable contract.
- A brief network outage does not blank the sign or freeze its age.
- The enclosure, diffuser, brightness, and palette make the object suitable for a shared living space.

---

## 21. Reference Links

### NOAA NDBC

- Realtime access: https://www.ndbc.noaa.gov/faq/rt_data_access.shtml
- Measurement descriptions: https://www.ndbc.noaa.gov/faq/measdes.shtml
- Observation descriptions: https://www.ndbc.noaa.gov/obsdes.shtml
- Station table: https://www.ndbc.noaa.gov/data/stations/station_table.txt
- Realtime directory: https://www.ndbc.noaa.gov/data/realtime2/
- Station `41070`: https://www.ndbc.noaa.gov/station_page.php?station=41070
- Station `41069`: https://www.ndbc.noaa.gov/station_page.php?station=41069
- Station `41113`: https://www.ndbc.noaa.gov/station_page.php?station=41113

### CDIP

- Data access: https://cdip.ucsd.edu/m/documents/data_access.html
- ERDDAP: https://erddap.cdip.ucsd.edu/erddap/
- Products/station status: https://cdip.ucsd.edu/m/products/

### Forecast

- Open-Meteo Marine API: https://open-meteo.com/en/docs/marine-weather-api

### Hardware

- Adafruit `64×32` RGB matrix panel: https://www.adafruit.com/product/2278
- RGB Matrix Bonnet: https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/overview
- MatrixPortal S3: https://learn.adafruit.com/adafruit-matrixportal-s3/overview
- Raspberry Pi RGB matrix library: https://github.com/hzeller/rpi-rgb-led-matrix

---

## 22. Final Design Summary

Swell Sign answers:

```text
What is the ocean doing right now?
```

Its default face is an honest instrument reading:

```text
NEW SMYRNA                 12M
SEAS  2.6FT  ↑   8.1S      NE
WIND  W                  8MPH
```

The owner supplies the surf knowledge. The product supplies measured data, its age, its direction of change, and the truth about where it came from.

**No score. Just swell.**
