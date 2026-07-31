"""YAML product configuration plus environment overrides."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import MeasurementBasis, SourceRole, Station


class FreshnessConfig(BaseModel):
    fresh_max_age_minutes: int = 90
    delayed_max_age_minutes: int = 180
    stale_max_age_minutes: int = 360


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
    stations: dict[str, Station]
    spots: dict[str, SpotConfig]

    def station_interval_minutes(self, station_id: str, default_minutes: int) -> int:
        """Poll cadence for one station.

        A station that reports hourly gains nothing from a twenty-minute poll,
        and NOAA gains nothing from serving it.
        """
        station = self.stations.get(station_id)
        declared = station.expected_interval_minutes if station is not None else None
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
