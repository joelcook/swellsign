"""Large-format rendering of the sign for screens rather than panels.

The Samsung Frame and the README both want the same thing: the exact 128x32
face, blown up and dressed as a physical object. This is presentation only.
Every lit pixel comes from :class:`DisplayRenderer`, so a screen can never show
a layout the panel would not, which is the same contract the browser simulator
holds to.

Bloom is applied at panel resolution and the result composited onto the large
canvas. Blurring 4K directly is far slower for an identical outcome.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from ..models import CompactDisplayPayload
from .font import ADVANCE, GLYPHS
from .renderer import HEIGHT, WIDTH, DisplayRenderer

FRAME_WIDTH = 3840
FRAME_HEIGHT = 2160
WALL = (9, 10, 11)


def led_panel(frame: Image.Image, cell: int) -> Image.Image:
    """Draw a rendered face as discrete emitters behind a diffuser."""
    gap = max(1, round(cell * 0.16))
    dot = cell - gap
    radius = dot / 2
    panel = Image.new("RGB", (WIDTH * cell, HEIGHT * cell), (0, 0, 0))
    draw = ImageDraw.Draw(panel)

    for y in range(HEIGHT):
        for x in range(WIDTH):
            centre = (x * cell + cell / 2, y * cell + cell / 2)
            pixel = frame.getpixel((x, y))
            if pixel == (0, 0, 0):
                # Unlit emitters are not truly black on a real panel, and the
                # faint grid is what keeps this from looking like a poster.
                r = radius * 0.42
                draw.ellipse(
                    [centre[0] - r, centre[1] - r, centre[0] + r, centre[1] + r],
                    fill=(7, 8, 8),
                )
            else:
                draw.ellipse(
                    [
                        centre[0] - radius,
                        centre[1] - radius,
                        centre[0] + radius,
                        centre[1] + radius,
                    ],
                    fill=pixel,
                )

    glow = panel.filter(ImageFilter.GaussianBlur(cell * 0.55))
    glow = glow.point(lambda value: int(value * 0.6))
    return ImageChops.screen(panel, glow)


def enclosure(panel: Image.Image, *, pad: int = 34, radius: int = 18) -> Image.Image:
    """Dark case with a continuous acrylic face across both panels."""
    body = Image.new("RGB", (panel.width + pad * 2, panel.height + pad * 2), WALL)
    draw = ImageDraw.Draw(body)
    draw.rounded_rectangle(
        [0, 0, body.width - 1, body.height - 1], radius=radius, fill=(23, 26, 28)
    )
    draw.rounded_rectangle(
        [1, 1, body.width - 2, body.height - 2], radius=radius, outline=(38, 42, 45)
    )
    body.paste(panel, (pad, pad))

    # A crisp edge reads as a rendering artifact rather than a reflection, so
    # the sheen is blurred hard.
    sheen = Image.new("L", body.size, 0)
    ImageDraw.Draw(sheen).polygon(
        [(0, 0), (body.width * 0.5, 0), (0, body.height * 1.8)], fill=22
    )
    sheen = sheen.filter(ImageFilter.GaussianBlur(body.height * 0.22))
    return ImageChops.screen(body, Image.merge("RGB", (sheen, sheen, sheen)))


def pixel_text(
    image: Image.Image,
    text: str,
    position: tuple[int, int],
    *,
    scale: int = 2,
    color: tuple[int, int, int] = (110, 110, 110),
) -> int:
    """Draw the bundled pixel font at an arbitrary scale. Returns its width.

    Used for credit lines, so attribution is rendered in the same typeface as
    the instrument rather than pulling in a system font that may not exist on
    whatever machine renders the frame.
    """
    draw = ImageDraw.Draw(image)
    x0, y0 = position
    for index, char in enumerate(text.upper()):
        glyph = GLYPHS.get(char, GLYPHS["?"])
        gx = x0 + index * ADVANCE * scale
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "1":
                    px, py = gx + column * scale, y0 + row * scale
                    draw.rectangle([px, py, px + scale - 1, py + scale - 1], fill=color)
    return len(text) * ADVANCE * scale


def _cover(background: Image.Image, width: int, height: int) -> Image.Image:
    """Scale and centre-crop to fill the canvas without distorting."""
    scale = max(width / background.width, height / background.height)
    resized = background.resize(
        (max(1, round(background.width * scale)), max(1, round(background.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _scrim(size: tuple[int, int], band_top: int, band_bottom: int) -> Image.Image:
    """Darkening mask that protects legibility without hiding the photograph.

    A flat overlay would either wash the sign out or mute the whole image. This
    darkens everything slightly, then deepens through the band the sign
    occupies, fading back out above and below so there is no visible edge.
    """
    width, height = size
    mask = Image.new("L", size, 90)
    draw = ImageDraw.Draw(mask)
    feather = max(1, (band_bottom - band_top) // 2)
    for y in range(max(0, band_top - feather), min(height, band_bottom + feather)):
        if y < band_top:
            weight = (y - (band_top - feather)) / feather
        elif y > band_bottom:
            weight = 1 - (y - band_bottom) / feather
        else:
            weight = 1.0
        draw.line([(0, y), (width, y)], fill=int(90 + 150 * max(0.0, min(1.0, weight))))
    return mask.filter(ImageFilter.GaussianBlur(feather * 0.3))


def render_frame_image(
    payload: CompactDisplayPayload | dict[str, Any],
    *,
    width: int = FRAME_WIDTH,
    height: int = FRAME_HEIGHT,
    cell: int = 20,
    brightness: float = 0.55,
    offline: bool = False,
    background: Image.Image | Path | str | None = None,
    credit: str | None = None,
    placement: str = "center",
) -> Image.Image:
    """Render the face as a framed object centred on a dark field.

    Sized for a Frame TV in Art Mode: mostly negative space, because on a matte
    panel in a dim room the black is the point. The sign should read as an
    object hanging there, not as a screen showing a dashboard.
    """
    face = DisplayRenderer(brightness=brightness).render(payload, offline=offline)
    sign = enclosure(led_panel(face, cell), pad=cell * 2, radius=cell)

    if background is not None:
        photo = background
        if not isinstance(photo, Image.Image):
            photo = Image.open(photo)
        scene = _cover(photo.convert("RGB"), width, height)
    else:
        scene = Image.new("RGB", (width, height), WALL)

    if placement == "lower":
        top = int(height * 0.70) - sign.height // 2
    else:
        top = (height - sign.height) // 2
    origin = ((width - sign.width) // 2, max(0, min(height - sign.height, top)))

    if background is not None:
        # Darken before compositing. The sign is deliberately dim, and dim
        # sea-glass over a bright sky is unreadable.
        mask = _scrim(scene.size, origin[1] - cell * 3, origin[1] + sign.height + cell * 3)
        scene = Image.composite(Image.new("RGB", scene.size, (0, 0, 0)), scene, mask)

    # The panel spills light onto the wall around it. Without this the sign
    # looks pasted onto the background rather than switched on in front of it.
    spill = Image.new("RGB", scene.size, (0, 0, 0))
    spill.paste(sign, origin)
    spill = spill.filter(ImageFilter.GaussianBlur(cell * 4))
    scene = ImageChops.screen(scene, spill.point(lambda value: int(value * 0.45)))

    shadow = Image.new("L", scene.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [
            origin[0] + cell,
            origin[1] + cell * 2,
            origin[0] + sign.width - cell,
            origin[1] + sign.height + cell * 2,
        ],
        radius=cell,
        fill=210,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(cell * 2))
    scene = Image.composite(Image.new("RGB", scene.size, (0, 0, 0)), scene, shadow)

    scene.paste(sign, origin)

    if credit:
        # Attribution is composited rather than shown as UI chrome, so a CC-BY
        # obligation is met by the image itself wherever it ends up displayed.
        scale = max(1, cell // 8)
        pixel_text(
            scene,
            credit,
            (cell * 2, height - cell * 2 - 7 * scale),
            scale=scale,
            color=(96, 96, 96),
        )

    return scene
