"""Compose an honest current observation from independently timed components."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from ..config import ProductConfig, SpotConfig, WaveSourceConfig
from ..directions import degrees_to_cardinal, normalize_degrees
from ..freshness import age_minutes, aggregate_data_state, classify_freshness
from ..models import (
    CompactDisplayPayload,
    CompactWave,
    CompactWind,
    ComponentSource,
    CurrentSnapshot,
    Freshness,
    FreshnessLimits,
    MeasurementBasis,
    SourceRole,
    SpotIdentity,
    TrendSnapshot,
    WaveObservation,
    WaveSnapshot,
    WindSnapshot,
)
from ..storage import SQLiteRepository
from ..units import meters_to_feet, mps_to_mph
from .trend import calculate_wave_height_trend


class UnknownSpotError(KeyError):
    """A requested spot is not present in the product configuration."""


class SnapshotComposer:
    """Select and compose stored observations without consulting forecasts."""

    def __init__(self, repository: SQLiteRepository, product_config: ProductConfig) -> None:
        self.repository = repository
        self.product_config = product_config

    def compose(self, spot_id: str, *, now: datetime | None = None) -> CurrentSnapshot:
        generated_at = (now or datetime.now(UTC)).astimezone(UTC)
        spot = self.product_config.spots.get(spot_id)
        if spot is None:
            raise UnknownSpotError(spot_id)

        wave, selected_wave = self._select_wave(spot, generated_at)
        wind = self._select_wind(spot, generated_at)

        warnings: list[str] = []
        if wave is None:
            warnings.append("no_usable_wave_observation")
        else:
            _append_freshness_warning(warnings, "wave", wave.freshness)
            if wave.source.fallback_used:
                warnings.append("wave_fallback_in_use")

        if wind is None:
            warnings.append("no_usable_wind_observation")
        else:
            _append_freshness_warning(warnings, "wind", wind.freshness)
            if wind.source.fallback_used:
                warnings.append("wind_fallback_in_use")

        trend = TrendSnapshot()
        if wave is not None and selected_wave is not None:
            start = selected_wave.observed_at - timedelta(
                hours=self.product_config.trend.window_hours
            )
            history = self.repository.wave_observations(
                selected_wave.station_id,
                start=start,
                end=selected_wave.observed_at,
                limit=2_000,
                ascending=True,
            )
            trend.wave_height = calculate_wave_height_trend(
                history,
                station_id=selected_wave.station_id,
                measurement_basis=wave.measurement_basis,
                config=self.product_config.trend,
                end_at=selected_wave.observed_at,
            )

        return CurrentSnapshot(
            spot=SpotIdentity(
                id=spot_id,
                name=spot.name,
                display_name=spot.display_name,
                timezone=spot.timezone,
            ),
            generated_at=generated_at,
            wave=wave,
            wind=wind,
            trend=trend,
            data_state=aggregate_data_state(
                wave.freshness if wave else None,
                wind.freshness if wind else None,
            ),
            fallback_used=bool(
                (wave and wave.source.fallback_used) or (wind and wind.source.fallback_used)
            ),
            warnings=warnings,
        )

    def _select_wave(
        self,
        spot: SpotConfig,
        now: datetime,
    ) -> tuple[WaveSnapshot | None, WaveObservation | None]:
        for source_config in spot.wave_sources:
            observations = self.repository.latest_wave_observations(
                source_config.station_id,
                limit=128,
            )
            by_timestamp: dict[datetime, list[WaveObservation]] = {}
            for observation in observations:
                by_timestamp.setdefault(observation.observed_at, []).append(observation)

            thresholds = self.product_config.freshness_for(source_config.station_id)
            for observed_at in sorted(by_timestamp, reverse=True):
                age = age_minutes(observed_at, now)
                hard_limit = min(
                    source_config.maximum_usable_age_minutes,
                    thresholds.stale_max_age_minutes,
                )
                if age > hard_limit or observed_at > now + timedelta(minutes=5):
                    continue
                selected = _select_wave_triplet(
                    by_timestamp[observed_at],
                    source_config,
                )
                if selected is None:
                    continue
                observation, triplet = selected
                basis, height_m, period_s, direction = triplet
                freshness = classify_freshness(age, thresholds)
                if freshness is Freshness.UNAVAILABLE:
                    continue
                station = self.product_config.stations.get(source_config.station_id)
                return (
                    WaveSnapshot(
                        height_m=height_m,
                        height_ft=meters_to_feet(height_m),
                        period_s=period_s,
                        direction_deg_true=direction,
                        direction_cardinal=degrees_to_cardinal(direction),
                        measurement_basis=basis,
                        display_label=basis.display_label,
                        observed_at=observation.observed_at,
                        age_minutes=age,
                        freshness=freshness,
                        source=ComponentSource(
                            station_id=source_config.station_id,
                            role=source_config.role,
                            fallback_used=source_config.role is SourceRole.FALLBACK,
                            distance_to_spot_m=_station_distance_m(station, spot),
                            qc_status=observation.qc_status,
                        ),
                    ),
                    observation,
                )
        return None, None

    def _select_wind(self, spot: SpotConfig, now: datetime) -> WindSnapshot | None:
        for source_config in spot.wind_sources:
            observations = self.repository.wind_observations(
                source_config.station_id,
                limit=64,
            )
            thresholds = self.product_config.freshness_for(source_config.station_id)
            for observation in observations:
                if not _accepted(observation.qc_status):
                    continue
                age = age_minutes(observation.observed_at, now)
                hard_limit = min(
                    source_config.maximum_usable_age_minutes,
                    thresholds.stale_max_age_minutes,
                )
                if age > hard_limit or observation.observed_at > now + timedelta(minutes=5):
                    continue
                if not _valid_nonnegative(observation.speed_mps):
                    continue
                direction = _valid_direction(observation.direction_deg_true)
                gust = observation.gust_mps if _valid_nonnegative(observation.gust_mps) else None
                freshness = classify_freshness(age, thresholds)
                if freshness is Freshness.UNAVAILABLE:
                    continue
                station = self.product_config.stations.get(source_config.station_id)
                return WindSnapshot(
                    speed_mps=observation.speed_mps,
                    speed_mph=mps_to_mph(observation.speed_mps),
                    gust_mps=gust,
                    gust_mph=mps_to_mph(gust) if gust is not None else None,
                    direction_deg_true=direction,
                    direction_cardinal=degrees_to_cardinal(direction),
                    observed_at=observation.observed_at,
                    age_minutes=age,
                    freshness=freshness,
                    source=ComponentSource(
                        station_id=source_config.station_id,
                        role=source_config.role,
                        fallback_used=source_config.role is SourceRole.FALLBACK,
                        distance_to_spot_m=_station_distance_m(station, spot),
                        qc_status=observation.qc_status,
                    ),
                )
        return None


def compact_display_payload(
    snapshot: CurrentSnapshot,
    product_config: ProductConfig | None = None,
) -> CompactDisplayPayload:
    """Project the exact current snapshot into the stable sign contract.

    Passing the configuration attaches each component's freshness thresholds so
    a display client can keep classifying correctly while the API is away.
    """

    def limits_for(component) -> FreshnessLimits | None:
        if product_config is None:
            return None
        effective = product_config.freshness_for(component.source.station_id)
        return FreshnessLimits(
            fresh_max_age_minutes=effective.fresh_max_age_minutes,
            delayed_max_age_minutes=effective.delayed_max_age_minutes,
            stale_max_age_minutes=effective.stale_max_age_minutes,
        )

    wave = None
    if snapshot.wave is not None:
        trend = (
            snapshot.trend.wave_height.state
            if snapshot.trend.wave_height is not None
            else "unknown"
        )
        # The sign's label may be overridden in configuration; the snapshot's
        # own display_label and measurement_basis stay basis-derived so `/now`
        # and storage keep saying exactly what the number is.
        label = snapshot.wave.display_label
        if product_config is not None and product_config.display.wave_label:
            label = product_config.display.wave_label

        wave = CompactWave(
            label=label,
            height_ft=snapshot.wave.height_ft,
            period_s=snapshot.wave.period_s,
            direction=snapshot.wave.direction_cardinal,
            observed_at=snapshot.wave.observed_at,
            age_minutes=snapshot.wave.age_minutes,
            freshness=snapshot.wave.freshness,
            trend=trend,
            limits=limits_for(snapshot.wave),
        )

    wind = None
    if snapshot.wind is not None:
        wind = CompactWind(
            direction=snapshot.wind.direction_cardinal,
            speed_mph=snapshot.wind.speed_mph,
            observed_at=snapshot.wind.observed_at,
            age_minutes=snapshot.wind.age_minutes,
            freshness=snapshot.wind.freshness,
            limits=limits_for(snapshot.wind),
        )

    return CompactDisplayPayload(
        spot=snapshot.spot.display_name,
        generated_at=snapshot.generated_at,
        wave=wave,
        wind=wind,
        data_state=snapshot.data_state,
        fallback_used=snapshot.fallback_used,
        warnings=snapshot.warnings,
    )


def _select_wave_triplet(
    observations: list[WaveObservation],
    source_config: WaveSourceConfig,
) -> tuple[
    WaveObservation,
    tuple[MeasurementBasis, float, float, float | None],
] | None:
    for basis in source_config.preferred_basis:
        for observation in observations:
            if not _accepted(observation.qc_status):
                continue
            if basis in (
                MeasurementBasis.SEPARATED_SWELL,
                MeasurementBasis.SPECTRAL_PARTITION,
            ):
                direction = _valid_direction(observation.swell_direction_deg_true)
                if (
                    _valid_nonnegative(observation.swell_height_m)
                    and _valid_period(observation.swell_period_s)
                    and direction is not None
                ):
                    return (
                        observation,
                        (
                            basis,
                            observation.swell_height_m,
                            observation.swell_period_s,
                            direction,
                        ),
                    )
            elif basis is MeasurementBasis.TOTAL_SEA and _valid_nonnegative(
                observation.significant_height_m
            ) and _valid_period(
                observation.dominant_period_s
            ):
                # Total-sea height and period stay coherent even if the provider
                # has no mean direction. Unknown direction remains None/"--".
                return (
                    observation,
                    (
                        basis,
                        observation.significant_height_m,
                        observation.dominant_period_s,
                        _valid_direction(observation.mean_direction_deg_true),
                    ),
                )
    return None


def _valid_nonnegative(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value >= 0


def _valid_period(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and 0 < value <= 60


def _valid_direction(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or not 0 <= value <= 360:
        return None
    return normalize_degrees(value)


def _accepted(qc_status: str) -> bool:
    return qc_status.casefold() in {"accepted", "ok", "good", "pass"}


def _station_distance_m(station: object | None, spot: SpotConfig) -> float | None:
    if station is None:
        return None
    latitude = getattr(station, "latitude", None)
    longitude = getattr(station, "longitude", None)
    if latitude is None or longitude is None:
        return None

    radius_m = 6_371_008.8
    lat1 = math.radians(spot.latitude)
    lat2 = math.radians(latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(longitude - spot.longitude)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * radius_m * math.asin(math.sqrt(haversine))


def _append_freshness_warning(
    warnings: list[str],
    component: str,
    freshness: Freshness,
) -> None:
    if freshness is Freshness.DELAYED:
        warnings.append(f"{component}_observation_delayed")
    elif freshness is Freshness.STALE:
        warnings.append(f"{component}_observation_stale")


# Older planning notes used this name. Keep it as a zero-cost integration alias.
CurrentSnapshotService = SnapshotComposer
