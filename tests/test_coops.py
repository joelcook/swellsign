from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from swellsign.providers.base import HttpFetcher
from swellsign.providers.coops import CoopsProvider

FIXTURES = Path(__file__).parent / "fixtures" / "provider"


def test_coops_fetches_only_gmt_metric_high_low_and_normalizes() -> None:
    fixture = (FIXTURES / "coops_hilo.json").read_bytes()
    seen_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            200,
            content=fixture,
            headers={"content-type": "application/json"},
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = CoopsProvider(fetcher=HttpFetcher(client=client))
            raw = await provider.fetch_high_low(
                "coops:8721147",
                datetime(2026, 7, 30, tzinfo=UTC),
                datetime(2026, 8, 1, tzinfo=UTC),
            )
            return raw, provider.normalize_high_low(raw)

    raw, predictions = asyncio.run(run())

    assert seen_request is not None
    assert seen_request.url.params["product"] == "predictions"
    assert seen_request.url.params["interval"] == "hilo"
    assert seen_request.url.params["time_zone"] == "gmt"
    assert seen_request.url.params["units"] == "metric"
    assert seen_request.url.params["datum"] == "MLLW"
    assert seen_request.url.params["station"] == "8721147"
    assert raw.error is None

    assert len(predictions) == 2
    assert predictions[0].station_id == "coops:8721147"
    assert predictions[0].kind == "high"
    assert predictions[0].height_m == 0.943
    assert predictions[0].predicted_at == datetime(2026, 7, 30, 2, 18, tzinfo=UTC)
    assert predictions[1].kind == "low"
    assert predictions[0].raw_fetch_id == raw.id


def test_coops_prediction_error_payload_is_not_observed_data() -> None:
    instant = datetime(2026, 7, 30, tzinfo=UTC)

    async def run():
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"error": {"message": "No data was found."}},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            provider = CoopsProvider(fetcher=HttpFetcher(client=client))
            raw = await provider.fetch_high_low("8721147", instant, instant)
            return provider.normalize_high_low(raw)

    assert asyncio.run(run()) == []
