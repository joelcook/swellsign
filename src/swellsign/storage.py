"""SQLite persistence for observations and the separate forecast archive.

The repository accepts and returns domain models; providers and API routes do
not need to know table names or SQLite details.  All timestamps are stored as
canonical UTC text so chronological ordering remains meaningful and databases
stay pleasant to inspect by hand.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import (
    ForecastPoint,
    ForecastResponse,
    ForecastRun,
    RawFetch,
    Station,
    TidePrediction,
    WaveObservation,
    WindObservation,
)

SCHEMA_VERSION = 2


class StorageError(RuntimeError):
    """Base class for repository failures with useful application semantics."""


class ImmutableRecordConflict(StorageError):
    """Raised when an existing immutable forecast record has different data."""


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("database timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _unjson(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_fetches (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    requested_at_utc TEXT NOT NULL,
    received_at_utc TEXT,
    http_status INTEGER,
    content_type TEXT,
    body_blob BLOB NOT NULL,
    body_sha256 TEXT NOT NULL,
    error_json TEXT
);

CREATE TABLE IF NOT EXISTS stations (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_station_id TEXT NOT NULL,
    canonical_physical_station_id TEXT NOT NULL,
    name TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    water_depth_m REAL,
    platform_type TEXT,
    active_status TEXT NOT NULL,
    expected_interval_minutes INTEGER,
    capabilities_json TEXT NOT NULL,
    attribution TEXT,
    metadata_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(provider, provider_station_id)
);

CREATE TABLE IF NOT EXISTS station_aliases (
    provider TEXT NOT NULL,
    provider_station_id TEXT NOT NULL,
    canonical_physical_station_id TEXT NOT NULL,
    valid_from_utc TEXT,
    valid_to_utc TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(provider, provider_station_id, canonical_physical_station_id)
);

CREATE TABLE IF NOT EXISTS wave_observations (
    id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES stations(id),
    observed_at_utc TEXT NOT NULL,
    received_at_utc TEXT,
    fetched_at_utc TEXT NOT NULL,
    processing_product TEXT NOT NULL,
    significant_height_m REAL,
    dominant_period_s REAL,
    average_period_s REAL,
    mean_direction_deg_true REAL,
    swell_height_m REAL,
    swell_period_s REAL,
    swell_direction_deg_true REAL,
    wind_wave_height_m REAL,
    wind_wave_period_s REAL,
    wind_wave_direction_deg_true REAL,
    water_temperature_c REAL,
    qc_status TEXT NOT NULL,
    qc_detail_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    raw_fetch_id TEXT NOT NULL REFERENCES raw_fetches(id),
    UNIQUE(station_id, observed_at_utc, processing_product)
);

CREATE TABLE IF NOT EXISTS wind_observations (
    id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES stations(id),
    observed_at_utc TEXT NOT NULL,
    received_at_utc TEXT,
    fetched_at_utc TEXT NOT NULL,
    processing_product TEXT NOT NULL,
    speed_mps REAL,
    gust_mps REAL,
    direction_deg_true REAL,
    qc_status TEXT NOT NULL,
    qc_detail_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    raw_fetch_id TEXT NOT NULL REFERENCES raw_fetches(id),
    UNIQUE(station_id, observed_at_utc, processing_product)
);

CREATE TABLE IF NOT EXISTS forecast_runs (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    location_id TEXT NOT NULL,
    issued_at_utc TEXT,
    fetched_at_utc TEXT NOT NULL,
    horizon_hours INTEGER NOT NULL,
    raw_fetch_id TEXT NOT NULL REFERENCES raw_fetches(id),
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_points (
    run_id TEXT NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
    valid_at_utc TEXT NOT NULL,
    wave_height_m REAL,
    wave_period_s REAL,
    wave_direction_deg_true REAL,
    swell_height_m REAL,
    swell_period_s REAL,
    swell_direction_deg_true REAL,
    wind_wave_height_m REAL,
    wind_wave_period_s REAL,
    wind_wave_direction_deg_true REAL,
    wind_speed_mps REAL,
    wind_direction_deg_true REAL,
    qc_status TEXT NOT NULL,
    PRIMARY KEY(run_id, valid_at_utc)
);

CREATE TABLE IF NOT EXISTS tide_predictions (
    id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    predicted_at_utc TEXT NOT NULL,
    height_m REAL NOT NULL,
    kind TEXT NOT NULL,
    datum TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    source_url TEXT NOT NULL,
    raw_fetch_id TEXT NOT NULL REFERENCES raw_fetches(id),
    UNIQUE(station_id, predicted_at_utc, kind, datum)
);

CREATE TABLE IF NOT EXISTS http_validators (
    validator_key TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tide_station_time
    ON tide_predictions(station_id, predicted_at_utc);
CREATE INDEX IF NOT EXISTS wave_station_time
    ON wave_observations(station_id, observed_at_utc DESC);
CREATE INDEX IF NOT EXISTS wave_station_product_time
    ON wave_observations(station_id, processing_product, observed_at_utc DESC);
CREATE INDEX IF NOT EXISTS wind_station_time
    ON wind_observations(station_id, observed_at_utc DESC);
CREATE INDEX IF NOT EXISTS forecast_location_run
    ON forecast_runs(location_id, fetched_at_utc DESC);
CREATE INDEX IF NOT EXISTS forecast_run_time
    ON forecast_points(run_id, valid_at_utc);
"""


class SQLiteRepository:
    """Small synchronous repository optimized for one appliance.

    Each method owns a short-lived connection.  That keeps it safe for FastAPI
    worker threads while WAL mode permits the collector to write concurrently.
    """

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at_utc) VALUES (?, ?)",
                (SCHEMA_VERSION, _utc_text(datetime.now(UTC))),
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        finally:
            connection.close()

    def is_ready(self) -> bool:
        if not self.path.exists():
            return False
        try:
            with self._connect() as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                connection.execute("SELECT 1 FROM stations LIMIT 1").fetchone()
            return version == SCHEMA_VERSION
        except sqlite3.Error:
            return False

    def upsert_station(self, station: Station) -> None:
        now = _utc_text(datetime.now(UTC))
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO stations (
                    id, provider, provider_station_id, canonical_physical_station_id,
                    name, latitude, longitude, water_depth_m, platform_type,
                    active_status, expected_interval_minutes, capabilities_json,
                    attribution, metadata_json, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider = excluded.provider,
                    provider_station_id = excluded.provider_station_id,
                    canonical_physical_station_id = excluded.canonical_physical_station_id,
                    name = excluded.name,
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    water_depth_m = excluded.water_depth_m,
                    platform_type = excluded.platform_type,
                    active_status = excluded.active_status,
                    expected_interval_minutes = excluded.expected_interval_minutes,
                    capabilities_json = excluded.capabilities_json,
                    attribution = excluded.attribution,
                    metadata_json = excluded.metadata_json,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    station.id,
                    station.provider,
                    station.provider_station_id,
                    station.canonical_physical_station_id,
                    station.name,
                    station.latitude,
                    station.longitude,
                    station.water_depth_m,
                    station.platform_type,
                    station.active_status,
                    station.expected_interval_minutes,
                    _json(station.capabilities),
                    station.attribution,
                    _json(station.metadata),
                    now,
                    now,
                ),
            )

    def upsert_stations(self, stations: Iterable[Station]) -> None:
        for station in stations:
            self.upsert_station(station)

    def get_station(self, station_id: str) -> Station | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
        return _station_from_row(row) if row else None

    def list_stations(self) -> list[Station]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM stations ORDER BY id").fetchall()
        return [_station_from_row(row) for row in rows]

    def save_raw_fetch(self, raw: RawFetch) -> None:
        body_hash = hashlib.sha256(raw.body).hexdigest()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO raw_fetches (
                    id, provider, resource_type, source_url, requested_at_utc,
                    received_at_utc, http_status, content_type, body_blob,
                    body_sha256, error_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    received_at_utc = excluded.received_at_utc,
                    http_status = excluded.http_status,
                    content_type = excluded.content_type,
                    body_blob = excluded.body_blob,
                    body_sha256 = excluded.body_sha256,
                    error_json = excluded.error_json
                """,
                (
                    raw.id,
                    raw.provider,
                    raw.resource_type,
                    raw.source_url,
                    _utc_text(raw.requested_at),
                    _utc_text(raw.received_at),
                    raw.http_status,
                    raw.content_type,
                    raw.body,
                    body_hash,
                    _json(raw.error) if raw.error is not None else None,
                ),
            )

    def get_raw_fetch(self, fetch_id: str) -> RawFetch | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM raw_fetches WHERE id = ?", (fetch_id,)).fetchone()
        if row is None:
            return None
        return RawFetch(
            id=row["id"],
            provider=row["provider"],
            resource_type=row["resource_type"],
            source_url=row["source_url"],
            requested_at=_parse_utc(row["requested_at_utc"]),
            received_at=_parse_utc(row["received_at_utc"]),
            http_status=row["http_status"],
            content_type=row["content_type"],
            body=bytes(row["body_blob"]),
            error=_unjson(row["error_json"], None),
        )

    def upsert_wave_observation(self, observation: WaveObservation) -> None:
        self.upsert_wave_observations([observation])

    def upsert_wave_observations(self, observations: Iterable[WaveObservation]) -> int:
        rows = list(observations)
        if not rows:
            return 0
        with self._transaction() as connection:
            connection.executemany(
                """
                INSERT INTO wave_observations (
                    id, station_id, observed_at_utc, received_at_utc, fetched_at_utc,
                    processing_product, significant_height_m, dominant_period_s,
                    average_period_s, mean_direction_deg_true, swell_height_m,
                    swell_period_s, swell_direction_deg_true, wind_wave_height_m,
                    wind_wave_period_s, wind_wave_direction_deg_true,
                    water_temperature_c, qc_status, qc_detail_json, source_url,
                    raw_fetch_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(station_id, observed_at_utc, processing_product) DO UPDATE SET
                    received_at_utc = excluded.received_at_utc,
                    fetched_at_utc = excluded.fetched_at_utc,
                    significant_height_m = excluded.significant_height_m,
                    dominant_period_s = excluded.dominant_period_s,
                    average_period_s = excluded.average_period_s,
                    mean_direction_deg_true = excluded.mean_direction_deg_true,
                    swell_height_m = excluded.swell_height_m,
                    swell_period_s = excluded.swell_period_s,
                    swell_direction_deg_true = excluded.swell_direction_deg_true,
                    wind_wave_height_m = excluded.wind_wave_height_m,
                    wind_wave_period_s = excluded.wind_wave_period_s,
                    wind_wave_direction_deg_true = excluded.wind_wave_direction_deg_true,
                    water_temperature_c = excluded.water_temperature_c,
                    qc_status = excluded.qc_status,
                    qc_detail_json = excluded.qc_detail_json,
                    source_url = excluded.source_url,
                    raw_fetch_id = excluded.raw_fetch_id
                """,
                [_wave_values(item) for item in rows],
            )
        return len(rows)

    def upsert_wind_observation(self, observation: WindObservation) -> None:
        self.upsert_wind_observations([observation])

    def upsert_wind_observations(self, observations: Iterable[WindObservation]) -> int:
        rows = list(observations)
        if not rows:
            return 0
        with self._transaction() as connection:
            connection.executemany(
                """
                INSERT INTO wind_observations (
                    id, station_id, observed_at_utc, received_at_utc, fetched_at_utc,
                    processing_product, speed_mps, gust_mps, direction_deg_true,
                    qc_status, qc_detail_json, source_url, raw_fetch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(station_id, observed_at_utc, processing_product) DO UPDATE SET
                    received_at_utc = excluded.received_at_utc,
                    fetched_at_utc = excluded.fetched_at_utc,
                    speed_mps = excluded.speed_mps,
                    gust_mps = excluded.gust_mps,
                    direction_deg_true = excluded.direction_deg_true,
                    qc_status = excluded.qc_status,
                    qc_detail_json = excluded.qc_detail_json,
                    source_url = excluded.source_url,
                    raw_fetch_id = excluded.raw_fetch_id
                """,
                [_wind_values(item) for item in rows],
            )
        return len(rows)

    def wave_observations(
        self,
        station_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1_000,
        ascending: bool = False,
    ) -> list[WaveObservation]:
        clauses = ["station_id = ?"]
        params: list[Any] = [station_id]
        if start is not None:
            clauses.append("observed_at_utc >= ?")
            params.append(_utc_text(start))
        if end is not None:
            clauses.append("observed_at_utc <= ?")
            params.append(_utc_text(end))
        params.append(limit)
        direction = "ASC" if ascending else "DESC"
        query = (
            "SELECT * FROM wave_observations WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY observed_at_utc {direction}, fetched_at_utc {direction} LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_wave_from_row(row) for row in rows]

    def wind_observations(
        self,
        station_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1_000,
        ascending: bool = False,
    ) -> list[WindObservation]:
        clauses = ["station_id = ?"]
        params: list[Any] = [station_id]
        if start is not None:
            clauses.append("observed_at_utc >= ?")
            params.append(_utc_text(start))
        if end is not None:
            clauses.append("observed_at_utc <= ?")
            params.append(_utc_text(end))
        params.append(limit)
        direction = "ASC" if ascending else "DESC"
        query = (
            "SELECT * FROM wind_observations WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY observed_at_utc {direction}, fetched_at_utc {direction} LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_wind_from_row(row) for row in rows]

    def latest_wave_observations(self, station_id: str, *, limit: int = 64) -> list[WaveObservation]:
        return self.wave_observations(station_id, limit=limit)

    def latest_wind_observation(self, station_id: str) -> WindObservation | None:
        rows = self.wind_observations(station_id, limit=1)
        return rows[0] if rows else None

    def save_forecast(self, run: ForecastRun, points: Iterable[ForecastPoint]) -> None:
        point_list = list(points)
        if any(point.run_id != run.id for point in point_list):
            raise ValueError("every forecast point must reference the supplied run")

        with self._transaction() as connection:
            existing_row = connection.execute(
                "SELECT * FROM forecast_runs WHERE id = ?", (run.id,)
            ).fetchone()
            if existing_row is not None:
                if _forecast_run_from_row(existing_row) != run:
                    raise ImmutableRecordConflict(f"forecast run {run.id!r} already has different data")
            else:
                connection.execute(
                    """
                    INSERT INTO forecast_runs (
                        id, provider, model, location_id, issued_at_utc, fetched_at_utc,
                        horizon_hours, raw_fetch_id, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _forecast_run_values(run),
                )

            for point in point_list:
                existing_point = connection.execute(
                    "SELECT * FROM forecast_points WHERE run_id = ? AND valid_at_utc = ?",
                    (point.run_id, _utc_text(point.valid_at)),
                ).fetchone()
                if existing_point is not None:
                    if _forecast_point_from_row(existing_point) != point:
                        raise ImmutableRecordConflict(
                            f"forecast point {point.run_id!r}/{_utc_text(point.valid_at)} "
                            "already has different data"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO forecast_points (
                        run_id, valid_at_utc, wave_height_m, wave_period_s,
                        wave_direction_deg_true, swell_height_m, swell_period_s,
                        swell_direction_deg_true, wind_wave_height_m,
                        wind_wave_period_s, wind_wave_direction_deg_true,
                        wind_speed_mps, wind_direction_deg_true, qc_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _forecast_point_values(point),
                )

    def list_forecast_runs(
        self,
        location_id: str,
        *,
        limit: int = 20,
        as_of: datetime | None = None,
    ) -> list[ForecastRun]:
        params: list[Any] = [location_id]
        as_of_clause = ""
        if as_of is not None:
            as_of_clause = " AND fetched_at_utc <= ?"
            params.append(_utc_text(as_of))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM forecast_runs
                WHERE location_id = ?
                """
                + as_of_clause
                + " ORDER BY fetched_at_utc DESC LIMIT ?",
                params,
            ).fetchall()
        return [_forecast_run_from_row(row) for row in rows]

    def forecast_response(
        self,
        run_id: str,
        *,
        start: datetime | None = None,
        hours: int | None = None,
    ) -> ForecastResponse | None:
        with self._connect() as connection:
            run_row = connection.execute(
                "SELECT * FROM forecast_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                return None

            clauses = ["run_id = ?"]
            params: list[Any] = [run_id]
            if start is not None:
                clauses.append("valid_at_utc >= ?")
                params.append(_utc_text(start))
                if hours is not None:
                    clauses.append("valid_at_utc <= ?")
                    params.append(_utc_text(start + timedelta(hours=hours)))
            rows = connection.execute(
                "SELECT * FROM forecast_points WHERE "
                + " AND ".join(clauses)
                + " ORDER BY valid_at_utc",
                params,
            ).fetchall()
        return ForecastResponse(
            run=_forecast_run_from_row(run_row),
            points=[_forecast_point_from_row(row) for row in rows],
        )

    def latest_forecast(
        self,
        location_id: str,
        *,
        start: datetime | None = None,
        hours: int | None = None,
        as_of: datetime | None = None,
    ) -> ForecastResponse | None:
        runs = self.list_forecast_runs(location_id, limit=1, as_of=as_of)
        if not runs:
            return None
        return self.forecast_response(runs[0].id, start=start, hours=hours)

    def upsert_tide_predictions(self, predictions: Iterable[TidePrediction]) -> int:
        """Store astronomical extremes idempotently.

        Predictions for the same station, time, kind, and datum are stable, so
        a repeated fetch simply refreshes provenance instead of duplicating.
        """
        rows = list(predictions)
        if not rows:
            return 0
        with self._transaction() as connection:
            for prediction in rows:
                connection.execute(
                    """
                    INSERT INTO tide_predictions (
                        id, station_id, predicted_at_utc, height_m, kind, datum,
                        fetched_at_utc, source_url, raw_fetch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(station_id, predicted_at_utc, kind, datum) DO UPDATE SET
                        height_m = excluded.height_m,
                        fetched_at_utc = excluded.fetched_at_utc,
                        source_url = excluded.source_url,
                        raw_fetch_id = excluded.raw_fetch_id
                    """,
                    (
                        prediction.id,
                        prediction.station_id,
                        _utc_text(prediction.predicted_at),
                        prediction.height_m,
                        prediction.kind,
                        prediction.datum,
                        _utc_text(prediction.fetched_at),
                        prediction.source_url,
                        prediction.raw_fetch_id,
                    ),
                )
        return len(rows)

    def tide_predictions(
        self,
        station_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[TidePrediction]:
        clauses = ["station_id = ?"]
        parameters: list[Any] = [station_id]
        if start is not None:
            clauses.append("predicted_at_utc >= ?")
            parameters.append(_utc_text(start))
        if end is not None:
            clauses.append("predicted_at_utc <= ?")
            parameters.append(_utc_text(end))
        sql = (
            "SELECT * FROM tide_predictions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY predicted_at_utc"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [_tide_from_row(row) for row in rows]

    def surrounding_tide_extremes(
        self,
        station_id: str,
        moment: datetime,
    ) -> tuple[TidePrediction | None, TidePrediction | None]:
        """Return the last extreme at or before ``moment`` and the next after it."""
        moment_text = _utc_text(moment)
        with self._connect() as connection:
            previous_row = connection.execute(
                """
                SELECT * FROM tide_predictions
                WHERE station_id = ? AND predicted_at_utc <= ?
                ORDER BY predicted_at_utc DESC LIMIT 1
                """,
                (station_id, moment_text),
            ).fetchone()
            next_row = connection.execute(
                """
                SELECT * FROM tide_predictions
                WHERE station_id = ? AND predicted_at_utc > ?
                ORDER BY predicted_at_utc LIMIT 1
                """,
                (station_id, moment_text),
            ).fetchone()
        return (
            _tide_from_row(previous_row) if previous_row is not None else None,
            _tide_from_row(next_row) if next_row is not None else None,
        )

    def get_validator(self, key: str) -> tuple[str | None, str | None]:
        """Read the stored ETag/Last-Modified pair for a conditional request."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT etag, last_modified FROM http_validators WHERE validator_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return (None, None)
        return (row["etag"], row["last_modified"])

    def set_validator(self, key: str, etag: str | None, last_modified: str | None) -> None:
        if etag is None and last_modified is None:
            return
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO http_validators (validator_key, etag, last_modified, updated_at_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(validator_key) DO UPDATE SET
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (key, etag, last_modified, _utc_text(datetime.now(UTC))),
            )

    def counts(self) -> dict[str, int]:
        tables = (
            "raw_fetches",
            "stations",
            "wave_observations",
            "wind_observations",
            "tide_predictions",
            "forecast_runs",
            "forecast_points",
        )
        with self._connect() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }


# A concise default name for callers that are not SQLite-specific.
Repository = SQLiteRepository


def _tide_from_row(row: sqlite3.Row) -> TidePrediction:
    return TidePrediction(
        id=row["id"],
        station_id=row["station_id"],
        predicted_at=_parse_utc(row["predicted_at_utc"]),
        height_m=row["height_m"],
        kind=row["kind"],
        datum=row["datum"],
        fetched_at=_parse_utc(row["fetched_at_utc"]),
        source_url=row["source_url"],
        raw_fetch_id=row["raw_fetch_id"],
    )


def _station_from_row(row: sqlite3.Row) -> Station:
    return Station(
        id=row["id"],
        provider=row["provider"],
        provider_station_id=row["provider_station_id"],
        canonical_physical_station_id=row["canonical_physical_station_id"],
        name=row["name"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        water_depth_m=row["water_depth_m"],
        platform_type=row["platform_type"],
        active_status=row["active_status"],
        expected_interval_minutes=row["expected_interval_minutes"],
        capabilities=_unjson(row["capabilities_json"], []),
        attribution=row["attribution"],
        metadata=_unjson(row["metadata_json"], {}),
    )


def _wave_values(item: WaveObservation) -> tuple[Any, ...]:
    return (
        item.id,
        item.station_id,
        _utc_text(item.observed_at),
        _utc_text(item.received_at),
        _utc_text(item.fetched_at),
        item.processing_product,
        item.significant_height_m,
        item.dominant_period_s,
        item.average_period_s,
        item.mean_direction_deg_true,
        item.swell_height_m,
        item.swell_period_s,
        item.swell_direction_deg_true,
        item.wind_wave_height_m,
        item.wind_wave_period_s,
        item.wind_wave_direction_deg_true,
        item.water_temperature_c,
        item.qc_status,
        _json(item.qc_detail),
        item.source_url,
        item.raw_fetch_id,
    )


def _wave_from_row(row: sqlite3.Row) -> WaveObservation:
    return WaveObservation(
        id=row["id"],
        station_id=row["station_id"],
        observed_at=_parse_utc(row["observed_at_utc"]),
        received_at=_parse_utc(row["received_at_utc"]),
        fetched_at=_parse_utc(row["fetched_at_utc"]),
        processing_product=row["processing_product"],
        significant_height_m=row["significant_height_m"],
        dominant_period_s=row["dominant_period_s"],
        average_period_s=row["average_period_s"],
        mean_direction_deg_true=row["mean_direction_deg_true"],
        swell_height_m=row["swell_height_m"],
        swell_period_s=row["swell_period_s"],
        swell_direction_deg_true=row["swell_direction_deg_true"],
        wind_wave_height_m=row["wind_wave_height_m"],
        wind_wave_period_s=row["wind_wave_period_s"],
        wind_wave_direction_deg_true=row["wind_wave_direction_deg_true"],
        water_temperature_c=row["water_temperature_c"],
        qc_status=row["qc_status"],
        qc_detail=_unjson(row["qc_detail_json"], {}),
        source_url=row["source_url"],
        raw_fetch_id=row["raw_fetch_id"],
    )


def _wind_values(item: WindObservation) -> tuple[Any, ...]:
    return (
        item.id,
        item.station_id,
        _utc_text(item.observed_at),
        _utc_text(item.received_at),
        _utc_text(item.fetched_at),
        item.processing_product,
        item.speed_mps,
        item.gust_mps,
        item.direction_deg_true,
        item.qc_status,
        _json(item.qc_detail),
        item.source_url,
        item.raw_fetch_id,
    )


def _wind_from_row(row: sqlite3.Row) -> WindObservation:
    return WindObservation(
        id=row["id"],
        station_id=row["station_id"],
        observed_at=_parse_utc(row["observed_at_utc"]),
        received_at=_parse_utc(row["received_at_utc"]),
        fetched_at=_parse_utc(row["fetched_at_utc"]),
        processing_product=row["processing_product"],
        speed_mps=row["speed_mps"],
        gust_mps=row["gust_mps"],
        direction_deg_true=row["direction_deg_true"],
        qc_status=row["qc_status"],
        qc_detail=_unjson(row["qc_detail_json"], {}),
        source_url=row["source_url"],
        raw_fetch_id=row["raw_fetch_id"],
    )


def _forecast_run_values(item: ForecastRun) -> tuple[Any, ...]:
    return (
        item.id,
        item.provider,
        item.model,
        item.location_id,
        _utc_text(item.issued_at),
        _utc_text(item.fetched_at),
        item.horizon_hours,
        item.raw_fetch_id,
        _json(item.metadata),
    )


def _forecast_run_from_row(row: sqlite3.Row) -> ForecastRun:
    return ForecastRun(
        id=row["id"],
        provider=row["provider"],
        model=row["model"],
        location_id=row["location_id"],
        issued_at=_parse_utc(row["issued_at_utc"]),
        fetched_at=_parse_utc(row["fetched_at_utc"]),
        horizon_hours=row["horizon_hours"],
        raw_fetch_id=row["raw_fetch_id"],
        metadata=_unjson(row["metadata_json"], {}),
    )


def _forecast_point_values(item: ForecastPoint) -> tuple[Any, ...]:
    return (
        item.run_id,
        _utc_text(item.valid_at),
        item.wave_height_m,
        item.wave_period_s,
        item.wave_direction_deg_true,
        item.swell_height_m,
        item.swell_period_s,
        item.swell_direction_deg_true,
        item.wind_wave_height_m,
        item.wind_wave_period_s,
        item.wind_wave_direction_deg_true,
        item.wind_speed_mps,
        item.wind_direction_deg_true,
        item.qc_status,
    )


def _forecast_point_from_row(row: sqlite3.Row) -> ForecastPoint:
    return ForecastPoint(
        run_id=row["run_id"],
        valid_at=_parse_utc(row["valid_at_utc"]),
        wave_height_m=row["wave_height_m"],
        wave_period_s=row["wave_period_s"],
        wave_direction_deg_true=row["wave_direction_deg_true"],
        swell_height_m=row["swell_height_m"],
        swell_period_s=row["swell_period_s"],
        swell_direction_deg_true=row["swell_direction_deg_true"],
        wind_wave_height_m=row["wind_wave_height_m"],
        wind_wave_period_s=row["wind_wave_period_s"],
        wind_wave_direction_deg_true=row["wind_wave_direction_deg_true"],
        wind_speed_mps=row["wind_speed_mps"],
        wind_direction_deg_true=row["wind_direction_deg_true"],
        qc_status=row["qc_status"],
    )
