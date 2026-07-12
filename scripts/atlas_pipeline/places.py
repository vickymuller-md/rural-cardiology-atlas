"""CDC PLACES CHD/AgeAdjPrv snapshot validation and canonicalization."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable
from urllib.parse import quote, urlencode

from .contracts import (
    PLACES_DATA_VALUE_TYPE_ID,
    PLACES_ESTIMATE_YEAR,
    PLACES_MEASURE_ID,
    PLACES_RELEASE,
    PLACES_UNIT,
)
from .errors import ContractError, IntegrityError

PLACES_REQUIRED_FIELDS = (
    "locationid",
    "stateabbr",
    "locationname",
    "year",
    "datasource",
    "category",
    "measure",
    "measureid",
    "data_value_unit",
    "data_value_type",
    "datavaluetypeid",
    "data_value",
    "low_confidence_limit",
    "high_confidence_limit",
    "data_value_footnote_symbol",
    "data_value_footnote",
)

PLACES_ENDPOINT = "https://data.cdc.gov/resource/swc5-untb.csv"
PLACES_QUERY_PARAMETERS = (
    (
        "$select",
        "locationid,stateabbr,locationname,year,datasource,category,measure,"
        "measureid,data_value_unit,data_value_type,datavaluetypeid,data_value,"
        "low_confidence_limit,high_confidence_limit,data_value_footnote_symbol,"
        "data_value_footnote",
    ),
    ("$where", 'measureid="CHD" AND datavaluetypeid="AgeAdjPrv"'),
    ("$order", "locationid"),
    ("$limit", "5000"),
)


def canonical_places_query_url() -> str:
    return PLACES_ENDPOINT + "?" + urlencode(
        PLACES_QUERY_PARAMETERS, quote_via=quote, safe=","
    )


@dataclass(frozen=True)
class PlacesCountyRow:
    fips: str
    chd_age_adjusted_prevalence_pct: str | None
    places_release: str
    estimate_year: str
    measure_id: str
    data_value_type_id: str
    unit: str
    low_confidence_limit: str | None
    high_confidence_limit: str | None
    footnote: str | None


def _nullable(value: str | None) -> str | None:
    text = "" if value is None else value.strip()
    return text or None


def _validate_decimal(value: str | None, field: str) -> str | None:
    value = _nullable(value)
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError(f"invalid PLACES {field}: {value!r}") from exc
    if not parsed.is_finite():
        raise ContractError(f"nonfinite PLACES {field}: {value!r}")
    return value


def parse_places_csv(raw: bytes) -> list[dict[str, str]]:
    if not raw:
        raise ContractError("PLACES response is empty")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    except UnicodeDecodeError as exc:
        raise ContractError("PLACES response is not UTF-8") from exc
    if reader.fieldnames is None:
        raise ContractError("PLACES response has no header")
    missing = [field for field in PLACES_REQUIRED_FIELDS if field not in reader.fieldnames]
    if missing:
        raise ContractError(f"PLACES source missing columns: {missing}")
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise ContractError("PLACES response is malformed CSV") from exc
    if not rows:
        raise ContractError("PLACES response has no data rows")
    return rows


def _validate_semantics(row: dict[str, str]) -> None:
    expected = {
        "year": PLACES_ESTIMATE_YEAR,
        "datasource": "BRFSS",
        "category": "Health Outcomes",
        "measure": "Coronary heart disease among adults",
        "measureid": PLACES_MEASURE_ID,
        "datavaluetypeid": PLACES_DATA_VALUE_TYPE_ID,
        "data_value_unit": PLACES_UNIT,
        "data_value_type": "Age-adjusted prevalence",
    }
    for field, value in expected.items():
        if (row.get(field) or "").strip() != value:
            raise ContractError(
                f"PLACES semantic mismatch for {field}: {row.get(field)!r} != {value!r}"
            )


def canonicalize_places(
    raw: bytes,
    county_universe: set[str],
    *,
    expected_raw_sha256: str | None = None,
    expected_county_rows: int = 2_957,
    expected_nonnull: int = 2_956,
    expected_suppressed_fips: str = "48301",
    expected_public_nulls: int = 188,
) -> list[PlacesCountyRow]:
    if expected_raw_sha256 and hashlib.sha256(raw).hexdigest() != expected_raw_sha256:
        raise IntegrityError("PLACES raw response hash mismatch")
    rows = parse_places_csv(raw)
    for row in rows:
        _validate_semantics(row)

    national = [row for row in rows if (row.get("locationid") or "").strip() == "59"]
    if len(national) != 1:
        raise ContractError("PLACES response must contain exactly one national row 59")
    national_row = national[0]
    if (national_row.get("stateabbr") or "").strip() != "US":
        raise ContractError("PLACES national row must use stateabbr=US")
    if _validate_decimal(national_row.get("data_value"), "national value") != "5.3":
        raise ContractError("PLACES national row value must be 5.3")

    county_source: dict[str, dict[str, str]] = {}
    for row in rows:
        fips = (row.get("locationid") or "").strip()
        if fips == "59":
            continue
        if not (len(fips) == 5 and fips.isascii() and fips.isdigit()):
            raise ContractError(f"non-county PLACES location ID: {fips!r}")
        if fips not in county_universe:
            raise ContractError(f"PLACES FIPS outside frozen universe: {fips}")
        if fips in county_source:
            raise IntegrityError(f"duplicate PLACES county FIPS: {fips}")
        county_source[fips] = row
    if len(county_source) != expected_county_rows:
        raise ContractError(
            f"PLACES county-row count {len(county_source)} != {expected_county_rows}"
        )

    result: list[PlacesCountyRow] = []
    nonnull = 0
    for fips in sorted(county_source):
        row = county_source[fips]
        value = _validate_decimal(row.get("data_value"), "data_value")
        low = _validate_decimal(row.get("low_confidence_limit"), "low confidence")
        high = _validate_decimal(row.get("high_confidence_limit"), "high confidence")
        for field, lexeme in (("data_value", value), ("low confidence", low), ("high confidence", high)):
            if lexeme is not None and not Decimal("0") <= Decimal(lexeme) <= Decimal("100"):
                raise ContractError(f"PLACES {field} outside percentage range: {lexeme}")
        if value is not None:
            nonnull += 1
        result.append(
            PlacesCountyRow(
                fips=fips,
                chd_age_adjusted_prevalence_pct=value,
                places_release=PLACES_RELEASE,
                estimate_year=PLACES_ESTIMATE_YEAR,
                measure_id=PLACES_MEASURE_ID,
                data_value_type_id=PLACES_DATA_VALUE_TYPE_ID,
                unit=PLACES_UNIT,
                low_confidence_limit=low,
                high_confidence_limit=high,
                footnote=_nullable(row.get("data_value_footnote")),
            )
        )
    if nonnull != expected_nonnull:
        raise ContractError(f"PLACES non-null count {nonnull} != {expected_nonnull}")
    suppressed = next((row for row in result if row.fips == expected_suppressed_fips), None)
    if suppressed is None or suppressed.chd_age_adjusted_prevalence_pct is not None:
        raise ContractError("expected suppressed PLACES county is missing or non-null")
    if "population less than 50" not in (suppressed.footnote or "").lower():
        raise ContractError("suppressed PLACES county lacks the expected footnote semantics")
    if len(county_universe) - nonnull != expected_public_nulls:
        raise ContractError(
            f"PLACES public null count {len(county_universe) - nonnull} != {expected_public_nulls}"
        )
    return result


def canonical_places_snapshot_bytes(rows: Iterable[PlacesCountyRow]) -> bytes:
    lines = [
        json.dumps(
            {
                key: unicodedata.normalize("NFC", value) if isinstance(value, str) else value
                for key, value in asdict(row).items()
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for row in sorted(rows, key=lambda item: item.fips)
    ]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
