"""Shared provider contracts and bounded HTTP acquisition.

Provider adapters return :class:`RawFetch` even when the network, server, or
response-size boundary fails.  That makes failed acquisition attempts as
observable and archivable as successful ones.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from swellsign.models import ForecastPoint, ForecastRun, RawFetch, WaveObservation, WindObservation

logger = logging.getLogger(__name__)


class ForecastLocation(BaseModel):
    """A stable forecast location independent of any one provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """Hard limits and retry behavior applied to every upstream request."""

    timeout_seconds: float = 15.0
    max_response_bytes: int = 5 * 1024 * 1024
    user_agent: str = "SwellSign/0.1 (+https://github.com/swellsign)"
    max_attempts: int = 3
    backoff_initial_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    backoff_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff_initial_seconds <= 0 or self.backoff_max_seconds <= 0:
            raise ValueError("backoff bounds must be positive")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1")

    def backoff_delay(self, attempt: int) -> float:
        """Full-jitter exponential backoff.

        Randomizing the whole interval rather than adding jitter to a fixed
        delay keeps several stations from retrying NOAA in lockstep after a
        shared outage.
        """
        ceiling = min(
            self.backoff_max_seconds,
            self.backoff_initial_seconds * (self.backoff_multiplier**attempt),
        )
        return random.uniform(0.0, ceiling)


class ValidatorStore(Protocol):
    """Remembers HTTP validators so repeat fetches can be conditional."""

    def get_validator(self, key: str) -> tuple[str | None, str | None]: ...

    def set_validator(self, key: str, etag: str | None, last_modified: str | None) -> None: ...


def _retry_after_seconds(response: httpx.Response, cap: float) -> float | None:
    """Honor a numeric Retry-After when the provider states one."""
    raw_value = response.headers.get("retry-after")
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, cap)


class HttpFetcher:
    """Fetch bytes with bounded time and memory, preserving every outcome.

    Transient failures are retried with jittered exponential backoff.  When a
    :class:`ValidatorStore` is supplied the fetcher issues conditional requests
    and reports an unchanged resource as HTTP 304 with an empty body, which
    costs NOAA almost nothing to answer.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        policy: FetchPolicy | None = None,
        validator_store: ValidatorStore | None = None,
    ) -> None:
        self._client = client
        self.policy = policy or FetchPolicy()
        self.validator_store = validator_store

    def _validator_key(self, provider: str, resource_type: str, url: str) -> str:
        return f"{provider}|{resource_type}|{url}"

    def _request_headers(self, validator_key: str | None) -> dict[str, str]:
        headers = {"User-Agent": self.policy.user_agent, "Accept": "*/*"}
        if validator_key is None or self.validator_store is None:
            return headers
        etag, last_modified = self.validator_store.get_validator(validator_key)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return headers

    async def fetch(
        self,
        *,
        provider: str,
        resource_type: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        conditional: bool = True,
    ) -> RawFetch:
        requested_at = datetime.now().astimezone()
        request_id = f"raw:{uuid4()}"
        validator_key = (
            self._validator_key(provider, resource_type, url)
            if conditional and self.validator_store is not None
            else None
        )

        response_url = url
        status: int | None = None
        content_type: str | None = None
        body = bytearray()
        error: dict[str, Any] | None = None
        retry_delay: float | None = None

        async def perform(client: httpx.AsyncClient) -> bool:
            """Run one attempt; return True when the outcome is worth retrying."""
            nonlocal response_url, status, content_type, error, retry_delay
            body.clear()
            error = None
            retry_delay = None
            try:
                async with client.stream(
                    "GET",
                    url,
                    params=params,
                    headers=self._request_headers(validator_key),
                    timeout=httpx.Timeout(self.policy.timeout_seconds),
                ) as response:
                    response_url = str(response.url)
                    status = response.status_code
                    content_type = response.headers.get("content-type")

                    if response.status_code == 304:
                        return False

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
                                return False
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
                            return False
                        body.extend(chunk)

                    if response.status_code >= 400:
                        error = {
                            "kind": "http_status",
                            "message": f"upstream returned HTTP {response.status_code}",
                        }
                        # 429 and 5xx are the provider asking us to come back;
                        # other 4xx mean this request will never succeed.
                        if response.status_code == 429 or response.status_code >= 500:
                            retry_delay = _retry_after_seconds(
                                response, self.policy.backoff_max_seconds
                            )
                            return True
                        return False

                    if validator_key is not None and self.validator_store is not None:
                        self.validator_store.set_validator(
                            validator_key,
                            response.headers.get("etag"),
                            response.headers.get("last-modified"),
                        )
            except httpx.TimeoutException as exc:
                error = {"kind": "timeout", "message": str(exc) or exc.__class__.__name__}
                return True
            except httpx.HTTPError as exc:
                error = {
                    "kind": "transport",
                    "message": str(exc) or exc.__class__.__name__,
                }
                return True
            return False

        async def attempt_all(client: httpx.AsyncClient) -> None:
            for attempt in range(self.policy.max_attempts):
                should_retry = await perform(client)
                if not should_retry or attempt == self.policy.max_attempts - 1:
                    if should_retry and error is not None:
                        error["attempts"] = attempt + 1
                    return
                delay = retry_delay
                if delay is None:
                    delay = self.policy.backoff_delay(attempt)
                logger.warning(
                    "upstream fetch retrying",
                    extra={
                        "provider": provider,
                        "resource_type": resource_type,
                        "attempt": attempt + 1,
                        "delay_seconds": round(delay, 3),
                    },
                )
                await asyncio.sleep(delay)

        if self._client is None:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                await attempt_all(client)
        else:
            await attempt_all(self._client)

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
