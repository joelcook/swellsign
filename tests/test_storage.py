from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from swellsign.models import (
    ForecastPoint,
    ForecastRun,
    RawFetch,
    Station,
    WaveObservation,
    WindObservation,
)
from swellsign.storage import ImmutableRecordConflict, SQLiteRepository

NOW = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)


def station() -> Station:
    return Station(
        id="ndbc:41070",
        provider="ndbc",
        provider_station_id="41070",
        canonical_physical_station_id="ndbc:41070",
        name="New Smyrna Beach Buoy",
        latitude=29.025,
        longitude=-80.884,
        capabilities=["waves", "wind"],
    )


def raw(fetch_id: str = "raw:one") -> RawFetch:
    return RawFetch(
        id=fetch_id,
        provider="test",
        resource_type="fixture",
        source_url="https://example.test/data",
        requested_at=NOW,
        received_at=NOW + timedelta(seconds=1),
        http_status=200,
        content_type="text/plain",
        body=b"ocean",
    )


def wave(*, height: float = 0.8, observation_id: str = "wave:one") -> WaveObservation:
    return WaveObservation(
        id=observation_id,
        station_id="ndbc:41070",
        observed_at=NOW - timedelta(minutes=10),
        fetched_at=NOW,
        processing_product="fixture_standard",
        significant_height_m=height,
        dominant_period_s=8.1,
        mean_direction_deg_true=45,
        source_url="https://example.test/data",
        raw_fetch_id="raw:one",
    )


def test_initializes_wal_and_idempotently_upserts_observations(tmp_path):
    repository = SQLiteRepository(tmp_path / "swell.db")
    repository.initialize()
    repository.upsert_station(station())
    repository.save_raw_fetch(raw())

    repository.upsert_wave_observation(wave(height=0.8))
    # The natural key wins even when a parser version produces another id.
    repository.upsert_wave_observation(wave(height=0.9, observation_id="wave:replacement"))
    repository.upsert_wind_observation(
        WindObservation(
            id="wind:one",
            station_id="ndbc:41070",
            observed_at=NOW - timedelta(minutes=5),
            fetched_at=NOW,
            processing_product="fixture_standard",
            speed_mps=3.0,
            direction_deg_true=270,
            source_url="https://example.test/data",
            raw_fetch_id="raw:one",
        )
    )

    latest = repository.latest_wave_observations("ndbc:41070")
    assert len(latest) == 1
    assert latest[0].significant_height_m == 0.9
    assert repository.latest_wind_observation("ndbc:41070").speed_mps == 3.0
    assert repository.counts()["wave_observations"] == 1
    assert repository.is_ready()

    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with repository._connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_archives_forecast_runs_immutably_and_supports_as_of(tmp_path):
    repository = SQLiteRepository(tmp_path / "swell.db")
    repository.initialize()
    repository.save_raw_fetch(raw("raw:forecast-1"))
    repository.save_raw_fetch(raw("raw:forecast-2"))

    run_one = ForecastRun(
        id="forecast:one",
        provider="test",
        model="wave-model",
        location_id="new-smyrna",
        fetched_at=NOW,
        horizon_hours=168,
        raw_fetch_id="raw:forecast-1",
    )
    run_two = ForecastRun(
        id="forecast:two",
        provider="test",
        model="wave-model",
        location_id="new-smyrna",
        fetched_at=NOW + timedelta(hours=6),
        horizon_hours=168,
        raw_fetch_id="raw:forecast-2",
    )
    valid_at = NOW + timedelta(hours=1)
    repository.save_forecast(
        run_one,
        [ForecastPoint(run_id=run_one.id, valid_at=valid_at, wave_height_m=1.0)],
    )
    repository.save_forecast(
        run_two,
        [ForecastPoint(run_id=run_two.id, valid_at=valid_at, wave_height_m=1.2)],
    )

    assert repository.latest_forecast("new-smyrna").run.id == "forecast:two"
    historical = repository.latest_forecast(
        "new-smyrna",
        as_of=NOW + timedelta(hours=1),
    )
    assert historical.run.id == "forecast:one"
    assert repository.counts()["forecast_points"] == 2

    conflicting = run_one.model_copy(update={"model": "different-model"})
    with pytest.raises(ImmutableRecordConflict):
        repository.save_forecast(conflicting, [])
