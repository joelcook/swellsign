"""YAML product configuration plus environment overrides."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import MeasurementBasis, SourceRole, Station


class FreshnessOverride(BaseModel):
    """Explicit thresholds for one station. Any field left unset is derived."""

    fresh_max_age_minutes: int | None = None
    delayed_max_age_minutes: int | None = None
    stale_max_age_minutes: int | None = None


class FreshnessConfig(BaseModel):
    """How old an observation may be before the sign says so.

    A single global threshold cannot be honest across stations. NDBC 41070
    stamps an observation with its measurement time and publishes it later, so
    with hourly reporting the newest available observation sweeps across a full
    hour of age. A 90 minute limit therefore reports DELAYED every hour while
    the buoy is working perfectly, which teaches the owner to ignore the one
    word that is supposed to mean something.

    Thresholds are therefore derived from each station's reporting interval by
    default, and an explicit per-station entry always wins.
    """

    fresh_max_age_minutes: int = 90
    delayed_max_age_minutes: int = 180
    stale_max_age_minutes: int = 360

    derive_from_reporting_interval: bool = True
    fresh_interval_multiplier: float = 2.5
    delayed_interval_multiplier: float = 4.0
    stale_interval_multiplier: float = 7.0

    stations: dict[str, FreshnessOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _thresholds_ascend(self) -> FreshnessConfig:
        if not (
            self.fresh_max_age_minutes
            <= self.delayed_max_age_minutes
            <= self.stale_max_age_minutes
        ):
            raise ValueError("freshness thresholds must ascend: fresh <= delayed <= stale")
        if min(
            self.fresh_interval_multiplier,
            self.delayed_interval_multiplier,
            self.stale_interval_multiplier,
        ) <= 0:
            raise ValueError("freshness interval multipliers must be positive")
        return self


class DisplayConfig(BaseModel):
    """How the physical sign labels things.

    `wave_label` overrides the basis-derived label on the sign only. It does
    not touch `measurement_basis` or `display_label` in `/now`, so the API
    still reports exactly what the number is and a total sea state is never
    recorded as a swell partition anywhere in storage or provenance.

    Set to `SWELL` by the product owner on 2026-07-31, with the tradeoff
    understood: 41070 cannot publish a partition, so the honest label would
    read `SEAS` permanently, and the 41113 fallback now shares the same word.
    Set to null to restore basis-derived labels (SEAS / SWELL / PART).
    """

    wave_label: str | None = None

    # Marine convention is knots; US surf reports usually say mph. Storage
    # stays m/s either way and this converts at the presentation boundary.
    wind_speed_unit: Literal["mph", "kts"] = "mph"

    @model_validator(mode="after")
    def _label_fits_the_field(self) -> DisplayConfig:
        if self.wave_label is not None and not 1 <= len(self.wave_label) <= 5:
            raise ValueError("wave_label must be 1 to 5 characters to fit the label box")
        return self


class TrendConfig(BaseModel):
    window_hours: float = 6
    minimum_samples: int = 4
    minimum_coverage_hours: float = 3
    change_threshold_ft: float = 0.30


class ForecastConfig(BaseModel):
    provider: str = "open_meteo"
    horizon_hours: int = 168
    collection_interval_minutes: int = 360


class WaveSourceConfig(BaseModel):
    station_id: str
    role: SourceRole
    maximum_usable_age_minutes: int = 360
    preferred_basis: list[MeasurementBasis] = Field(
        default_factory=lambda: [
            MeasurementBasis.SEPARATED_SWELL,
            MeasurementBasis.TOTAL_SEA,
        ]
    )


class WindSourceConfig(BaseModel):
    station_id: str
    role: SourceRole
    maximum_usable_age_minutes: int = 360


class TideSourceConfig(BaseModel):
    """Optional CO-OPS prediction context.

    Tide is deliberately not part of the wave/wind source lists: it is model
    output, it never participates in snapshot source selection, and it is not
    required on the default sign face.
    """

    station_id: str
    name: str | None = None
    datum: str = "MLLW"
    horizon_days: int = 3
    collection_interval_minutes: int = 720


class SpotConfig(BaseModel):
    name: str
    display_name: str
    timezone: str
    latitude: float
    longitude: float
    wave_sources: list[WaveSourceConfig]
    wind_sources: list[WindSourceConfig]
    tide_source: TideSourceConfig | None = None


class ProductConfig(BaseModel):
    freshness: FreshnessConfig = Field(default_factory=FreshnessConfig)
    trend: TrendConfig = Field(default_factory=TrendConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    stations: dict[str, Station]
    spots: dict[str, SpotConfig]

    def freshness_for(self, station_id: str | None) -> FreshnessConfig:
        """Effective thresholds for one station.

        Precedence is explicit override, then derivation from the station's
        reporting interval, then the global default. A faster station gets a
        tighter window rather than inheriting a slow station's patience.
        """
        policy = self.freshness
        fresh = policy.fresh_max_age_minutes
        delayed = policy.delayed_max_age_minutes
        stale = policy.stale_max_age_minutes

        station = self.stations.get(station_id) if station_id else None
        interval = station.expected_interval_minutes if station is not None else None
        if policy.derive_from_reporting_interval and interval and interval > 0:
            fresh = round(interval * policy.fresh_interval_multiplier)
            delayed = round(interval * policy.delayed_interval_multiplier)
            stale = round(interval * policy.stale_interval_multiplier)

        override = policy.stations.get(station_id) if station_id else None
        if override is not None:
            fresh = override.fresh_max_age_minutes or fresh
            delayed = override.delayed_max_age_minutes or delayed
            stale = override.stale_max_age_minutes or stale

        # Keep the ladder monotonic even if a partial override inverts it.
        delayed = max(delayed, fresh)
        stale = max(stale, delayed)
        return FreshnessConfig(
            fresh_max_age_minutes=fresh,
            delayed_max_age_minutes=delayed,
            stale_max_age_minutes=stale,
            derive_from_reporting_interval=False,
        )

    def station_interval_minutes(self, station_id: str, default_minutes: int) -> int:
        """How often to ask this station, in minutes.

        An explicit `poll_interval_minutes` always wins, including when it is
        faster than the station reports. That is deliberate: a provider stamps
        an observation with its measurement time and publishes it later, so
        polling only as often as the station reports can leave the sign a full
        reporting cycle behind whatever is actually available. Conditional
        requests make the extra asks cost a 304 apiece.

        With no explicit value, fall back to the reporting cadence, since a
        station that reports hourly gains nothing from a faster blind poll.
        """
        station = self.stations.get(station_id)
        if station is None:
            return default_minutes
        if station.poll_interval_minutes and station.poll_interval_minutes > 0:
            return station.poll_interval_minutes
        declared = station.expected_interval_minutes
        if declared is None or declared <= 0:
            return default_minutes
        return max(default_minutes, declared)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SWELLSIGN_",
        env_file=".env",
        extra="ignore",
    )

    config_path: Path = Path("config/spots.yaml")
    database_path: Path = Path("data/swellsign.db")
    snapshot_dir: Path = Path("data/snapshots")
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    observation_interval_minutes: int = 20


def load_product_config(path: Path | str) -> ProductConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return ProductConfig.model_validate(raw)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_product_config() -> ProductConfig:
    return load_product_config(get_settings().config_path)
