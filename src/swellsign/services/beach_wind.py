"""Modeled wind at the beach itself.

The configured anemometer sits 31.6 km offshore. Over open water there is no
surface friction, so it reads far higher than what is actually blowing on the
sand, and offshore-versus-onshore — the thing a surfer actually needs — cannot
be judged from it. No anemometer stands on that beach, so a measurement of
beach wind does not exist to be had.

What does exist is a model evaluated at the spot's own coordinates, which the
forecast collector already archives hourly. This reads the nearest hour from the
newest run and labels it as model output. It is never mixed into
`CurrentSnapshot`, which stays measurement-only; it travels beside the snapshot
the way tide does.

Surfline reports the same class of number for the same reason, and marks it
"Model Forecast" on their own page. The honest thing is to show it and say what
it is, not to withhold the only available answer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..config import ProductConfig
from ..directions import degrees_to_cardinal
from ..models import ModeledWind
from ..storage import SQLiteRepository
from ..units import mps_to_mph

# Beyond this the nearest archived hour is too far from now to describe it.
MAX_OFFSET = timedelta(hours=2)


class BeachWindService:
    def __init__(self, repository: SQLiteRepository, product_config: ProductConfig) -> None:
        self.repository = repository
        self.product_config = product_config

    def current(self, spot_id: str, *, now: datetime | None = None) -> ModeledWind | None:
        """Nearest modeled hour to `now`, or None when the archive cannot cover it.

        The nearest hour is used rather than an interpolation between hours.
        The model itself is hourly, so interpolating would imply a precision the
        source does not have.
        """
        if spot_id not in self.product_config.spots:
            return None
        moment = (now or datetime.now(UTC)).astimezone(UTC)

        response = self.repository.latest_forecast(
            spot_id,
            start=moment - MAX_OFFSET,
            hours=int(MAX_OFFSET.total_seconds() // 3600) * 2,
        )
        if response is None or not response.points:
            return None

        usable = [
            point
            for point in response.points
            if point.wind_speed_mps is not None and point.wind_direction_deg_true is not None
        ]
        if not usable:
            return None

        nearest = min(usable, key=lambda p: abs(p.valid_at - moment))
        offset = abs(nearest.valid_at - moment)
        if offset > MAX_OFFSET:
            return None

        return ModeledWind(
            provider=response.run.provider,
            model=response.run.model,
            valid_at=nearest.valid_at,
            offset_minutes=int(offset.total_seconds() // 60),
            speed_mps=nearest.wind_speed_mps,
            speed_mph=mps_to_mph(nearest.wind_speed_mps),
            direction_deg_true=nearest.wind_direction_deg_true,
            direction_cardinal=degrees_to_cardinal(nearest.wind_direction_deg_true),
        )
