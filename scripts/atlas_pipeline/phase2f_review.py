"""Mandatory Phase-2F sensitivity and pre-review artifact construction.

This module never performs human review and never changes the PRIMARY result.
It freezes the complete frames, deterministic samples, blank worksheet, QA
source identities, and all five sensitivity tables before Gate B can begin.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Iterable, Mapping

from .addresses import NormalizedAddress
from .build import OfflineBuildArtifacts, OfflineBuildInputs
from .contracts import NPPES_ADDRESS_FIELDS, STATE_FIPS
from .errors import IntegrityError
from .geoqa import AdjacencyReceipt, PointQaSetup
from .hospitals import HospitalRecord, deduplicate_hospitals, in_scope_hospitals
from .hud import HudCandidate
from .nppes import classify_disposition, deduplicate_rows
from .quality import (
    SensitivityEntity,
    SensitivityReport,
    build_sensitivity_report,
    route_sensitivities,
)
from .validation import (
    GATE_THRESHOLDS,
    FrozenValidationArtifacts,
    ReviewEvidence,
    SampleCandidate,
    canonical_double_review_bytes,
    canonical_sample_bytes,
    freeze_validation_artifacts,
    provider_stratum,
    select_double_review,
    select_hospital_sample,
    select_provider_sample,
    assigned_context,
)


@dataclass(frozen=True)
class Phase2FReviewArtifacts:
    sensitivity: SensitivityReport
    validation: FrozenValidationArtifacts
    selected_sample_bytes: bytes
    selected_sample_sha256: str
    double_review_sample_bytes: bytes
    double_review_sample_sha256: str
    adjacency_receipt: AdjacencyReceipt
    point_qa_setup: PointQaSetup
    validation_receipt_bytes: bytes
    validation_receipt_sha256: str


def parse_adjacency_snapshot(
    rows: Iterable[Mapping[str, object]], county_universe: set[str]
) -> dict[str, tuple[str, ...]]:
    """Load one canonical row per primary FIPS, preserving all listed links."""

    result: dict[str, tuple[str, ...]] = {}
    for row in rows:
        if tuple(row) != ("primary_fips", "neighbor_fips"):
            raise IntegrityError("adjacency snapshot row schema/order mismatch")
        primary = row["primary_fips"]
        neighbors = row["neighbor_fips"]
        if (
            not isinstance(primary, str)
            or primary not in county_universe
            or primary in result
            or not isinstance(neighbors, list)
            or any(
                not isinstance(neighbor, str) or neighbor not in county_universe
                for neighbor in neighbors
            )
            or neighbors != sorted(set(neighbors))
        ):
            raise IntegrityError("adjacency snapshot identity/set contract failed")
        result[primary] = tuple(neighbors)
    if set(result) != county_universe:
        raise IntegrityError("adjacency snapshot primary FIPS differ from county universe")
    return result


def _hud_evidence(
    address: NormalizedAddress, candidates: tuple[HudCandidate, ...]
) -> tuple[dict[str, object], ...]:
    state_fips = STATE_FIPS.get(address.state)
    rows = []
    for candidate in sorted(
        (item for item in candidates if item.zip5 == address.zip5),
        key=lambda item: item.county_fips,
    ):
        rows.append(
            {
                "county_fips": candidate.county_fips,
                "bus_ratio": (
                    None if candidate.bus_ratio is None else format(candidate.bus_ratio, "f")
                ),
                "res_ratio": (
                    None if candidate.res_ratio is None else format(candidate.res_ratio, "f")
                ),
                "tot_ratio": (
                    None if candidate.tot_ratio is None else format(candidate.tot_ratio, "f")
                ),
                "oth_ratio": (
                    None if candidate.oth_ratio is None else format(candidate.oth_ratio, "f")
                ),
                "same_state": candidate.county_fips[:2] == state_fips,
            }
        )
    return tuple(rows)


def _method(assignment_method: str, county_fips: str | None) -> str:
    if county_fips is None:
        return "UNMATCHED"
    return "DIRECT" if assignment_method == "CENSUS_MATCH" else "HUD"


def _provider_unmatched_context(
    address: NormalizedAddress,
    assignment_method: str,
    hud_candidates: tuple[HudCandidate, ...],
    rucc: Mapping[str, int],
) -> tuple[str, str]:
    positive = tuple(
        item
        for item in hud_candidates
        if item.zip5 == address.zip5
        and item.bus_ratio is not None
        and item.bus_ratio > Decimal(0)
    )
    candidate_ruccs = tuple(rucc.get(item.county_fips) for item in positive)
    candidate_states = {item.county_fips[:2] for item in positive}
    mixed_rucc = bool(candidate_ruccs) and not (
        all(value is not None and value >= 4 for value in candidate_ruccs)
        or all(value is not None and value <= 3 for value in candidate_ruccs)
    )
    complex_unknown = (
        address.classification != "street_capable"
        or not address.zip5
        or not positive
        or len(candidate_states) > 1
        or assignment_method in {"HUD_BUS_TIE", "HUD_RES_TIE", "HUD_TOT_TIE"}
        or mixed_rucc
        or any(value is None for value in candidate_ruccs)
    )
    stratum = provider_stratum(
        "UNMATCHED",
        positive_candidate_ruccs=candidate_ruccs,
        complex_unknown=complex_unknown,
    )
    context = {
        "P_UNMATCHED_RURAL": "rural_context",
        "P_UNMATCHED_URBAN": "urban_context",
        "P_UNMATCHED_COMPLEX": "complex_unknown",
    }[stratum]
    return stratum, context


def _candidate_and_evidence(
    *,
    entity_kind: str,
    entity_id: str,
    address: NormalizedAddress,
    raw_address_fields: tuple[str, str, str, str, str, str],
    route,
    source_snapshot_id: str,
    county_universe: set[str],
    rucc: Mapping[str, int],
    state_borders: frozenset[str],
    geocodes: Mapping[str, object],
    hud_candidates: tuple[HudCandidate, ...],
    hospital_type: str | None = None,
    is_cah: bool = False,
) -> tuple[SampleCandidate, ReviewEvidence]:
    method = _method(route.assignment_method, route.county_fips)
    if method == "UNMATCHED":
        if entity_kind == "P":
            stratum, context = _provider_unmatched_context(
                address, route.assignment_method, hud_candidates, rucc
            )
        else:
            stratum, context = "H_UNMATCHED", None
    else:
        if route.county_fips not in county_universe:
            raise IntegrityError("assigned validation candidate is outside county universe")
        context = assigned_context(
            rucc[route.county_fips], route.county_fips in state_borders
        )
        stratum = f"{entity_kind}_{method}_{context}"
    candidate = SampleCandidate(
        entity_kind,
        stratum,
        entity_id,
        address.address_id,
        source_snapshot_id,
        method,
        context,
        is_cah,
    )
    geocode = geocodes.get(address.address_id)
    evidence = ReviewEvidence(
        raw_address_fields,
        (
            address.street1,
            address.street2,
            address.city,
            address.state,
            address.zip5,
            address.country,
        ),
        route.assignment_method,
        address.state,
        route.county_fips,
        route.longitude,
        route.latitude,
        None if geocode is None else geocode.matched_address,
        None if geocode is None else geocode.outcome,
        _hud_evidence(address, hud_candidates),
        hospital_type,
        is_cah if entity_kind == "H" else None,
    )
    return candidate, evidence


def _canonical_validation_receipt(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def build_phase2f_review_artifacts(
    inputs: OfflineBuildInputs,
    build: OfflineBuildArtifacts,
    *,
    independent_reviewer: str,
    adjacency_receipt: AdjacencyReceipt,
    state_borders: frozenset[str],
    point_qa_setup: PointQaSetup,
) -> Phase2FReviewArtifacts:
    if not independent_reviewer.strip():
        raise IntegrityError("Phase-2F requires a named independent reviewer")
    universe = set(inputs.county_points)
    rucc = {
        fips: int(inputs.county_attributes[fips]["rucc_2023"])
        for fips in universe
    }
    if set(rucc) != universe or any(not 1 <= value <= 9 for value in rucc.values()):
        raise IntegrityError("validation RUCC map differs from the valid county universe")
    if not state_borders.issubset(universe):
        raise IntegrityError("state-border set escapes the county universe")
    if adjacency_receipt.county_universe_sha256 != point_qa_setup.county_universe_sha256:
        raise IntegrityError("adjacency/TIGER QA universe identities differ")

    provider_rows: dict[str, Mapping[str, object]] = {}
    provider_addresses: dict[str, NormalizedAddress] = {}
    unique_nppes, _duplicate_audit = deduplicate_rows(inputs.nppes_rows)
    for row in unique_nppes:
        disposition = classify_disposition(row)
        if disposition.bucket != "final_unmatched":
            continue
        if disposition.npi in provider_rows:
            raise IntegrityError("Phase-2F selected NPPES rows duplicate D_geo identity")
        provider_rows[disposition.npi] = row
        provider_addresses[disposition.npi] = disposition.address
    hospital_records = in_scope_hospitals(deduplicate_hospitals(inputs.hospital_rows))
    hospital_by_id: dict[str, HospitalRecord] = {
        record.entity_id: record for record in hospital_records
    }
    if len(hospital_by_id) != len(hospital_records):
        raise IntegrityError("hospital identities are duplicated before sensitivity routing")
    hospital_addresses = {
        entity_id: record.address for entity_id, record in hospital_by_id.items()
    }
    raw_hospital_rows: dict[str, Mapping[str, object]] = {}
    for row in inputs.hospital_rows:
        facility_id = str(row["Facility ID"]).strip().upper()
        prior = raw_hospital_rows.setdefault(facility_id, row)
        if prior != row:
            raise IntegrityError("conflicting raw hospital rows reached review setup")
    geocodes = {row.request_id: row for row in inputs.geocodes}
    hud_candidates = tuple(inputs.hud_candidates)

    provider_routes = route_sensitivities(
        provider_addresses, geocodes, hud_candidates, inputs.county_points,
        hud_source_snapshot_id=inputs.hud_source_snapshot_id,
        gazetteer_source_snapshot_id=inputs.gazetteer_source_snapshot_id,
    )
    hospital_routes = route_sensitivities(
        hospital_addresses, geocodes, hud_candidates, inputs.county_points,
        hud_source_snapshot_id=inputs.hud_source_snapshot_id,
        gazetteer_source_snapshot_id=inputs.gazetteer_source_snapshot_id,
    )
    sensitivities = {
        name: tuple(
            [SensitivityEntity(route, "P") for route in provider_routes[name]]
            + [
                SensitivityEntity(route, "H", hospital_by_id[route.entity_id].is_cah)
                for route in hospital_routes[name]
            ]
        )
        for name in provider_routes
    }
    primary_assignments = {
        **{row.npi: row.county_fips for row in build.provider_locations},
        **{"H-" + row.facility_id: row.county_fips for row in build.hospital_locations},
    }
    primary_routes = {entity.route.entity_id: entity.route for entity in sensitivities["PRIMARY"]}
    canonical_route_reasons = {
        **{
            row.npi: row.assignment_method if row.assigned else row.quality_flag
            for row in build.provider_locations
        },
        **{
            "H-" + row.facility_id:
            row.assignment_method if row.assigned else row.quality_flag
            for row in build.hospital_locations
        },
    }
    if (
        set(primary_routes) != set(primary_assignments)
        or set(canonical_route_reasons) != set(primary_assignments)
        or any(
            primary_routes[entity_id].county_fips != county_fips
            or primary_routes[entity_id].assignment_method
            != canonical_route_reasons[entity_id]
            for entity_id, county_fips in primary_assignments.items()
        )
    ):
        raise IntegrityError("PRIMARY sensitivity assignments differ from canonical locations")
    sensitivity = build_sensitivity_report(
        sensitivities,
        county_universe=universe,
        county_points=inputs.county_points,
        rucc=rucc,
        primary_assignments=primary_assignments,
    )

    candidates: list[SampleCandidate] = []
    evidence: dict[str, ReviewEvidence] = {}
    for npi, address in sorted(provider_addresses.items()):
        candidate, row_evidence = _candidate_and_evidence(
            entity_kind="P",
            entity_id=npi,
            address=address,
            raw_address_fields=tuple(str(provider_rows[npi][field]) for field in NPPES_ADDRESS_FIELDS),
            route=primary_routes[npi],
            source_snapshot_id=inputs.source_snapshot_id,
            county_universe=universe,
            rucc=rucc,
            state_borders=state_borders,
            geocodes=geocodes,
            hud_candidates=hud_candidates,
        )
        candidates.append(candidate)
        evidence[candidate.sample_key] = row_evidence
    for entity_id, record in sorted(hospital_by_id.items()):
        raw_hospital = raw_hospital_rows[record.facility_id]
        candidate, row_evidence = _candidate_and_evidence(
            entity_kind="H",
            entity_id=entity_id,
            address=record.address,
            raw_address_fields=(
                str(raw_hospital["Address"]),
                "",
                str(raw_hospital["City/Town"]),
                record.source_state,
                str(raw_hospital["ZIP Code"]),
                "US",
            ),
            route=primary_routes[entity_id],
            source_snapshot_id=inputs.source_snapshot_id,
            county_universe=universe,
            rucc=rucc,
            state_borders=state_borders,
            geocodes=geocodes,
            hud_candidates=hud_candidates,
            hospital_type=record.hospital_type,
            is_cah=record.is_cah,
        )
        candidates.append(candidate)
        evidence[candidate.sample_key] = row_evidence

    provider_sample = select_provider_sample(
        item for item in candidates if item.entity_kind == "P"
    )
    hospital_sample = select_hospital_sample(
        item for item in candidates if item.entity_kind == "H"
    )
    selected = tuple(provider_sample.selected) + tuple(hospital_sample.selected)
    selected_evidence = {item.sample_key: evidence[item.sample_key] for item in selected}
    validation = freeze_validation_artifacts(candidates, selected, selected_evidence)
    selected_bytes = canonical_sample_bytes(selected)
    double_rows = tuple(
        select_double_review(provider_sample.selected, hospital=False)
    ) + tuple(select_double_review(hospital_sample.selected, hospital=True))
    double_bytes = canonical_double_review_bytes(double_rows)

    gate_thresholds = {
        name: {
            "numerator": threshold.numerator,
            "denominator": threshold.denominator,
        }
        for name, threshold in sorted(GATE_THRESHOLDS.items())
    }
    receipt_payload = {
        "schema_version": "atlas-v19-phase2f-validation-receipt-1",
        "source_snapshot_id": inputs.source_snapshot_id,
        "independent_reviewer": independent_reviewer,
        "human_review_status": "REQUIRED_BEFORE_GATE_B",
        "sampling_frame": {
            "rows": len(candidates),
            "bytes": len(validation.sampling_frame_bytes),
            "sha256": validation.sampling_frame_sha256,
        },
        "selected_sample": {
            "rows": len(selected),
            "bytes": len(selected_bytes),
            "sha256": hashlib.sha256(selected_bytes).hexdigest(),
        },
        "blank_worksheet": {
            "rows": len(selected),
            "bytes": len(validation.blank_worksheet_bytes),
            "sha256": validation.blank_worksheet_sha256,
        },
        "double_review_sample": {
            "rows": len(double_rows),
            "bytes": len(double_bytes),
            "sha256": hashlib.sha256(double_bytes).hexdigest(),
            "minimum_fraction_numerator": 1,
            "minimum_fraction_denominator": 5,
        },
        "provider_sample": {
            "target": provider_sample.target,
            "actual": provider_sample.actual,
            "structural_shortfalls": dict(sorted(provider_sample.shortfalls.items())),
        },
        "hospital_sample": {
            "target": hospital_sample.quotas.target,
            "actual": len(hospital_sample.selected),
            "direct_quota": hospital_sample.quotas.direct,
            "hud_quota": hospital_sample.quotas.hud,
            "unmatched_quota": hospital_sample.quotas.unmatched,
            "structural_shortfall": hospital_sample.quotas.structural_shortfall,
            "required_contexts": list(hospital_sample.required_contexts),
            "cah_target": hospital_sample.cah_target,
            "maximum_feasible_cah": hospital_sample.maximum_feasible_cah,
        },
        "adjacency_receipt_sha256": adjacency_receipt.sha256,
        "point_qa_setup_sha256": point_qa_setup.sha256,
        "criterion_thresholds": gate_thresholds,
        "double_review_kappa_threshold": {"numerator": 4, "denominator": 5},
        "sensitivity_report_sha256": sensitivity.sha256,
    }
    receipt_bytes = _canonical_validation_receipt(receipt_payload)
    return Phase2FReviewArtifacts(
        sensitivity,
        validation,
        selected_bytes,
        hashlib.sha256(selected_bytes).hexdigest(),
        double_bytes,
        hashlib.sha256(double_bytes).hexdigest(),
        adjacency_receipt,
        point_qa_setup,
        receipt_bytes,
        hashlib.sha256(receipt_bytes).hexdigest(),
    )
