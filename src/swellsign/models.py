"""Canonical domain and API models.

Observed and forecast values deliberately use separate model families so a
forecast point cannot accidentally satisfy the current-observation contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeasurementBasis(StrEnum):
    SEPARATED_SWELL = "separated_swell"
    TOTAL_SEA = "total_sea"
    SPECTRAL_PARTITION = "spectral_partition"

    @property
    def display_label(self) -> str:
        return {
            self.SEPARATED_SWELL: "SWELL",
            self.TOTAL_SEA: "SEAS",
            self.SPECTRAL_PARTITION: "PART",
        }[self]


class Freshness(StrEnum):
    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class DataState(StrEnum):
    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class TrendState(StrEnum):
    RISING = "rising"
    FALLING = "falling"
    STEADY = "steady"
    UNKNOWN = "unknown"


class SourceRole(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"


class RawFetch(StrictModel):
    id: str
    provider: str
    resource_type: str
    source_url: str
    requested_at: datetime
    received_at: datetime | None = None
    http_status: int | None = None
    content_type: str | None = None
    body: bytes = b""
    error: dict[str, Any] | None = None

    _utc_requested = field_validator("requested_at")(require_utc)
    _utc_received = field_validator("received_at")(lambda value: require_utc(value) if value else value)


class Station(StrictModel):
    id: str
    provider: str
    provider_station_id: str
    canonical_physical_station_id: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    water_depth_m: float | None = None
    platform_type: str | None = None
    active_status: str = "active"
    expected_interval_minutes: int | None = None
    capabilities: list[str] = Field(default_factory=list)
    attribution: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservationBase(StrictModel):
    id: str
    station_id: str
    observed_at: datetime
    received_at: datetime | None = None
    fetched_at: datetime
    processing_product: str
    qc_status: str = "accepted"
    qc_detail: dict[str, Any] = Field(default_factory=dict)
    source_url: str
    raw_fetch_id: str

    _utc_observed = field_validator("observed_at")(require_utc)
    _utc_received = field_validator("received_at")(lambda value: require_utc(value) if value else value)
    _utc_fetched = field_validator("fetched_at")(require_utc)


class WaveObservation(ObservationBase):
    significant_height_m: float | None = None
    dominant_period_s: float | None = None
    average_period_s: float | None = None
    mean_direction_deg_true: float | None = None
    swell_height_m: float | None = None
    swell_period_s: float | None = None
    swell_direction_deg_true: float | None = None
    wind_wave_height_m: float | None = None
    wind_wave_period_s: float | None = None
    wind_wave_direction_deg_true: float | None = None
    water_temperature_c: float | None = None


class WindObservation(ObservationBase):
    speed_mps: float | None = None
    gust_mps: float | None = None
    direction_deg_true: float | None = None


class TidePrediction(StrictModel):
    """One astronomical high or low water extreme.

    Tide predictions are model output, not measurement.  They live in their own
    model and table so they can never be mistaken for an observed water level
    or reach a :class:`CurrentSnapshot`.
    """

    id: str
    station_id: str
    predicted_at: datetime
    height_m: float
    kind: Literal["high", "low"]
    datum: str
    fetched_at: datetime
    source_url: str
    raw_fetch_id: str

    _utc_predicted = field_validator("predicted_at")(require_utc)
    _utc_fetched = field_validator("fetched_at")(require_utc)


class TidePhase(StrictModel):
    """Derived phase between two adjacent predicted extremes."""

    mode: Literal["prediction"] = "prediction"
    station_id: str
    state: Literal["rising", "falling"]
    previous_extreme: TidePrediction
    next_extreme: TidePrediction
    minutes_to_next_extreme: int
    percent_through: float = Field(ge=0.0, le=100.0)
    datum: str


class ForecastRun(StrictModel):
    id: str
    provider: str
    model: str
    location_id: str
    issued_at: datetime | None = None
    fetched_at: datetime
    horizon_hours: int
    raw_fetch_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    _utc_issued = field_validator("issued_at")(lambda value: require_utc(value) if value else value)
    _utc_fetched = field_validator("fetched_at")(require_utc)


class ForecastPoint(StrictModel):
    run_id: str
    valid_at: datetime
    wave_height_m: float | None = None
    wave_period_s: float | None = None
    wave_direction_deg_true: float | None = None
    swell_height_m: float | None = None
    swell_period_s: float | None = None
    swell_direction_deg_true: float | None = None
    wind_wave_height_m: float | None = None
    wind_wave_period_s: float | None = None
    wind_wave_direction_deg_true: float | None = None
    wind_speed_mps: float | None = None
    wind_direction_deg_true: float | None = None
    qc_status: str = "accepted"

    _utc_valid = field_validator("valid_at")(require_utc)


class ComponentSource(StrictModel):
    station_id: str
    role: SourceRole
    fallback_used: bool
    distance_to_spot_m: float | None = None
    qc_status: str


class WaveSnapshot(StrictModel):
    height_m: float
    height_ft: float
    period_s: float
    direction_deg_true: float | None = None
    direction_cardinal: str | None = None
    measurement_basis: MeasurementBasis
    display_label: str
    observed_at: datetime
    age_minutes: int
    freshness: Freshness
    source: ComponentSource

    _utc_observed = field_validator("observed_at")(require_utc)


class WindSnapshot(StrictModel):
    speed_mps: float
    speed_mph: float
    gust_mps: float | None = None
    gust_mph: float | None = None
    direction_deg_true: float | None = None
    direction_cardinal: str | None = None
    observed_at: datetime
    age_minutes: int
    freshness: Freshness
    source: ComponentSource

    _utc_observed = field_validator("observed_at")(require_utc)


class WaveHeightTrend(StrictModel):
    state: TrendState
    window_hours: float
    estimated_change_m: float | None = None
    estimated_change_ft: float | None = None
    sample_count: int
    station_id: str
    measurement_basis: MeasurementBasis


class TrendSnapshot(StrictModel):
    wave_height: WaveHeightTrend | None = None


class SpotIdentity(StrictModel):
    id: str
    name: str
    display_name: str
    timezone: str


class CurrentSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    mode: Literal["observed"] = "observed"
    spot: SpotIdentity
    generated_at: datetime
    wave: WaveSnapshot | None = None
    wind: WindSnapshot | None = None
    trend: TrendSnapshot = Field(default_factory=TrendSnapshot)
    data_state: DataState
    fallback_used: bool = False
    warnings: list[str] = Field(default_factory=list)

    _utc_generated = field_validator("generated_at")(require_utc)


class CompactWave(StrictModel):
    label: str
    height_ft: float
    period_s: float
    direction: str | None = None
    observed_at: datetime
    age_minutes: int
    freshness: Freshness
    trend: TrendState = TrendState.UNKNOWN

    _utc_observed = field_validator("observed_at")(require_utc)


class CompactWind(StrictModel):
    direction: str | None = None
    speed_mph: float
    observed_at: datetime
    age_minutes: int
    freshness: Freshness

    _utc_observed = field_validator("observed_at")(require_utc)


class CompactDisplayPayload(StrictModel):
    schema_version: Literal[1] = 1
    mode: Literal["observed"] = "observed"
    spot: str
    generated_at: datetime
    wave: CompactWave | None = None
    wind: CompactWind | None = None
    data_state: DataState
    fallback_used: bool = False
    warnings: list[str] = Field(default_factory=list)

    _utc_generated = field_validator("generated_at")(require_utc)


class ForecastResponse(StrictModel):
    mode: Literal["forecast"] = "forecast"
    run: ForecastRun
    points: list[ForecastPoint]

