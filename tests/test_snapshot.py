from __future__ import annotations

from datetime import UTC, datetime, timedelta

from swellsign.config import ProductConfig
from swellsign.models import (
    DataState,
    MeasurementBasis,
    RawFetch,
    SourceRole,
    Station,
    WaveObservation,
    WindObservation,
)
from swellsign.services.snapshot import SnapshotComposer, compact_display_payload
from swellsign.storage import SQLiteRepository

NOW = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)


def product_config() -> ProductConfig:
    return ProductConfig.model_validate(
        {
            "stations": {
                "ndbc:41070": Station(
                    id="ndbc:41070",
                    provider="ndbc",
                    provider_station_id="41070",
                    canonical_physical_station_id="ndbc:41070",
                    name="Local wave buoy",
                    latitude=29.025,
                    longitude=-80.884,
                    capabilities=["waves"],
                ),
                "ndbc:41113": Station(
                    id="ndbc:41113",
                    provider="ndbc",
                    provider_station_id="41113",
                    canonical_physical_station_id="cdip:143",
                    name="Cape Canaveral fallback",
                    latitude=28.4,
                    longitude=-80.533,
                    capabilities=["waves", "separated_swell"],
                ),
                "ndbc:41069": Station(
                    id="ndbc:41069",
                    provider="ndbc",
                    provider_station_id="41069",
                    canonical_physical_station_id="ndbc:41069",
                    name="Local wind",
                    latitude=29.025,
                    longitude=-80.884,
                    capabilities=["wind"],
                ),
            },
            "spots": {
                "new-smyrna": {
                    "name": "New Smyrna Beach",
                    "display_name": "NEW SMYRNA",
                    "timezone": "America/New_York",
                    "latitude": 29.0258,
                    "longitude": -80.927,
                    "wave_sources": [
                        {
                            "station_id": "ndbc:41070",
                            "role": SourceRole.PRIMARY,
                            "maximum_usable_age_minutes": 240,
                            "preferred_basis": [
                                MeasurementBasis.SEPARATED_SWELL,
                                MeasurementBasis.TOTAL_SEA,
                            ],
                        },
                        {
                            "station_id": "ndbc:41113",
                            "role": SourceRole.FALLBACK,
                            "maximum_usable_age_minutes": 240,
                            "preferred_basis": [
                                MeasurementBasis.SEPARATED_SWELL,
                                MeasurementBasis.TOTAL_SEA,
                            ],
                        },
                    ],
                    "wind_sources": [
                        {
                            "station_id": "ndbc:41069",
                            "role": SourceRole.PRIMARY,
                            "maximum_usable_age_minutes": 240,
                        }
                    ],
                }
            },
        }
    )


def repository(tmp_path) -> SQLiteRepository:
    result = SQLiteRepository(tmp_path / "swell.db")
    result.initialize()
    result.upsert_stations(product_config().stations.values())
    return result


def save_wave(
    storage: SQLiteRepository,
    *,
    station_id: str,
    observed_at: datetime,
    product: str,
    significant_height_m: float | None = None,
    dominant_period_s: float | None = None,
    mean_direction_deg_true: float | None = None,
    swell_height_m: float | None = None,
    swell_period_s: float | None = None,
    swell_direction_deg_true: float | None = None,
) -> None:
    identity = f"{station_id}:{product}:{observed_at.isoformat()}"
    raw_id = f"raw:{identity}"
    storage.save_raw_fetch(
        RawFetch(
            id=raw_id,
            provider="fixture",
            resource_type=product,
            source_url="https://example.test/wave",
            requested_at=NOW,
            received_at=NOW,
            http_status=200,
        )
    )
    storage.upsert_wave_observation(
        WaveObservation(
            id=f"wave:{identity}",
            station_id=station_id,
            observed_at=observed_at,
            fetched_at=NOW,
            processing_product=product,
            significant_height_m=significant_height_m,
            dominant_period_s=dominant_period_s,
            mean_direction_deg_true=mean_direction_deg_true,
            swell_height_m=swell_height_m,
            swell_period_s=swell_period_s,
            swell_direction_deg_true=swell_direction_deg_true,
            source_url="https://example.test/wave",
            raw_fetch_id=raw_id,
        )
    )


def save_wind(storage: SQLiteRepository, *, observed_at: datetime) -> None:
    storage.save_raw_fetch(
        RawFetch(
            id="raw:wind",
            provider="fixture",
            resource_type="wind",
            source_url="https://example.test/wind",
            requested_at=NOW,
            received_at=NOW,
            http_status=200,
        )
    )
    storage.upsert_wind_observation(
        WindObservation(
            id="wind:one",
            station_id="ndbc:41069",
            observed_at=observed_at,
            fetched_at=NOW,
            processing_product="standard",
            speed_mps=4.0,
            gust_mps=5.0,
            direction_deg_true=270,
            source_url="https://example.test/wind",
            raw_fetch_id="raw:wind",
        )
    )


def test_local_primary_total_seas_beats_remote_separated_swell(tmp_path):
    storage = repository(tmp_path)
    save_wave(
        storage,
        station_id="ndbc:41070",
        observed_at=NOW - timedelta(minutes=25),
        product="standard",
        significant_height_m=0.8,
        dominant_period_s=8.1,
        mean_direction_deg_true=45,
    )
    save_wave(
        storage,
        station_id="ndbc:41113",
        observed_at=NOW - timedelta(minutes=5),
        product="spectral",
        swell_height_m=1.2,
        swell_period_s=10,
        swell_direction_deg_true=90,
    )
    save_wind(storage, observed_at=NOW - timedelta(minutes=10))

    snapshot = SnapshotComposer(storage, product_config()).compose("new-smyrna", now=NOW)

    assert snapshot.wave.display_label == "SEAS"
    assert snapshot.wave.measurement_basis is MeasurementBasis.TOTAL_SEA
    assert snapshot.wave.source.station_id == "ndbc:41070"
    assert snapshot.wave.source.fallback_used is False
    assert snapshot.wave.age_minutes == 25
    assert snapshot.wind.age_minutes == 10
    assert snapshot.data_state is DataState.FRESH
    assert snapshot.wave.source.distance_to_spot_m is not None


def test_incomplete_swell_tuple_falls_back_to_coherent_total_seas(tmp_path):
    storage = repository(tmp_path)
    save_wave(
        storage,
        station_id="ndbc:41070",
        observed_at=NOW - timedelta(minutes=15),
        product="merged",
        significant_height_m=0.7,
        dominant_period_s=7.5,
        mean_direction_deg_true=32,
        swell_height_m=0.6,
        swell_period_s=9,
        swell_direction_deg_true=None,
    )

    snapshot = SnapshotComposer(storage, product_config()).compose("new-smyrna", now=NOW)

    assert snapshot.wave.measurement_basis is MeasurementBasis.TOTAL_SEA
    assert snapshot.wave.height_m == 0.7
    assert snapshot.wave.period_s == 7.5
    assert snapshot.data_state is DataState.PARTIAL


def test_same_timestamp_spectral_product_is_preferred_without_cross_stitching(tmp_path):
    storage = repository(tmp_path)
    observed_at = NOW - timedelta(minutes=15)
    save_wave(
        storage,
        station_id="ndbc:41070",
        observed_at=observed_at,
        product="standard",
        significant_height_m=0.7,
        dominant_period_s=7.5,
        mean_direction_deg_true=32,
    )
    save_wave(
        storage,
        station_id="ndbc:41070",
        observed_at=observed_at,
        product="spectral",
        swell_height_m=0.6,
        swell_period_s=9,
        swell_direction_deg_true=70,
    )

    snapshot = SnapshotComposer(storage, product_config()).compose("new-smyrna", now=NOW)

    assert snapshot.wave.measurement_basis is MeasurementBasis.SEPARATED_SWELL
    assert snapshot.wave.height_m == 0.6
    assert snapshot.wave.period_s == 9


def test_expired_primary_uses_fallback_and_compact_payload_is_numeric(tmp_path):
    storage = repository(tmp_path)
    save_wave(
        storage,
        station_id="ndbc:41070",
        observed_at=NOW - timedelta(minutes=241),
        product="standard",
        significant_height_m=0.5,
        dominant_period_s=6,
        mean_direction_deg_true=20,
    )
    save_wave(
        storage,
        station_id="ndbc:41113",
        observed_at=NOW - timedelta(minutes=20),
        product="spectral",
        swell_height_m=1.0,
        swell_period_s=11,
        swell_direction_deg_true=100,
    )

    snapshot = SnapshotComposer(storage, product_config()).compose("new-smyrna", now=NOW)
    compact = compact_display_payload(snapshot)

    assert snapshot.wave.display_label == "SWELL"
    assert snapshot.wave.source.fallback_used
    assert snapshot.fallback_used
    assert "wave_fallback_in_use" in snapshot.warnings
    assert isinstance(compact.wave.height_ft, float)
    assert compact.mode == "observed"


def test_forecast_storage_is_never_consulted_for_current_snapshot(tmp_path):
    storage = repository(tmp_path)

    snapshot = SnapshotComposer(storage, product_config()).compose("new-smyrna", now=NOW)

    assert snapshot.wave is None
    assert snapshot.wind is None
    assert snapshot.data_state is DataState.UNAVAILABLE
