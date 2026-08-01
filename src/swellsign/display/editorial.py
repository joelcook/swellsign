"""Editorial layout: a photograph of the place, with the reading over it.

A different object from the sign. The sign is an instrument you glance at; this
is décor that happens to answer the question, meant for a wall someone chose to
hang it on. It shows one spot large rather than many spots small.

Unlike :mod:`swellsign.display.frame`, this does not composite the 128x32 face.
It is its own typographic layout, so it needs a real typeface. A system font is
located at render time and the layout degrades to the bundled pixel font rather
than failing if none is found.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..models import CompactDisplayPayload
from .frame import FRAME_HEIGHT, FRAME_WIDTH, _cover, pixel_text

# Ordered by preference. Helvetica is on every mac; DejaVu ships with most
# Linux images, which is what the server will be.
FONT_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("/System/Library/Fonts/HelveticaNeue.ttc", 0),
    ("/System/Library/Fonts/Helvetica.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 0),
    ("/Library/Fonts/Arial.ttf", 0),
)

GLASS = (79, 189, 180)
WARM = (216, 201, 166)
AMBER = (210, 130, 58)
MUTED = (110, 122, 126)
# Labels sit between the cyan heading and the warm values: clearly legible,
# clearly subordinate, and a different hue from both so the three tiers read
# as a hierarchy rather than as one washed-out family.
LABEL = (150, 170, 176)
PAPER = (242, 236, 224)


def _font(size: int) -> ImageFont.FreeTypeFont | None:
    for path, index in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return None


def _tracked(
    draw: ImageDraw.ImageDraw,
    text: str,
    origin: tuple[int, int],
    font: ImageFont.FreeTypeFont,
    color: tuple[int, int, int],
    tracking: float = 0.0,
) -> int:
    """Draw text with letter spacing, which Pillow has no native support for.

    Wide tracking is most of what makes the small labels read as considered
    rather than as a caption, so it is worth the per-character loop.
    """
    x, y = origin
    for char in text:
        draw.text((x, y), char, font=font, fill=color)
        x += draw.textlength(char, font=font) + tracking
    return int(x - origin[0])


def _tracked_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    tracking: float = 0.0,
) -> float:
    return sum(draw.textlength(c, font=font) + tracking for c in text)


def _veil(size: tuple[int, int]) -> Image.Image:
    """Bottom-weighted gradient so type reads without flattening the picture."""
    width, height = size
    mask = Image.new("L", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        if t < 0.42:
            value = 12
        elif t < 0.74:
            value = int(12 + (150 - 12) * ((t - 0.42) / 0.32))
        else:
            value = int(150 + (245 - 150) * ((t - 0.74) / 0.26))
        mask.putpixel((0, y), value)
    return mask.resize((width, height)).filter(ImageFilter.GaussianBlur(2))


def render_editorial_image(
    payload: CompactDisplayPayload | dict[str, Any],
    *,
    background: Image.Image | Path | str,
    width: int = FRAME_WIDTH,
    height: int = FRAME_HEIGHT,
    place: str | None = None,
    credit: str | None = None,
    speed_suffix: str = "mph",
    speed_scale: float = 1.0,
) -> Image.Image:
    data = (
        payload
        if isinstance(payload, CompactDisplayPayload)
        else CompactDisplayPayload.model_validate(payload)
    )

    photo = background if isinstance(background, Image.Image) else Image.open(background)
    scene = _cover(photo.convert("RGB"), width, height)
    scene = Image.composite(Image.new("RGB", scene.size, (4, 6, 7)), scene, _veil(scene.size))
    draw = ImageDraw.Draw(scene)

    # Everything is derived from canvas width so the layout holds at any size.
    unit = width / 1600
    left = int(width * 0.05)
    bottom = int(height * 0.955)

    place_font = _font(int(21 * unit))
    probe = _font(int(62 * unit))

    if probe is None:
        # No system typeface: fall back to the bundled pixel font rather than
        # failing. Ugly, but it still tells you what the ocean is doing.
        pixel_text(scene, data.spot, (left, bottom - int(120 * unit)), scale=max(1, int(6 * unit)), color=WARM)
        if data.wave:
            pixel_text(
                scene,
                f"{data.wave.height_ft:.1f}FT {data.wave.period_s}S {data.wave.direction or '--'}",
                (left, bottom - int(60 * unit)),
                scale=max(1, int(8 * unit)),
                color=GLASS,
            )
        return scene

    facts: list[tuple[str, str, tuple[int, int, int]]] = []
    if data.wave:
        facts.append(("Height", f"{data.wave.height_ft:.1f}ft", PAPER))
        facts.append(("Period", f"{data.wave.period_s:g}s", PAPER))
        facts.append(("Swell", data.wave.direction or "--", PAPER))
    if data.wind:
        # Every other value carries its unit. Without one, 9 is ambiguous
        # between knots and mph, which is a 15% difference a surfer cares about.
        speed = data.wind.speed_mph * speed_scale
        facts.append(
            ("Wind", f"{data.wind.direction or '--'} {speed:.0f}{speed_suffix}", AMBER)
        )
    if data.tide:
        # Drawn as a polygon below; Helvetica has no U+25B2 and renders tofu.
        facts.append(("Tide", data.tide.level.title(), WARM))

    value_font = _font(int(62 * unit))
    label_font = _font(int(19 * unit))

    fact_top = bottom - int(62 * unit) - int(34 * unit)
    label_top = fact_top - int(19 * unit) - int(20 * unit)

    x = left
    for key, value, colour in facts:
        _tracked(draw, key.upper(), (int(x), label_top), label_font, LABEL, 5.0 * unit)
        start = x
        if key == "Tide" and data.tide is not None:
            size = int(26 * unit)
            top = fact_top + int(26 * unit)
            if data.tide.state == "rising":
                points = [(x, top + size), (x + size, top + size), (x + size / 2, top)]
            else:
                points = [(x, top), (x + size, top), (x + size / 2, top + size)]
            draw.polygon(points, fill=colour)
            x += size + int(16 * unit)
        draw.text((int(x), fact_top), value, font=value_font, fill=colour)
        x = start + max(
            _tracked_width(draw, key.upper(), label_font, 5.0 * unit),
            (x - start) + draw.textlength(value, font=value_font),
        ) + int(88 * unit)

    heading = (place or data.spot).upper()
    _tracked(
        draw,
        heading,
        (left, label_top - int(21 * unit) - int(34 * unit)),
        place_font,
        GLASS,
        8.5 * unit,
    )

    if credit:
        small = _font(int(11 * unit)) or place_font
        w = _tracked_width(draw, credit.upper(), small, 3.0 * unit)
        _tracked(
            draw,
            credit.upper(),
            (int(width - width * 0.05 - w), int(height * 0.965)),
            small,
            (69, 77, 79),
            3.0 * unit,
        )

    return scene
