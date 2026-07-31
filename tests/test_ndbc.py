from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from swellsign.models import RawFetch, WaveObservation, WindObservation
from swellsign.providers.base import FetchPolicy, HttpFetcher
from swellsign.providers.ndbc import (
    NdbcParseError,
    NdbcProvider,
    parse_ndbc_spec,
    parse_ndbc_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "provider"


def _raw(filename: str, source_url: str, resource_type: str) -> RawFetch:
    instant = datetime(2026, 7, 30, 17, 15, tzinfo=UTC)
    return RawFetch(
        id=f"raw:{filename}",
        provider="ndbc",
        resource_type=resource_type,
        source_url=source_url,
        requested_at=instant,
        received_at=instant,
        http_status=200,
        content_type="text/plain",
        body=(FIXTURES / filename).read_bytes(),
    )


def test_standard_parser_discovers_columns_missing_values_and_short_rows() -> None:
    rows = parse_ndbc_text((FIXTURES / "ndbc_standard.txt").read_text())

    assert len(rows) == 4
    assert rows[0].observed_at == datetime(2026, 7, 30, 17, 10, tzinfo=UTC)
    assert rows[0].value("WVHT") == 0.8
    assert rows[0].value("EXTRA") == "A"
    assert rows[2].observed_at.year == 2026
    assert rows[2].value("WVHT") is None
    assert "WVHT" in rows[2].missing_fields
    assert rows[3].value("GST") is None
    assert rows[3].warnings == ("short_row:7/20",)


def test_standard_normalization_is_coherent_idempotent_and_si() -> None:
    raw = _raw(
        "ndbc_standard.txt",
        "https://www.ndbc.noaa.gov/data/realtime2/41070.txt",
        "ndbc_standard",
    )

    observations = NdbcProvider().normalize_observations(raw)

    assert len(observations) == 6
    assert len({item.id for item in observations}) == len(observations)
    latest_wave = next(
        item
        for item in observations
        if isinstance(item, WaveObservation)
        and item.observed_at == datetime(2026, 7, 30, 17, 10, tzinfo=UTC)
    )
    latest_wind = next(
        item
        for item in observations
        if isinstance(item, WindObservation)
        and item.observed_at == latest_wave.observed_at
    )
    assert latest_wave.station_id == "ndbc:41070"
    assert latest_wave.significant_height_m == 0.8
    assert latest_wave.dominant_period_s == 8.1
    assert latest_wave.mean_direction_deg_true == 43
    assert latest_wave.water_temperature_c == 27.2
    assert latest_wind.speed_mps == 3.5
    assert latest_wind.gust_mps == 4.2
    assert latest_wind.direction_deg_true == 270
    assert latest_wave.raw_fetch_id == latest_wind.raw_fetch_id


def test_spec_parser_and_normalizer_accept_cardinal_directions() -> None:
    rows = parse_ndbc_spec((FIXTURES / "ndbc_41113.spec").read_text())
    assert rows[0].value("SwD") == "ESE"
    assert rows[1].value("SwH") is None

    raw = _raw(
        "ndbc_41113.spec",
        "https://www.ndbc.noaa.gov/data/realtime2/41113.spec",
        "ndbc_spec",
    )
    observations = NdbcProvider().normalize_observations(raw)
    waves = [item for item in observations if isinstance(item, WaveObservation)]
    latest = waves[-1]

    assert len(waves) == 2
    assert latest.processing_product == "ndbc_realtime_spec"
    assert latest.swell_height_m == 0.7
    assert latest.swell_period_s == 8.3
    assert latest.swell_direction_deg_true == 112.5
    assert latest.wind_wave_direction_deg_true == 292.5


def test_realistic_41070_spec_missing_markers_never_become_zero() -> None:
    raw = _raw(
        "ndbc_41070_missing.spec",
        "https://www.ndbc.noaa.gov/data/realtime2/41070.spec",
        "ndbc_spec",
    )
    observations = NdbcProvider().normalize_observations(raw)
    waves = [item for item in observations if isinstance(item, WaveObservation)]

    assert waves
    assert all(item.swell_height_m is None for item in waves)
    assert all(item.swell_period_s is None for item in waves)
    assert all(item.swell_direction_deg_true is None for item in waves)
    assert waves[-1].significant_height_m == 0.8


def test_normalization_is_deterministic_for_duplicates_order_and_future_rows() -> None:
    body = b"""\
#YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD
#yr mo dy hr mn degT m/s m/s m sec sec degT
2026 07 30 16 00 180 2.0 3.0 0.6 7.0 5.0 90
2026 07 30 17 10 270 3.5 4.2 0.8 8.1 5.8 43
2026 07 30 15 00 170 1.5 2.0 0.5 6.5 4.8 80
2026 07 30 17 10 090 9.9 9.9 9.9 19.0 9.0 180
2026 07 30 17 21 000 4.0 5.0 1.1 9.0 6.0 50
2026 07 30 17 20 010 3.8 4.8 1.0 8.8 5.9 48
"""
    fetched_at = datetime(2026, 7, 30, 17, 15, tzinfo=UTC)
    raw = RawFetch(
        id="raw:ordering",
        provider="ndbc",
        resource_type="ndbc_standard",
        source_url="https://www.ndbc.noaa.gov/data/realtime2/41070.txt",
        requested_at=fetched_at,
        received_at=fetched_at,
        http_status=200,
        body=body,
    )
    provider = NdbcProvider()

    first_pass = provider.normalize_observations(raw)
    second_pass = provider.normalize_observations(raw)
    waves = [item for item in first_pass if isinstance(item, WaveObservation)]

    assert [item.model_dump() for item in first_pass] == [
        item.model_dump() for item in second_pass
    ]
    assert [item.observed_at.hour for item in waves] == [15, 16, 17, 17]
    assert [item.observed_at.minute for item in waves] == [0, 0, 10, 20]
    duplicate_winner = next(
        item
        for item in waves
        if item.observed_at == datetime(2026, 7, 30, 17, 10, tzinfo=UTC)
    )
    assert duplicate_winner.significant_height_m == 0.8
    assert duplicate_winner.dominant_period_s == 8.1
    assert all(item.observed_at.minute != 21 for item in first_pass)


def test_html_maintenance_page_is_not_an_ndbc_table() -> None:
    html = """<!doctype html><html><title>Maintenance</title><body>Try later</body></html>"""

    with pytest.raises(NdbcParseError, match="header"):
        parse_ndbc_text(html)


def test_http_fetcher_returns_an_archivable_error_when_body_is_too_large() -> None:
    async def run() -> RawFetch:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"0123456789")
        )
        async with httpx.AsyncClient(transport=transport) as client:
            fetcher = HttpFetcher(
                client=client,
                policy=FetchPolicy(max_response_bytes=5),
            )
            return await fetcher.fetch(
                provider="test",
                resource_type="fixture",
                url="https://example.test/data",
            )

    raw = asyncio.run(run())

    assert raw.http_status == 200
    assert raw.error is not None
    assert raw.error["kind"] == "response_too_large"
    assert raw.source_url == "https://example.test/data"
