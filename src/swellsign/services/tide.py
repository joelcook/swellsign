"""Objective tide phase derived from adjacent predicted extremes.

CO-OPS station 8721147 publishes astronomical high/low predictions rather than
a live observed water level, so the only honest derivation is where "now" sits
between two neighbouring extremes.  Everything this module returns is labeled a
prediction and is kept out of :class:`~swellsign.models.CurrentSnapshot`.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from ..config import ProductConfig
from ..models import TidePhase
from ..storage import SQLiteRepository

# Where the level bands sit within the range between two adjacent extremes.
LOW_BAND = 1 / 3
HIGH_BAND = 2 / 3


def height_fraction(percent_through: float, *, rising: bool) -> float:
    """Normalized water level between two adjacent extremes, 0 low to 1 high.

    Tide is not linear in time. It lingers near the extremes and moves fastest
    through the middle, which is why a quarter of the way through a cycle by
    the clock is only about fifteen percent of the way up by water level. A
    linear reading would call that MID when the sandbar is still working like
    low. This is the standard harmonic approximation, the smooth form of the
    rule of twelfths.
    """
    phase = max(0.0, min(1.0, percent_through / 100.0))
    ascending = (1 - math.cos(math.pi * phase)) / 2
    return ascending if rising else 1.0 - ascending


def classify_level(fraction: float) -> str:
    if fraction < LOW_BAND:
        return "low"
    if fraction < HIGH_BAND:
        return "mid"
    return "high"


class TideContextService:
    def __init__(self, repository: SQLiteRepository, product_config: ProductConfig) -> None:
        self.repository = repository
        self.product_config = product_config

    def phase(self, spot_id: str, *, now: datetime | None = None) -> TidePhase | None:
        """Derive the current phase, or ``None`` when it cannot be bracketed.

        A phase requires a predicted extreme on both sides of the moment.  When
        the archive runs out, no phase is reported rather than extrapolating
        past the last known extreme.
        """
        spot = self.product_config.spots.get(spot_id)
        if spot is None or spot.tide_source is None:
            return None

        moment = (now or datetime.now(UTC)).astimezone(UTC)
        previous, following = self.repository.surrounding_tide_extremes(
            spot.tide_source.station_id,
            moment,
        )
        if previous is None or following is None:
            return None
        if previous.kind == following.kind:
            # Two like extremes in a row means a gap in the archive; a phase
            # spanning that gap would be fiction.
            return None

        span_seconds = (following.predicted_at - previous.predicted_at).total_seconds()
        if span_seconds <= 0:
            return None

        elapsed_seconds = (moment - previous.predicted_at).total_seconds()
        percent = max(0.0, min(100.0, elapsed_seconds / span_seconds * 100.0))
        minutes_remaining = max(
            0,
            int((following.predicted_at - moment).total_seconds() // 60),
        )

        rising = following.kind == "high"
        fraction = height_fraction(percent, rising=rising)

        return TidePhase(
            station_id=spot.tide_source.station_id,
            state="rising" if rising else "falling",
            previous_extreme=previous,
            next_extreme=following,
            minutes_to_next_extreme=minutes_remaining,
            percent_through=round(percent, 1),
            height_fraction=round(fraction, 3),
            level=classify_level(fraction),
            datum=following.datum,
        )
