from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from swellsign.api import create_app
from swellsign.config import ProductConfig, Settings
from swellsign.models import (
    ForecastPoint,
    ForecastRun,
    RawFetch,
    Station,
    WaveObservation,
    WindObservation,
)
from swellsign.storage import SQLiteRepository

NOW = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)


def config() -> ProductConfig:
    return ProductConfig.model_validate(
        {
            "stations": {
                "ndbc:41070": Station(
                    id="ndbc:41070",
                    provider="ndbc",
                    provider_station_id="41070",
                    canonical_physical_station_id="ndbc:41070",
                    name="Wave buoy",
                    capabilities=["waves"],
                ),
                "ndbc:41069": Station(
                    id="ndbc:41069",
                    provider="ndbc",
                    provider_station_id="41069",
                    canonical_physical_station_id="ndbc:41069",
                    name="Wind station",
                    capabilities=["wind"],
                ),
            },
            "spots": {
                "new-smyrna": {
                    "name": "New Smyrna Beach",
                    "display_name": "NEW SMYRNA",
                    "timezone": "America/New_York",
                    "latitude": 29.0258,
                    "longitude": -80.927,
                    "wave_sources": [
                        {
                            "station_id": "ndbc:41070",
                            "role": "primary",
                            "maximum_usable_age_minutes": 240,
                            "preferred_basis": ["separated_swell", "total_sea"],
                        }
                    ],
                    "wind_sources": [
                        {
                            "station_id": "ndbc:41069",
                            "role": "primary",
                            "maximum_usable_age_minutes": 240,
                        }
                    ],
                }
            },
        }
    )


def prepare_repository(path, *, with_observations: bool = True) -> SQLiteRepository:
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.upsert_stations(config().stations.values())
    if not with_observations:
        return repository

    repository.save_raw_fetch(
        RawFetch(
            id="raw:observed",
            provider="fixture",
            resource_type="observed",
            source_url="https://example.test/observed",
            requested_at=NOW,
            received_at=NOW,
            http_status=200,
        )
    )
    repository.upsert_wave_observation(
        WaveObservation(
            id="wave:one",
            station_id="ndbc:41070",
            observed_at=NOW - timedelta(minutes=12),
            fetched_at=NOW,
            processing_product="standard",
            significant_height_m=0.8,
            dominant_period_s=8.1,
            mean_direction_deg_true=45,
            source_url="https://example.test/observed",
            raw_fetch_id="raw:observed",
        )
    )
    repository.upsert_wind_observation(
        WindObservation(
            id="wind:one",
            station_id="ndbc:41069",
            observed_at=NOW - timedelta(minutes=22),
            fetched_at=NOW,
            processing_product="standard",
            speed_mps=3.5,
            direction_deg_true=270,
            source_url="https://example.test/observed",
            raw_fetch_id="raw:observed",
        )
    )
    return repository


def application(tmp_path, *, with_observations: bool = True):
    database_path = tmp_path / "api.db"
    repository = prepare_repository(database_path, with_observations=with_observations)
    settings = Settings(
        database_path=database_path,
        snapshot_dir=tmp_path / "snapshots",
    )
    return (
        create_app(
            settings,
            config(),
            repository,
            clock=lambda: NOW,
        ),
        repository,
    )


def test_observation_and_compact_endpoints_share_one_truth(tmp_path):
    app, _ = application(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/health").json() == {"status": "ok"}
        assert client.get("/v1/ready").status_code == 200

        current = client.get("/v1/spots/new-smyrna/now")
        display = client.get("/v1/spots/new-smyrna/display")

    assert current.status_code == 200
    assert display.status_code == 200
    current_body = current.json()
    display_body = display.json()
    assert current_body["mode"] == display_body["mode"] == "observed"
    assert current_body["wave"]["display_label"] == display_body["wave"]["label"] == "SEAS"
    assert current_body["wave"]["age_minutes"] == display_body["wave"]["age_minutes"] == 12
    assert current_body["wind"]["age_minutes"] == display_body["wind"]["age_minutes"] == 22
    assert isinstance(display_body["wave"]["height_ft"], float)
    assert "score" not in display_body


def test_now_is_503_without_wave_but_display_remains_renderable(tmp_path):
    app, _ = application(tmp_path, with_observations=False)
    with TestClient(app) as client:
        current = client.get("/v1/spots/new-smyrna/now")
        display = client.get("/v1/spots/new-smyrna/display")

    assert current.status_code == 503
    assert display.status_code == 200
    assert display.json()["mode"] == "observed"
    assert display.json()["wave"] is None
    assert display.json()["data_state"] == "unavailable"


def test_forecast_archive_has_an_explicit_separate_mode(tmp_path):
    app, repository = application(tmp_path)
    repository.save_raw_fetch(
        RawFetch(
            id="raw:forecast",
            provider="fixture",
            resource_type="forecast",
            source_url="https://example.test/forecast",
            requested_at=NOW,
            received_at=NOW,
            http_status=200,
        )
    )
    run = ForecastRun(
        id="forecast:one",
        provider="fixture",
        model="model",
        location_id="new-smyrna",
        fetched_at=NOW,
        horizon_hours=168,
        raw_fetch_id="raw:forecast",
    )
    repository.save_forecast(
        run,
        [
            ForecastPoint(
                run_id=run.id,
                valid_at=NOW + timedelta(hours=1),
                wave_height_m=1.1,
            )
        ],
    )

    with TestClient(app) as client:
        forecast = client.get("/v1/spots/new-smyrna/forecast?hours=24")
        current = client.get("/v1/spots/new-smyrna/now")

    assert forecast.status_code == 200
    assert forecast.json()["mode"] == "forecast"
    assert forecast.json()["points"][0]["wave_height_m"] == 1.1
    assert current.json()["mode"] == "observed"
    assert "run" not in current.json()


def test_unknown_resources_are_404_and_windows_are_bounded(tmp_path):
    app, _ = application(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/spots/atlantis").status_code == 404
        assert client.get("/v1/stations/nope").status_code == 404
        assert client.get("/v1/spots/new-smyrna/history?hours=9999").status_code == 422
