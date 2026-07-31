from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from swellsign.config import FreshnessConfig, load_product_config
from swellsign.directions import degrees_to_cardinal, normalize_degrees
from swellsign.freshness import aggregate_data_state, classify_freshness
from swellsign.models import DataState, ForecastPoint, Freshness, RawFetch


@pytest.mark.parametrize(
    ("degrees", "expected"),
    [
        (0, "N"),
        (11.24, "N"),
        (11.25, "NNE"),
        (22.5, "NNE"),
        (348.74, "NNW"),
        (348.75, "N"),
        (359.99, "N"),
        (360, "N"),
        (-1, "N"),
    ],
)
def test_cardinal_boundaries(degrees, expected):
    assert degrees_to_cardinal(degrees) == expected


def test_degrees_normalize_without_turning_missing_into_zero():
    assert normalize_degrees(360) == 0
    assert normalize_degrees(-1) == 359
    assert degrees_to_cardinal(None) is None


def test_station_config_loads_real_v1_sources():
    config = load_product_config("config/spots.yaml")
    spot = config.spots["new-smyrna"]
    assert spot.wave_sources[0].station_id == "ndbc:41070"
    assert spot.wave_sources[1].station_id == "ndbc:41113"
    assert spot.wind_sources[0].station_id == "ndbc:41069"


def test_freshness_tracks_hourly_station_cadence():
    thresholds = FreshnessConfig()
    assert classify_freshness(90, thresholds) is Freshness.FRESH
    assert classify_freshness(91, thresholds) is Freshness.DELAYED
    assert classify_freshness(181, thresholds) is Freshness.STALE
    assert classify_freshness(361, thresholds) is Freshness.UNAVAILABLE
    assert aggregate_data_state(Freshness.FRESH, None) is DataState.PARTIAL


def test_domain_timestamps_must_be_aware():
    with pytest.raises(ValidationError):
        RawFetch(
            id="bad",
            provider="fixture",
            resource_type="test",
            source_url="https://example.invalid",
            requested_at=datetime(2026, 1, 1),
        )


def test_forecast_model_is_structurally_distinct_from_observation():
    point = ForecastPoint(
        run_id="run-1",
        valid_at=datetime(2026, 7, 30, tzinfo=UTC),
        wave_height_m=1.2,
    )
    dumped = point.model_dump()
    assert "observed_at" not in dumped
    assert "station_id" not in dumped
