"""NOAA CO-OPS high/low tide prediction adapter.

Predictions use a provider-local model so they cannot be mistaken for measured
water levels or current wave observations.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from swellsign.models import RawFetch, TidePrediction

from .base import HttpFetcher

COOPS_API_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

__all__ = ["COOPS_API_URL", "CoopsProvider", "TidePrediction"]


def _station_from_url(source_url: str) -> str | None:
    values = parse_qs(urlparse(source_url).query).get("station")
    return values[0] if values else None


def _query_value(source_url: str, key: str, default: str) -> str:
    values = parse_qs(urlparse(source_url).query).get(key)
    return values[0] if values else default


def _parse_prediction_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            parsed = datetime.strptime(cleaned, "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _height_m(value: object, units: str) -> float | None:
    try:
        height = float(value)
    except (TypeError, ValueError):
        return None
    if units.lower() in {"english", "imperial"}:
        height *= 0.3048
    elif units.lower() != "metric":
        return None
    return height if -20 <= height <= 20 else None


def _prediction_id(station_id: str, predicted_at: datetime, kind: str, datum: str) -> str:
    identity = f"{station_id}|{predicted_at.isoformat()}|{kind}|{datum}"
    return f"tide:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


class CoopsProvider:
    """Acquire NOAA CO-OPS astronomical high/low predictions."""

    name = "coops"

    def __init__(self, fetcher: HttpFetcher | None = None) -> None:
        self.fetcher = fetcher or HttpFetcher()

    async def fetch_high_low(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
    ) -> RawFetch:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if end < start:
            raise ValueError("end must not precede start")
        provider_station_id = station_id.split(":", 1)[-1].strip()
        if not provider_station_id:
            raise ValueError("station_id cannot be empty")
        return await self.fetcher.fetch(
            provider=self.name,
            resource_type="coops_tide_predictions_hilo",
            url=COOPS_API_URL,
            params={
                "product": "predictions",
                "application": "SwellSign",
                "begin_date": start.astimezone(UTC).strftime("%Y%m%d"),
                "end_date": end.astimezone(UTC).strftime("%Y%m%d"),
                "datum": "MLLW",
                "station": provider_station_id,
                "time_zone": "gmt",
                "units": "metric",
                "interval": "hilo",
                "format": "json",
            },
        )

    def normalize_high_low(
        self,
        raw: RawFetch,
        station_id: str | None = None,
    ) -> list[TidePrediction]:
        if raw.provider != self.name:
            raise ValueError(f"expected provider {self.name!r}, received {raw.provider!r}")
        if raw.error is not None:
            return []
        try:
            payload = json.loads(raw.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("CO-OPS response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("CO-OPS response is not a JSON object")
        if "error" in payload:
            return []

        rows = payload.get("predictions")
        if not isinstance(rows, list):
            return []
        inferred_station = station_id or _station_from_url(raw.source_url)
        if inferred_station is None:
            raise ValueError("station_id is required when it cannot be inferred from source_url")
        canonical_station = f"coops:{inferred_station.split(':', 1)[-1]}"
        units = _query_value(raw.source_url, "units", "metric")
        datum = _query_value(raw.source_url, "datum", "MLLW").upper()
        fetched_at = raw.received_at or raw.requested_at
        predictions: dict[str, TidePrediction] = {}

        for row in rows:
            if not isinstance(row, dict):
                continue
            predicted_at = _parse_prediction_time(row.get("t"))
            height = _height_m(row.get("v"), units)
            raw_kind = row.get("type")
            if isinstance(raw_kind, str):
                kind = {"H": "high", "L": "low"}.get(raw_kind.strip().upper())
            else:
                kind = None
            if predicted_at is None or height is None or kind is None:
                continue
            prediction_id = _prediction_id(
                canonical_station,
                predicted_at,
                kind,
                datum,
            )
            predictions.setdefault(
                prediction_id,
                TidePrediction(
                    id=prediction_id,
                    station_id=canonical_station,
                    predicted_at=predicted_at,
                    height_m=height,
                    kind=kind,
                    datum=datum,
                    fetched_at=fetched_at,
                    source_url=raw.source_url,
                    raw_fetch_id=raw.id,
                ),
            )
        return sorted(predictions.values(), key=lambda item: item.predicted_at)
