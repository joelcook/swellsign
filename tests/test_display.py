from datetime import UTC, datetime, timedelta

from swellsign.display.client import recalculate_ages
from swellsign.display.renderer import DisplayRenderer
from swellsign.models import CompactDisplayPayload

NOW = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)


def payload(**overrides):
    raw = {
        "schema_version": 1,
        "mode": "observed",
        "spot": "NEW SMYRNA",
        "generated_at": NOW.isoformat(),
        "wave": {
            "label": "SEAS",
            "height_ft": 2.6,
            "period_s": 8.1,
            "direction": "NE",
            "observed_at": (NOW - timedelta(minutes=12)).isoformat(),
            "age_minutes": 12,
            "freshness": "fresh",
            "trend": "rising",
        },
        "wind": {
            "direction": "W",
            "speed_mph": 7.8,
            "observed_at": (NOW - timedelta(minutes=22)).isoformat(),
            "age_minutes": 22,
            "freshness": "fresh",
        },
        "data_state": "fresh",
        "fallback_used": False,
        "warnings": [],
    }
    raw.update(overrides)
    return CompactDisplayPayload.model_validate(raw)


def test_renderer_is_exact_and_deterministic():
    renderer = DisplayRenderer(brightness=0.5)
    first = renderer.render(payload(), now=NOW)
    second = renderer.render(payload(), now=NOW)
    assert first.size == (128, 32)
    assert first.mode == "RGB"
    assert first.tobytes() == second.tobytes()
    assert first.getbbox() is not None


def test_renderer_handles_fallback_offline_partial_and_no_wave():
    renderer = DisplayRenderer()
    fallback = payload(fallback_used=True)
    offline = renderer.render(fallback, now=NOW, offline=True)
    no_wave = payload(wave=None, data_state="partial")
    partial = renderer.render(no_wave, now=NOW)
    assert offline.size == (128, 32)
    assert partial.size == (128, 32)
    assert offline.tobytes() != partial.tobytes()


def test_client_recalculates_component_ages_and_freshness():
    later = NOW + timedelta(minutes=100)
    updated = recalculate_ages(payload(), now=later)
    assert updated.wave.age_minutes == 112
    assert updated.wind.age_minutes == 122
    assert updated.wave.freshness == "delayed"
    assert updated.data_state == "delayed"


def test_forecast_payload_is_rejected():
    raw = payload().model_dump(mode="json")
    raw["mode"] = "forecast"
    try:
        CompactDisplayPayload.model_validate(raw)
    except ValueError:
        pass
    else:
        raise AssertionError("display accepted a forecast payload")
