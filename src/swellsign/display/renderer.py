"""Deterministic 128-by-32 reference renderer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image

from ..models import CompactDisplayPayload, DataState, Freshness, TrendState
from .font import ADVANCE, GLYPHS, text_width
from .palette import DEFAULT_PALETTE, Palette, dim

WIDTH = 128
HEIGHT = 32


def _safe_number(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "--"
    return f"{value:.{decimals}f}"


def _age(observed_at: datetime, now: datetime) -> int:
    return max(0, int((now - observed_at).total_seconds() // 60))


class DisplayRenderer:
    """Renders the stable NOW face without installed font dependencies."""

    def __init__(
        self,
        *,
        palette: Palette = DEFAULT_PALETTE,
        brightness: float = 0.55,
    ) -> None:
        self.palette = palette
        self.brightness = brightness

    def _color(self, color: tuple[int, int, int]) -> tuple[int, int, int]:
        return dim(color, self.brightness)

    def _text(
        self,
        image: Image.Image,
        position: tuple[int, int],
        text: str,
        color: tuple[int, int, int],
        *,
        max_x: int = WIDTH,
    ) -> None:
        pixels = image.load()
        start_x, start_y = position
        for char_index, char in enumerate(text.upper()):
            glyph = GLYPHS.get(char, GLYPHS["?"])
            glyph_x = start_x + char_index * ADVANCE
            if glyph_x >= max_x:
                break
            for row, bits in enumerate(glyph):
                y = start_y + row
                if not 0 <= y < HEIGHT:
                    continue
                for column, bit in enumerate(bits):
                    x = glyph_x + column
                    if bit == "1" and 0 <= x < min(WIDTH, max_x):
                        pixels[x, y] = color

    def _right_text(
        self,
        image: Image.Image,
        y: int,
        text: str,
        color: tuple[int, int, int],
    ) -> None:
        self._text(image, (WIDTH - text_width(text), y), text, color)

    def _trend_mark(
        self,
        image: Image.Image,
        state: TrendState,
        *,
        x: int = 63,
        y: int = 12,
    ) -> None:
        color = self._color(self.palette.sea_glass)
        pixels = image.load()
        if state is TrendState.RISING:
            points = ((x, y + 5), (x + 1, y + 4), (x + 2, y + 3), (x + 3, y + 2),
                      (x + 4, y + 1), (x + 4, y + 2), (x + 4, y + 3))
        elif state is TrendState.FALLING:
            points = ((x, y + 1), (x + 1, y + 2), (x + 2, y + 3), (x + 3, y + 4),
                      (x + 4, y + 5), (x + 4, y + 4), (x + 4, y + 3))
        elif state is TrendState.STEADY:
            points = tuple((x + offset, y + 3) for offset in range(5))
        else:
            return
        for px, py in points:
            if 0 <= px < WIDTH and 0 <= py < HEIGHT:
                pixels[px, py] = color

    def _status_text(
        self,
        payload: CompactDisplayPayload,
        wave_age: int | None,
        *,
        offline: bool,
    ) -> tuple[str, tuple[int, int, int]]:
        age_text = "--M" if wave_age is None else f"{min(999, wave_age)}M"
        if offline:
            return f"OFF {age_text}", self._color(self.palette.warning_amber)
        if payload.fallback_used:
            return f"ALT {age_text}", self._color(self.palette.warning_amber)
        if payload.data_state is DataState.STALE:
            return f"STL {age_text}", self._color(self.palette.warning_amber)
        if payload.data_state is DataState.DELAYED:
            return f"DLY {age_text}", self._color(self.palette.warning_amber)
        return age_text, self._color(self.palette.warm_white)

    def render(
        self,
        payload: CompactDisplayPayload | dict[str, Any],
        *,
        now: datetime | None = None,
        offline: bool = False,
        animation_phase: float | None = None,
    ) -> Image.Image:
        data = (
            payload
            if isinstance(payload, CompactDisplayPayload)
            else CompactDisplayPayload.model_validate(payload)
        )
        render_time = (now or datetime.now(UTC)).astimezone(UTC)
        image = Image.new("RGB", (WIDTH, HEIGHT), self.palette.background)
        warm = self._color(self.palette.warm_white)
        cyan = self._color(self.palette.sea_glass)
        amber = self._color(self.palette.wind_amber)

        wave_age = _age(data.wave.observed_at, render_time) if data.wave else None
        status, status_color = self._status_text(data, wave_age, offline=offline)
        status_x = WIDTH - text_width(status)
        self._text(image, (0, 1), data.spot[:18], warm, max_x=max(0, status_x - 2))
        self._right_text(image, 1, status, status_color)

        # A quiet two-pixel marine trace divides the header. It conveys no rating.
        pixels = image.load()
        quiet = self._color(self.palette.quiet_water)
        for x in range(WIDTH):
            if x % 8 in (2, 3):
                pixels[x, 9] = quiet

        if data.wave is None:
            error = self._color(self.palette.unavailable_red)
            message = "NO WAVE DATA"
            self._text(image, ((WIDTH - text_width(message)) // 2, 12), message, error)
        else:
            wave = data.wave
            height = _safe_number(wave.height_ft)
            if len(height) > 4:
                height = f"{wave.height_ft:.0f}"
            period = _safe_number(wave.period_s)
            if len(period) > 4:
                period = f"{wave.period_s:.0f}"
            self._text(image, (0, 12), wave.label[:5], warm)
            self._text(image, (29, 12), f"{height}FT", cyan, max_x=61)
            self._trend_mark(image, wave.trend)
            self._text(image, (72, 12), f"{period}S", cyan, max_x=104)
            self._right_text(image, 12, (wave.direction or "--")[:3], cyan)

            # Optional one-pixel breathing mark; period-paced by the caller.
            if (
                animation_phase is not None
                and wave.period_s > 0
                and wave.freshness is Freshness.FRESH
            ):
                phase = animation_phase % 1.0
                crest_x = 106 + int(phase * 8)
                crest_y = 18 - (1 if 0.25 <= phase < 0.75 else 0)
                pixels[crest_x, crest_y] = quiet

        self._text(image, (0, 24), "WIND", warm)
        if data.wind is None:
            self._text(image, (31, 24), "--", self._color(self.palette.warning_amber))
        else:
            wind = data.wind
            self._text(image, (31, 24), (wind.direction or "--")[:3], amber)
            speed = f"{wind.speed_mph:.0f}MPH"
            if len(speed) > 6:
                speed = "99+MPH"
            self._right_text(image, 24, speed, amber)

        return image

    def save(
        self,
        payload: CompactDisplayPayload | dict[str, Any],
        path: Path | str,
        *,
        scale: int = 1,
        now: datetime | None = None,
        offline: bool = False,
    ) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image = self.render(payload, now=now, offline=offline)
        if scale > 1:
            image = image.resize((WIDTH * scale, HEIGHT * scale), Image.Resampling.NEAREST)
        image.save(output_path)
        return output_path


def render_payload(
    payload: CompactDisplayPayload | dict[str, Any],
    *,
    brightness: float = 0.55,
    now: datetime | None = None,
    offline: bool = False,
) -> Image.Image:
    return DisplayRenderer(brightness=brightness).render(payload, now=now, offline=offline)
