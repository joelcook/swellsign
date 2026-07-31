from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from swellsign.config import TrendConfig
from swellsign.models import MeasurementBasis, TrendState, WaveObservation
from swellsign.services.trend import calculate_wave_height_trend

END = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)
CONFIG = TrendConfig(
    window_hours=6,
    minimum_samples=4,
    minimum_coverage_hours=3,
    change_threshold_ft=0.30,
)


def observation(
    hour: int,
    *,
    total: float,
    swell: float | None = None,
    station_id: str = "ndbc:41070",
) -> WaveObservation:
    observed_at = END - timedelta(hours=6 - hour)
    return WaveObservation(
        id=f"{station_id}:{hour}",
        station_id=station_id,
        observed_at=observed_at,
        fetched_at=END,
        processing_product="fixture",
        significant_height_m=total,
        dominant_period_s=8,
        mean_direction_deg_true=45,
        swell_height_m=swell,
        swell_period_s=10 if swell is not None else None,
        swell_direction_deg_true=80 if swell is not None else None,
        source_url="https://example.test",
        raw_fetch_id="raw:test",
    )


def trend(rows, basis=MeasurementBasis.TOTAL_SEA):
    return calculate_wave_height_trend(
        rows,
        station_id="ndbc:41070",
        measurement_basis=basis,
        config=CONFIG,
        end_at=END,
    )


@pytest.mark.parametrize(
    ("heights", "expected"),
    [
        ([0.7, 0.74, 0.78, 0.82, 0.86, 0.9, 0.94], TrendState.RISING),
        ([1.0, 0.96, 0.92, 0.88, 0.84, 0.8, 0.76], TrendState.FALLING),
        ([0.8, 0.81, 0.79, 0.8, 0.81, 0.8, 0.8], TrendState.STEADY),
    ],
)
def test_theil_sen_classification(heights, expected):
    result = trend([observation(index, total=value) for index, value in enumerate(heights)])

    assert result.state is expected
    assert result.sample_count == 7
    assert result.estimated_change_m is not None


def test_one_extreme_outlier_does_not_dominate_rising_series():
    heights = [0.7, 0.74, 0.78, 20.0, 0.86, 0.9, 0.94]

    result = trend([observation(index, total=value) for index, value in enumerate(heights)])

    assert result.state is TrendState.RISING
    assert result.estimated_change_m == pytest.approx(0.24)


def test_insufficient_samples_or_coverage_is_unknown():
    too_few = trend([observation(index, total=0.8) for index in range(3)])
    clustered_rows = [
        observation(index, total=0.8 + index * 0.02).model_copy(
            update={"observed_at": END - timedelta(minutes=(3 - index) * 30)}
        )
        for index in range(4)
    ]
    clustered = trend(clustered_rows)

    assert too_few.state is TrendState.UNKNOWN
    assert too_few.estimated_change_m is None
    assert clustered.state is TrendState.UNKNOWN


def test_measurement_bases_and_stations_are_never_combined():
    rows = [
        observation(index, total=0.7 + index * 0.04, swell=1.0 - index * 0.04)
        for index in range(7)
    ]
    rows.extend(
        observation(index, total=20, station_id="ndbc:41113") for index in range(7)
    )

    total = trend(rows, MeasurementBasis.TOTAL_SEA)
    swell = trend(rows, MeasurementBasis.SEPARATED_SWELL)

    assert total.state is TrendState.RISING
    assert swell.state is TrendState.FALLING
    assert total.station_id == swell.station_id == "ndbc:41070"
