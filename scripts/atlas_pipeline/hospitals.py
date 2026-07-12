"""CMS Hospital General Information Facility-ID and CAH contracts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Iterable, Mapping

from .addresses import NormalizedAddress, normalize_address
from .contracts import HOSPITAL_FIELDS, STATE_FIPS
from .errors import ContractError, IntegrityError

if TYPE_CHECKING:
    from .canonical import HospitalLocation

_FACILITY_ID = re.compile(r"^(?:[0-9]{6}|[0-9]{5}F)$")
CAH_TYPE = "CRITICAL ACCESS HOSPITALS"


def normalize_facility_id(value: object) -> str:
    facility_id = "" if value is None else str(value).strip(" \t\r\n\f\v").upper()
    if not _FACILITY_ID.fullmatch(facility_id):
        raise ContractError(f"invalid Facility ID: {facility_id!r}")
    return facility_id


def normalize_hospital_type(value: object) -> str:
    from .addresses import normalize_text

    return normalize_text(value)


@dataclass(frozen=True)
class HospitalRecord:
    facility_id: str
    ccn: str | None
    federal_facility: bool
    facility_name: str
    source_state: str
    county_parish: str
    hospital_type: str
    is_cah: bool
    address: NormalizedAddress

    @property
    def entity_id(self) -> str:
        return "H-" + self.facility_id


def parse_hospital(row: Mapping[str, object]) -> HospitalRecord:
    missing = [field for field in HOSPITAL_FIELDS if field not in row]
    if missing:
        raise ContractError(f"hospital source missing columns: {missing}")
    facility_id = normalize_facility_id(row["Facility ID"])
    hospital_type = normalize_hospital_type(row["Hospital Type"])
    is_cah = hospital_type == CAH_TYPE
    federal = facility_id.endswith("F")
    if is_cah and federal:
        raise ContractError("F-suffix facility classified as CAH requires preregistration review")
    state = str(row["State"] or "").strip().upper()
    address = normalize_address(
        row["Address"], "", row["City/Town"], state, row["ZIP Code"], "US"
    )
    return HospitalRecord(
        facility_id=facility_id,
        ccn=facility_id if facility_id.isdigit() else None,
        federal_facility=federal,
        facility_name=str(row["Facility Name"] or ""),
        source_state=state,
        county_parish=str(row["County/Parish"] or ""),
        hospital_type=hospital_type,
        is_cah=is_cah,
        address=address,
    )


def deduplicate_hospitals(rows: Iterable[Mapping[str, object]]) -> list[HospitalRecord]:
    result: dict[str, HospitalRecord] = {}
    for row in rows:
        parsed = parse_hospital(row)
        prior = result.get(parsed.facility_id)
        if prior is not None and prior != parsed:
            raise IntegrityError(f"conflicting Facility ID: {parsed.facility_id}")
        result[parsed.facility_id] = parsed
    return [result[key] for key in sorted(result)]


def in_scope_hospitals(records: Iterable[HospitalRecord]) -> list[HospitalRecord]:
    return [record for record in records if record.source_state in STATE_FIPS]


@dataclass(frozen=True)
class HospitalCountyNameQaRow:
    facility_id: str
    source_county_parish: str
    assigned_county_fips: str | None
    assigned_gazetteer_county_name: str | None
    exact_normalized_mismatch: bool | None


def hospital_county_name_qa_bytes(
    records: Iterable[HospitalRecord],
    locations: Iterable["HospitalLocation"],
    gazetteer_county_names: Mapping[str, str],
) -> bytes:
    """Emit observational source-name QA without changing canonical assignment."""

    record_rows = list(records)
    location_rows = list(locations)
    record_by_id = {record.facility_id: record for record in record_rows}
    location_by_id = {location.facility_id: location for location in location_rows}
    if len(record_by_id) != len(record_rows) or len(location_by_id) != len(location_rows):
        raise IntegrityError("hospital county-name QA inputs contain duplicate Facility IDs")
    if set(record_by_id) != set(location_by_id):
        raise IntegrityError("hospital county-name QA record/location sets differ")
    rows: list[HospitalCountyNameQaRow] = []
    for facility_id in sorted(record_by_id):
        record = record_by_id[facility_id]
        location = location_by_id[facility_id]
        source_name = normalize_hospital_type(record.county_parish)
        assigned_name = None
        mismatch = None
        if location.county_fips is not None:
            if location.county_fips not in gazetteer_county_names:
                raise IntegrityError("hospital QA assigned county lacks Gazetteer name")
            assigned_name = normalize_hospital_type(
                gazetteer_county_names[location.county_fips]
            )
            if source_name:
                mismatch = source_name != assigned_name
        rows.append(
            HospitalCountyNameQaRow(
                facility_id,
                source_name,
                location.county_fips,
                assigned_name,
                mismatch,
            )
        )
    summary = {
        "facility_total": len(rows),
        "source_county_missing": sum(not row.source_county_parish for row in rows),
        "unassigned_total": sum(row.assigned_county_fips is None for row in rows),
        "comparable_total": sum(row.exact_normalized_mismatch is not None for row in rows),
        "exact_normalized_mismatch_total": sum(
            row.exact_normalized_mismatch is True for row in rows
        ),
    }
    payload = {"summary": summary, "rows": [asdict(row) for row in rows]}
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
