"""Explicit observation and forecast collection jobs.

The collector is intentionally a separate process from FastAPI.  That avoids
duplicate upstream polling when the API has multiple workers and makes the two
different collection cadences visible.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import ProductConfig, Settings
from ..models import ForecastPoint, ForecastRun, RawFetch, WaveObservation, WindObservation
from ..storage import SQLiteRepository
from .snapshot import SnapshotComposer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CollectionResult:
    """A compact operational summary suitable for logs and CLI output."""

    started_at: datetime
    finished_at: datetime | None = None
    raw_fetches: int = 0
    wave_observations: int = 0
    wind_observations: int = 0
    forecast_runs: int = 0
    forecast_points: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


class CollectionService:
    def __init__(
        self,
        repository: SQLiteRepository,
        product_config: ProductConfig,
        *,
        observation_providers: Mapping[str, Any] | None = None,
        forecast_provider: Any | None = None,
        snapshot_dir: Path | str | None = None,
        composer: SnapshotComposer | None = None,
    ) -> None:
        self.repository = repository
        self.product_config = product_config
        self.observation_providers = dict(observation_providers or {})
        self.forecast_provider = forecast_provider
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir is not None else None
        self.composer = composer or SnapshotComposer(repository, product_config)

    async def collect_observations_once(self) -> CollectionResult:
        result = CollectionResult(started_at=datetime.now(UTC))

        for station in self.product_config.stations.values():
            if not {"waves", "wind", "separated_swell"}.intersection(station.capabilities):
                continue
            provider = self.observation_providers.get(station.provider)
            if provider is None:
                result.errors[station.id] = f"no observation provider configured for {station.provider}"
                continue

            try:
                raw_fetches = await _fetch_station(provider, station.id, station.capabilities)
            except Exception as exc:  # one source must not stop independent sources
                result.errors[station.id] = f"{type(exc).__name__}: {exc}"
                logger.exception("observation fetch failed", extra={"station_id": station.id})
                continue

            for raw in raw_fetches:
                resource_key = f"{station.id}:{raw.resource_type}"
                try:
                    self.repository.save_raw_fetch(raw)
                    result.raw_fetches += 1
                    if raw.error is not None:
                        result.errors[resource_key] = _raw_error_summary(raw)
                        continue
                    normalized = _normalize_observations(provider, raw, station.id)
                    wave_rows = [
                        row for row in normalized if isinstance(row, WaveObservation)
                    ]
                    wind_rows = [
                        row for row in normalized if isinstance(row, WindObservation)
                    ]
                    result.wave_observations += self.repository.upsert_wave_observations(
                        wave_rows
                    )
                    result.wind_observations += self.repository.upsert_wind_observations(
                        wind_rows
                    )
                    logger.info(
                        "observation resource collected",
                        extra={
                            "station_id": station.id,
                            "resource_type": raw.resource_type,
                            "rows": len(normalized),
                        },
                    )
                except Exception as exc:
                    result.errors[resource_key] = f"{type(exc).__name__}: {exc}"
                    logger.exception(
                        "observation resource failed",
                        extra={
                            "station_id": station.id,
                            "resource_type": raw.resource_type,
                        },
                    )

        if self.snapshot_dir is not None:
            for spot_id in self.product_config.spots:
                try:
                    snapshot = self.composer.compose(spot_id)
                    if snapshot.wave is not None:
                        _atomic_snapshot(self.snapshot_dir, spot_id, snapshot.model_dump_json())
                except Exception as exc:
                    result.errors[f"snapshot:{spot_id}"] = f"{type(exc).__name__}: {exc}"
                    logger.exception("snapshot rebuild failed", extra={"spot_id": spot_id})

        result.finished_at = datetime.now(UTC)
        return result

    async def collect_forecast_once(self) -> CollectionResult:
        result = CollectionResult(started_at=datetime.now(UTC))
        if self.forecast_provider is None:
            result.errors["forecast"] = "no forecast provider configured"
            result.finished_at = datetime.now(UTC)
            return result

        # Import lazily so observation-only installations and parser tests do
        # not require a particular provider package layout.
        from ..providers.base import ForecastLocation

        for spot_id, spot in self.product_config.spots.items():
            try:
                location = ForecastLocation(
                    id=spot_id,
                    latitude=spot.latitude,
                    longitude=spot.longitude,
                    timezone=spot.timezone,
                )
                raw: RawFetch = await self.forecast_provider.fetch_run(
                    location,
                    self.product_config.forecast.horizon_hours,
                )
                self.repository.save_raw_fetch(raw)
                result.raw_fetches += 1
                run, points = self.forecast_provider.normalize_forecast(raw)
                _validate_forecast_result(run, points, spot_id)
                self.repository.save_forecast(run, points)
                result.forecast_runs += 1
                result.forecast_points += len(points)
            except Exception as exc:
                result.errors[spot_id] = f"{type(exc).__name__}: {exc}"
                logger.exception("forecast source failed", extra={"spot_id": spot_id})

        result.finished_at = datetime.now(UTC)
        return result

    async def run(
        self,
        *,
        observation_interval_minutes: int,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run both jobs on independent monotonic schedules until stopped."""

        stop = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        next_observation = loop.time()
        next_forecast = loop.time()
        forecast_interval_seconds = self.product_config.forecast.collection_interval_minutes * 60
        observation_interval_seconds = observation_interval_minutes * 60

        while not stop.is_set():
            monotonic_now = loop.time()
            if monotonic_now >= next_observation:
                await self.collect_observations_once()
                next_observation = monotonic_now + observation_interval_seconds
            if monotonic_now >= next_forecast:
                await self.collect_forecast_once()
                next_forecast = monotonic_now + forecast_interval_seconds

            delay = max(0.1, min(next_observation, next_forecast) - loop.time())
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=min(delay, 30))


def build_default_collection_service(
    settings: Settings,
    product_config: ProductConfig,
    repository: SQLiteRepository,
) -> CollectionService:
    """Construct built-in providers without coupling imports to module startup."""

    observation_providers: dict[str, Any] = {}
    if any(station.provider == "ndbc" for station in product_config.stations.values()):
        from ..providers.ndbc import NdbcProvider

        observation_providers["ndbc"] = NdbcProvider()

    forecast_provider: Any | None = None
    if product_config.forecast.provider == "open_meteo":
        from ..providers.open_meteo import OpenMeteoProvider

        forecast_provider = OpenMeteoProvider()

    return CollectionService(
        repository,
        product_config,
        observation_providers=observation_providers,
        forecast_provider=forecast_provider,
        snapshot_dir=settings.snapshot_dir,
    )


async def _fetch_station(
    provider: Any,
    station_id: str,
    capabilities: list[str],
) -> list[RawFetch]:
    if hasattr(provider, "fetch_products") and (
        "waves" in capabilities or "separated_swell" in capabilities
    ):
        result = await provider.fetch_products(station_id)
    else:
        result = await provider.fetch_latest(station_id)
    if isinstance(result, RawFetch):
        return [result]
    rows = list(result)
    if not all(isinstance(row, RawFetch) for row in rows):
        raise TypeError("observation provider returned a non-RawFetch value")
    return rows


def _normalize_observations(
    provider: Any,
    raw: RawFetch,
    station_id: str,
) -> list[WaveObservation | WindObservation]:
    normalize = provider.normalize_observations
    parameters = inspect.signature(normalize).parameters
    if "station_id" in parameters:
        return list(normalize(raw, station_id=station_id))
    return list(normalize(raw))


def _validate_forecast_result(
    run: ForecastRun,
    points: list[ForecastPoint],
    expected_location_id: str,
) -> None:
    if run.location_id != expected_location_id:
        raise ValueError(
            f"forecast location {run.location_id!r} does not match {expected_location_id!r}"
        )
    if any(point.run_id != run.id for point in points):
        raise ValueError("forecast provider returned points for another run")


def _raw_error_summary(raw: RawFetch) -> str:
    kind = raw.error.get("kind") if raw.error else None
    detail = raw.error.get("message") if raw.error else None
    return str(detail or kind or "provider fetch failed")


def _atomic_snapshot(directory: Path, spot_id: str, payload: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{spot_id}.json"
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{spot_id}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
    return destination


# The implementation plan and CLI use both spellings.
CollectorService = CollectionService
