"""Canonical SI-to-display unit conversions."""

METERS_TO_FEET = 3.280839895
MPS_TO_MPH = 2.236936292


def meters_to_feet(value: float) -> float:
    return value * METERS_TO_FEET


def mps_to_mph(value: float) -> float:
    return value * MPS_TO_MPH


def celsius_to_fahrenheit(value: float) -> float:
    return value * 9 / 5 + 32

