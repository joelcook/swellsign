"""Browser panel: another output adapter, not a second renderer.

The page is a dumb panel in exactly the way the HUB75 panel is dumb. Python
renders every frame with the real :class:`DisplayRenderer` and ships raw pixels
to the browser, so what you see cannot drift from what the sign shows. A
JavaScript reimplementation of the layout would be a second source of truth and
would quietly break the simulator contract in section 17 of the spec.

Colors are sent unmodified, without the panel gamma. A monitor already applies
that curve, so these are the same perceptual values the PNG previews use.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from ..models import CompactDisplayPayload
from .client import DisplayClient, recalculate_ages
from .palette import DEFAULT_PALETTE, BrightnessSchedule
from .renderer import HEIGHT, WIDTH, DisplayRenderer, animation_phase

PAGE_PATH = Path(__file__).with_name("simulator.html")

# Every state the sign must remain legible in, per spec section 12.3 and 18.5.
# They are synthesized from one base payload by moving timestamps and dropping
# components, which keeps them honest: each is a real payload the renderer
# accepts, not a hand-drawn mock of what we hope it looks like.
STATES: dict[str, str] = {
    "fresh": "Fresh primary observation",
    "swell": "Long-period groundswell reading",
    "rising": "Rising six-hour trend",
    "falling": "Falling six-hour trend",
    "steady": "Steady six-hour trend",
    "unknown-trend": "Insufficient evidence for a trend",
    "delayed": "Delayed observation, values retained",
    "stale": "Stale cached observation, three-digit age",
    "fallback": "Fresh fallback source, ALT",
    "partial-wind": "Valid wave, no wind",
    "no-direction": "Missing direction, values retained",
    "no-wave": "No usable wave observation",
    "offline": "API unreachable, cached payload ageing",
    "long-values": "Wide values and clamped typography",
}


def _base_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "observed",
        "spot": "NEW SMYRNA",
        "generated_at": "2026-07-30T18:00:00Z",
        "wave": {
            # Matches config/spots.yaml display.wave_label. These fixtures are
            # raw compact payloads, so they do not pass through the override.
            "label": "SWELL",
            "height_ft": 2.6,
            "period_s": 8.1,
            "direction": "NE",
            "observed_at": "2026-07-30T17:48:00Z",
            "age_minutes": 12,
            "freshness": "fresh",
            "trend": "rising",
        },
        "wind": {
            "direction": "W",
            "speed_mph": 7.8,
            "observed_at": "2026-07-30T17:38:00Z",
            "age_minutes": 22,
            "freshness": "fresh",
        },
        "data_state": "fresh",
        "fallback_used": False,
        "warnings": [],
    }


def _aged(payload: dict[str, Any], minutes: int, now: datetime) -> dict[str, Any]:
    """Re-time a payload so ages are computed, never hand-written."""
    observed = (now - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    payload["generated_at"] = now.isoformat().replace("+00:00", "Z")
    for component in ("wave", "wind"):
        if payload.get(component):
            payload[component]["observed_at"] = observed
            payload[component]["age_minutes"] = minutes
    return payload


def build_state(state: str, *, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Return one payload for the named state plus its offline flag."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    payload = _aged(_base_payload(), 12, moment)
    offline = False

    if state == "fresh":
        pass
    elif state == "swell":
        payload["wave"]["period_s"] = 13.2
        payload["wave"]["direction"] = "ENE"
    elif state in {"rising", "falling", "steady"}:
        payload["wave"]["trend"] = state
    elif state == "unknown-trend":
        payload["wave"]["trend"] = "unknown"
    elif state == "delayed":
        payload = _aged(payload, 132, moment)
        payload["data_state"] = "delayed"
        payload["wave"]["freshness"] = "delayed"
        payload["wind"]["freshness"] = "delayed"
    elif state == "stale":
        payload = _aged(payload, 247, moment)
        payload["data_state"] = "stale"
        payload["wave"]["freshness"] = "stale"
        payload["wind"]["freshness"] = "stale"
    elif state == "fallback":
        payload["fallback_used"] = True
        payload["wave"]["direction"] = "ESE"
    elif state == "partial-wind":
        payload["wind"] = None
        payload["data_state"] = "partial"
    elif state == "no-direction":
        payload["wave"]["direction"] = None
        payload["wind"]["direction"] = None
    elif state == "no-wave":
        payload["wave"] = None
        payload["data_state"] = "partial"
    elif state == "offline":
        payload = _aged(payload, 96, moment)
        offline = True
    elif state == "long-values":
        payload["spot"] = "PONCE INLET NORTH"
        payload["wave"]["height_ft"] = 14.75
        payload["wave"]["period_s"] = 17.5
        payload["wind"]["speed_mph"] = 42.4
        payload["wave"]["direction"] = "SSW"
    else:
        raise KeyError(state)

    return payload, offline


def create_simulator_app(
    *,
    api_url: str | None = None,
    schedule: BrightnessSchedule | None = None,
) -> FastAPI:
    """Build the simulator server.

    ``api_url`` enables the live state, which polls the real display endpoint
    so the browser can show the actual ocean rather than a fixture.
    """
    application = FastAPI(title="Swell Sign simulator", docs_url=None, redoc_url=None)
    brightness_schedule = schedule or BrightnessSchedule()
    live_client = (
        DisplayClient(api_url, Path("data/simulator-cache.json")) if api_url else None
    )

    @application.get("/", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"))

    @application.get("/api/states")
    def states() -> dict[str, Any]:
        return {
            "states": [{"id": key, "label": value} for key, value in STATES.items()],
            "live_available": live_client is not None,
            "api_url": api_url,
            "width": WIDTH,
            "height": HEIGHT,
            "background": list(DEFAULT_PALETTE.background),
            "schedule": {
                "day": brightness_schedule.day,
                "evening": brightness_schedule.evening,
                "night": brightness_schedule.night,
                "day_start_hour": brightness_schedule.day_start_hour,
                "evening_start_hour": brightness_schedule.evening_start_hour,
                "night_start_hour": brightness_schedule.night_start_hour,
            },
        }

    def _resolve(state: str) -> tuple[CompactDisplayPayload, bool, dict[str, Any]]:
        if state == "live":
            if live_client is None:
                raise HTTPException(
                    status_code=409,
                    detail="the simulator was started without --api-url",
                )
            fetched = live_client.fetch()
            if fetched is None:
                raise HTTPException(
                    status_code=503,
                    detail="the display endpoint is unreachable and no cache exists",
                )
            return fetched, live_client.offline, fetched.model_dump(mode="json")
        try:
            raw, offline = build_state(state)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=f"unknown state {state!r}") from error
        payload = recalculate_ages(CompactDisplayPayload.model_validate(raw))
        return payload, offline, payload.model_dump(mode="json")

    @application.get("/api/payload")
    def payload_for(state: Annotated[str, Query()] = "fresh") -> dict[str, Any]:
        _, offline, serialized = _resolve(state)
        return {"state": state, "offline": offline, "payload": serialized}

    @application.get("/api/frame")
    def frame(
        state: Annotated[str, Query()] = "fresh",
        elapsed: Annotated[float, Query(ge=0)] = 0.0,
        brightness: Annotated[float, Query(ge=0.0, le=1.0)] = 0.55,
        motion: Annotated[bool, Query()] = True,
    ) -> Response:
        """Return one rendered frame as raw RGB bytes.

        Raw pixels rather than PNG keeps the browser from having to decode at
        frame rate, and 128x32x3 is only twelve kilobytes.
        """
        payload, offline, _ = _resolve(state)
        image = DisplayRenderer(brightness=brightness).render(
            payload,
            offline=offline,
            animation_phase=animation_phase(payload, elapsed, motion=motion),
        )
        return Response(
            content=image.convert("RGB").tobytes(),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    return application


def write_state_fixtures(directory: Path | str) -> list[Path]:
    """Write every synthesized state to disk for golden-image work."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for state in STATES:
        payload, _ = build_state(state)
        path = target / f"display-{state}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written
