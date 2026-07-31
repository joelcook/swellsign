"""Robust, objective wave-height trend calculations."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from statistics import median

from ..config import TrendConfig
from ..models import (
    MeasurementBasis,
    TrendState,
    WaveHeightTrend,
    WaveObservation,
)
from ..units import meters_to_feet


def calculate_wave_height_trend(
    observations: Iterable[WaveObservation],
    *,
    station_id: str,
    measurement_basis: MeasurementBasis,
    config: TrendConfig,
    end_at: datetime,
) -> WaveHeightTrend:
    """Estimate change across the configured window with a Theil--Sen slope.

    Only one station and one explicitly requested measurement basis contribute.
    Duplicate timestamps collapse to one point, so fetching the same provider
    product repeatedly cannot increase confidence.
    """

    window_start = end_at - timedelta(hours=config.window_hours)
    points_by_time: dict[datetime, float] = {}

    for observation in observations:
        if observation.station_id != station_id or observation.qc_status != "accepted":
            continue
        if not window_start <= observation.observed_at <= end_at:
            continue
        height = _height_for_basis(observation, measurement_basis)
        if height is None or not math.isfinite(height) or height < 0:
            continue
        # Repository order is deterministic. Exact duplicates represent the
        # same physical instant, so retaining one is the honest sample count.
        points_by_time.setdefault(observation.observed_at, height)

    points = sorted(points_by_time.items())
    if len(points) < config.minimum_samples:
        return _unknown(station_id, measurement_basis, config.window_hours, len(points))

    coverage_hours = (points[-1][0] - points[0][0]).total_seconds() / 3_600
    if coverage_hours < config.minimum_coverage_hours:
        return _unknown(station_id, measurement_basis, config.window_hours, len(points))

    slopes: list[float] = []
    for left_index, (left_time, left_height) in enumerate(points):
        for right_time, right_height in points[left_index + 1 :]:
            hours = (right_time - left_time).total_seconds() / 3_600
            if hours > 0:
                slopes.append((right_height - left_height) / hours)

    if not slopes:
        return _unknown(station_id, measurement_basis, config.window_hours, len(points))

    estimated_change_m = median(slopes) * config.window_hours
    estimated_change_ft = meters_to_feet(estimated_change_m)
    if estimated_change_ft >= config.change_threshold_ft:
        state = TrendState.RISING
    elif estimated_change_ft <= -config.change_threshold_ft:
        state = TrendState.FALLING
    else:
        state = TrendState.STEADY

    return WaveHeightTrend(
        state=state,
        window_hours=config.window_hours,
        estimated_change_m=estimated_change_m,
        estimated_change_ft=estimated_change_ft,
        sample_count=len(points),
        station_id=station_id,
        measurement_basis=measurement_basis,
    )


def _height_for_basis(
    observation: WaveObservation,
    measurement_basis: MeasurementBasis,
) -> float | None:
    if measurement_basis is MeasurementBasis.TOTAL_SEA:
        return observation.significant_height_m
    if measurement_basis in (
        MeasurementBasis.SEPARATED_SWELL,
        MeasurementBasis.SPECTRAL_PARTITION,
    ):
        return observation.swell_height_m
    return None


def _unknown(
    station_id: str,
    measurement_basis: MeasurementBasis,
    window_hours: float,
    sample_count: int,
) -> WaveHeightTrend:
    return WaveHeightTrend(
        state=TrendState.UNKNOWN,
        window_hours=window_hours,
        estimated_change_m=None,
        estimated_change_ft=None,
        sample_count=sample_count,
        station_id=station_id,
        measurement_basis=measurement_basis,
    )


# Friendly alias for callers and tests.
compute_wave_height_trend = calculate_wave_height_trend
