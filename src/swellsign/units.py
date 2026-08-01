"""Canonical SI-to-display unit conversions."""

METERS_TO_FEET = 3.280839895
MPS_TO_MPH = 2.236936292
MPS_TO_KNOTS = 1.943844492
MPH_TO_KNOTS = 0.868976242


def meters_to_feet(value: float) -> float:
    return value * METERS_TO_FEET


def mps_to_mph(value: float) -> float:
    return value * MPS_TO_MPH


def mps_to_knots(value: float) -> float:
    return value * MPS_TO_KNOTS


def celsius_to_fahrenheit(value: float) -> float:
    return value * 9 / 5 + 32

