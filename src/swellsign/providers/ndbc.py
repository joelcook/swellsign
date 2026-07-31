"""NOAA NDBC realtime observation adapter.

The parser is deliberately table-driven: the first data header defines the
columns, so harmless upstream additions do not shift known fields.  Each
normalized model is built from exactly one source row.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from swellsign.directions import cardinal_to_degrees, normalize_degrees
from swellsign.models import RawFetch, WaveObservation, WindObservation

from .base import HttpFetcher

NDBC_REALTIME_ROOT = "https://www.ndbc.noaa.gov/data/realtime2"
MISSING_MARKERS = frozenset({"MM", "N/A", "NA", ""})

type NdbcValue = float | str | None

_DATE_COLUMN_NAMES = frozenset({"YY", "YYYY", "YR", "MM", "DD", "hh", "mm"})
_WAVE_COLUMNS = frozenset(
    {
        "WVHT",
        "DPD",
        "APD",
        "MWD",
        "SwH",
        "SwP",
        "SwD",
        "WWH",
        "WWP",
        "WWD",
        "WTMP",
    }
)
_WIND_COLUMNS = frozenset({"WDIR", "WSPD", "GST"})
_STATION_PATTERN = re.compile(r"/([^/?]+)\.(?:txt|spec)(?:[?#]|$)", re.IGNORECASE)


class NdbcParseError(ValueError):
    """The response was not a recognizable NDBC realtime table."""


@dataclass(frozen=True, slots=True)
class NdbcRow:
    """One coherent timestamped NDBC row."""

    observed_at: datetime
    values: dict[str, NdbcValue]
    missing_fields: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def value(self, field: str) -> NdbcValue:
        return self.values.get(field)


def _find_header(lines: list[str]) -> tuple[int, list[str]]:
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            continue
        tokens = line.strip().split()
        if not tokens:
            continue
        tokens[0] = tokens[0].removeprefix("#")
        if (
            tokens[0].upper() in {"YY", "YYYY", "YR"}
            and "DD" in tokens
            and "hh" in tokens
        ):
            return index, tokens
    raise NdbcParseError("NDBC table header was not found")


def _year_from_token(token: str) -> int:
    year = int(token)
    if year < 100:
        return 2000 + year if year < 70 else 1900 + year
    return year


def _date_index(columns: list[str], *names: str) -> int | None:
    for name in names:
        try:
            return columns.index(name)
        except ValueError:
            continue
    return None


def _parse_timestamp(columns: list[str], tokens: list[str]) -> datetime | None:
    year_index = _date_index(columns, "YYYY", "YY", "YR")
    month_index = _date_index(columns, "MM")
    day_index = _date_index(columns, "DD")
    hour_index = _date_index(columns, "hh")
    minute_index = _date_index(columns, "mm")
    required = (year_index, month_index, day_index, hour_index)
    if any(index is None for index in required):
        return None
    try:
        assert year_index is not None
        assert month_index is not None
        assert day_index is not None
        assert hour_index is not None
        minute = int(tokens[minute_index]) if minute_index is not None else 0
        return datetime(
            _year_from_token(tokens[year_index]),
            int(tokens[month_index]),
            int(tokens[day_index]),
            int(tokens[hour_index]),
            minute,
            tzinfo=UTC,
        )
    except (IndexError, TypeError, ValueError):
        return None


def _parse_value(token: str | None) -> NdbcValue:
    if token is None:
        return None
    cleaned = token.strip()
    if cleaned.upper() in MISSING_MARKERS:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return cleaned.upper()


def parse_ndbc_table(text: str) -> list[NdbcRow]:
    """Parse a standard or spectral-summary NDBC whitespace table.

    Header columns are discovered at runtime.  Unit/comment rows and malformed
    timestamp rows are ignored; short data rows retain all available values and
    mark trailing fields missing.
    """

    lines = text.splitlines()
    header_index, columns = _find_header(lines)
    date_columns = set(_DATE_COLUMN_NAMES)
    rows: list[NdbcRow] = []

    for line in lines[header_index + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        observed_at = _parse_timestamp(columns, tokens)
        if observed_at is None:
            continue

        values: dict[str, NdbcValue] = {}
        missing_fields: list[str] = []
        warnings: list[str] = []
        for index, column in enumerate(columns):
            if column in date_columns:
                continue
            token = tokens[index] if index < len(tokens) else None
            value = _parse_value(token)
            values[column] = value
            if value is None:
                missing_fields.append(column)
        if len(tokens) < len(columns):
            warnings.append(f"short_row:{len(tokens)}/{len(columns)}")
        elif len(tokens) > len(columns):
            warnings.append(f"extra_values:{len(tokens) - len(columns)}")
        rows.append(
            NdbcRow(
                observed_at=observed_at,
                values=values,
                missing_fields=tuple(missing_fields),
                warnings=tuple(warnings),
            )
        )
    return rows


def parse_ndbc_text(text: str) -> list[NdbcRow]:
    """Parse NDBC standard realtime ``.txt`` content."""

    return parse_ndbc_table(text)


def parse_ndbc_spec(text: str) -> list[NdbcRow]:
    """Parse NDBC separated-swell realtime ``.spec`` content."""

    return parse_ndbc_table(text)


def _number(
    row: NdbcRow,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    rejected: list[str],
) -> float | None:
    value = row.value(field)
    if value is None:
        return None
    if not isinstance(value, (float, int)):
        rejected.append(field)
        return None
    numeric = float(value)
    if minimum is not None and numeric < minimum:
        rejected.append(field)
        return None
    if maximum is not None and numeric > maximum:
        rejected.append(field)
        return None
    return numeric


def _direction(row: NdbcRow, field: str, rejected: list[str]) -> float | None:
    value = row.value(field)
    if value is None:
        return None
    if isinstance(value, str):
        direction = cardinal_to_degrees(value)
        if direction is None:
            rejected.append(field)
        return direction
    numeric = float(value)
    if not 0 <= numeric <= 360:
        rejected.append(field)
        return None
    return normalize_degrees(numeric)


def _stable_observation_id(
    station_id: str,
    product: str,
    kind: str,
    observed_at: datetime,
) -> str:
    identity = f"{station_id}|{product}|{kind}|{observed_at.isoformat()}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return f"obs:{digest}"


def _station_from_url(source_url: str) -> str | None:
    match = _STATION_PATTERN.search(source_url)
    if match is None:
        return None
    return match.group(1).upper()


def _canonical_station_id(station_id: str) -> str:
    provider_id = station_id.split(":", 1)[-1].strip().upper()
    if not provider_id:
        raise ValueError("station_id cannot be empty")
    return f"ndbc:{provider_id}"


def _product_for(raw: RawFetch) -> tuple[str, bool]:
    spectral = "spec" in raw.resource_type.lower() or ".spec" in raw.source_url.lower()
    if spectral:
        return "ndbc_realtime_spec", True
    return "ndbc_realtime_standard", False


class NdbcProvider:
    """Acquire and normalize current NDBC standard and spectral summaries."""

    name = "ndbc"

    def __init__(self, fetcher: HttpFetcher | None = None) -> None:
        self.fetcher = fetcher or HttpFetcher()

    async def fetch_latest(self, station_id: str, product: str = "standard") -> RawFetch:
        provider_station_id = station_id.split(":", 1)[-1].strip().upper()
        if not provider_station_id:
            raise ValueError("station_id cannot be empty")
        normalized_product = product.lower().replace("-", "_")
        if normalized_product in {"standard", "txt", "stdmet"}:
            suffix = "txt"
            resource_type = "ndbc_standard"
        elif normalized_product in {"spec", "spectral", "spectral_summary"}:
            suffix = "spec"
            resource_type = "ndbc_spec"
        else:
            raise ValueError(f"unsupported NDBC realtime product: {product}")
        return await self.fetcher.fetch(
            provider=self.name,
            resource_type=resource_type,
            url=f"{NDBC_REALTIME_ROOT}/{provider_station_id}.{suffix}",
        )

    async def fetch_products(self, station_id: str) -> list[RawFetch]:
        """Fetch standard and separated-swell summaries concurrently."""

        standard, spectral = await asyncio.gather(
            self.fetch_latest(station_id, "standard"),
            self.fetch_latest(station_id, "spec"),
        )
        return [standard, spectral]

    async def fetch_range(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
    ) -> RawFetch:
        """Return the realtime file; callers may filter its rolling window."""

        if end < start:
            raise ValueError("end must not precede start")
        return await self.fetch_latest(station_id)

    def normalize_observations(
        self,
        raw: RawFetch,
        station_id: str | None = None,
    ) -> list[WaveObservation | WindObservation]:
        if raw.provider != self.name:
            raise ValueError(f"expected provider {self.name!r}, received {raw.provider!r}")
        if raw.error is not None:
            return []

        inferred_station = station_id or _station_from_url(raw.source_url)
        if inferred_station is None:
            raise ValueError("station_id is required when it cannot be inferred from source_url")
        canonical_station = _canonical_station_id(inferred_station)
        product, spectral = _product_for(raw)
        parser = parse_ndbc_spec if spectral else parse_ndbc_text
        rows = parser(raw.body.decode("utf-8", errors="replace"))
        fetched_at = raw.received_at or raw.requested_at

        observations: dict[str, WaveObservation | WindObservation] = {}
        for row in rows:
            # A small tolerance accommodates buoy/server clock skew.  Anything
            # farther ahead is preserved in RawFetch but is not normalized into
            # a current observation.
            if row.observed_at > fetched_at + timedelta(minutes=5):
                continue
            available = set(row.values)
            common_detail = {
                "missing_fields": list(row.missing_fields),
                "parser_warnings": list(row.warnings),
            }

            if available & _WAVE_COLUMNS:
                rejected_wave: list[str] = []
                wave = WaveObservation(
                    id=_stable_observation_id(
                        canonical_station,
                        product,
                        "wave",
                        row.observed_at,
                    ),
                    station_id=canonical_station,
                    observed_at=row.observed_at,
                    received_at=None,
                    fetched_at=fetched_at,
                    processing_product=product,
                    significant_height_m=_number(
                        row,
                        "WVHT",
                        minimum=0,
                        maximum=40,
                        rejected=rejected_wave,
                    ),
                    dominant_period_s=_number(
                        row,
                        "DPD",
                        minimum=0.1,
                        maximum=60,
                        rejected=rejected_wave,
                    ),
                    average_period_s=_number(
                        row,
                        "APD",
                        minimum=0.1,
                        maximum=60,
                        rejected=rejected_wave,
                    ),
                    mean_direction_deg_true=_direction(row, "MWD", rejected_wave),
                    swell_height_m=_number(
                        row,
                        "SwH",
                        minimum=0,
                        maximum=40,
                        rejected=rejected_wave,
                    ),
                    swell_period_s=_number(
                        row,
                        "SwP",
                        minimum=0.1,
                        maximum=60,
                        rejected=rejected_wave,
                    ),
                    swell_direction_deg_true=_direction(row, "SwD", rejected_wave),
                    wind_wave_height_m=_number(
                        row,
                        "WWH",
                        minimum=0,
                        maximum=40,
                        rejected=rejected_wave,
                    ),
                    wind_wave_period_s=_number(
                        row,
                        "WWP",
                        minimum=0.1,
                        maximum=60,
                        rejected=rejected_wave,
                    ),
                    wind_wave_direction_deg_true=_direction(row, "WWD", rejected_wave),
                    water_temperature_c=_number(
                        row,
                        "WTMP",
                        minimum=-5,
                        maximum=50,
                        rejected=rejected_wave,
                    ),
                    qc_status="accepted" if not rejected_wave else "suspect",
                    qc_detail={**common_detail, "rejected_fields": rejected_wave},
                    source_url=raw.source_url,
                    raw_fetch_id=raw.id,
                )
                observations.setdefault(wave.id, wave)

            if available & _WIND_COLUMNS:
                rejected_wind: list[str] = []
                wind = WindObservation(
                    id=_stable_observation_id(
                        canonical_station,
                        product,
                        "wind",
                        row.observed_at,
                    ),
                    station_id=canonical_station,
                    observed_at=row.observed_at,
                    received_at=None,
                    fetched_at=fetched_at,
                    processing_product=product,
                    speed_mps=_number(
                        row,
                        "WSPD",
                        minimum=0,
                        maximum=100,
                        rejected=rejected_wind,
                    ),
                    gust_mps=_number(
                        row,
                        "GST",
                        minimum=0,
                        maximum=120,
                        rejected=rejected_wind,
                    ),
                    direction_deg_true=_direction(row, "WDIR", rejected_wind),
                    qc_status="accepted" if not rejected_wind else "suspect",
                    qc_detail={**common_detail, "rejected_fields": rejected_wind},
                    source_url=raw.source_url,
                    raw_fetch_id=raw.id,
                )
                observations.setdefault(wind.id, wind)

        return sorted(observations.values(), key=lambda item: (item.observed_at, item.id))
