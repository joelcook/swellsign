"""The sign's restrained, non-evaluative color language."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    background: tuple[int, int, int] = (0, 0, 0)
    sea_glass: tuple[int, int, int] = (35, 142, 135)
    warm_white: tuple[int, int, int] = (161, 146, 116)
    wind_amber: tuple[int, int, int] = (165, 93, 34)
    warning_amber: tuple[int, int, int] = (184, 104, 30)
    unavailable_red: tuple[int, int, int] = (142, 35, 29)
    quiet_water: tuple[int, int, int] = (18, 45, 44)


DEFAULT_PALETTE = Palette()


def dim(color: tuple[int, int, int], brightness: float) -> tuple[int, int, int]:
    """Scale source colors; HUB75 brightness is implemented in pixels, not PWM."""
    level = min(1.0, max(0.0, brightness))
    return tuple(round(channel * level) for channel in color)

