from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from swellsign.providers.base import ForecastLocation, HttpFetcher
from swellsign.providers.open_meteo import (
    MARINE_HOURLY_FIELDS,
    WEATHER_HOURLY_FIELDS,
    ForecastSchemaError,
    OpenMeteoProvider,
)

FIXTURES = Path(__file__).parent / "fixtures" / "provider"


def test_fetch_and_normalize_join_wave_and_wind_by_valid_time() -> None:
    marine_body = (FIXTURES / "open_meteo_marine.json").read_bytes()
    weather_body = (FIXTURES / "open_meteo_weather.json").read_bytes()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["forecast_hours"] == "168"
        assert request.url.params["timezone"] == "GMT"
        assert request.url.params["latitude"] == "29.0258"
        assert request.url.params["longitude"] == "-80.927"
        if request.url.host == "marine-api.open-meteo.com":
            assert request.url.params["models"] == "ncep_gfswave016"
            assert request.url.params["hourly"] == ",".join(MARINE_HOURLY_FIELDS)
            assert request.url.params["cell_selection"] == "sea"
            assert request.url.params["length_unit"] == "metric"
            return httpx.Response(
                200,
                content=marine_body,
                headers={"content-type": "application/json"},
            )
        assert request.url.host == "api.open-meteo.com"
        assert request.url.params["models"] == "gfs_seamless"
        assert request.url.params["hourly"] == ",".join(WEATHER_HOURLY_FIELDS)
        assert request.url.params["wind_speed_unit"] == "ms"
        return httpx.Response(
            200,
            content=weather_body,
            headers={"content-type": "application/json"},
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenMeteoProvider(fetcher=HttpFetcher(client=client))
            raw = await provider.fetch_run(
                ForecastLocation(
                    id="new-smyrna",
                    latitude=29.0258,
                    longitude=-80.927,
                ),
                168,
            )
            return raw, provider.normalize_forecast(raw)

    raw, (run, points) = asyncio.run(run())

    assert len(requests) == 2
    assert raw.error is None
    assert run.location_id == "new-smyrna"
    assert run.horizon_hours == 168
    assert run.raw_fetch_id == raw.id
    assert run.model == "marine:ncep_gfswave016+weather:gfs_seamless"
    assert [point.valid_at.hour for point in points] == [12, 13, 14, 15]

    by_hour = {point.valid_at.hour: point for point in points}
    assert by_hour[12].wave_height_m == 0.8
    assert by_hour[12].wind_speed_mps == 3.0
    assert by_hour[12].qc_status == "accepted"
    assert by_hour[13].wave_height_m == 0.9
    assert by_hour[13].wind_speed_mps is None
    assert by_hour[13].qc_status == "partial"
    assert by_hour[14].wave_height_m == 1.0
    assert by_hour[14].wind_speed_mps == 6.0
    assert by_hour[15].wave_height_m is None
    assert by_hour[15].wind_direction_deg_true == 280

    bundle = json.loads(raw.body)
    preserved_marine = base64.b64decode(bundle["marine_fetch"]["body_base64"])
    preserved_weather = base64.b64decode(bundle["weather_fetch"]["body_base64"])
    assert preserved_marine == marine_body
    assert preserved_weather == weather_body


def test_forecast_normalization_keeps_naive_gmt_times_utc() -> None:
    marine_body = (FIXTURES / "open_meteo_marine.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "marine-api.open-meteo.com":
            return httpx.Response(200, content=marine_body)
        return httpx.Response(503, json={"reason": "maintenance"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenMeteoProvider(fetcher=HttpFetcher(client=client))
            raw = await provider.fetch_run(
                ForecastLocation(id="test", latitude=29, longitude=-81),
                24,
            )
            return raw, provider.normalize_forecast(raw)

    raw, (_run, points) = asyncio.run(run())

    assert raw.error is not None
    assert raw.error["kind"] == "partial_fetch"
    assert len(points) == 3
    assert all(point.valid_at.tzinfo == UTC for point in points)
    assert all(point.wind_speed_mps is None for point in points)
    assert points[0].valid_at == datetime(2026, 7, 30, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("defect", "error_fragment"),
    [
        ("wrong_unit", "wave_height"),
        ("missing_array", "wave_period"),
        ("wrong_timezone", "timezone"),
        ("core_undefined", "wave_period"),
        ("optional_undefined_with_values", "wave_peak_period"),
    ],
)
def test_schema_or_unit_drift_cannot_be_archived_as_a_healthy_run(
    defect: str,
    error_fragment: str,
) -> None:
    marine = json.loads((FIXTURES / "open_meteo_marine.json").read_text())
    weather_body = (FIXTURES / "open_meteo_weather.json").read_bytes()
    if defect == "wrong_unit":
        marine["hourly_units"]["wave_height"] = "ft"
    elif defect == "missing_array":
        del marine["hourly"]["wave_period"]
    elif defect == "wrong_timezone":
        marine["timezone"] = "America/New_York"
        marine["utc_offset_seconds"] = -14_400
    elif defect == "core_undefined":
        marine["hourly_units"]["wave_period"] = "undefined"
        marine["hourly"]["wave_period"] = [None, None, None]
    elif defect == "optional_undefined_with_values":
        marine["hourly_units"]["wave_peak_period"] = "undefined"
    marine_body = json.dumps(marine).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            marine_body
            if request.url.host == "marine-api.open-meteo.com"
            else weather_body
        )
        return httpx.Response(200, content=body)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenMeteoProvider(fetcher=HttpFetcher(client=client))
            raw = await provider.fetch_run(
                ForecastLocation(id="test", latitude=29, longitude=-81),
                24,
            )
            return provider, raw

    provider, raw = asyncio.run(fetch())

    assert raw.error is None
    with pytest.raises(ForecastSchemaError, match=error_fragment):
        provider.normalize_forecast(raw)


def test_unsupported_optional_enrichment_remains_raw_without_rejecting_core() -> None:
    marine = json.loads((FIXTURES / "open_meteo_marine.json").read_text())
    weather_body = (FIXTURES / "open_meteo_weather.json").read_bytes()

    for field in (
        "wave_peak_period",
        "swell_wave_peak_period",
        "wind_wave_peak_period",
    ):
        marine["hourly_units"][field] = "undefined"
        marine["hourly"][field] = [None, None, None]
    for field in (
        "secondary_swell_wave_height",
        "secondary_swell_wave_direction",
        "secondary_swell_wave_period",
    ):
        marine["hourly_units"].pop(field)
        marine["hourly"].pop(field)
    marine_body = json.dumps(marine, separators=(",", ":")).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            marine_body
            if request.url.host == "marine-api.open-meteo.com"
            else weather_body
        )
        return httpx.Response(200, content=body)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenMeteoProvider(fetcher=HttpFetcher(client=client))
            raw = await provider.fetch_run(
                ForecastLocation(id="test", latitude=29, longitude=-81),
                24,
            )
            return raw, provider.normalize_forecast(raw)

    raw, (_run, points) = asyncio.run(fetch())

    assert points
    assert points[0].wave_height_m == 0.8
    assert points[0].wave_period_s == 8.1
    bundle = json.loads(raw.body)
    preserved = json.loads(base64.b64decode(bundle["marine_fetch"]["body_base64"]))
    assert preserved["hourly_units"]["wave_peak_period"] == "undefined"
    assert preserved["hourly"]["wave_peak_period"] == [None, None, None]
    assert "secondary_swell_wave_height" not in preserved["hourly"]
