"""Optional Raspberry Pi output for rpi-rgb-led-matrix."""

from __future__ import annotations

from PIL import Image


class PiMatrixOutput:
    """A lazy adapter so development machines do not need RGB matrix bindings."""

    def __init__(
        self,
        *,
        hardware_mapping: str = "adafruit-hat-pwm",
        brightness: int = 35,
        gpio_slowdown: int = 4,
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
        self.matrix = RGBMatrix(options=options)

    def draw(self, image: Image.Image) -> None:
        if image.size != (128, 32):
            raise ValueError("HUB75 frame must be exactly 128x32")
        self.matrix.SetImage(image.convert("RGB"))

    def clear(self) -> None:
        self.matrix.Clear()

