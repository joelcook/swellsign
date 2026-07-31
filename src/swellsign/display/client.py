"""Validated API polling with a durable last-good observed payload."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..config import FreshnessConfig
from ..freshness import aggregate_data_state, classify_freshness
from ..models import CompactDisplayPayload


def recalculate_ages(
    payload: CompactDisplayPayload,
    *,
    now: datetime | None = None,
    freshness: FreshnessConfig | None = None,
) -> CompactDisplayPayload:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    thresholds = freshness or FreshnessConfig()
    updated = payload.model_copy(deep=True)
    wave_state = None
    wind_state = None
    if updated.wave:
        updated.wave.age_minutes = max(
            0, int((current - updated.wave.observed_at).total_seconds() // 60)
        )
        updated.wave.freshness = classify_freshness(updated.wave.age_minutes, thresholds)
        wave_state = updated.wave.freshness
    if updated.wind:
        updated.wind.age_minutes = max(
            0, int((current - updated.wind.observed_at).total_seconds() // 60)
        )
        updated.wind.freshness = classify_freshness(updated.wind.age_minutes, thresholds)
        wind_state = updated.wind.freshness
    updated.data_state = aggregate_data_state(wave_state, wind_state)
    return updated


class DisplayClient:
    def __init__(
        self,
        api_url: str,
        cache_path: Path | str,
        *,
        timeout_seconds: float = 10.0,
        freshness: FreshnessConfig | None = None,
    ) -> None:
        self.api_url = api_url
        self.cache_path = Path(cache_path)
        self.timeout_seconds = timeout_seconds
        self.freshness = freshness or FreshnessConfig()
        self.offline = False

    def load_cache(self) -> CompactDisplayPayload | None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return CompactDisplayPayload.model_validate(raw)
        except (OSError, ValueError):
            return None

    def _save_cache(self, payload: CompactDisplayPayload) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.cache_path)

    def fetch(self) -> CompactDisplayPayload | None:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(self.api_url, headers={"Accept": "application/json"})
                response.raise_for_status()
            payload = CompactDisplayPayload.model_validate(response.json())
            self._save_cache(payload)
            self.offline = False
            return recalculate_ages(payload, freshness=self.freshness)
        except (httpx.HTTPError, ValueError):
            self.offline = True
            cached = self.load_cache()
            if cached is None:
                return None
            return recalculate_ages(cached, freshness=self.freshness)

    def run(
        self,
        draw: Callable[[CompactDisplayPayload, bool], None],
        *,
        interval_seconds: float = 15.0,
    ) -> None:
        while True:
            payload = self.fetch()
            if payload is not None:
                draw(payload, self.offline)
            time.sleep(interval_seconds)
