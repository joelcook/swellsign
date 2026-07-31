"""Versioned FastAPI surface for the observation instrument and forecast archive."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .config import (
    ProductConfig,
    Settings,
    get_product_config,
    get_settings,
)
from .models import (
    CompactDisplayPayload,
    CurrentSnapshot,
    ForecastResponse,
    SpotIdentity,
)
from .services.snapshot import (
    SnapshotComposer,
    UnknownSpotError,
    compact_display_payload,
)
from .services.tide import TideContextService
from .storage import SQLiteRepository

Clock = Callable[[], datetime]


def create_app(
    settings: Settings | None = None,
    product_config: ProductConfig | None = None,
    repository: SQLiteRepository | None = None,
    composer: SnapshotComposer | None = None,
    *,
    clock: Clock | None = None,
) -> FastAPI:
    """Build an application with injectable persistence and time for tests."""

    resolved_settings = settings or get_settings()
    resolved_config = product_config or get_product_config()
    resolved_repository = repository or SQLiteRepository(resolved_settings.database_path)
    resolved_composer = composer or SnapshotComposer(resolved_repository, resolved_config)
    resolved_tide = TideContextService(resolved_repository, resolved_config)
    utc_clock = clock or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        resolved_repository.initialize()
        resolved_repository.upsert_stations(resolved_config.stations.values())
        application.state.repository = resolved_repository
        application.state.product_config = resolved_config
        application.state.composer = resolved_composer
        yield

    application = FastAPI(
        title="Swell Sign",
        version="0.1.0",
        description="Observation-first ocean data for a 128x32 LED instrument.",
        lifespan=lifespan,
    )

    @application.get("/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/v1/ready")
    def ready() -> JSONResponse:
        if not resolved_repository.is_ready():
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "database": "unavailable"},
            )
        return JSONResponse(content={"status": "ready", "database": "ok"})

    @application.get("/v1/spots", response_model=list[SpotIdentity])
    def spots() -> list[SpotIdentity]:
        return [
            SpotIdentity(
                id=spot_id,
                name=spot.name,
                display_name=spot.display_name,
                timezone=spot.timezone,
            )
            for spot_id, spot in resolved_config.spots.items()
        ]

    @application.get("/v1/spots/{spot_id}")
    def spot_detail(spot_id: str) -> dict[str, Any]:
        spot = _spot_or_404(resolved_config, spot_id)
        return {
            "id": spot_id,
            **spot.model_dump(mode="json"),
        }

    @application.get("/v1/spots/{spot_id}/now", response_model=CurrentSnapshot)
    def current(spot_id: str) -> CurrentSnapshot:
        snapshot = _compose_or_404(resolved_composer, spot_id, utc_clock())
        if snapshot.wave is None:
            raise HTTPException(
                status_code=503,
                detail="no usable observed wave data inside the stale-cache limit",
            )
        return snapshot

    @application.get(
        "/v1/spots/{spot_id}/display",
        response_model=CompactDisplayPayload,
    )
    def display(spot_id: str) -> CompactDisplayPayload:
        snapshot = _compose_or_404(resolved_composer, spot_id, utc_clock())
        return compact_display_payload(snapshot)

    @application.get("/v1/spots/{spot_id}/history")
    def history(
        spot_id: str,
        hours: Annotated[int, Query(ge=1, le=168)] = 24,
    ) -> dict[str, Any]:
        spot = _spot_or_404(resolved_config, spot_id)
        generated_at = _aware_utc(utc_clock())
        start = generated_at - timedelta(hours=hours)
        wave: list[Any] = []
        wind: list[Any] = []
        seen_wave_sources: set[str] = set()
        seen_wind_sources: set[str] = set()
        for source in spot.wave_sources:
            if source.station_id in seen_wave_sources:
                continue
            seen_wave_sources.add(source.station_id)
            wave.extend(
                resolved_repository.wave_observations(
                    source.station_id,
                    start=start,
                    end=generated_at,
                    limit=5_000,
                    ascending=True,
                )
            )
        for source in spot.wind_sources:
            if source.station_id in seen_wind_sources:
                continue
            seen_wind_sources.add(source.station_id)
            wind.extend(
                resolved_repository.wind_observations(
                    source.station_id,
                    start=start,
                    end=generated_at,
                    limit=5_000,
                    ascending=True,
                )
            )
        return {
            "mode": "observed",
            "spot_id": spot_id,
            "generated_at": generated_at,
            "hours": hours,
            "wave_observations": wave,
            "wind_observations": wind,
        }

    @application.get("/v1/spots/{spot_id}/sources")
    def spot_sources(spot_id: str) -> dict[str, Any]:
        spot = _spot_or_404(resolved_config, spot_id)
        return {
            "spot_id": spot_id,
            "wave": [
                {
                    **source.model_dump(mode="json"),
                    "station": resolved_config.stations.get(source.station_id),
                }
                for source in spot.wave_sources
            ],
            "wind": [
                {
                    **source.model_dump(mode="json"),
                    "station": resolved_config.stations.get(source.station_id),
                }
                for source in spot.wind_sources
            ],
            "tide": (
                None
                if spot.tide_source is None
                else {
                    **spot.tide_source.model_dump(mode="json"),
                    "mode": "prediction",
                    "station": resolved_config.stations.get(spot.tide_source.station_id),
                }
            ),
        }

    @application.get("/v1/spots/{spot_id}/tide")
    def tide(
        spot_id: str,
        hours: Annotated[int, Query(ge=1, le=336)] = 48,
    ) -> dict[str, Any]:
        """Astronomical high/low context.

        This is model output, and `mode` says so on every response. It is a
        sibling of the forecast archive, never a component of `/now`.
        """
        spot = _spot_or_404(resolved_config, spot_id)
        if spot.tide_source is None:
            raise HTTPException(
                status_code=404,
                detail="no tide prediction source is configured for this spot",
            )
        now = _aware_utc(utc_clock())
        extremes = resolved_repository.tide_predictions(
            spot.tide_source.station_id,
            start=now - timedelta(hours=6),
            end=now + timedelta(hours=hours),
        )
        return {
            "mode": "prediction",
            "spot_id": spot_id,
            "station_id": spot.tide_source.station_id,
            "datum": spot.tide_source.datum,
            "generated_at": now,
            "phase": resolved_tide.phase(spot_id, now=now),
            "extremes": extremes,
        }

    @application.get("/v1/stations")
    def stations() -> list[Any]:
        return resolved_repository.list_stations()

    @application.get("/v1/stations/{station_id}")
    def station(station_id: str) -> Any:
        stored = resolved_repository.get_station(station_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="unknown station")
        return stored

    @application.get("/v1/stations/{station_id}/latest")
    def station_latest(station_id: str) -> dict[str, Any]:
        _station_or_404(resolved_repository, station_id)
        wave_rows = resolved_repository.latest_wave_observations(station_id, limit=1)
        return {
            "mode": "observed",
            "station_id": station_id,
            "wave": wave_rows[0] if wave_rows else None,
            "wind": resolved_repository.latest_wind_observation(station_id),
        }

    @application.get("/v1/stations/{station_id}/observations")
    def station_observations(
        station_id: str,
        hours: Annotated[int, Query(ge=1, le=720)] = 24,
    ) -> dict[str, Any]:
        _station_or_404(resolved_repository, station_id)
        end = _aware_utc(utc_clock())
        start = end - timedelta(hours=hours)
        return {
            "mode": "observed",
            "station_id": station_id,
            "generated_at": end,
            "wave_observations": resolved_repository.wave_observations(
                station_id,
                start=start,
                end=end,
                limit=10_000,
                ascending=True,
            ),
            "wind_observations": resolved_repository.wind_observations(
                station_id,
                start=start,
                end=end,
                limit=10_000,
                ascending=True,
            ),
        }

    @application.get(
        "/v1/spots/{spot_id}/forecast",
        response_model=ForecastResponse,
    )
    def forecast(
        spot_id: str,
        hours: Annotated[int, Query(ge=1, le=168)] = 168,
        as_of: datetime | None = None,
    ) -> ForecastResponse:
        _spot_or_404(resolved_config, spot_id)
        if as_of is not None:
            as_of = _aware_utc(as_of)
        response = resolved_repository.latest_forecast(
            spot_id,
            start=_aware_utc(utc_clock()),
            hours=hours,
            as_of=as_of,
        )
        if response is None:
            raise HTTPException(status_code=404, detail="no archived forecast run")
        return response

    @application.get("/v1/spots/{spot_id}/forecast/runs")
    def forecast_runs(
        spot_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        _spot_or_404(resolved_config, spot_id)
        if as_of is not None:
            as_of = _aware_utc(as_of)
        return {
            "mode": "forecast",
            "spot_id": spot_id,
            "runs": resolved_repository.list_forecast_runs(
                spot_id,
                limit=limit,
                as_of=as_of,
            ),
        }

    @application.get(
        "/v1/spots/{spot_id}/forecast/runs/{run_id}",
        response_model=ForecastResponse,
    )
    def forecast_run(spot_id: str, run_id: str) -> ForecastResponse:
        _spot_or_404(resolved_config, spot_id)
        response = resolved_repository.forecast_response(run_id)
        if response is None or response.run.location_id != spot_id:
            raise HTTPException(status_code=404, detail="unknown forecast run")
        return response

    return application


def _spot_or_404(product_config: ProductConfig, spot_id: str):
    spot = product_config.spots.get(spot_id)
    if spot is None:
        raise HTTPException(status_code=404, detail="unknown spot")
    return spot


def _station_or_404(repository: SQLiteRepository, station_id: str):
    station = repository.get_station(station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="unknown station")
    return station


def _compose_or_404(
    composer: SnapshotComposer,
    spot_id: str,
    now: datetime,
) -> CurrentSnapshot:
    try:
        return composer.compose(spot_id, now=_aware_utc(now))
    except UnknownSpotError as exc:
        raise HTTPException(status_code=404, detail="unknown spot") from exc


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise HTTPException(status_code=422, detail="timestamp must include a UTC offset")
    return value.astimezone(UTC)


# Uvicorn import target: ``uvicorn swellsign.api:app``.
app = create_app()
