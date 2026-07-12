"""Network-denied analytical construction from validated frozen snapshots."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Iterable, Mapping

from .addresses import EntityAddressMapping, NormalizedAddress, entity_address_mapping_bytes
from .acquisition import FrozenInput, canonical_manifest_bytes
from .canonical import (
    CahAccounting,
    CanonicalNumber,
    HospitalLocation,
    ProviderLocation,
    build_summary,
    compute_cah_accounting,
    hospital_table_bytes,
    nearest_provider,
    provider_table_bytes,
    public_counties_bytes,
    public_one_decimal,
    public_summary_bytes,
    quantize_coordinate,
    table_sha256,
)
from .contracts import (
    COUNTY_PUBLIC_FIELDS,
    PLACES_DATA_VALUE_TYPE_ID,
    PLACES_ESTIMATE_YEAR,
    PLACES_MEASURE_ID,
    PLACES_RELEASE,
    PLACES_UNIT,
    METHOD_LINEAGE_ID,
    STATE_FIPS,
)
from .errors import ContractError, IntegrityError
from .geocoder import CanonicalGeocode, validate_canonical_geocodes
from .hospitals import (
    HospitalRecord,
    deduplicate_hospitals,
    hospital_county_name_qa_bytes,
    in_scope_hospitals,
)
from .hud import HudAssignment, HudCandidate, assign_hud
from .nppes import (
    GeoDisposition,
    DeduplicationAudit,
    NpiDisposition,
    RouteCrossTabRow,
    WaterfallAudit,
    audit_dispositions,
    classify_disposition,
    deduplicate_rows,
    route_cross_tab_row,
)
from .places import PlacesCountyRow, canonical_places_snapshot_bytes
from .quality import (
    RateDiagnostic,
    RateGate,
    evaluate_geography_diagnostics,
    evaluate_geography_gates,
)


def _exact_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise IntegrityError(f"{field} must be an integer, not boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip(" \t\r\n\f\v")):
        return int(value)
    raise IntegrityError(f"{field} is not an exact integer value/lexeme: {value!r}")


@dataclass(frozen=True)
class ResolvedAddress:
    final: str
    county_fips: str | None
    latitude: str | None
    longitude: str | None
    assignment_method: str | None
    census: CanonicalGeocode | None
    hud: HudAssignment | None
    quality_flag: str


def resolve_address(
    address: NormalizedAddress,
    geocodes: Mapping[str, CanonicalGeocode],
    hud_candidates: Iterable[HudCandidate],
    county_points: Mapping[str, tuple[str, str]],
) -> ResolvedAddress:
    if address.out_of_scope_reason:
        return ResolvedAddress("final_unmatched", None, None, None, None, None, None, address.out_of_scope_reason)
    geocode = None
    if address.direct_eligible:
        if address.address_id not in geocodes:
            raise IntegrityError(f"missing geocoder snapshot row: {address.address_id}")
        geocode = geocodes[address.address_id]
        if geocode.outcome == "Match":
            return ResolvedAddress(
                "assigned_direct_census",
                geocode.county_fips,
                quantize_coordinate(geocode.latitude, latitude=True),
                quantize_coordinate(geocode.longitude, latitude=False),
                "CENSUS_MATCH",
                geocode,
                None,
                "DIRECT_MATCH",
            )
    if address.hud_eligible:
        hud = assign_hud(address.zip5, address.state, hud_candidates)
        if hud.outcome == "HUD_ASSIGNED":
            if hud.county_fips not in county_points:
                raise IntegrityError(f"missing Gazetteer internal point: {hud.county_fips}")
            latitude, longitude = county_points[hud.county_fips]
            return ResolvedAddress(
                "assigned_hud_fallback",
                hud.county_fips,
                quantize_coordinate(latitude, latitude=True),
                quantize_coordinate(longitude, latitude=False),
                "HUD_BUS_RATIO_SAME_STATE_COUNTY_INTERNAL_POINT",
                geocode,
                hud,
                hud.outcome,
            )
        return ResolvedAddress("final_unmatched", None, None, None, None, geocode, hud, hud.outcome)
    return ResolvedAddress(
        "final_unmatched", None, None, None, None, geocode, None,
        "NO_VALID_ZIP_FOR_HUD" if not address.zip5 else "DIRECT_INELIGIBLE",
    )


def _geo_disposition(resolved: ResolvedAddress) -> GeoDisposition:
    return GeoDisposition(
        resolved.final,
        resolved.census.outcome if resolved.census else "NOT_SUBMITTED",
        resolved.hud.outcome if resolved.hud else "NOT_ATTEMPTED",
    )


def _provider_location(
    disposition: NpiDisposition,
    resolved: ResolvedAddress,
    snapshot_id: str,
    hud_snapshot_id: str,
    gazetteer_snapshot_id: str,
) -> ProviderLocation:
    hud = resolved.hud
    return ProviderLocation(
        npi=disposition.npi,
        address_id=disposition.address.address_id,
        primary_taxonomy=disposition.primary_taxonomy or "",
        active_status=disposition.active_status,
        source_state=disposition.address.state,
        county_fips=resolved.county_fips,
        latitude=resolved.latitude,
        longitude=resolved.longitude,
        assignment_method=resolved.assignment_method,
        direct_eligible=disposition.address.direct_eligible,
        hud_eligible=disposition.address.hud_eligible,
        census_outcome=(resolved.census.outcome if resolved.census else "NOT_SUBMITTED"),
        hud_attempted=resolved.hud is not None,
        hud_outcome=(resolved.hud.outcome if resolved.hud else "NOT_ATTEMPTED"),
        final_disposition=resolved.final,
        geocoder_match_indicator=(resolved.census.match_indicator if resolved.census else None),
        geocoder_match_type=(resolved.census.match_type if resolved.census else None),
        hud_ratio_basis=(hud.ratio_basis if hud else None),
        hud_selected_ratio=(
            str(hud.selected_ratio) if hud and hud.selected_ratio is not None else None
        ),
        hud_bus_ratio=(str(hud.bus_ratio) if hud and hud.bus_ratio is not None else None),
        hud_res_ratio=(str(hud.res_ratio) if hud and hud.res_ratio is not None else None),
        hud_tot_ratio=(str(hud.tot_ratio) if hud and hud.tot_ratio is not None else None),
        hud_same_state_candidate_count=(hud.same_state_candidate_count if hud else None),
        hud_second_highest_ratio=(
            str(hud.second_highest_ratio)
            if hud and hud.second_highest_ratio is not None
            else None
        ),
        hud_bus_margin_to_second=(str(hud.margin_to_second) if hud and hud.margin_to_second is not None else None),
        quality_flag=resolved.quality_flag,
        census_source_response_sha256=(
            resolved.census.source_response_sha256 if resolved.census else None
        ),
        hud_source_snapshot_id=(hud_snapshot_id if resolved.hud else None),
        gazetteer_source_snapshot_id=(
            gazetteer_snapshot_id if resolved.final == "assigned_hud_fallback" else None
        ),
        source_snapshot_id=snapshot_id,
    )


def _hospital_location(
    hospital: HospitalRecord,
    resolved: ResolvedAddress,
    snapshot_id: str,
    hud_snapshot_id: str,
    gazetteer_snapshot_id: str,
) -> HospitalLocation:
    hud = resolved.hud
    return HospitalLocation(
        facility_id=hospital.facility_id,
        address_id=hospital.address.address_id,
        ccn=hospital.ccn,
        federal_facility=hospital.federal_facility,
        is_cah=hospital.is_cah,
        source_state=hospital.source_state,
        county_fips=resolved.county_fips,
        latitude=resolved.latitude,
        longitude=resolved.longitude,
        assignment_method=resolved.assignment_method,
        direct_eligible=hospital.address.direct_eligible,
        hud_eligible=hospital.address.hud_eligible,
        census_outcome=(resolved.census.outcome if resolved.census else "NOT_SUBMITTED"),
        hud_attempted=resolved.hud is not None,
        hud_outcome=(resolved.hud.outcome if resolved.hud else "NOT_ATTEMPTED"),
        final_disposition=resolved.final,
        geocoder_match_indicator=(resolved.census.match_indicator if resolved.census else None),
        geocoder_match_type=(resolved.census.match_type if resolved.census else None),
        hud_ratio_basis=(hud.ratio_basis if hud else None),
        hud_selected_ratio=(
            str(hud.selected_ratio) if hud and hud.selected_ratio is not None else None
        ),
        hud_bus_ratio=(str(hud.bus_ratio) if hud and hud.bus_ratio is not None else None),
        hud_res_ratio=(str(hud.res_ratio) if hud and hud.res_ratio is not None else None),
        hud_tot_ratio=(str(hud.tot_ratio) if hud and hud.tot_ratio is not None else None),
        hud_same_state_candidate_count=(hud.same_state_candidate_count if hud else None),
        hud_second_highest_ratio=(
            str(hud.second_highest_ratio)
            if hud and hud.second_highest_ratio is not None
            else None
        ),
        hud_bus_margin_to_second=(
            str(hud.margin_to_second)
            if hud and hud.margin_to_second is not None
            else None
        ),
        quality_flag=resolved.quality_flag,
        census_source_response_sha256=(
            resolved.census.source_response_sha256 if resolved.census else None
        ),
        hud_source_snapshot_id=(hud_snapshot_id if resolved.hud else None),
        gazetteer_source_snapshot_id=(
            gazetteer_snapshot_id if resolved.final == "assigned_hud_fallback" else None
        ),
        source_snapshot_id=snapshot_id,
    )


@dataclass(frozen=True)
class OfflineBuildInputs:
    nppes_rows: tuple[Mapping[str, object], ...]
    hospital_rows: tuple[Mapping[str, object], ...]
    geocodes: tuple[CanonicalGeocode, ...]
    hud_candidates: tuple[HudCandidate, ...]
    county_points: Mapping[str, tuple[str, str]]
    county_attributes: Mapping[str, Mapping[str, object]]
    places_rows: tuple[PlacesCountyRow, ...]
    source_snapshot_id: str
    hud_source_snapshot_id: str
    gazetteer_source_snapshot_id: str
    method_lineage_id: str


@dataclass(frozen=True)
class OfflineBuildArtifacts:
    provider_locations: tuple[ProviderLocation, ...]
    hospital_locations: tuple[HospitalLocation, ...]
    waterfall: WaterfallAudit
    duplicate_audit: DeduplicationAudit
    route_cross_tab: tuple[RouteCrossTabRow, ...]
    unique_address_counts: Mapping[str, int]
    entity_address_mapping_bytes: bytes
    entity_address_mapping_sha256: str
    provider_locations_bytes: bytes
    provider_locations_sha256: str
    hospital_locations_bytes: bytes
    hospital_locations_sha256: str
    hospital_county_name_qa_bytes: bytes
    hospital_county_name_qa_sha256: str
    provider_hash_used_for_counts: str
    provider_hash_used_for_distances: str
    hospital_hash_used_for_counts: str
    hospital_hash_used_for_cah_distances: str
    county_rows: tuple[Mapping[str, object], ...]
    counties_json: bytes
    summary: Mapping[str, object]
    summary_json: bytes
    cah: CahAccounting
    quality_gates: tuple[RateGate, ...]
    geography_diagnostics: tuple[RateDiagnostic, ...]


@dataclass(frozen=True)
class BuildExpectations:
    county_count: int
    population_total: int
    places_county_rows: int
    places_nonnull: int
    places_public_nulls: int
    places_snapshot_sha256: str
    input_manifest_sha256: str
    required_input_hashes: Mapping[str, str]
    method_lineage_id: str
    places_suppressed_fips: str = "48301"


class ReleaseQualityGateError(IntegrityError):
    """Fail a release while preserving every exact gate receipt for review."""

    def __init__(
        self,
        gates: tuple[RateGate, ...],
        artifacts: OfflineBuildArtifacts | None = None,
    ) -> None:
        self.gates = gates
        self.artifacts = artifacts
        failures = [gate.name for gate in gates if not gate.passed]
        details = "; ".join(
            f"{gate.name}={gate.numerator}/{gate.denominator} "
            f"({gate.operator} {gate.threshold.numerator}/{gate.threshold.denominator})"
            for gate in gates
        )
        super().__init__(
            f"release numerical quality gates failed: {failures}; receipts: {details}"
        )


V19_RELEASE_EXPECTATIONS = BuildExpectations(
    county_count=3_144,
    population_total=334_922_499,
    places_county_rows=2_957,
    places_nonnull=2_956,
    places_public_nulls=188,
    places_snapshot_sha256="PENDING_CANONICAL_PLACES_SHA256",
    input_manifest_sha256="PENDING_FROZEN_INPUT_MANIFEST_SHA256",
    required_input_hashes={},
    method_lineage_id=METHOD_LINEAGE_ID,
)


def validate_release_preflight(
    inputs: OfflineBuildInputs,
    expectations: BuildExpectations,
    *,
    places_snapshot_bytes: bytes,
    frozen_inputs: tuple[FrozenInput, ...],
) -> None:
    if (
        inputs.method_lineage_id != METHOD_LINEAGE_ID
        or expectations.method_lineage_id != METHOD_LINEAGE_ID
        or inputs.method_lineage_id != expectations.method_lineage_id
    ):
        raise IntegrityError("release method lineage identity mismatch")
    universe = set(inputs.county_points)
    if len(universe) != expectations.county_count:
        raise IntegrityError("release county universe count mismatch")
    populations = [
        _exact_int(inputs.county_attributes[fips]["population"], f"population for {fips}")
        for fips in universe
    ]
    if sum(populations) != expectations.population_total:
        raise IntegrityError("release population denominator mismatch")
    if len(inputs.places_rows) != expectations.places_county_rows:
        raise IntegrityError("release PLACES county-row count mismatch")
    for row in inputs.places_rows:
        if (
            row.places_release,
            row.estimate_year,
            row.measure_id,
            row.data_value_type_id,
            row.unit,
        ) != (
            PLACES_RELEASE,
            PLACES_ESTIMATE_YEAR,
            PLACES_MEASURE_ID,
            PLACES_DATA_VALUE_TYPE_ID,
            PLACES_UNIT,
        ):
            raise IntegrityError("release PLACES semantic contract mismatch")
    nonnull = sum(row.chd_age_adjusted_prevalence_pct is not None for row in inputs.places_rows)
    if nonnull != expectations.places_nonnull:
        raise IntegrityError("release PLACES non-null count mismatch")
    if len(universe) - nonnull != expectations.places_public_nulls:
        raise IntegrityError("release PLACES public-null count mismatch")
    suppressed = next(
        (row for row in inputs.places_rows if row.fips == expectations.places_suppressed_fips),
        None,
    )
    if (
        suppressed is None
        or suppressed.chd_age_adjusted_prevalence_pct is not None
        or "population less than 50" not in (suppressed.footnote or "").lower()
    ):
        raise IntegrityError("release PLACES suppressed-county contract mismatch")
    canonical = canonical_places_snapshot_bytes(inputs.places_rows)
    if canonical != places_snapshot_bytes:
        raise IntegrityError("release PLACES snapshot bytes differ from canonical rows")
    expected_places_hash = expectations.places_snapshot_sha256
    if not re.fullmatch(r"[0-9a-f]{64}", expected_places_hash):
        raise IntegrityError("release PLACES snapshot hash has not been frozen")
    if hashlib.sha256(places_snapshot_bytes).hexdigest() != expected_places_hash:
        raise IntegrityError("release PLACES snapshot hash mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", expectations.input_manifest_sha256):
        raise IntegrityError("release input-manifest hash has not been frozen")
    manifest_bytes = canonical_manifest_bytes(list(frozen_inputs))
    input_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if input_manifest_sha256 != expectations.input_manifest_sha256:
        raise IntegrityError("release input-manifest identity mismatch")
    if inputs.source_snapshot_id != input_manifest_sha256:
        raise IntegrityError("canonical source snapshot ID is not the input-manifest hash")
    input_hashes = {entry.source_id: entry.sha256 for entry in frozen_inputs}
    if input_hashes != dict(expectations.required_input_hashes):
        raise IntegrityError("release input hashes do not equal the frozen set")


def build_release(
    inputs: OfflineBuildInputs,
    expectations: BuildExpectations,
    *,
    places_snapshot_bytes: bytes,
    frozen_inputs: tuple[FrozenInput, ...],
) -> OfflineBuildArtifacts:
    """Only release entrypoint; permissive miniature defaults are impossible."""

    validate_release_preflight(
        inputs,
        expectations,
        places_snapshot_bytes=places_snapshot_bytes,
        frozen_inputs=frozen_inputs,
    )
    artifacts = build_offline(inputs)
    provider_direct = sum(
        row.assignment_method == "CENSUS_MATCH" for row in artifacts.provider_locations
    )
    provider_hud = sum(
        row.assignment_method == "HUD_BUS_RATIO_SAME_STATE_COUNTY_INTERNAL_POINT"
        for row in artifacts.provider_locations
    )
    provider_unmatched = sum(not row.assigned for row in artifacts.provider_locations)
    provider_total = len(artifacts.provider_locations)
    hospital_direct = sum(
        row.assignment_method == "CENSUS_MATCH" for row in artifacts.hospital_locations
    )
    hospital_hud = sum(
        row.assignment_method == "HUD_BUS_RATIO_SAME_STATE_COUNTY_INTERNAL_POINT"
        for row in artifacts.hospital_locations
    )
    hospital_unmatched = sum(not row.assigned for row in artifacts.hospital_locations)
    tie_ids = {
        row.address_id
        for row in artifacts.route_cross_tab
        if row.direct_eligible and row.census_outcome == "Tie"
    }
    if (
        provider_direct
        != artifacts.waterfall.terminal_counts["assigned_direct_census"]
        or provider_hud
        != artifacts.waterfall.terminal_counts["assigned_hud_fallback"]
        or provider_unmatched
        != artifacts.waterfall.terminal_counts["final_unmatched"]
        or provider_total != artifacts.waterfall.d_geo
    ):
        raise IntegrityError("provider canonical routes differ from waterfall equations")
    gates = evaluate_geography_gates(
        provider_direct=provider_direct,
        provider_hud=provider_hud,
        provider_unmatched=provider_unmatched,
        provider_total=provider_total,
        hospital_direct=hospital_direct,
        hospital_hud=hospital_hud,
        hospital_unmatched=hospital_unmatched,
        hospital_total=len(artifacts.hospital_locations),
        cah_unmatched=artifacts.cah.unmatched,
        cah_total=artifacts.cah.total_in_scope,
    )
    diagnostics = evaluate_geography_diagnostics(
        provider_direct=provider_direct,
        provider_hud=provider_hud,
        provider_unmatched=provider_unmatched,
        provider_total=provider_total,
        provider_ties=len(tie_ids),
        submitted_unique_addresses=artifacts.unique_address_counts["provider_direct_unique"],
        hospital_direct=hospital_direct,
        hospital_hud=hospital_hud,
        hospital_unmatched=hospital_unmatched,
        hospital_total=len(artifacts.hospital_locations),
    )
    artifacts = replace(
        artifacts, quality_gates=gates, geography_diagnostics=diagnostics
    )
    if any(not gate.passed for gate in gates):
        raise ReleaseQualityGateError(gates, artifacts)
    return artifacts


def build_offline(inputs: OfflineBuildInputs) -> OfflineBuildArtifacts:
    """Run the miniature/full analytical build without touching the network."""

    canonical_county_points = {
        fips: (
            quantize_coordinate(point[0], latitude=True),
            quantize_coordinate(point[1], latitude=False),
        )
        for fips, point in inputs.county_points.items()
    }
    universe = set(canonical_county_points)
    if set(inputs.county_attributes) != universe:
        raise IntegrityError("county attributes do not equal Gazetteer universe")
    if not universe:
        raise IntegrityError("county universe is empty")
    if not all(len(fips) == 5 and fips.isdigit() for fips in universe):
        raise IntegrityError("county universe contains malformed FIPS")
    unique_nppes, duplicate_audit = deduplicate_rows(inputs.nppes_rows)
    hospital_records = in_scope_hospitals(deduplicate_hospitals(inputs.hospital_rows))
    preliminary_nppes = [(row, classify_disposition(row)) for row in unique_nppes]
    provider_direct_ids = {
        disposition.address.address_id
        for _, disposition in preliminary_nppes
        if disposition.bucket == "final_unmatched" and disposition.address.direct_eligible
    }
    hospital_direct_ids = {
        hospital.address.address_id
        for hospital in hospital_records
        if hospital.address.direct_eligible
    }
    required_geocode_ids = provider_direct_ids | hospital_direct_ids
    request_states: dict[str, str] = {}
    for address in (
        [
            disposition.address
            for _, disposition in preliminary_nppes
            if disposition.bucket == "final_unmatched"
            and disposition.address.direct_eligible
        ]
        + [
            hospital.address
            for hospital in hospital_records
            if hospital.address.direct_eligible
        ]
    ):
        prior = request_states.setdefault(address.address_id, address.state)
        if prior != address.state:
            raise IntegrityError("shared direct address has conflicting source states")
    validated_geocodes = validate_canonical_geocodes(
        inputs.geocodes,
        expected_ids=required_geocode_ids,
        request_states=request_states,
        county_universe=universe,
    )
    geocode_by_id = {row.request_id: row for row in validated_geocodes}
    mappings: list[EntityAddressMapping] = []
    for _, disposition in preliminary_nppes:
        if disposition.bucket != "final_unmatched":
            continue
        address = disposition.address
        mappings.append(EntityAddressMapping(
            "P", disposition.npi, address.address_id, address.street1, address.street2,
            address.city, address.state, address.zip5, address.country,
            address.classification, address.direct_eligible, address.hud_eligible,
        ))
    for hospital in hospital_records:
        address = hospital.address
        mappings.append(EntityAddressMapping(
            "H", hospital.entity_id, address.address_id, address.street1, address.street2,
            address.city, address.state, address.zip5, address.country,
            address.classification, address.direct_eligible, address.hud_eligible,
        ))
    mapping_bytes = entity_address_mapping_bytes(mappings)
    final_dispositions: list[NpiDisposition] = []
    providers: list[ProviderLocation] = []
    cross_tab: list[RouteCrossTabRow] = []
    for row, preliminary in preliminary_nppes:
        if preliminary.bucket != "final_unmatched":
            final_dispositions.append(preliminary)
            cross_tab.append(route_cross_tab_row(preliminary, None))
            continue
        resolved = resolve_address(
            preliminary.address, geocode_by_id, inputs.hud_candidates, canonical_county_points
        )
        geo = _geo_disposition(resolved)
        final = classify_disposition(row, geo)
        final_dispositions.append(final)
        cross_tab.append(route_cross_tab_row(final, geo))
        providers.append(_provider_location(
            final, resolved, inputs.source_snapshot_id,
            inputs.hud_source_snapshot_id, inputs.gazetteer_source_snapshot_id,
        ))
    waterfall = audit_dispositions(final_dispositions)
    if waterfall.d_geo != len(providers):
        raise IntegrityError("D_geo does not reconcile to canonical provider rows")

    hospitals: list[HospitalLocation] = []
    for hospital in hospital_records:
        resolved = resolve_address(
            hospital.address, geocode_by_id, inputs.hud_candidates, canonical_county_points
        )
        hospitals.append(_hospital_location(
            hospital, resolved, inputs.source_snapshot_id,
            inputs.hud_source_snapshot_id, inputs.gazetteer_source_snapshot_id,
        ))

    if any("county" not in inputs.county_attributes[fips] for fips in universe):
        raise IntegrityError("county attributes lack Gazetteer county names for hospital QA")
    hospital_county_qa = hospital_county_name_qa_bytes(
        hospital_records,
        hospitals,
        {fips: str(inputs.county_attributes[fips]["county"]) for fips in universe},
    )

    provider_bytes = provider_table_bytes(providers, universe)
    hospital_bytes = hospital_table_bytes(hospitals, universe)
    provider_hash = table_sha256(provider_bytes)
    hospital_hash = table_sha256(hospital_bytes)
    cah = compute_cah_accounting(hospitals, providers)

    provider_counts = Counter(row.county_fips for row in providers if row.assigned)
    hospital_counts = Counter(row.county_fips for row in hospitals if row.assigned)
    cah_counts = Counter(row.county_fips for row in hospitals if row.assigned and row.is_cah)
    cah_gte_counts = Counter(row.county_fips for row in cah.distances if row.gte_20)
    place_map = {row.fips: row for row in inputs.places_rows}
    if len(place_map) != len(inputs.places_rows) or not set(place_map).issubset(universe):
        raise IntegrityError("PLACES canonical rows duplicate or escape county universe")

    assigned_providers = [row for row in providers if row.assigned]
    county_rows: list[Mapping[str, object]] = []
    rural_raw: list[float] = []
    for fips in sorted(universe):
        attributes = inputs.county_attributes[fips]
        missing = [
            field for field in ("state", "county", "population", "median_hh_income", "rucc_2023", "hpsa_primary_care")
            if field not in attributes
        ]
        if missing:
            raise IntegrityError(f"county attributes missing fields for {fips}: {missing}")
        rucc = _exact_int(attributes["rucc_2023"], f"RUCC for {fips}")
        if not 1 <= rucc <= 9:
            raise IntegrityError(f"invalid RUCC for {fips}")
        nearest = nearest_provider(*canonical_county_points[fips], assigned_providers)
        displayed_distance = public_one_decimal(nearest.distance_raw)
        rural = rucc >= 4
        if rural:
            rural_raw.append(nearest.distance_raw)
        place = place_map.get(fips)
        if place and place.chd_age_adjusted_prevalence_pct is not None:
            chd_decimal = Decimal(place.chd_age_adjusted_prevalence_pct)
            if chd_decimal < 0 or chd_decimal > 100:
                raise IntegrityError(f"PLACES CHD outside 0..100 for {fips}")
            chd = public_one_decimal(chd_decimal)
        else:
            chd = None
        state = str(attributes["state"])
        if STATE_FIPS.get(state) != fips[:2]:
            raise IntegrityError(f"county state/FIPS mismatch for {fips}")
        population = _exact_int(attributes["population"], f"population for {fips}")
        if population < 0:
            raise IntegrityError(f"negative county population for {fips}")
        median_income = attributes["median_hh_income"]
        if median_income is not None and _exact_int(median_income, f"median income for {fips}") < 0:
            raise IntegrityError(f"negative median income for {fips}")
        if type(attributes["hpsa_primary_care"]) is not bool:
            raise IntegrityError(f"HPSA flag is not boolean for {fips}")
        row = {
            "fips": fips,
            "state": state,
            "county": str(attributes["county"]),
            "population": population,
            "median_hh_income": (
                None if median_income is None else _exact_int(median_income, f"median income for {fips}")
            ),
            "rucc_2023": rucc,
            "rural": rural,
            "n_cardiologists": provider_counts[fips],
            "miles_to_nearest_cardiologist": displayed_distance,
            "n_hospitals": hospital_counts[fips],
            "n_cah": cah_counts[fips],
            "n_cah_20plus_miles_to_nearest_cardiologist": cah_gte_counts[fips],
            "hpsa_primary_care": bool(attributes["hpsa_primary_care"]),
            "chd_age_adjusted_prevalence_pct": chd,
        }
        if tuple(row) != COUNTY_PUBLIC_FIELDS:
            raise IntegrityError("internal county schema construction drift")
        county_rows.append(row)
    counties_json = public_counties_bytes(county_rows, universe)
    summary = build_summary(
        county_rows,
        counties_json,
        provider_locations_sha256=provider_hash,
        hospital_locations_sha256=hospital_hash,
        rural_raw_distances=rural_raw,
        cah=cah,
    )
    summary_json = public_summary_bytes(summary)
    return OfflineBuildArtifacts(
        provider_locations=tuple(providers),
        hospital_locations=tuple(hospitals),
        waterfall=waterfall,
        duplicate_audit=duplicate_audit,
        route_cross_tab=tuple(cross_tab),
        unique_address_counts={
            "provider_direct_unique": len(provider_direct_ids),
            "hospital_direct_unique": len(hospital_direct_ids),
            "combined_direct_unique": len(required_geocode_ids),
            "provider_d_geo_unique": len({row.address_id for row in providers}),
            "hospital_in_scope_unique": len({row.address_id for row in hospitals}),
        },
        entity_address_mapping_bytes=mapping_bytes,
        entity_address_mapping_sha256=hashlib.sha256(mapping_bytes).hexdigest(),
        provider_locations_bytes=provider_bytes,
        provider_locations_sha256=provider_hash,
        hospital_locations_bytes=hospital_bytes,
        hospital_locations_sha256=hospital_hash,
        hospital_county_name_qa_bytes=hospital_county_qa,
        hospital_county_name_qa_sha256=hashlib.sha256(hospital_county_qa).hexdigest(),
        provider_hash_used_for_counts=provider_hash,
        provider_hash_used_for_distances=provider_hash,
        hospital_hash_used_for_counts=hospital_hash,
        hospital_hash_used_for_cah_distances=hospital_hash,
        county_rows=tuple(county_rows),
        counties_json=counties_json,
        summary=summary,
        summary_json=summary_json,
        cah=cah,
        quality_gates=(),
        geography_diagnostics=(),
    )
