"""Age and aggregate data-state calculations."""

from __future__ import annotations

from datetime import datetime

from .config import FreshnessConfig
from .models import DataState, Freshness


def age_minutes(observed_at: datetime, now: datetime) -> int:
    return max(0, int((now - observed_at).total_seconds() // 60))


def classify_freshness(age: int, config: FreshnessConfig) -> Freshness:
    if age <= config.fresh_max_age_minutes:
        return Freshness.FRESH
    if age <= config.delayed_max_age_minutes:
        return Freshness.DELAYED
    if age <= config.stale_max_age_minutes:
        return Freshness.STALE
    return Freshness.UNAVAILABLE


def aggregate_data_state(
    wave: Freshness | None,
    wind: Freshness | None,
) -> DataState:
    if wave is None or wave is Freshness.UNAVAILABLE:
        return DataState.PARTIAL if wind not in (None, Freshness.UNAVAILABLE) else DataState.UNAVAILABLE
    if wind is None or wind is Freshness.UNAVAILABLE:
        return DataState.PARTIAL
    if Freshness.STALE in (wave, wind):
        return DataState.STALE
    if Freshness.DELAYED in (wave, wind):
        return DataState.DELAYED
    return DataState.FRESH
