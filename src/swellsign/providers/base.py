"""Shared provider contracts and bounded HTTP acquisition.

Provider adapters return :class:`RawFetch` even when the network, server, or
response-size boundary fails.  That makes failed acquisition attempts as
observable and archivable as successful ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from swellsign.models import ForecastPoint, ForecastRun, RawFetch, WaveObservation, WindObservation


class ForecastLocation(BaseModel):
    """A stable forecast location independent of any one provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """Hard limits applied to every upstream request."""

    timeout_seconds: float = 15.0
    max_response_bytes: int = 5 * 1024 * 1024
    user_agent: str = "SwellSign/0.1 (+https://github.com/swellsign)"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")


class HttpFetcher:
    """Fetch bytes with bounded time and memory, preserving every outcome."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        policy: FetchPolicy | None = None,
    ) -> None:
        self._client = client
        self.policy = policy or FetchPolicy()

    async def fetch(
        self,
        *,
        provider: str,
        resource_type: str,
        url: str,
        params: Mapping[str, Any] | None = None,
    ) -> RawFetch:
        requested_at = datetime.now().astimezone()
        request_id = f"raw:{uuid4()}"
        response_url = url
        status: int | None = None
        content_type: str | None = None
        body = bytearray()
        error: dict[str, Any] | None = None

        async def perform(client: httpx.AsyncClient) -> None:
            nonlocal response_url, status, content_type, error
            try:
                async with client.stream(
                    "GET",
                    url,
                    params=params,
                    headers={"User-Agent": self.policy.user_agent, "Accept": "*/*"},
                    timeout=httpx.Timeout(self.policy.timeout_seconds),
                ) as response:
                    response_url = str(response.url)
                    status = response.status_code
                    content_type = response.headers.get("content-type")

                    declared_length = response.headers.get("content-length")
                    if declared_length is not None:
                        try:
                            if int(declared_length) > self.policy.max_response_bytes:
                                error = {
                                    "kind": "response_too_large",
                                    "message": (
                                        f"declared response is {declared_length} bytes; "
                                        f"limit is {self.policy.max_response_bytes}"
                                    ),
                                }
                                return
                        except ValueError:
                            pass

                    async for chunk in response.aiter_bytes():
                        remaining = self.policy.max_response_bytes - len(body)
                        if len(chunk) > remaining:
                            body.extend(chunk[:remaining])
                            error = {
                                "kind": "response_too_large",
                                "message": (
                                    "response exceeded "
                                    f"{self.policy.max_response_bytes} byte limit"
                                ),
                                "truncated": True,
                            }
                            return
                        body.extend(chunk)

                    if response.status_code >= 400:
                        error = {
                            "kind": "http_status",
                            "message": f"upstream returned HTTP {response.status_code}",
                        }
            except httpx.TimeoutException as exc:
                error = {"kind": "timeout", "message": str(exc) or exc.__class__.__name__}
            except httpx.HTTPError as exc:
                error = {
                    "kind": "transport",
                    "message": str(exc) or exc.__class__.__name__,
                }

        if self._client is None:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                await perform(client)
        else:
            await perform(self._client)

        received_at = datetime.now().astimezone()
        return RawFetch(
            id=request_id,
            provider=provider,
            resource_type=resource_type,
            source_url=response_url,
            requested_at=requested_at,
            received_at=received_at,
            http_status=status,
            content_type=content_type,
            body=bytes(body),
            error=error,
        )


class ObservationProvider(Protocol):
    name: str

    async def fetch_latest(self, station_id: str) -> RawFetch: ...

    async def fetch_range(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
    ) -> RawFetch: ...

    def normalize_observations(
        self,
        raw: RawFetch,
    ) -> list[WaveObservation | WindObservation]: ...


class ForecastProvider(Protocol):
    name: str

    async def fetch_run(
        self,
        location: ForecastLocation,
        horizon_hours: int,
    ) -> RawFetch: ...

    def normalize_forecast(
        self,
        raw: RawFetch,
    ) -> tuple[ForecastRun, list[ForecastPoint]]: ...
