"""Render the README images from the real renderer.

These are product shots of an object that does not physically exist yet, so
they must come from the same pixels the sign will show rather than from a
mockup. Everything here is presentation only: LED geometry, bloom, and the
enclosure. The face itself is whatever `DisplayRenderer` produced.

    PYTHONPATH=src python3 scripts/render_readme_images.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from swellsign.display.client import recalculate_ages
from swellsign.display.renderer import HEIGHT, WIDTH, DisplayRenderer, animation_phase
from swellsign.display.web import build_state
from swellsign.models import CompactDisplayPayload

DOCS = Path("docs")
WALL = (13, 15, 16)


def face(state: str, *, brightness: float = 0.55, elapsed: float = 2.0) -> Image.Image:
    raw, offline = build_state(state)
    payload = recalculate_ages(CompactDisplayPayload.model_validate(raw))
    return DisplayRenderer(brightness=brightness).render(
        payload,
        offline=offline,
        animation_phase=animation_phase(payload, elapsed),
    )


def leds(frame: Image.Image, cell: int) -> Image.Image:
    """Draw the frame as discrete emitters behind a diffuser."""
    gap = max(1, round(cell * 0.16))
    dot = cell - gap
    radius = dot / 2
    panel = Image.new("RGB", (WIDTH * cell, HEIGHT * cell), (0, 0, 0))
    draw = ImageDraw.Draw(panel)

    for y in range(HEIGHT):
        for x in range(WIDTH):
            cx, cy = x * cell + cell / 2, y * cell + cell / 2
            pixel = frame.getpixel((x, y))
            if pixel == (0, 0, 0):
                # Unlit emitters are not truly black on a real panel.
                r = radius * 0.42
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(7, 8, 8))
            else:
                draw.ellipse(
                    [cx - radius, cy - radius, cx + radius, cy + radius], fill=pixel
                )

    glow = panel.filter(ImageFilter.GaussianBlur(cell * 0.55))
    glow = glow.point(lambda value: int(value * 0.6))
    return ImageChops.screen(panel, glow)


def enclosure(panel: Image.Image, *, pad: int = 34, radius: int = 18) -> Image.Image:
    """Dark case, shallow shadow gap from the wall, continuous acrylic face."""
    body = Image.new("RGB", (panel.width + pad * 2, panel.height + pad * 2), WALL)
    draw = ImageDraw.Draw(body)
    draw.rounded_rectangle(
        [0, 0, body.width - 1, body.height - 1], radius=radius, fill=(23, 26, 28)
    )
    draw.rounded_rectangle(
        [1, 1, body.width - 2, body.height - 2], radius=radius, outline=(38, 42, 45)
    )
    body.paste(panel, (pad, pad))

    # A faint diagonal sheen reads as acrylic. It has to be blurred hard: a
    # crisp edge looks like a rendering artifact rather than a reflection.
    sheen = Image.new("L", body.size, 0)
    ImageDraw.Draw(sheen).polygon(
        [(0, 0), (body.width * 0.5, 0), (0, body.height * 1.8)], fill=22
    )
    sheen = sheen.filter(ImageFilter.GaussianBlur(body.height * 0.22))
    body = ImageChops.screen(body, Image.merge("RGB", (sheen, sheen, sheen)))
    return body


def mount(sign: Image.Image, *, margin: int = 72) -> Image.Image:
    """Place the sign on a wall, lit by its own glow, with a shadow gap."""
    scene = Image.new("RGB", (sign.width + margin * 2, sign.height + margin * 2), WALL)

    # The panel spills a little light onto the wall around it. Without this the
    # sign looks pasted on rather than switched on.
    spill = Image.new("RGB", scene.size, (0, 0, 0))
    spill.paste(sign, (margin, margin))
    spill = spill.filter(ImageFilter.GaussianBlur(margin * 0.8))
    scene = ImageChops.screen(scene, spill.point(lambda value: int(value * 0.5)))

    shadow = Image.new("L", scene.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [margin + 10, margin + 22, margin + sign.width - 10, margin + sign.height + 26],
        radius=24,
        fill=210,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    scene = Image.composite(Image.new("RGB", scene.size, (0, 0, 0)), scene, shadow)

    scene.paste(sign, (margin, margin))
    return scene


def stack(frames: list[Image.Image], *, gap: int = 22) -> Image.Image:
    width = max(frame.width for frame in frames)
    height = sum(frame.height for frame in frames) + gap * (len(frames) - 1)
    out = Image.new("RGB", (width, height), WALL)
    y = 0
    for frame in frames:
        out.paste(frame, ((width - frame.width) // 2, y))
        y += frame.height + gap
    return out


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)

    hero = mount(enclosure(leds(face("swell"), cell=13)))
    hero.save(DOCS / "swell-sign.png")
    print(f"docs/swell-sign.png {hero.size}")

    states = stack(
        [enclosure(leds(face(name), cell=7), pad=18, radius=10)
         for name in ("fresh", "fallback", "stale", "no-wave")]
    )
    states.save(DOCS / "states.png")
    print(f"docs/states.png {states.size}")


if __name__ == "__main__":
    main()
