"""Canonical locations, numerical reference methods, and public schemas."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence

from .contracts import (
    ADULT_CARDIOLOGY_TAXONOMIES,
    COUNTY_PUBLIC_FIELDS,
    FORBIDDEN_LEGACY_COUNTY_FIELD,
    STATE_FIPS,
    SUMMARY_FIELDS,
)
from .errors import ContractError, IntegrityError

_SIX = Decimal("0.000001")
_ONE = Decimal("0.1")
_JSON_NUMBER = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class CanonicalNumber(str):
    """A validated JSON number lexeme emitted without quotes."""

    def __new__(cls, value: str):
        if not _JSON_NUMBER.fullmatch(value):
            raise ContractError(f"invalid canonical JSON number: {value!r}")
        return super().__new__(cls, value)


def quantize_coordinate(value: object, *, latitude: bool) -> str:
    raw = str(value).strip(" \t\r\n\f\v")
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ContractError(f"invalid coordinate lexeme: {raw!r}") from exc
    if not parsed.is_finite():
        raise ContractError(f"nonfinite coordinate: {raw!r}")
    limit = Decimal("90" if latitude else "180")
    if parsed < -limit or parsed > limit:
        raise ContractError(f"coordinate outside range: {raw!r}")
    result = parsed.quantize(_SIX, rounding=ROUND_HALF_UP)
    if result == 0:
        result = Decimal("0.000000")
    return format(result, ".6f")


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlambda / 2) ** 2
    )
    a = min(1.0, max(0.0, a))
    return 2 * 3958.7613 * math.asin(math.sqrt(a))


def public_one_decimal(value: float | Decimal) -> CanonicalNumber:
    decimal_value = value if isinstance(value, Decimal) else Decimal(repr(value))
    return CanonicalNumber(format(decimal_value.quantize(_ONE, rounding=ROUND_HALF_UP), ".1f"))


def _nfc(value: object) -> object:
    if isinstance(value, str) and not isinstance(value, CanonicalNumber):
        return unicodedata.normalize("NFC", value)
    return value


def _json_value(value: object) -> str:
    value = _nfc(value)
    if isinstance(value, CanonicalNumber):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("NaN/Infinity forbidden in canonical JSON")
        return json.dumps(value, allow_nan=False)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ContractError("nonfinite Decimal forbidden in canonical JSON")
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def canonical_object(row: Mapping[str, object], fields: Sequence[str]) -> str:
    if tuple(row.keys()) != tuple(fields):
        raise IntegrityError(
            f"canonical schema/order mismatch: {tuple(row.keys())!r} != {tuple(fields)!r}"
        )
    return "{" + ",".join(
        json.dumps(field) + ":" + _json_value(row[field]) for field in fields
    ) + "}"


def canonical_jsonl(
    rows: Iterable[Mapping[str, object]], fields: Sequence[str], sort_key
) -> bytes:
    ordered = sorted(rows, key=sort_key)
    lines = [canonical_object(row, fields) for row in ordered]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


PROVIDER_LOCATION_FIELDS = (
    "npi",
    "address_id",
    "primary_taxonomy",
    "active_status",
    "source_state",
    "county_fips",
    "latitude",
    "longitude",
    "assignment_method",
    "direct_eligible",
    "hud_eligible",
    "census_outcome",
    "hud_attempted",
    "hud_outcome",
    "final_disposition",
    "geocoder_match_indicator",
    "geocoder_match_type",
    "hud_ratio_basis",
    "hud_selected_ratio",
    "hud_bus_ratio",
    "hud_res_ratio",
    "hud_tot_ratio",
    "hud_same_state_candidate_count",
    "hud_second_highest_ratio",
    "hud_bus_margin_to_second",
    "quality_flag",
    "census_source_response_sha256",
    "hud_source_snapshot_id",
    "gazetteer_source_snapshot_id",
    "source_snapshot_id",
)

HOSPITAL_LOCATION_FIELDS = (
    "facility_id",
    "address_id",
    "ccn",
    "federal_facility",
    "is_cah",
    "source_state",
    "county_fips",
    "latitude",
    "longitude",
    "assignment_method",
    "direct_eligible",
    "hud_eligible",
    "census_outcome",
    "hud_attempted",
    "hud_outcome",
    "final_disposition",
    "geocoder_match_indicator",
    "geocoder_match_type",
    "hud_ratio_basis",
    "hud_selected_ratio",
    "hud_bus_ratio",
    "hud_res_ratio",
    "hud_tot_ratio",
    "hud_same_state_candidate_count",
    "hud_second_highest_ratio",
    "hud_bus_margin_to_second",
    "quality_flag",
    "census_source_response_sha256",
    "hud_source_snapshot_id",
    "gazetteer_source_snapshot_id",
    "source_snapshot_id",
)


@dataclass(frozen=True)
class ProviderLocation:
    npi: str
    address_id: str
    primary_taxonomy: str
    active_status: str
    source_state: str
    county_fips: str | None
    latitude: str | None
    longitude: str | None
    assignment_method: str | None
    direct_eligible: bool
    hud_eligible: bool
    census_outcome: str
    hud_attempted: bool
    hud_outcome: str
    final_disposition: str
    geocoder_match_indicator: str | None
    geocoder_match_type: str | None
    hud_ratio_basis: str | None
    hud_selected_ratio: str | None
    hud_bus_ratio: str | None
    hud_res_ratio: str | None
    hud_tot_ratio: str | None
    hud_same_state_candidate_count: int | None
    hud_second_highest_ratio: str | None
    hud_bus_margin_to_second: str | None
    quality_flag: str
    census_source_response_sha256: str | None
    hud_source_snapshot_id: str | None
    gazetteer_source_snapshot_id: str | None
    source_snapshot_id: str

    @property
    def assigned(self) -> bool:
        return self.county_fips is not None


@dataclass(frozen=True)
class HospitalLocation:
    facility_id: str
    address_id: str
    ccn: str | None
    federal_facility: bool
    is_cah: bool
    source_state: str
    county_fips: str | None
    latitude: str | None
    longitude: str | None
    assignment_method: str | None
    direct_eligible: bool
    hud_eligible: bool
    census_outcome: str
    hud_attempted: bool
    hud_outcome: str
    final_disposition: str
    geocoder_match_indicator: str | None
    geocoder_match_type: str | None
    hud_ratio_basis: str | None
    hud_selected_ratio: str | None
    hud_bus_ratio: str | None
    hud_res_ratio: str | None
    hud_tot_ratio: str | None
    hud_same_state_candidate_count: int | None
    hud_second_highest_ratio: str | None
    hud_bus_margin_to_second: str | None
    quality_flag: str
    census_source_response_sha256: str | None
    hud_source_snapshot_id: str | None
    gazetteer_source_snapshot_id: str | None
    source_snapshot_id: str

    @property
    def assigned(self) -> bool:
        return self.county_fips is not None


def _validate_location(
    source_state: str,
    county_fips: str | None,
    latitude: str | None,
    longitude: str | None,
    assignment_method: str | None,
    source_snapshot_id: str,
    county_universe: set[str],
) -> None:
    if source_state not in STATE_FIPS:
        raise IntegrityError("canonical source state is outside 50-state/DC scope")
    values = (county_fips, latitude, longitude, assignment_method)
    if any(value is not None for value in values) and not all(value is not None for value in values):
        raise IntegrityError("assigned location is incomplete")
    if county_fips is not None:
        if county_fips not in county_universe:
            raise IntegrityError(f"assigned county outside universe: {county_fips}")
        expected = STATE_FIPS.get(source_state)
        if expected is None or county_fips[:2] != expected:
            raise IntegrityError("assigned county/source-state mismatch")
        if quantize_coordinate(latitude, latitude=True) != latitude:
            raise IntegrityError("latitude is not canonical six-decimal text")
        if quantize_coordinate(longitude, latitude=False) != longitude:
            raise IntegrityError("longitude is not canonical six-decimal text")
    if not re.fullmatch(r"[0-9a-f]{64}", source_snapshot_id):
        raise IntegrityError("source snapshot ID must be lowercase SHA-256")


def _validate_route_provenance(row: ProviderLocation | HospitalLocation) -> None:
    """Enforce one route and the same provenance contract for both entity kinds."""

    if type(row.direct_eligible) is not bool or type(row.hud_eligible) is not bool:
        raise IntegrityError("canonical route eligibility flags must be boolean")
    if type(row.hud_attempted) is not bool:
        raise IntegrityError("canonical HUD-attempt flag must be boolean")
    allowed_census = {
        "NOT_SUBMITTED", "Match", "Tie", "No_Match",
        "Invalid_Match", "Invalid_Match_State",
    }
    if row.census_outcome not in allowed_census:
        raise IntegrityError("unknown canonical Census outcome")
    if row.direct_eligible != (row.census_outcome != "NOT_SUBMITTED"):
        raise IntegrityError("direct eligibility/Census submission provenance mismatch")
    for name in (
        "census_source_response_sha256", "hud_source_snapshot_id",
        "gazetteer_source_snapshot_id",
    ):
        value = getattr(row, name)
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise IntegrityError(f"canonical {name} is not lowercase SHA-256")
    if (row.census_source_response_sha256 is not None) != row.direct_eligible:
        raise IntegrityError("Census source snapshot/eligibility mismatch")
    expected_indicator = {
        "NOT_SUBMITTED": None,
        "Match": "Match",
        "Tie": "Tie",
        "No_Match": "No_Match",
        "Invalid_Match": "Match",
        "Invalid_Match_State": "Match",
    }[row.census_outcome]
    if row.geocoder_match_indicator != expected_indicator:
        raise IntegrityError("canonical Census outcome/source indicator mismatch")
    if row.census_outcome == "NOT_SUBMITTED" and row.geocoder_match_type is not None:
        raise IntegrityError("non-submitted Census route carries match metadata")
    if row.hud_attempted != (row.hud_outcome != "NOT_ATTEMPTED"):
        raise IntegrityError("HUD attempt/outcome provenance mismatch")
    if (row.hud_source_snapshot_id is not None) != row.hud_attempted:
        raise IntegrityError("HUD source snapshot/attempt mismatch")
    allowed_hud = {
        "NOT_ATTEMPTED", "HUD_ASSIGNED", "HUD_INVALID_SOURCE_STATE",
        "HUD_INVALID_OR_MISSING_ZIP", "HUD_ZIP_ABSENT",
        "HUD_NO_SAME_STATE_CANDIDATE", "HUD_NO_POSITIVE_RATIO", "HUD_BUS_TIE",
    }
    if row.hud_outcome not in allowed_hud:
        raise IntegrityError("unknown canonical primary HUD outcome")
    if row.hud_attempted and (not row.hud_eligible or row.census_outcome == "Match"):
        raise IntegrityError("ineligible or direct-Match route attempted HUD")

    hud_values = (
        row.hud_ratio_basis,
        row.hud_selected_ratio,
        row.hud_bus_ratio,
        row.hud_res_ratio,
        row.hud_tot_ratio,
        row.hud_same_state_candidate_count,
        row.hud_second_highest_ratio,
        row.hud_bus_margin_to_second,
    )
    if not row.hud_attempted and any(value is not None for value in hud_values):
        raise IntegrityError("non-attempted HUD route carries HUD metadata")
    if row.hud_attempted:
        if (
            row.hud_ratio_basis != "BUS_RATIO"
            or row.hud_same_state_candidate_count is None
            or row.hud_same_state_candidate_count < 0
        ):
            raise IntegrityError("primary HUD attempt basis/candidate count is invalid")
        try:
            selected_any = (
                None if row.hud_selected_ratio is None else Decimal(row.hud_selected_ratio)
            )
            second_any = (
                None if row.hud_second_highest_ratio is None
                else Decimal(row.hud_second_highest_ratio)
            )
            margin_any = (
                None if row.hud_bus_margin_to_second is None
                else Decimal(row.hud_bus_margin_to_second)
            )
        except InvalidOperation as exc:
            raise IntegrityError("primary HUD ranking provenance is invalid") from exc
        if (second_any is None) != (margin_any is None) or (
            second_any is not None
            and (
                selected_any is None or second_any > selected_any
                or margin_any != selected_any - second_any
            )
        ):
            raise IntegrityError("primary HUD selected/second/margin mismatch")
        if row.hud_outcome == "HUD_BUS_TIE" and (
            selected_any is None or selected_any <= 0
            or second_any != selected_any or margin_any != 0
            or row.hud_same_state_candidate_count < 2
        ):
            raise IntegrityError("primary HUD tie provenance is not reconstructable")

    if row.final_disposition == "assigned_direct_census":
        if (
            row.assignment_method != "CENSUS_MATCH"
            or row.census_outcome != "Match"
            or row.hud_attempted
            or row.gazetteer_source_snapshot_id is not None
            or not row.assigned
        ):
            raise IntegrityError("direct canonical route provenance is inconsistent")
    elif row.final_disposition == "assigned_hud_fallback":
        try:
            bus_ratio = Decimal(row.hud_bus_ratio or "")
            selected_ratio = Decimal(row.hud_selected_ratio or "")
            res_ratio = Decimal(row.hud_res_ratio or "")
            tot_ratio = Decimal(row.hud_tot_ratio or "")
        except InvalidOperation as exc:
            raise IntegrityError("HUD assignment lacks valid selected ratios") from exc
        if (
            row.assignment_method
            != "HUD_BUS_RATIO_SAME_STATE_COUNTY_INTERNAL_POINT"
            or not row.assigned
            or not row.hud_attempted
            or row.hud_outcome != "HUD_ASSIGNED"
            or row.hud_ratio_basis != "BUS_RATIO"
            or selected_ratio != bus_ratio
            or not bus_ratio.is_finite()
            or bus_ratio <= 0
            or bus_ratio > Decimal(1)
            or not res_ratio.is_finite()
            or not Decimal(0) <= res_ratio <= Decimal(1)
            or not tot_ratio.is_finite()
            or not Decimal(0) <= tot_ratio <= Decimal(1)
            or row.hud_same_state_candidate_count is None
            or row.hud_same_state_candidate_count < 1
            or row.gazetteer_source_snapshot_id is None
        ):
            raise IntegrityError("HUD canonical route provenance is inconsistent")
        second = (
            None
            if row.hud_second_highest_ratio is None
            else Decimal(row.hud_second_highest_ratio)
        )
        margin = (
            None
            if row.hud_bus_margin_to_second is None
            else Decimal(row.hud_bus_margin_to_second)
        )
        if (second is None) != (margin is None) or (
            second is not None
            and (
                not second.is_finite()
                or second < 0
                or second > selected_ratio
                or margin != selected_ratio - second
            )
        ):
            raise IntegrityError("HUD selected/second/margin provenance is inconsistent")
    elif row.final_disposition == "final_unmatched":
        if row.assigned or row.assignment_method is not None:
            raise IntegrityError("unmatched canonical route carries an assignment")
        if row.hud_outcome == "HUD_ASSIGNED":
            raise IntegrityError("unmatched canonical route has assigned HUD outcome")
        if row.gazetteer_source_snapshot_id is not None:
            raise IntegrityError("unmatched canonical route carries Gazetteer point provenance")
    else:
        raise IntegrityError("unknown canonical final disposition")


def provider_table_bytes(
    rows: Iterable[ProviderLocation], county_universe: set[str]
) -> bytes:
    items = list(rows)
    npis: set[str] = set()
    keys: set[tuple[str, str]] = set()
    mappings = []
    for row in items:
        from .nppes import valid_npi

        if not valid_npi(row.npi):
            raise IntegrityError(f"invalid canonical NPI: {row.npi}")
        if row.primary_taxonomy not in ADULT_CARDIOLOGY_TAXONOMIES:
            raise IntegrityError("canonical provider has an ineligible taxonomy")
        if row.active_status not in {"active_never_deactivated", "active_reactivated"}:
            raise IntegrityError("canonical provider is not active")
        if not re.fullmatch(r"A-[0-9a-f]{64}", row.address_id):
            raise IntegrityError("canonical provider address_id is invalid")
        key = (row.npi, row.address_id)
        if key in keys or row.npi in npis:
            raise IntegrityError("canonical provider key/NPI is not unique")
        keys.add(key)
        npis.add(row.npi)
        _validate_location(
            row.source_state, row.county_fips, row.latitude, row.longitude,
            row.assignment_method, row.source_snapshot_id, county_universe,
        )
        _validate_route_provenance(row)
        mapping = asdict(row)
        if mapping["latitude"] is not None:
            mapping["latitude"] = CanonicalNumber(mapping["latitude"])
            mapping["longitude"] = CanonicalNumber(mapping["longitude"])
        for field in (
            "hud_bus_ratio",
            "hud_selected_ratio",
            "hud_res_ratio",
            "hud_tot_ratio",
            "hud_bus_margin_to_second",
            "hud_second_highest_ratio",
        ):
            if mapping[field] is not None:
                try:
                    mapping[field] = CanonicalNumber(format(Decimal(mapping[field]), "f"))
                except InvalidOperation as exc:
                    raise IntegrityError(f"invalid canonical HUD decimal: {field}") from exc
        mappings.append(mapping)
    return canonical_jsonl(mappings, PROVIDER_LOCATION_FIELDS, lambda row: (row["npi"], row["address_id"]))


def hospital_table_bytes(
    rows: Iterable[HospitalLocation], county_universe: set[str]
) -> bytes:
    items = list(rows)
    ids: set[str] = set()
    mappings = []
    for row in items:
        if not re.fullmatch(r"(?:[0-9]{6}|[0-9]{5}F)", row.facility_id):
            raise IntegrityError("invalid canonical Facility ID")
        if row.ccn != (row.facility_id if row.facility_id.isdigit() else None):
            raise IntegrityError("canonical CCN/federal Facility-ID contract failed")
        if row.is_cah and row.ccn is None:
            raise IntegrityError("canonical CAH is not a numeric CCN")
        if row.federal_facility != row.facility_id.endswith("F"):
            raise IntegrityError("canonical federal Facility-ID flag is inconsistent")
        if not re.fullmatch(r"A-[0-9a-f]{64}", row.address_id):
            raise IntegrityError("canonical hospital address_id is invalid")
        if row.facility_id in ids:
            raise IntegrityError("canonical Facility ID is not unique")
        ids.add(row.facility_id)
        _validate_location(
            row.source_state, row.county_fips, row.latitude, row.longitude,
            row.assignment_method, row.source_snapshot_id, county_universe,
        )
        _validate_route_provenance(row)
        mapping = asdict(row)
        if mapping["latitude"] is not None:
            mapping["latitude"] = CanonicalNumber(mapping["latitude"])
            mapping["longitude"] = CanonicalNumber(mapping["longitude"])
        for field in (
            "hud_bus_ratio",
            "hud_selected_ratio",
            "hud_res_ratio",
            "hud_tot_ratio",
            "hud_bus_margin_to_second",
            "hud_second_highest_ratio",
        ):
            if mapping[field] is not None:
                try:
                    mapping[field] = CanonicalNumber(format(Decimal(mapping[field]), "f"))
                except InvalidOperation as exc:
                    raise IntegrityError(f"invalid canonical HUD decimal: {field}") from exc
        mappings.append(mapping)
    return canonical_jsonl(mappings, HOSPITAL_LOCATION_FIELDS, lambda row: (row["facility_id"], row["address_id"]))


def table_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class NearestProvider:
    npi: str
    address_id: str
    distance_raw: float


def nearest_provider(
    latitude: str,
    longitude: str,
    providers: Iterable[ProviderLocation],
) -> NearestProvider:
    candidates: list[tuple[tuple[object, ...], ProviderLocation]] = []
    lat1, lon1 = float(latitude), float(longitude)
    for provider in providers:
        if not provider.assigned:
            continue
        distance = haversine_miles(
            lat1, lon1, float(provider.latitude), float(provider.longitude)
        )
        tie = (
            distance,
            Decimal(provider.latitude),
            Decimal(provider.longitude),
            provider.npi,
            provider.address_id,
        )
        candidates.append((tie, provider))
    if not candidates:
        raise IntegrityError("canonical provider-coordinate set is empty")
    tie, winner = min(candidates, key=lambda item: item[0])
    return NearestProvider(winner.npi, winner.address_id, tie[0])


@dataclass(frozen=True)
class CahDistance:
    facility_id: str
    county_fips: str
    assignment_method: str
    distance_raw: float
    gte_20: bool


@dataclass(frozen=True)
class CahAccounting:
    total_in_scope: int
    evaluable: int
    unmatched: int
    gte_20: int
    coordinate_method_counts: dict[str, int]
    distances: tuple[CahDistance, ...]


def compute_cah_accounting(
    hospitals: Iterable[HospitalLocation], providers: Iterable[ProviderLocation]
) -> CahAccounting:
    provider_rows = list(providers)
    cah = [row for row in hospitals if row.is_cah]
    if any(row.ccn is None for row in cah):
        raise IntegrityError("CAH set contains a non-CCN Facility ID")
    if cah and not any(row.assigned for row in provider_rows):
        raise IntegrityError("zero-size provider-coordinate set")
    distances: list[CahDistance] = []
    method_counts: dict[str, int] = {}
    for facility in cah:
        if not facility.assigned:
            continue
        nearest = nearest_provider(facility.latitude, facility.longitude, provider_rows)
        method = facility.assignment_method or ""
        method_counts[method] = method_counts.get(method, 0) + 1
        distances.append(
            CahDistance(
                facility.facility_id,
                facility.county_fips or "",
                method,
                nearest.distance_raw,
                nearest.distance_raw >= 20.0,
            )
        )
    gte = sum(item.gte_20 for item in distances)
    return CahAccounting(
        total_in_scope=len(cah),
        evaluable=len(distances),
        unmatched=len(cah) - len(distances),
        gte_20=gte,
        coordinate_method_counts=dict(sorted(method_counts.items())),
        distances=tuple(sorted(distances, key=lambda item: item.facility_id)),
    )


def validate_county_rows(
    rows: Iterable[Mapping[str, object]], county_universe: set[str]
) -> list[Mapping[str, object]]:
    items = list(rows)
    if any(FORBIDDEN_LEGACY_COUNTY_FIELD in row for row in items):
        raise IntegrityError("forbidden legacy mortality key in county output")
    if any(tuple(row.keys()) != COUNTY_PUBLIC_FIELDS for row in items):
        raise IntegrityError("county schema or key order mismatch")
    fips = [str(row["fips"]) for row in items]
    if len(fips) != len(set(fips)) or set(fips) != county_universe:
        raise IntegrityError("county output does not equal frozen universe")
    if fips != sorted(fips):
        raise IntegrityError("county rows are not sorted by FIPS")
    return items


def public_counties_bytes(
    rows: Iterable[Mapping[str, object]], county_universe: set[str]
) -> bytes:
    items = validate_county_rows(rows, county_universe)
    objects = [canonical_object(row, COUNTY_PUBLIC_FIELDS) for row in items]
    return ("[\n" + ",\n".join(objects) + "\n]\n").encode("utf-8")


def conventional_median(values: Sequence[float]) -> float:
    if not values:
        raise ContractError("median denominator is zero")
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def build_summary(
    county_rows: Iterable[Mapping[str, object]],
    county_bytes: bytes,
    *,
    provider_locations_sha256: str,
    hospital_locations_sha256: str,
    rural_raw_distances: Sequence[float],
    cah: CahAccounting,
) -> dict[str, object]:
    rows = list(county_rows)
    zero_rows = [row for row in rows if row["n_cardiologists"] == 0]
    county_count = len(rows)
    if county_count == 0:
        raise IntegrityError("summary county denominator is zero")
    if cah.evaluable == 0:
        raise IntegrityError("CAH evaluable denominator is zero")
    zero_pct = (Decimal(len(zero_rows) * 100) / Decimal(county_count)).quantize(
        _ONE, rounding=ROUND_HALF_UP
    )
    cah_pct = (Decimal(cah.gte_20 * 100) / Decimal(cah.evaluable)).quantize(
        _ONE, rounding=ROUND_HALF_UP
    )
    summary = {
        "counties_json_sha256": hashlib.sha256(county_bytes).hexdigest(),
        "provider_locations_sha256": provider_locations_sha256,
        "hospital_locations_sha256": hospital_locations_sha256,
        "county_count": county_count,
        "zero_provider_count": len(zero_rows),
        "zero_provider_pct": CanonicalNumber(format(zero_pct, ".1f")),
        "population_in_zero_provider_counties": sum(int(row["population"]) for row in zero_rows),
        "rural_count_with_evaluable_distance": len(rural_raw_distances),
        "rural_median_distance_miles": public_one_decimal(conventional_median(rural_raw_distances)),
        "cah_total_in_scope": cah.total_in_scope,
        "cah_evaluable": cah.evaluable,
        "cah_unmatched": cah.unmatched,
        "cah_gte_20_miles": cah.gte_20,
        "cah_gte_20_pct_evaluable": CanonicalNumber(format(cah_pct, ".1f")),
    }
    if tuple(summary.keys()) != SUMMARY_FIELDS:
        raise IntegrityError("summary schema/order mismatch")
    return summary


def public_summary_bytes(summary: Mapping[str, object]) -> bytes:
    return (canonical_object(summary, SUMMARY_FIELDS) + "\n").encode("utf-8")


def assert_copy_hash_binding(
    county_a: bytes,
    county_b: bytes,
    summary_a: bytes,
    summary_b: bytes,
) -> None:
    if county_a != county_b or summary_a != summary_b:
        raise IntegrityError("pipeline/app output copies are not byte-identical")
    loaded = json.loads(summary_a)
    if loaded["counties_json_sha256"] != hashlib.sha256(county_a).hexdigest():
        raise IntegrityError("summary-to-county hash binding failed")
