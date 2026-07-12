"""Exact numerical gates and preregistered sensitivity escalation checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Mapping

from .addresses import NormalizedAddress
from .contracts import GATES, STATE_FIPS
from .errors import ContractError, IntegrityError
from .geocoder import CanonicalGeocode
from .hud import HudCandidate, assign_hud
from .canonical import conventional_median, haversine_miles, quantize_coordinate


@dataclass(frozen=True)
class RateGate:
    name: str
    numerator: int
    denominator: int
    ratio: Fraction
    operator: str
    threshold: Fraction
    passed: bool


@dataclass(frozen=True)
class RateDiagnostic:
    """Exact route-composition rate that cannot pass or fail a release."""

    name: str
    numerator: int
    denominator: int
    ratio: Fraction


def rate_gate(
    name: str,
    numerator: int,
    denominator: int,
    *,
    operator: str,
    threshold: Fraction,
) -> RateGate:
    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise ContractError(f"invalid rate counts for {name}")
    ratio = Fraction(numerator, denominator)
    if operator == ">=":
        passed = ratio >= threshold
    elif operator == "<=":
        passed = ratio <= threshold
    elif operator == "==":
        passed = ratio == threshold
    else:
        raise ContractError(f"unsupported gate operator: {operator}")
    return RateGate(name, numerator, denominator, ratio, operator, threshold, passed)


def evaluate_geography_gates(
    *,
    provider_direct: int,
    provider_hud: int,
    provider_unmatched: int,
    provider_total: int,
    hospital_direct: int,
    hospital_hud: int,
    hospital_unmatched: int,
    hospital_total: int,
    cah_unmatched: int,
    cah_total: int,
) -> tuple[RateGate, ...]:
    if provider_direct + provider_hud + provider_unmatched != provider_total:
        raise IntegrityError("provider route accounting failed")
    if hospital_direct + hospital_hud + hospital_unmatched != hospital_total:
        raise IntegrityError("hospital route accounting failed")
    if cah_unmatched < 0 or cah_total < 0 or cah_unmatched > cah_total:
        raise IntegrityError("CAH route accounting failed")
    if cah_total > hospital_total:
        raise IntegrityError("CAH scope exceeds hospital scope")
    thresholds = {
        "provider_unmatched": Fraction(str(GATES.provider_unmatched_max)),
        "hospital_unmatched": Fraction(str(GATES.hospital_unmatched_max)),
        "cah_unmatched": Fraction(str(GATES.cah_unmatched_max)),
    }
    return (
        rate_gate("provider_unmatched", provider_unmatched, provider_total, operator="<=", threshold=thresholds["provider_unmatched"]),
        rate_gate("hospital_unmatched", hospital_unmatched, hospital_total, operator="<=", threshold=thresholds["hospital_unmatched"]),
        rate_gate("cah_unmatched", cah_unmatched, cah_total, operator="<=", threshold=thresholds["cah_unmatched"]),
    )


def evaluate_geography_diagnostics(
    *,
    provider_direct: int,
    provider_hud: int,
    provider_unmatched: int,
    provider_total: int,
    provider_ties: int,
    submitted_unique_addresses: int,
    hospital_direct: int,
    hospital_hud: int,
    hospital_unmatched: int,
    hospital_total: int,
) -> tuple[RateDiagnostic, ...]:
    """Return the frozen direct/HUD/Tie diagnostics without release semantics."""

    if provider_direct + provider_hud + provider_unmatched != provider_total:
        raise IntegrityError("provider diagnostic route accounting failed")
    if hospital_direct + hospital_hud + hospital_unmatched != hospital_total:
        raise IntegrityError("hospital diagnostic route accounting failed")
    if (
        submitted_unique_addresses <= 0
        or provider_ties < 0
        or provider_ties > submitted_unique_addresses
    ):
        raise ContractError("invalid provider Census Tie diagnostic counts")

    def diagnostic(name: str, numerator: int, denominator: int) -> RateDiagnostic:
        if denominator <= 0 or numerator < 0 or numerator > denominator:
            raise ContractError(f"invalid diagnostic rate counts for {name}")
        return RateDiagnostic(name, numerator, denominator, Fraction(numerator, denominator))

    return (
        diagnostic("provider_direct", provider_direct, provider_total),
        diagnostic("provider_hud", provider_hud, provider_total),
        diagnostic("provider_census_tie_unique_address", provider_ties, submitted_unique_addresses),
        diagnostic("hospital_direct", hospital_direct, hospital_total),
        diagnostic("hospital_hud", hospital_hud, hospital_total),
    )


@dataclass(frozen=True)
class SensitivitySummary:
    name: str
    assignment_total: int
    unmatched_total: int
    provider_total: int
    provider_assignment_total: int
    provider_unmatched_total: int
    hospital_total: int
    hospital_assignment_total: int
    hospital_unmatched_total: int
    cah_total: int
    county_assignment_discordance: int
    cross_state_selections: int
    zero_provider_count: int
    zero_provider_pct: Decimal
    rural_median_distance_miles: Decimal
    cah_gte_20: int
    cah_evaluable: int


@dataclass(frozen=True)
class SensitivityComparison:
    name: str
    assignment_total_delta: int
    unmatched_total_delta: int
    provider_unmatched_delta: int
    hospital_unmatched_delta: int
    zero_provider_count_delta: int
    county_assignment_discordance: int
    cross_state_selection_delta: int
    cah_gte_20_delta: int
    cah_evaluable_delta: int
    zero_provider_delta_pp: Decimal
    rural_median_delta_miles: Decimal
    zero_provider_escalation: bool
    rural_median_escalation: bool


def compare_sensitivity(
    primary: SensitivitySummary, alternate: SensitivitySummary
) -> SensitivityComparison:
    if primary.name != "PRIMARY":
        raise ContractError("sensitivity comparison requires PRIMARY baseline")
    zero_delta = abs(alternate.zero_provider_pct - primary.zero_provider_pct)
    rural_delta = abs(
        alternate.rural_median_distance_miles - primary.rural_median_distance_miles
    )
    return SensitivityComparison(
        alternate.name,
        alternate.assignment_total - primary.assignment_total,
        alternate.unmatched_total - primary.unmatched_total,
        alternate.provider_unmatched_total - primary.provider_unmatched_total,
        alternate.hospital_unmatched_total - primary.hospital_unmatched_total,
        alternate.zero_provider_count - primary.zero_provider_count,
        alternate.county_assignment_discordance,
        alternate.cross_state_selections - primary.cross_state_selections,
        alternate.cah_gte_20 - primary.cah_gte_20,
        alternate.cah_evaluable - primary.cah_evaluable,
        zero_delta,
        rural_delta,
        zero_delta > Decimal("2.0"),
        rural_delta > Decimal("5.0"),
    )


def require_sensitivity_set(rows: Mapping[str, SensitivitySummary]) -> None:
    required = {
        "PRIMARY",
        "DIRECT_ONLY",
        "HUD_RES_SAME_STATE",
        "HUD_TOT_SAME_STATE",
        "HUD_BUS_STATE_UNRESTRICTED",
    }
    if set(rows) != required or any(rows[name].name != name for name in required):
        raise IntegrityError("mandatory sensitivity set is incomplete or mislabeled")
    primary = rows["PRIMARY"]
    denominators = (primary.provider_total, primary.hospital_total, primary.cah_total)
    if any(
        (row.provider_total, row.hospital_total, row.cah_total) != denominators
        for row in rows.values()
    ):
        raise IntegrityError("sensitivity entity denominators differ across analyses")


@dataclass(frozen=True)
class SensitivityRoute:
    entity_id: str
    address_id: str
    analysis: str
    county_fips: str | None
    latitude: str | None
    longitude: str | None
    assignment_method: str
    cross_state: bool
    direct_eligible: bool
    hud_eligible: bool
    census_outcome: str
    census_source_response_sha256: str | None
    hud_attempted: bool
    hud_outcome: str
    hud_ratio_basis: str | None
    hud_selected_ratio: str | None
    hud_bus_ratio: str | None
    hud_res_ratio: str | None
    hud_tot_ratio: str | None
    hud_candidate_count: int | None
    hud_second_highest_ratio: str | None
    hud_margin_to_second: str | None
    final_disposition: str
    hud_source_snapshot_id: str | None
    gazetteer_source_snapshot_id: str | None


def _validate_sensitivity_route(route: SensitivityRoute) -> None:
    expected_basis = {
        "PRIMARY": "BUS_RATIO",
        "DIRECT_ONLY": None,
        "HUD_RES_SAME_STATE": "RES_RATIO",
        "HUD_TOT_SAME_STATE": "TOT_RATIO",
        "HUD_BUS_STATE_UNRESTRICTED": "BUS_RATIO",
    }.get(route.analysis)
    if route.analysis not in {
        "PRIMARY", "DIRECT_ONLY", "HUD_RES_SAME_STATE", "HUD_TOT_SAME_STATE",
        "HUD_BUS_STATE_UNRESTRICTED",
    }:
        raise IntegrityError("unknown sensitivity analysis")
    for value in (
        route.census_source_response_sha256, route.hud_source_snapshot_id,
        route.gazetteer_source_snapshot_id,
    ):
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise IntegrityError("sensitivity source identity is not SHA-256")
    if route.direct_eligible != (route.census_outcome != "NOT_SUBMITTED"):
        raise IntegrityError("sensitivity Census eligibility/outcome mismatch")
    if (route.census_source_response_sha256 is not None) != route.direct_eligible:
        raise IntegrityError("sensitivity Census source identity mismatch")
    if route.hud_attempted != (route.hud_outcome != "NOT_ATTEMPTED"):
        raise IntegrityError("sensitivity HUD attempt/outcome mismatch")
    if (route.hud_source_snapshot_id is not None) != route.hud_attempted:
        raise IntegrityError("sensitivity HUD source identity mismatch")
    if route.hud_attempted and (
        not route.hud_eligible or route.census_outcome == "Match"
        or route.analysis == "DIRECT_ONLY" or route.hud_ratio_basis != expected_basis
        or route.hud_candidate_count is None or route.hud_candidate_count < 0
    ):
        raise IntegrityError("sensitivity HUD eligibility/basis/count mismatch")
    if route.hud_attempted:
        decimal_fields = {
            name: (None if value is None else Decimal(value))
            for name, value in {
                "selected": route.hud_selected_ratio,
                "bus": route.hud_bus_ratio,
                "res": route.hud_res_ratio,
                "tot": route.hud_tot_ratio,
                "second": route.hud_second_highest_ratio,
                "margin": route.hud_margin_to_second,
            }.items()
        }
        if any(value is not None and not value.is_finite() for value in decimal_fields.values()):
            raise IntegrityError("sensitivity HUD decimal provenance is nonfinite")
        selected = decimal_fields["selected"]
        second = decimal_fields["second"]
        margin = decimal_fields["margin"]
        if (second is None) != (margin is None) or (
            second is not None
            and (selected is None or second > selected or margin != selected - second)
        ):
            raise IntegrityError("sensitivity HUD selected/second/margin mismatch")
        tie_code = {
            "BUS_RATIO": "HUD_BUS_TIE",
            "RES_RATIO": "HUD_RES_TIE",
            "TOT_RATIO": "HUD_TOT_TIE",
        }[route.hud_ratio_basis]
        if route.hud_outcome == tie_code and (
            selected is None or selected <= 0 or second != selected
            or margin != 0 or route.hud_candidate_count < 2
        ):
            raise IntegrityError("sensitivity HUD tie provenance is not reconstructable")
    if not route.hud_attempted and any(
        value is not None for value in (
            route.hud_ratio_basis, route.hud_selected_ratio, route.hud_bus_ratio,
            route.hud_res_ratio, route.hud_tot_ratio, route.hud_candidate_count,
            route.hud_second_highest_ratio, route.hud_margin_to_second,
        )
    ):
        raise IntegrityError("non-attempted sensitivity route carries HUD provenance")
    assigned = route.county_fips is not None
    if assigned != all(value is not None for value in (route.latitude, route.longitude)):
        raise IntegrityError("sensitivity assigned coordinate completeness mismatch")
    if route.final_disposition == "assigned_direct_census":
        if route.census_outcome != "Match" or route.hud_attempted or not assigned:
            raise IntegrityError("sensitivity direct route is not reconstructable")
    elif route.final_disposition == "assigned_hud_fallback":
        chosen = {
            "BUS_RATIO": route.hud_bus_ratio,
            "RES_RATIO": route.hud_res_ratio,
            "TOT_RATIO": route.hud_tot_ratio,
        }[route.hud_ratio_basis]
        if (
            not assigned or route.hud_outcome != "HUD_ASSIGNED"
            or route.gazetteer_source_snapshot_id is None
            or route.hud_selected_ratio is None
            or route.hud_selected_ratio != chosen
        ):
            raise IntegrityError("sensitivity HUD route is not reconstructable")
    elif route.final_disposition == "final_unmatched":
        if assigned or route.gazetteer_source_snapshot_id is not None:
            raise IntegrityError("sensitivity unmatched route carries geography")
    else:
        raise IntegrityError("unknown sensitivity final disposition")
    if route.analysis != "HUD_BUS_STATE_UNRESTRICTED" and route.cross_state:
        raise IntegrityError("cross-state selection outside unrestricted sensitivity")


def route_sensitivities(
    entities: Mapping[str, NormalizedAddress],
    geocodes: Mapping[str, CanonicalGeocode],
    hud_candidates: tuple[HudCandidate, ...],
    county_points: Mapping[str, tuple[str, str]],
    *,
    hud_source_snapshot_id: str,
    gazetteer_source_snapshot_id: str,
) -> dict[str, tuple[SensitivityRoute, ...]]:
    """Apply the five fixed route analyses without choosing among results."""

    names = (
        "PRIMARY",
        "DIRECT_ONLY",
        "HUD_RES_SAME_STATE",
        "HUD_TOT_SAME_STATE",
        "HUD_BUS_STATE_UNRESTRICTED",
    )
    output: dict[str, list[SensitivityRoute]] = {name: [] for name in names}
    for entity_id, address in sorted(entities.items()):
        expected_state_fips = STATE_FIPS.get(address.state)
        if address.out_of_scope_reason or expected_state_fips is None:
            raise IntegrityError(
                f"out-of-scope entity reached sensitivity routing: {entity_id}"
            )
        direct = geocodes.get(address.address_id) if address.direct_eligible else None
        if address.direct_eligible and direct is None:
            raise IntegrityError(f"missing sensitivity geocoder row: {address.address_id}")
        if not address.direct_eligible and address.address_id in geocodes:
            raise IntegrityError(
                f"direct-ineligible sensitivity entity has a geocoder row: {address.address_id}"
            )
        if direct is not None and direct.outcome == "Match":
            if (
                direct.county_fips not in county_points
                or direct.county_fips[:2] != expected_state_fips
                or direct.latitude is None
                or direct.longitude is None
            ):
                raise IntegrityError("sensitivity direct Match violates canonical scope")
            for name in names:
                route = SensitivityRoute(
                    entity_id=entity_id,
                    address_id=address.address_id,
                    analysis=name,
                    county_fips=direct.county_fips,
                    latitude=quantize_coordinate(direct.latitude, latitude=True),
                    longitude=quantize_coordinate(direct.longitude, latitude=False),
                    assignment_method="CENSUS_MATCH",
                    cross_state=False,
                    direct_eligible=True,
                    hud_eligible=address.hud_eligible,
                    census_outcome="Match",
                    census_source_response_sha256=direct.source_response_sha256,
                    hud_attempted=False,
                    hud_outcome="NOT_ATTEMPTED",
                    hud_ratio_basis=None,
                    hud_selected_ratio=None,
                    hud_bus_ratio=None,
                    hud_res_ratio=None,
                    hud_tot_ratio=None,
                    hud_candidate_count=None,
                    hud_second_highest_ratio=None,
                    hud_margin_to_second=None,
                    final_disposition="assigned_direct_census",
                    hud_source_snapshot_id=None,
                    gazetteer_source_snapshot_id=None,
                )
                _validate_sensitivity_route(route)
                output[name].append(route)
            continue
        direct_only = SensitivityRoute(
            entity_id=entity_id,
            address_id=address.address_id,
            analysis="DIRECT_ONLY",
            county_fips=None,
            latitude=None,
            longitude=None,
            assignment_method="UNMATCHED",
            cross_state=False,
            direct_eligible=address.direct_eligible,
            hud_eligible=address.hud_eligible,
            census_outcome=(direct.outcome if direct else "NOT_SUBMITTED"),
            census_source_response_sha256=(
                direct.source_response_sha256 if direct else None
            ),
            hud_attempted=False,
            hud_outcome="NOT_ATTEMPTED",
            hud_ratio_basis=None,
            hud_selected_ratio=None,
            hud_bus_ratio=None,
            hud_res_ratio=None,
            hud_tot_ratio=None,
            hud_candidate_count=None,
            hud_second_highest_ratio=None,
            hud_margin_to_second=None,
            final_disposition="final_unmatched",
            hud_source_snapshot_id=None,
            gazetteer_source_snapshot_id=None,
        )
        _validate_sensitivity_route(direct_only)
        output["DIRECT_ONLY"].append(direct_only)
        variants = {
            "PRIMARY": ("BUS_RATIO", True),
            "HUD_RES_SAME_STATE": ("RES_RATIO", True),
            "HUD_TOT_SAME_STATE": ("TOT_RATIO", True),
            "HUD_BUS_STATE_UNRESTRICTED": ("BUS_RATIO", False),
        }
        for name, (basis, restrict_state) in variants.items():
            assignment = (
                assign_hud(
                    address.zip5,
                    address.state,
                    hud_candidates,
                    basis=basis,
                    restrict_state=restrict_state,
                )
                if address.hud_eligible
                else None
            )
            if assignment is None:
                route = SensitivityRoute(
                    entity_id=entity_id,
                    address_id=address.address_id,
                    analysis=name,
                    county_fips=None,
                    latitude=None,
                    longitude=None,
                    assignment_method="NO_VALID_ZIP_FOR_HUD",
                    cross_state=False,
                    direct_eligible=address.direct_eligible,
                    hud_eligible=False,
                    census_outcome=(direct.outcome if direct else "NOT_SUBMITTED"),
                    census_source_response_sha256=(
                        direct.source_response_sha256 if direct else None
                    ),
                    hud_attempted=False,
                    hud_outcome="NOT_ATTEMPTED",
                    hud_ratio_basis=None,
                    hud_selected_ratio=None,
                    hud_bus_ratio=None,
                    hud_res_ratio=None,
                    hud_tot_ratio=None,
                    hud_candidate_count=None,
                    hud_second_highest_ratio=None,
                    hud_margin_to_second=None,
                    final_disposition="final_unmatched",
                    hud_source_snapshot_id=None,
                    gazetteer_source_snapshot_id=None,
                )
                _validate_sensitivity_route(route)
                output[name].append(route)
                continue
            county = assignment.county_fips if assignment.outcome == "HUD_ASSIGNED" else None
            # Compare FIPS prefixes through STATE_FIPS without treating a postal
            # preferred-state field as county state.
            cross_state = False
            latitude = longitude = None
            if county:
                cross_state = county[:2] != expected_state_fips
                if county not in county_points:
                    raise IntegrityError(f"missing sensitivity county internal point: {county}")
                latitude = quantize_coordinate(county_points[county][0], latitude=True)
                longitude = quantize_coordinate(county_points[county][1], latitude=False)
            if county and name == "PRIMARY":
                method = "HUD_BUS_RATIO_SAME_STATE_COUNTY_INTERNAL_POINT"
            elif county and restrict_state:
                method = f"HUD_{basis}_SAME_STATE_COUNTY_INTERNAL_POINT"
            elif county:
                method = f"HUD_{basis}_STATE_UNRESTRICTED_COUNTY_INTERNAL_POINT"
            else:
                method = assignment.outcome
            route = SensitivityRoute(
                entity_id=entity_id,
                address_id=address.address_id,
                analysis=name,
                county_fips=county,
                latitude=latitude,
                longitude=longitude,
                assignment_method=method,
                cross_state=cross_state,
                direct_eligible=address.direct_eligible,
                hud_eligible=address.hud_eligible,
                census_outcome=(direct.outcome if direct else "NOT_SUBMITTED"),
                census_source_response_sha256=(
                    direct.source_response_sha256 if direct else None
                ),
                hud_attempted=True,
                hud_outcome=assignment.outcome,
                hud_ratio_basis=assignment.ratio_basis,
                hud_selected_ratio=(
                    None if assignment.selected_ratio is None
                    else format(assignment.selected_ratio, "f")
                ),
                hud_bus_ratio=(
                    None if assignment.bus_ratio is None
                    else format(assignment.bus_ratio, "f")
                ),
                hud_res_ratio=(
                    None if assignment.res_ratio is None
                    else format(assignment.res_ratio, "f")
                ),
                hud_tot_ratio=(
                    None if assignment.tot_ratio is None
                    else format(assignment.tot_ratio, "f")
                ),
                hud_candidate_count=assignment.same_state_candidate_count,
                hud_second_highest_ratio=(
                    None if assignment.second_highest_ratio is None
                    else format(assignment.second_highest_ratio, "f")
                ),
                hud_margin_to_second=(
                    None if assignment.margin_to_second is None
                    else format(assignment.margin_to_second, "f")
                ),
                final_disposition=(
                    "assigned_hud_fallback" if county else "final_unmatched"
                ),
                hud_source_snapshot_id=hud_source_snapshot_id,
                gazetteer_source_snapshot_id=(
                    gazetteer_source_snapshot_id if county else None
                ),
            )
            _validate_sensitivity_route(route)
            output[name].append(route)
    return {name: tuple(rows) for name, rows in output.items()}


@dataclass(frozen=True)
class SensitivityEntity:
    route: SensitivityRoute
    entity_kind: str
    is_cah: bool = False


def require_sensitivity_entity_equality(
    rows: Mapping[str, tuple[SensitivityEntity, ...]],
) -> None:
    required = {
        "PRIMARY",
        "DIRECT_ONLY",
        "HUD_RES_SAME_STATE",
        "HUD_TOT_SAME_STATE",
        "HUD_BUS_STATE_UNRESTRICTED",
    }
    if set(rows) != required:
        raise IntegrityError("mandatory sensitivity entity tables are incomplete")
    signatures: dict[str, frozenset[tuple[str, str, str, bool]]] = {}
    for name, entities in rows.items():
        for entity in entities:
            _validate_sensitivity_route(entity.route)
        keys = [
            (
                entity.entity_kind,
                entity.route.entity_id,
                entity.route.address_id,
                entity.is_cah,
            )
            for entity in entities
        ]
        if len(keys) != len(set(keys)):
            raise IntegrityError(f"duplicate sensitivity entity in {name}")
        if any(entity.route.analysis != name for entity in entities):
            raise IntegrityError(f"sensitivity route analysis label mismatch in {name}")
        signatures[name] = frozenset(keys)
    primary = signatures["PRIMARY"]
    if any(signature != primary for signature in signatures.values()):
        raise IntegrityError("provider/hospital entity sets differ across sensitivities")


def summarize_sensitivity(
    analysis: str,
    entities: tuple[SensitivityEntity, ...],
    *,
    county_universe: set[str],
    county_points: Mapping[str, tuple[str, str]],
    rucc: Mapping[str, int],
    primary_assignments: Mapping[str, str | None],
) -> SensitivitySummary:
    relevant = [entity for entity in entities if entity.route.analysis == analysis]
    if len({entity.route.entity_id for entity in relevant}) != len(relevant):
        raise IntegrityError("duplicate sensitivity entity ID")
    if set(county_points) != county_universe or set(rucc) != county_universe:
        raise IntegrityError("sensitivity county context does not equal universe")
    providers = [entity.route for entity in relevant if entity.entity_kind == "P"]
    hospitals = [entity for entity in relevant if entity.entity_kind == "H"]
    if any(entity.entity_kind not in {"P", "H"} for entity in relevant):
        raise ContractError("sensitivity entity kind must be P or H")
    assigned_providers = [route for route in providers if route.county_fips is not None]
    if not assigned_providers:
        raise IntegrityError("sensitivity has no evaluable provider coordinates")
    provider_counties = Counter(route.county_fips for route in assigned_providers)
    zero_count = sum(provider_counties[fips] == 0 for fips in county_universe)

    def nearest_raw(latitude: str, longitude: str) -> float:
        candidates = []
        for route in assigned_providers:
            distance = haversine_miles(
                float(latitude), float(longitude), float(route.latitude), float(route.longitude)
            )
            candidates.append(
                (
                    distance,
                    Decimal(route.latitude),
                    Decimal(route.longitude),
                    route.entity_id,
                    route.address_id,
                )
            )
        return min(candidates)[0]

    rural_raw = [
        nearest_raw(
            quantize_coordinate(county_points[fips][0], latitude=True),
            quantize_coordinate(county_points[fips][1], latitude=False),
        )
        for fips in sorted(county_universe)
        if rucc[fips] >= 4
    ]
    if not rural_raw:
        raise IntegrityError("sensitivity rural median denominator is zero")
    cah = [entity.route for entity in hospitals if entity.is_cah]
    cah_evaluable = [route for route in cah if route.county_fips is not None]
    cah_gte = sum(nearest_raw(route.latitude, route.longitude) >= 20.0 for route in cah_evaluable)
    discordance = sum(
        primary_assignments.get(entity.route.entity_id) != entity.route.county_fips
        for entity in relevant
    )
    assigned = sum(entity.route.county_fips is not None for entity in relevant)
    return SensitivitySummary(
        name=analysis,
        assignment_total=assigned,
        unmatched_total=len(relevant) - assigned,
        provider_total=len(providers),
        provider_assignment_total=len(assigned_providers),
        provider_unmatched_total=len(providers) - len(assigned_providers),
        hospital_total=len(hospitals),
        hospital_assignment_total=sum(
            entity.route.county_fips is not None for entity in hospitals
        ),
        hospital_unmatched_total=sum(
            entity.route.county_fips is None for entity in hospitals
        ),
        cah_total=len(cah),
        county_assignment_discordance=discordance,
        cross_state_selections=sum(entity.route.cross_state for entity in relevant),
        zero_provider_count=zero_count,
        zero_provider_pct=Decimal(zero_count * 100) / Decimal(len(county_universe)),
        rural_median_distance_miles=Decimal(repr(conventional_median(rural_raw))),
        cah_gte_20=cah_gte,
        cah_evaluable=len(cah_evaluable),
    )


@dataclass(frozen=True)
class SensitivityReport:
    summaries: Mapping[str, SensitivitySummary]
    comparisons: tuple[SensitivityComparison, ...]
    table_bytes: Mapping[str, bytes]
    table_sha256: Mapping[str, str]
    canonical_bytes: bytes
    sha256: str


def build_sensitivity_report(
    entities_by_analysis: Mapping[str, tuple[SensitivityEntity, ...]],
    *,
    county_universe: set[str],
    county_points: Mapping[str, tuple[str, str]],
    rucc: Mapping[str, int],
    primary_assignments: Mapping[str, str | None],
) -> SensitivityReport:
    require_sensitivity_entity_equality(entities_by_analysis)
    primary_ids = {entity.route.entity_id for entity in entities_by_analysis["PRIMARY"]}
    if set(primary_assignments) != primary_ids:
        raise IntegrityError("PRIMARY assignment keys do not equal sensitivity entities")
    summaries = {
        name: summarize_sensitivity(
            name,
            entities,
            county_universe=county_universe,
            county_points=county_points,
            rucc=rucc,
            primary_assignments=primary_assignments,
        )
        for name, entities in sorted(entities_by_analysis.items())
    }
    require_sensitivity_set(summaries)
    comparisons = tuple(
        compare_sensitivity(summaries["PRIMARY"], summaries[name])
        for name in (
            "DIRECT_ONLY",
            "HUD_RES_SAME_STATE",
            "HUD_TOT_SAME_STATE",
            "HUD_BUS_STATE_UNRESTRICTED",
        )
    )

    table_bytes: dict[str, bytes] = {}
    table_sha256: dict[str, str] = {}
    table_receipts: list[dict[str, object]] = []
    for name in sorted(entities_by_analysis):
        lines: list[str] = []
        for entity in sorted(
            entities_by_analysis[name],
            key=lambda item: (
                item.entity_kind,
                item.route.entity_id,
                item.route.address_id,
            ),
        ):
            row = {
                "analysis": entity.route.analysis,
                "entity_kind": entity.entity_kind,
                "entity_id": entity.route.entity_id,
                "address_id": entity.route.address_id,
                "is_cah": entity.is_cah,
                "county_fips": entity.route.county_fips,
                "latitude": entity.route.latitude,
                "longitude": entity.route.longitude,
                "assignment_method": entity.route.assignment_method,
                "cross_state": entity.route.cross_state,
                "direct_eligible": entity.route.direct_eligible,
                "hud_eligible": entity.route.hud_eligible,
                "census_outcome": entity.route.census_outcome,
                "census_source_response_sha256": entity.route.census_source_response_sha256,
                "hud_attempted": entity.route.hud_attempted,
                "hud_outcome": entity.route.hud_outcome,
                "hud_ratio_basis": entity.route.hud_ratio_basis,
                "hud_selected_ratio": entity.route.hud_selected_ratio,
                "hud_bus_ratio": entity.route.hud_bus_ratio,
                "hud_res_ratio": entity.route.hud_res_ratio,
                "hud_tot_ratio": entity.route.hud_tot_ratio,
                "hud_candidate_count": entity.route.hud_candidate_count,
                "hud_second_highest_ratio": entity.route.hud_second_highest_ratio,
                "hud_margin_to_second": entity.route.hud_margin_to_second,
                "final_disposition": entity.route.final_disposition,
                "hud_source_snapshot_id": entity.route.hud_source_snapshot_id,
                "gazetteer_source_snapshot_id": entity.route.gazetteer_source_snapshot_id,
            }
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        body = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        table_bytes[name] = body
        table_sha256[name] = digest
        summary = summaries[name]
        table_receipts.append(
            {
                "name": name,
                "bytes": len(body),
                "sha256": digest,
                "provider_total": summary.provider_total,
                "hospital_total": summary.hospital_total,
                "cah_total": summary.cah_total,
            }
        )

    def jsonable(value: object) -> object:
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, dict):
            return {key: jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [jsonable(item) for item in value]
        return value

    payload = {
        "tables": table_receipts,
        "summaries": [jsonable(asdict(summaries[name])) for name in sorted(summaries)],
        "comparisons": [jsonable(asdict(comparison)) for comparison in comparisons],
    }
    canonical = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    return SensitivityReport(
        summaries,
        comparisons,
        table_bytes,
        table_sha256,
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )
