from datetime import UTC, datetime, timedelta

from swellsign.config import ProductConfig
from swellsign.models import ForecastPoint, ForecastRun, RawFetch, Station
from swellsign.services.snapshot import SnapshotComposer, compact_display_payload
from swellsign.storage import SQLiteRepository


def test_forecast_only_database_cannot_fill_current_or_display_wave(tmp_path):
    now = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)
    station = Station(
        id="ndbc:41070",
        provider="ndbc",
        provider_station_id="41070",
        canonical_physical_station_id="ndbc:41070",
        name="Wave buoy",
        capabilities=["waves"],
    )
    config = ProductConfig.model_validate(
        {
            "stations": {station.id: station},
            "spots": {
                "new-smyrna": {
                    "name": "New Smyrna Beach",
                    "display_name": "NEW SMYRNA",
                    "timezone": "America/New_York",
                    "latitude": 29.0258,
                    "longitude": -80.927,
                    "wave_sources": [
                        {
                            "station_id": station.id,
                            "role": "primary",
                            "preferred_basis": ["separated_swell", "total_sea"],
                        }
                    ],
                    "wind_sources": [],
                }
            },
        }
    )
    repository = SQLiteRepository(tmp_path / "boundary.db")
    repository.initialize()
    repository.upsert_station(station)
    repository.save_raw_fetch(
        RawFetch(
            id="raw:forecast-only",
            provider="fixture",
            resource_type="forecast",
            source_url="https://example.test/forecast",
            requested_at=now,
            received_at=now,
            http_status=200,
        )
    )
    run = ForecastRun(
        id="forecast:only",
        provider="fixture",
        model="model",
        location_id="new-smyrna",
        fetched_at=now,
        horizon_hours=168,
        raw_fetch_id="raw:forecast-only",
    )
    repository.save_forecast(
        run,
        [
            ForecastPoint(
                run_id=run.id,
                valid_at=now + timedelta(hours=1),
                wave_height_m=4.2,
                wave_period_s=15,
                wave_direction_deg_true=90,
            )
        ],
    )

    current = SnapshotComposer(repository, config).compose("new-smyrna", now=now)
    display = compact_display_payload(current)

    assert repository.latest_forecast("new-smyrna").points[0].wave_height_m == 4.2
    assert current.mode == "observed"
    assert current.wave is None
    assert display.mode == "observed"
    assert display.wave is None
