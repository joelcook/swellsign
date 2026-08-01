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

from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from ..models import CompactDisplayPayload
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


def render_frame_image(
    payload: CompactDisplayPayload | dict[str, Any],
    *,
    width: int = FRAME_WIDTH,
    height: int = FRAME_HEIGHT,
    cell: int = 20,
    brightness: float = 0.55,
    offline: bool = False,
) -> Image.Image:
    """Render the face as a framed object centred on a dark field.

    Sized for a Frame TV in Art Mode: mostly negative space, because on a matte
    panel in a dim room the black is the point. The sign should read as an
    object hanging there, not as a screen showing a dashboard.
    """
    face = DisplayRenderer(brightness=brightness).render(payload, offline=offline)
    sign = enclosure(led_panel(face, cell), pad=cell * 2, radius=cell)

    scene = Image.new("RGB", (width, height), WALL)
    origin = ((width - sign.width) // 2, (height - sign.height) // 2)

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
    return scene
