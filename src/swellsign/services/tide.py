"""Objective tide phase derived from adjacent predicted extremes.

CO-OPS station 8721147 publishes astronomical high/low predictions rather than
a live observed water level, so the only honest derivation is where "now" sits
between two neighbouring extremes.  Everything this module returns is labeled a
prediction and is kept out of :class:`~swellsign.models.CurrentSnapshot`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..config import ProductConfig
from ..models import TidePhase
from ..storage import SQLiteRepository


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

        return TidePhase(
            station_id=spot.tide_source.station_id,
            state="rising" if following.kind == "high" else "falling",
            previous_extreme=previous,
            next_extreme=following,
            minutes_to_next_extreme=minutes_remaining,
            percent_through=round(percent, 1),
            datum=following.datum,
        )
