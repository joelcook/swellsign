"""Open-Meteo marine and weather forecast adapter.

Wave and wind calls remain separately preserved inside one immutable raw
bundle.  Normalization joins them only on equal UTC ``valid_at`` timestamps.
These models are forecast-only and never produce observation objects.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from swellsign.directions import normalize_degrees
from swellsign.models import ForecastPoint, ForecastRun, RawFetch

from .base import ForecastLocation, HttpFetcher

MARINE_API_URL = "https://marine-api.open-meteo.com/v1/marine"
WEATHER_API_URL = "https://api.open-meteo.com/v1/forecast"

MARINE_HOURLY_FIELDS = (
    "wave_height",
    "wave_direction",
    "wave_period",
    "wave_peak_period",
    "swell_wave_height",
    "swell_wave_direction",
    "swell_wave_period",
    "swell_wave_peak_period",
    "secondary_swell_wave_height",
    "secondary_swell_wave_direction",
    "secondary_swell_wave_period",
    "tertiary_swell_wave_height",
    "tertiary_swell_wave_direction",
    "tertiary_swell_wave_period",
    "wind_wave_height",
    "wind_wave_direction",
    "wind_wave_period",
    "wind_wave_peak_period",
)
WEATHER_HOURLY_FIELDS = ("wind_speed_10m", "wind_direction_10m")

_MARINE_CORE_FIELDS = (
    "wave_height",
    "wave_direction",
    "wave_period",
    "swell_wave_height",
    "swell_wave_direction",
    "swell_wave_period",
    "wind_wave_height",
    "wind_wave_direction",
    "wind_wave_period",
)
_MARINE_CORE_EXPECTED_UNITS = {
    field: (
        "m"
        if field.endswith("_height")
        else "°"
        if field.endswith("_direction")
        else "s"
    )
    for field in _MARINE_CORE_FIELDS
}
_MARINE_OPTIONAL_EXPECTED_UNITS = {
    field: (
        "m"
        if field.endswith("_height")
        else "°"
        if field.endswith("_direction")
        else "s"
    )
    for field in MARINE_HOURLY_FIELDS
    if field not in _MARINE_CORE_EXPECTED_UNITS
}
_WEATHER_EXPECTED_UNITS = {
    "wind_speed_10m": "m/s",
    "wind_direction_10m": "°",
}


class ForecastSchemaError(ValueError):
    """An HTTP-successful forecast response violated the requested contract."""


def _fetch_envelope(fetch: RawFetch) -> dict[str, Any]:
    return {
        "id": fetch.id,
        "source_url": fetch.source_url,
        "requested_at": fetch.requested_at.isoformat(),
        "received_at": fetch.received_at.isoformat() if fetch.received_at else None,
        "http_status": fetch.http_status,
        "content_type": fetch.content_type,
        "error": fetch.error,
        "body_base64": base64.b64encode(fetch.body).decode("ascii"),
    }


def _decode_fetch_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    if envelope.get("error") is not None:
        return {}
    status = envelope.get("http_status")
    if not isinstance(status, int) or status >= 400:
        return {}
    encoded = envelope.get("body_base64")
    if not isinstance(encoded, str):
        return {}
    try:
        body = base64.b64decode(encoded, validate=True)
        payload = json.loads(body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _hourly_by_time(payload: dict[str, Any]) -> dict[datetime, dict[str, Any]]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return {}
    times = hourly.get("time")
    if not isinstance(times, list):
        return {}
    rows: dict[datetime, dict[str, Any]] = {}
    fields = [key for key, values in hourly.items() if key != "time" and isinstance(values, list)]
    for index, value in enumerate(times):
        valid_at = _parse_utc(value)
        if valid_at is None:
            continue
        row = {field: hourly[field][index] if index < len(hourly[field]) else None for field in fields}
        rows[valid_at] = row
    return rows


def _validate_hourly_schema(
    payload: dict[str, Any],
    *,
    source: str,
    expected_units: dict[str, str],
    optional_units: dict[str, str] | None = None,
) -> None:
    """Reject silent unit changes and missing requested core arrays.

    Array lengths may differ; normalization handles those as missing values at
    individual valid times.  The arrays and their units must nevertheless be
    declared so a provider schema change cannot look like a healthy run.
    """

    problems: list[str] = []
    if payload.get("utc_offset_seconds") != 0:
        problems.append("utc_offset_seconds must be 0")
    if payload.get("timezone") not in {"GMT", "UTC"}:
        problems.append("timezone must be GMT/UTC")

    hourly = payload.get("hourly")
    units = payload.get("hourly_units")
    if not isinstance(hourly, dict):
        problems.append("hourly must be an object")
        hourly = {}
    if not isinstance(units, dict):
        problems.append("hourly_units must be an object")
        units = {}
    if not isinstance(hourly.get("time"), list):
        problems.append("hourly.time must be an array")
    if units.get("time") != "iso8601":
        problems.append("hourly_units.time must be iso8601")

    for field, expected_unit in expected_units.items():
        if not isinstance(hourly.get(field), list):
            problems.append(f"hourly.{field} must be an array")
        actual_unit = units.get(field)
        if actual_unit != expected_unit:
            problems.append(
                f"hourly_units.{field} must be {expected_unit!r}, got {actual_unit!r}"
            )

    for field, expected_unit in (optional_units or {}).items():
        values = hourly.get(field)
        actual_unit = units.get(field)
        if actual_unit == expected_unit:
            if not isinstance(values, list):
                problems.append(f"hourly.{field} must be an array")
            continue

        unsupported_unit = actual_unit in {None, "undefined"}
        absent_or_null = values is None or (
            isinstance(values, list) and all(value is None for value in values)
        )
        if unsupported_unit and absent_or_null:
            # Some model/variable combinations advertise the requested field
            # with an undefined unit and nulls.  The exact response remains in
            # RawFetch for future use, but it cannot feed normalized values.
            continue
        problems.append(
            f"optional hourly_units.{field} must be {expected_unit!r} when values exist, "
            f"got {actual_unit!r}"
        )

    if problems:
        raise ForecastSchemaError(f"{source} forecast schema mismatch: {'; '.join(problems)}")


def _finite_float(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not minimum <= number <= maximum:
        return None
    return number


def _height_m(value: Any, unit: str | None) -> float | None:
    height = _finite_float(value, 0, 1000)
    if height is None:
        return None
    if unit is None:
        return None
    normalized_unit = unit.strip().lower()
    if normalized_unit in {"m", "meter", "metre", "meters", "metres"}:
        converted = height
    elif normalized_unit in {"ft", "feet", "foot"}:
        converted = height * 0.3048
    else:
        return None
    return converted if converted <= 40 else None


def _period_s(value: Any, unit: str | None) -> float | None:
    if unit is None or unit.strip().lower() not in {
        "s",
        "sec",
        "second",
        "seconds",
    }:
        return None
    return _finite_float(value, 0.1, 60)


def _direction_deg(value: Any, unit: str | None) -> float | None:
    if unit is None or unit.strip().lower() not in {"°", "deg", "degree", "degrees"}:
        return None
    direction = _finite_float(value, 0, 360)
    return normalize_degrees(direction) if direction is not None else None


def _wind_mps(value: Any, unit: str | None) -> float | None:
    speed = _finite_float(value, 0, 1000)
    if speed is None:
        return None
    if unit is None:
        return None
    normalized_unit = unit.strip().lower().replace(" ", "")
    if normalized_unit in {"m/s", "ms", "mps"}:
        converted = speed
    elif normalized_unit in {"km/h", "kmh", "kph"}:
        converted = speed / 3.6
    elif normalized_unit in {"mph"}:
        converted = speed * 0.44704
    elif normalized_unit in {"kn", "kt", "kts", "knot", "knots"}:
        converted = speed * 0.514444
    else:
        return None
    return converted if converted <= 100 else None


def _unit(payload: dict[str, Any], field: str) -> str | None:
    units = payload.get("hourly_units")
    if not isinstance(units, dict):
        return None
    value = units.get(field)
    return value if isinstance(value, str) else None


def _issued_at(*payloads: dict[str, Any]) -> datetime | None:
    for payload in payloads:
        for key in ("model_initialization_time", "model_run", "issued_at"):
            parsed = _parse_utc(payload.get(key))
            if parsed is not None:
                return parsed
    return None


class OpenMeteoProvider:
    """Collect an immutable seven-day wave + wind forecast run."""

    name = "open_meteo"

    def __init__(
        self,
        fetcher: HttpFetcher | None = None,
        *,
        marine_model: str = "ncep_gfswave016",
        weather_model: str = "gfs_seamless",
    ) -> None:
        self.fetcher = fetcher or HttpFetcher()
        self.marine_model = marine_model
        self.weather_model = weather_model

    async def fetch_run(
        self,
        location: ForecastLocation,
        horizon_hours: int = 168,
    ) -> RawFetch:
        if not 1 <= horizon_hours <= 384:
            raise ValueError("horizon_hours must be between 1 and 384")

        common_params: dict[str, Any] = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "forecast_hours": horizon_hours,
            "timezone": "GMT",
        }
        marine_params = {
            **common_params,
            "hourly": ",".join(MARINE_HOURLY_FIELDS),
            "models": self.marine_model,
            "length_unit": "metric",
            "cell_selection": "sea",
        }
        weather_params = {
            **common_params,
            "hourly": ",".join(WEATHER_HOURLY_FIELDS),
            "models": self.weather_model,
            "wind_speed_unit": "ms",
        }
        marine_fetch, weather_fetch = await asyncio.gather(
            self.fetcher.fetch(
                provider=self.name,
                resource_type="open_meteo_marine",
                url=MARINE_API_URL,
                params=marine_params,
            ),
            self.fetcher.fetch(
                provider=self.name,
                resource_type="open_meteo_weather",
                url=WEATHER_API_URL,
                params=weather_params,
            ),
        )

        errors: dict[str, Any] = {}
        if marine_fetch.error is not None:
            errors["marine"] = marine_fetch.error
        if weather_fetch.error is not None:
            errors["weather"] = weather_fetch.error
        envelope = {
            "schema_version": 1,
            "location": location.model_dump(mode="json"),
            "horizon_hours": horizon_hours,
            "marine_model": self.marine_model,
            "weather_model": self.weather_model,
            "marine_fetch": _fetch_envelope(marine_fetch),
            "weather_fetch": _fetch_envelope(weather_fetch),
        }
        requested_at = min(marine_fetch.requested_at, weather_fetch.requested_at)
        received = [
            value
            for value in (marine_fetch.received_at, weather_fetch.received_at)
            if value is not None
        ]
        statuses = [marine_fetch.http_status, weather_fetch.http_status]
        combined_status = 200 if statuses == [200, 200] else next(
            (status for status in statuses if status != 200),
            None,
        )
        return RawFetch(
            id=f"raw:{uuid4()}",
            provider=self.name,
            resource_type="open_meteo_forecast_bundle",
            source_url=f"open-meteo://forecast/{location.id}",
            requested_at=requested_at,
            received_at=max(received) if received else None,
            http_status=combined_status,
            content_type="application/vnd.swellsign.forecast-bundle+json",
            body=json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(),
            error={"kind": "partial_fetch", "sources": errors} if errors else None,
        )

    def normalize_forecast(
        self,
        raw: RawFetch,
    ) -> tuple[ForecastRun, list[ForecastPoint]]:
        if raw.provider != self.name:
            raise ValueError(f"expected provider {self.name!r}, received {raw.provider!r}")
        try:
            bundle = json.loads(raw.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("forecast bundle is not valid JSON") from exc
        if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
            raise ValueError("unsupported forecast bundle")

        location = bundle.get("location")
        marine_envelope = bundle.get("marine_fetch")
        weather_envelope = bundle.get("weather_fetch")
        if not isinstance(location, dict):
            raise ValueError("forecast bundle has no location")
        if not isinstance(marine_envelope, dict) or not isinstance(weather_envelope, dict):
            raise ValueError("forecast bundle is missing source fetches")

        marine = _decode_fetch_payload(marine_envelope)
        weather = _decode_fetch_payload(weather_envelope)
        if not marine and not weather:
            raise ValueError("neither forecast source returned usable JSON")
        if marine:
            _validate_hourly_schema(
                marine,
                source="marine",
                expected_units=_MARINE_CORE_EXPECTED_UNITS,
                optional_units=_MARINE_OPTIONAL_EXPECTED_UNITS,
            )
        if weather:
            _validate_hourly_schema(
                weather,
                source="weather",
                expected_units=_WEATHER_EXPECTED_UNITS,
            )

        marine_rows = _hourly_by_time(marine)
        weather_rows = _hourly_by_time(weather)
        valid_times = sorted(set(marine_rows) | set(weather_rows))
        if not valid_times:
            raise ForecastSchemaError("forecast sources returned no valid UTC timestamps")
        run_id = f"forecast:{raw.id.removeprefix('raw:')}"
        run = ForecastRun(
            id=run_id,
            provider=self.name,
            model=(
                f"marine:{bundle.get('marine_model', 'unknown')}"
                f"+weather:{bundle.get('weather_model', 'unknown')}"
            ),
            location_id=str(location.get("id", "unknown")),
            issued_at=_issued_at(marine, weather),
            fetched_at=raw.received_at or raw.requested_at,
            horizon_hours=int(bundle.get("horizon_hours", len(valid_times))),
            raw_fetch_id=raw.id,
            metadata={
                "requested_latitude": location.get("latitude"),
                "requested_longitude": location.get("longitude"),
                "marine_grid": {
                    "latitude": marine.get("latitude"),
                    "longitude": marine.get("longitude"),
                },
                "weather_grid": {
                    "latitude": weather.get("latitude"),
                    "longitude": weather.get("longitude"),
                },
                "marine_source_url": marine_envelope.get("source_url"),
                "weather_source_url": weather_envelope.get("source_url"),
                "archived_marine_fields": list(MARINE_HOURLY_FIELDS),
                "attribution": "Open-Meteo",
            },
        )

        points: list[ForecastPoint] = []
        for valid_at in valid_times:
            marine_row = marine_rows.get(valid_at, {})
            weather_row = weather_rows.get(valid_at, {})
            values = {
                "wave_height_m": _height_m(
                    marine_row.get("wave_height"),
                    _unit(marine, "wave_height"),
                ),
                "wave_period_s": _period_s(
                    marine_row.get("wave_period"),
                    _unit(marine, "wave_period"),
                ),
                "wave_direction_deg_true": _direction_deg(
                    marine_row.get("wave_direction"),
                    _unit(marine, "wave_direction"),
                ),
                "swell_height_m": _height_m(
                    marine_row.get("swell_wave_height"),
                    _unit(marine, "swell_wave_height"),
                ),
                "swell_period_s": _period_s(
                    marine_row.get("swell_wave_period"),
                    _unit(marine, "swell_wave_period"),
                ),
                "swell_direction_deg_true": _direction_deg(
                    marine_row.get("swell_wave_direction"),
                    _unit(marine, "swell_wave_direction"),
                ),
                "wind_wave_height_m": _height_m(
                    marine_row.get("wind_wave_height"),
                    _unit(marine, "wind_wave_height"),
                ),
                "wind_wave_period_s": _period_s(
                    marine_row.get("wind_wave_period"),
                    _unit(marine, "wind_wave_period"),
                ),
                "wind_wave_direction_deg_true": _direction_deg(
                    marine_row.get("wind_wave_direction"),
                    _unit(marine, "wind_wave_direction"),
                ),
                "wind_speed_mps": _wind_mps(
                    weather_row.get("wind_speed_10m"),
                    _unit(weather, "wind_speed_10m"),
                ),
                "wind_direction_deg_true": _direction_deg(
                    weather_row.get("wind_direction_10m"),
                    _unit(weather, "wind_direction_10m"),
                ),
            }
            if not any(value is not None for value in values.values()):
                continue
            qc_status = (
                "accepted"
                if valid_at in marine_rows and valid_at in weather_rows
                else "partial"
            )
            points.append(
                ForecastPoint(
                    run_id=run_id,
                    valid_at=valid_at,
                    qc_status=qc_status,
                    **values,
                )
            )
        if not points:
            raise ForecastSchemaError("forecast sources returned no usable values")
        return run, points
