"""Utilities for true-bearing directions."""

CARDINALS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


def normalize_degrees(degrees: float) -> float:
    return degrees % 360.0


def degrees_to_cardinal(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    return CARDINALS[int((normalize_degrees(degrees) + 11.25) // 22.5) % 16]


def cardinal_to_degrees(cardinal: str | None) -> float | None:
    if cardinal is None:
        return None
    cleaned = cardinal.strip().upper()
    try:
        return CARDINALS.index(cleaned) * 22.5
    except ValueError:
        return None


def circular_difference(start: float, end: float) -> float:
    return ((end - start + 180.0) % 360.0) - 180.0

