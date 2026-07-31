"""Optional Raspberry Pi output for rpi-rgb-led-matrix.

This is the one place gamma is applied. The renderer produces perceptual
values so its PNG matches what an sRGB monitor shows; the panel applies no
curve of its own, so the correction happens here on the way to the hardware.
"""

from __future__ import annotations

from PIL import Image

from .palette import DEFAULT_GAMMA, gamma_table


class PiMatrixOutput:
    """A lazy adapter so development machines do not need RGB matrix bindings."""

    def __init__(
        self,
        *,
        hardware_mapping: str = "adafruit-hat-pwm",
        brightness: int = 35,
        gpio_slowdown: int = 4,
        gamma: float = DEFAULT_GAMMA,
        limit_refresh_rate_hz: int = 120,
    ) -> None:
        try:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions
        except ImportError as error:
            raise RuntimeError(
                "Install rpi-rgb-led-matrix on the Raspberry Pi to drive HUB75 panels"
            ) from error

        options = RGBMatrixOptions()
        options.rows = 32
        options.cols = 64
        options.chain_length = 2
        options.parallel = 1
        options.hardware_mapping = hardware_mapping
        options.brightness = max(1, min(100, brightness))
        options.gpio_slowdown = gpio_slowdown
        options.drop_privileges = False
        # Capping refresh keeps the panel from whining audibly in a quiet room.
        options.limit_refresh_rate_hz = limit_refresh_rate_hz
        self.matrix = RGBMatrix(options=options)
        self._lookup = gamma_table(gamma) * 3

    def draw(self, image: Image.Image) -> None:
        if image.size != (128, 32):
            raise ValueError("HUB75 frame must be exactly 128x32")
        corrected = image.convert("RGB").point(self._lookup)
        self.matrix.SetImage(corrected)

    def clear(self) -> None:
        self.matrix.Clear()
