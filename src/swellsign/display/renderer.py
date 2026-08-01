"""Deterministic 128-by-32 reference renderer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image

from ..models import CompactDisplayPayload, DataState, Freshness, TrendState
from ..units import MPH_TO_KNOTS
from .font import ADVANCE, GLYPHS, text_width
from .palette import DEFAULT_PALETTE, Palette, dim

WIDTH = 128
HEIGHT = 32

# Text ran flush to columns 0 and 127, so on a real panel it would touch the
# bezel on both sides. Two pixels of quiet edge costs one character of budget
# and makes the face look deliberate rather than cropped.
MARGIN = 2

# Field boxes for the wave row, sized for the widest value each can hold:
# a five-character label (SWELL), a height with units, the trend mark, a
# period, and a right-aligned three-character direction.
LABEL_X = MARGIN
HEIGHT_X = 35
HEIGHT_MAX_X = 65
TREND_X = 68
PERIOD_X = 76
PERIOD_MAX_X = 106

# Wind row: label, direction, speed, then tide anchored to the right edge.
# Tide is the one predicted value on an otherwise measured face, so it gets a
# triangle rather than the wave row's diagonal mark, and label-tier warm white
# rather than a measurement color.
WIND_DIR_X = 31
WIND_SPEED_X = 53
WIND_SPEED_MAX_X = 90
TIDE_ARROW_WIDTH = 5
TIDE_ARROW_GAP = 3

# A solid triangle rather than a stemmed arrow: at this size a long stem under
# a wide head reads as a dagger, and the shape stays clearly distinct from the
# diagonal wave-trend mark on the row above.
_TIDE_ARROW_UP = (
    "..#..",
    ".###.",
    "#####",
)


def _safe_number(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "--"
    return f"{value:.{decimals}f}"


def _fit_measurement(value: float | None, suffix: str, available_px: int) -> str:
    """Render a value with its units, dropping precision before dropping units.

    The suffix has to be measured too. Checking only the number let `14.8` pass
    while `14.8FT` overflowed its box and lost the T, which reads as a unit the
    sign does not actually use.
    """
    if value is None:
        return f"--{suffix}"
    for candidate in (f"{value:.1f}{suffix}", f"{value:.0f}{suffix}", f"{value:.0f}"):
        if text_width(candidate) <= available_px:
            return candidate
    return f"{value:.0f}"


def _age(observed_at: datetime, now: datetime) -> int:
    return max(0, int((now - observed_at).total_seconds() // 60))


class DisplayRenderer:
    """Renders the stable NOW face without installed font dependencies."""

    def __init__(
        self,
        *,
        palette: Palette = DEFAULT_PALETTE,
        brightness: float = 0.55,
        speed_suffix: str = "MPH",
        speed_scale: float = 1.0,
    ) -> None:
        self.palette = palette
        self.brightness = brightness
        # The compact payload always carries mph, since that is the published
        # contract the firmware reads. Converting here keeps the wire format
        # stable while letting the face speak either dialect.
        self.speed_suffix = speed_suffix
        self.speed_scale = speed_scale

    @classmethod
    def for_display(
        cls,
        display_config: Any = None,
        **kwargs: Any,
    ) -> DisplayRenderer:
        """Build a renderer honoring `display.wind_speed_unit` from config."""
        unit = getattr(display_config, "wind_speed_unit", "mph")
        if unit == "kts":
            return cls(speed_suffix="KTS", speed_scale=MPH_TO_KNOTS, **kwargs)
        return cls(speed_suffix="MPH", speed_scale=1.0, **kwargs)

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
        self._text(image, (WIDTH - MARGIN - text_width(text), y), text, color)

    def _trend_mark(
        self,
        image: Image.Image,
        state: TrendState,
        *,
        x: int = TREND_X,
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

    def _tide_mark(
        self,
        image: Image.Image,
        rising: bool,
        *,
        x: int,
        y: int = 26,
    ) -> None:
        pixels = image.load()
        color = self._color(self.palette.warm_white)
        rows = _TIDE_ARROW_UP if rising else tuple(reversed(_TIDE_ARROW_UP))
        for row_index, bits in enumerate(rows):
            for column, bit in enumerate(bits):
                px, py = x + column, y + row_index
                if bit == "#" and 0 <= px < WIDTH and 0 <= py < HEIGHT:
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
        status_x = WIDTH - MARGIN - text_width(status)
        self._text(image, (MARGIN, 1), data.spot[:18], warm, max_x=max(0, status_x - 3))
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
            height = _fit_measurement(wave.height_ft, "FT", HEIGHT_MAX_X - HEIGHT_X)
            period = _fit_measurement(wave.period_s, "S", PERIOD_MAX_X - PERIOD_X)
            self._text(image, (LABEL_X, 12), wave.label[:5], warm, max_x=HEIGHT_X - 3)
            self._text(image, (HEIGHT_X, 12), height, cyan, max_x=HEIGHT_MAX_X)
            self._trend_mark(image, wave.trend)
            self._text(image, (PERIOD_X, 12), period, cyan, max_x=PERIOD_MAX_X)
            self._right_text(image, 12, (wave.direction or "--")[:3], cyan)

            # Optional one-pixel crest, period-paced by the caller.
            #
            # It rides the divider row rather than the wave row. Sharing a row
            # with the measurements meant it collided with three-letter
            # directions like ESE, and a decorative mark must never sit on top
            # of a reading. Here it has the row to itself and can cross the
            # whole face, which reads more like a passing swell anyway.
            if (
                animation_phase is not None
                and wave.period_s > 0
                and wave.freshness is Freshness.FRESH
            ):
                phase = animation_phase % 1.0
                crest_x = int(phase * WIDTH)
                if 0 <= crest_x < WIDTH:
                    pixels[crest_x, 8] = cyan
                    pixels[crest_x, 9] = quiet

        self._text(image, (MARGIN, 24), "WIND", warm)

        if data.wind is None:
            self._text(image, (WIND_DIR_X, 24), "--", self._color(self.palette.warning_amber))
        else:
            wind = data.wind
            self._text(image, (WIND_DIR_X, 24), (wind.direction or "--")[:3], amber)
            speed = f"{wind.speed_mph * self.speed_scale:.0f}{self.speed_suffix}"
            if text_width(speed) > WIND_SPEED_MAX_X - WIND_SPEED_X:
                speed = f"99+{self.speed_suffix}"
            self._text(image, (WIND_SPEED_X, 24), speed, amber, max_x=WIND_SPEED_MAX_X)

        # Tide is astronomical prediction, not measurement. It is shown only
        # when a pair of predicted extremes brackets now; otherwise the right
        # of the row stays empty rather than guessing.
        if data.tide is not None:
            level = data.tide.level.upper()
            block = TIDE_ARROW_WIDTH + TIDE_ARROW_GAP + text_width(level)
            start = WIDTH - MARGIN - block
            self._tide_mark(image, data.tide.state == "rising", x=start)
            self._text(image, (start + TIDE_ARROW_WIDTH + TIDE_ARROW_GAP, 24), level, warm)

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


def animation_phase(
    payload: CompactDisplayPayload,
    elapsed_seconds: float,
    *,
    motion: bool = True,
) -> float | None:
    """Position within one dominant-period cycle, or ``None`` for no motion.

    Shared by every display client so the Pi, the browser simulator, and any
    future output stay on one definition of the crest's pace. The mark is
    decorative: it never implies phase-accurate ocean motion.
    """
    if not motion or payload.wave is None:
        return None
    period = payload.wave.period_s
    if not period or period <= 0:
        return None
    return (elapsed_seconds % period) / period
