"""Strict NPPES individual-provider selection and audit accounting."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Iterable, Mapping

from .addresses import NormalizedAddress, normalize_address
from .contracts import (
    ADULT_CARDIOLOGY_TAXONOMIES,
    NPPES_ADDRESS_FIELDS,
    NPPES_ANALYTICAL_FIELDS,
    WATERFALL_BUCKETS,
)
from .errors import ContractError, IntegrityError

AS_OF_DATE = date(2026, 6, 8)


def _luhn_check_digit(prefix: str) -> str:
    total = 0
    parity = 1 - (len(prefix) % 2)
    for index, char in enumerate(prefix):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - total % 10) % 10)


def valid_npi(value: object) -> bool:
    npi = "" if value is None else str(value).strip()
    return len(npi) == 10 and npi.isascii() and npi.isdigit() and (
        _luhn_check_digit("80840" + npi[:9]) == npi[9]
    )


def parse_nppes_date(value: object) -> date | None:
    normalized = "" if value is None else str(value)
    if not normalized:
        return None
    if not re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", normalized, flags=re.ASCII):
        raise ContractError(f"invalid NPPES date: {normalized!r}")
    try:
        return datetime.strptime(normalized, "%m/%d/%Y").date()
    except ValueError as exc:
        raise ContractError(f"invalid NPPES date: {normalized!r}") from exc


@dataclass(frozen=True)
class PrimaryTaxonomy:
    classification: str
    code: str | None
    diagnostics: frozenset[str]


def classify_primary_taxonomy(row: Mapping[str, object]) -> PrimaryTaxonomy:
    populated: list[tuple[str, str]] = []
    malformed = False
    diagnostics: set[str] = set()
    for slot in range(1, 16):
        code = str(row.get(f"Healthcare Provider Taxonomy Code_{slot}") or "").strip().upper()
        switch = str(
            row.get(f"Healthcare Provider Primary Taxonomy Switch_{slot}") or ""
        ).strip().upper()
        if not code and not switch:
            continue
        if not code or switch not in {"Y", "N"}:
            malformed = True
            diagnostics.add("primary_flag_malformed")
        if code:
            populated.append((code, switch))

    primary = [code for code, switch in populated if switch == "Y"]
    if not primary:
        diagnostics.add("no_primary_taxonomy")
    if len(primary) > 1:
        diagnostics.add("multiple_primary_taxonomies")
    if len(primary) == 1 and primary[0] not in ADULT_CARDIOLOGY_TAXONOMIES:
        diagnostics.add("primary_taxonomy_not_allowlisted")

    if malformed:
        return PrimaryTaxonomy("primary_flag_malformed", None, frozenset(diagnostics))
    if not primary:
        return PrimaryTaxonomy("no_primary_taxonomy", None, frozenset(diagnostics))
    if len(primary) > 1:
        return PrimaryTaxonomy(
            "multiple_primary_taxonomies", None, frozenset(diagnostics)
        )
    if primary[0] not in ADULT_CARDIOLOGY_TAXONOMIES:
        return PrimaryTaxonomy(
            "primary_taxonomy_not_allowlisted", primary[0], frozenset(diagnostics)
        )
    return PrimaryTaxonomy("eligible", primary[0], frozenset(diagnostics))


@dataclass(frozen=True)
class NpiStatus:
    classification: str
    active: bool


def classify_status(row: Mapping[str, object]) -> NpiStatus:
    deactivation_raw = str(row.get("NPI Deactivation Date") or "")
    reactivation_raw = str(row.get("NPI Reactivation Date") or "")
    try:
        deactivation = parse_nppes_date(deactivation_raw)
        reactivation = parse_nppes_date(reactivation_raw)
    except ContractError:
        return NpiStatus("status_inconsistent", False)

    if deactivation is None and reactivation is None:
        return NpiStatus("active_never_deactivated", True)
    if deactivation is None or deactivation > AS_OF_DATE:
        return NpiStatus("status_inconsistent", False)
    if reactivation is None:
        return NpiStatus("inactive_deactivated", False)
    if reactivation <= deactivation or reactivation > AS_OF_DATE:
        return NpiStatus("status_inconsistent", False)
    return NpiStatus("active_reactivated", True)


def normalized_nppes_address(row: Mapping[str, object]) -> NormalizedAddress:
    return normalize_address(*(row.get(field) for field in NPPES_ADDRESS_FIELDS))


def _field_tuple(row: Mapping[str, object]) -> tuple[str, ...]:
    missing = [field for field in NPPES_ANALYTICAL_FIELDS if field not in row]
    if missing:
        raise IntegrityError(f"NPPES source missing analytical fields: {missing}")
    nonstrings = [field for field in NPPES_ANALYTICAL_FIELDS if not isinstance(row[field], str)]
    if nonstrings:
        raise IntegrityError(f"NPPES analytical fields must be exact strings: {nonstrings}")
    return tuple(row[field] for field in NPPES_ANALYTICAL_FIELDS)


@dataclass(frozen=True)
class DeduplicationAudit:
    raw_rows: int
    retained_unique_npis: int
    identical_duplicate_extra_rows: int


def deduplicate_rows(
    rows: Iterable[Mapping[str, object]],
) -> tuple[list[Mapping[str, object]], DeduplicationAudit]:
    grouped: dict[str, tuple[tuple[str, ...], Mapping[str, object], int]] = {}
    raw_count = 0
    for row in rows:
        raw_count += 1
        npi = str(row.get("NPI") or "").strip()
        if not valid_npi(npi):
            raise IntegrityError(f"invalid NPI: {npi!r}")
        fingerprint = _field_tuple(row)
        if npi not in grouped:
            grouped[npi] = (fingerprint, row, 1)
            continue
        prior_fingerprint, prior_row, count = grouped[npi]
        if fingerprint != prior_fingerprint:
            raise IntegrityError(f"conflicting duplicate NPI: {npi}")
        grouped[npi] = (prior_fingerprint, prior_row, count + 1)

    unique = [grouped[npi][1] for npi in sorted(grouped)]
    extras = sum(count - 1 for _, _, count in grouped.values())
    if raw_count != len(unique) + extras:
        raise IntegrityError("NPPES duplicate reconciliation failed")
    return unique, DeduplicationAudit(raw_count, len(unique), extras)


@dataclass(frozen=True)
class GeoDisposition:
    final: str
    census_outcome: str | None = None
    hud_outcome: str | None = None

    def __post_init__(self) -> None:
        if self.final not in {
            "assigned_direct_census",
            "assigned_hud_fallback",
            "final_unmatched",
        }:
            raise ContractError(f"invalid geography disposition: {self.final}")


@dataclass(frozen=True)
class NpiDisposition:
    npi: str
    bucket: str
    primary_taxonomy: str | None
    active_status: str
    address: NormalizedAddress
    diagnostics: frozenset[str]


def classify_disposition(
    row: Mapping[str, object], geo: GeoDisposition | None = None
) -> NpiDisposition:
    npi = str(row.get("NPI") or "").strip()
    if not valid_npi(npi):
        raise IntegrityError(f"invalid NPI: {npi!r}")
    taxonomy = classify_primary_taxonomy(row)
    status = classify_status(row)
    address = normalized_nppes_address(row)
    diagnostics = set(taxonomy.diagnostics)
    primary_codes: list[str] = []
    all_codes: list[str] = []
    for slot in range(1, 16):
        code = str(row.get(f"Healthcare Provider Taxonomy Code_{slot}") or "").strip().upper()
        switch = str(
            row.get(f"Healthcare Provider Primary Taxonomy Switch_{slot}") or ""
        ).strip().upper()
        if code:
            all_codes.append(code)
            if switch == "Y":
                primary_codes.append(code)
    if any(code in ADULT_CARDIOLOGY_TAXONOMIES for code in all_codes):
        diagnostics.add("allowlisted_taxonomy_code_present")
    if any(code not in ADULT_CARDIOLOGY_TAXONOMIES for code in all_codes):
        diagnostics.add("ineligible_taxonomy_code_present")
    if any(code in ADULT_CARDIOLOGY_TAXONOMIES for code in primary_codes):
        diagnostics.add("primary_allowlisted_code_present")
    if any(code not in ADULT_CARDIOLOGY_TAXONOMIES for code in primary_codes):
        diagnostics.add("primary_ineligible_code_present")
    entity_type = str(row.get("Entity Type Code") or "")
    diagnostics.add(
        "entity_type_individual"
        if entity_type == "1"
        else "entity_type_not_individual"
    )
    diagnostics.add(f"taxonomy_{taxonomy.classification}")
    if taxonomy.classification == "eligible":
        diagnostics.add("primary_taxonomy_allowlisted")
    diagnostics.add(status.classification)
    diagnostics.update(address.diagnostic_flags)
    if geo is not None:
        diagnostics.add(f"census_{geo.census_outcome or 'not_submitted'}")
        diagnostics.add(f"hud_{geo.hud_outcome or 'not_attempted'}")
    if entity_type != "1":
        bucket = "entity_type_not_individual"
        diagnostics.add(bucket)
    elif taxonomy.classification != "eligible":
        bucket = taxonomy.classification
    elif status.classification == "status_inconsistent":
        bucket = "status_inconsistent"
    elif status.classification == "inactive_deactivated":
        bucket = "inactive_deactivated"
    elif address.classification == "country_out_of_scope":
        bucket = "country_out_of_scope"
    elif address.classification == "state_out_of_scope_or_invalid":
        bucket = "state_out_of_scope_or_invalid"
    else:
        bucket = (geo or GeoDisposition("final_unmatched")).final
    if bucket not in WATERFALL_BUCKETS:
        raise IntegrityError(f"unknown waterfall bucket: {bucket}")
    return NpiDisposition(
        npi=npi,
        bucket=bucket,
        primary_taxonomy=taxonomy.code,
        active_status=status.classification,
        address=address,
        diagnostics=frozenset(diagnostics),
    )


@dataclass(frozen=True)
class WaterfallAudit:
    terminal_counts: dict[str, int]
    diagnostic_counts: dict[str, int]
    d_geo: int
    counted_providers: int


@dataclass(frozen=True)
class RouteCrossTabRow:
    npi: str
    address_id: str
    direct_eligible: bool
    direct_reason: str
    census_outcome: str
    hud_eligible: bool
    hud_outcome: str
    final_disposition: str


def route_cross_tab_row(
    disposition: NpiDisposition, geo: GeoDisposition | None
) -> RouteCrossTabRow:
    return RouteCrossTabRow(
        npi=disposition.npi,
        address_id=disposition.address.address_id,
        direct_eligible=disposition.address.direct_eligible,
        direct_reason=disposition.address.direct_reason,
        census_outcome=(geo.census_outcome if geo else None) or "NOT_SUBMITTED",
        hud_eligible=disposition.address.hud_eligible,
        hud_outcome=(geo.hud_outcome if geo else None) or "NOT_ATTEMPTED",
        final_disposition=disposition.bucket,
    )


def audit_dispositions(dispositions: Iterable[NpiDisposition]) -> WaterfallAudit:
    items = list(dispositions)
    terminal = Counter(item.bucket for item in items)
    diagnostics = Counter(flag for item in items for flag in item.diagnostics)
    if sum(terminal.values()) != len(items):
        raise IntegrityError("terminal NPI disposition accounting failed")
    d_geo = sum(
        terminal[bucket]
        for bucket in (
            "assigned_direct_census",
            "assigned_hud_fallback",
            "final_unmatched",
        )
    )
    counted = terminal["assigned_direct_census"] + terminal["assigned_hud_fallback"]
    return WaterfallAudit(
        {bucket: terminal[bucket] for bucket in WATERFALL_BUCKETS},
        dict(sorted(diagnostics.items())),
        d_geo,
        counted,
    )
