"""The sign's restrained, non-evaluative color language.

Color carries atmosphere and redundancy here, never a judgment about surf.
Nothing in this module encodes quality; amber means old data, not bad waves.
"""

from __future__ import annotations

from dataclasses import dataclass

# An HUB75 panel is close to linear in duty cycle; human perception is not.
#
# Where the correction belongs depends on the output. A PNG is viewed on an
# sRGB display that already applies roughly this curve, so the simulator stores
# perceptual values and stays untouched. The panel applies no curve at all, so
# gamma is applied once on the way to the hardware. Doing it in both places
# would darken the preview twice and break simulator/panel parity.
DEFAULT_GAMMA = 2.2


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


def dim(
    color: tuple[int, int, int],
    brightness: float,
    *,
    gamma: float = 1.0,
) -> tuple[int, int, int]:
    """Scale a color in perceptual space.

    HUB75 brightness is implemented in pixel values rather than PWM so the
    simulator and the panel agree. The default is deliberately linear: these
    values are perceptual, and :func:`gamma_table` converts them for hardware.
    """
    level = min(1.0, max(0.0, brightness))
    if gamma != 1.0:
        level = level**gamma
    return tuple(round(channel * level) for channel in color)


def gamma_table(gamma: float = DEFAULT_GAMMA) -> list[int]:
    """Build a 256-entry perceptual-to-duty-cycle lookup table for the panel."""
    if gamma <= 0:
        raise ValueError("gamma must be positive")
    return [round(255 * ((value / 255) ** gamma)) for value in range(256)]


@dataclass(frozen=True)
class BrightnessSchedule:
    """Local-hour brightness plan for a sign that lives in a shared room.

    Hours are local wall-clock and each boundary is inclusive of its start.
    Evening begins when the room lighting drops, night when the sign should be
    barely present rather than off.
    """

    day: float = 0.55
    evening: float = 0.35
    night: float = 0.12
    day_start_hour: int = 7
    evening_start_hour: int = 19
    night_start_hour: int = 22

    def __post_init__(self) -> None:
        for value in (self.day, self.evening, self.night):
            if not 0.0 <= value <= 1.0:
                raise ValueError("brightness levels must fall within 0.0 and 1.0")
        for hour in (self.day_start_hour, self.evening_start_hour, self.night_start_hour):
            if not 0 <= hour <= 23:
                raise ValueError("schedule hours must fall within 0 and 23")

    def level_for_hour(self, hour: int) -> float:
        hour %= 24
        if self.day_start_hour <= hour < self.evening_start_hour:
            return self.day
        if self.evening_start_hour <= hour < self.night_start_hour:
            return self.evening
        return self.night


class BrightnessController:
    """Eases between scheduled levels instead of stepping.

    A sudden brightness change in peripheral vision reads as a flash, which is
    precisely the quality this object is trying not to have.
    """

    def __init__(
        self,
        schedule: BrightnessSchedule | None = None,
        *,
        initial: float | None = None,
        fade_step: float = 0.02,
    ) -> None:
        if fade_step <= 0:
            raise ValueError("fade_step must be positive")
        self.schedule = schedule or BrightnessSchedule()
        self.fade_step = fade_step
        self.current = self.schedule.day if initial is None else initial

    def target_for_hour(self, hour: int) -> float:
        return self.schedule.level_for_hour(hour)

    def advance(self, hour: int) -> float:
        """Move one frame toward the scheduled level and return the new value."""
        target = self.target_for_hour(hour)
        difference = target - self.current
        if abs(difference) <= self.fade_step:
            self.current = target
        else:
            self.current += self.fade_step if difference > 0 else -self.fade_step
        return self.current
