"""Strict Phase-2F bundle adapter for the existing offline analytical build."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .acquisition import canonical_manifest_bytes
from .build import BuildExpectations, OfflineBuildInputs
from .bundle import FrozenBundle, _load_json_bytes, _require_exact_keys
from .contracts import FIPS_TO_STATE, METHOD_LINEAGE_ID
from .errors import IntegrityError
from .geocoder import CanonicalGeocode, validate_canonical_geocodes
from .gazetteer_provenance import verify_gazetteer_provenance
from .hospitals import deduplicate_hospitals, in_scope_hospitals
from .hud import parse_candidates
from .places import PlacesCountyRow
from .source_tables import (
    parse_acs_income,
    parse_acs_population,
    parse_gazetteer,
    parse_hrsa_hpsa,
    parse_rucc,
)
from .streaming_nppes import iter_selected_source_rows
from .nppes import classify_disposition

FAILED_R3_SOURCE_TREE_ID = (
    "d1cd35bb5001c6c5d400e848651fe67506b35c38f60fd88b30839e4ed2c7fa97"
)

PHASE2F_ANALYTICAL_ROLES = {
    "nppes_selected",
    "nppes_dispositions",
    "nppes_audit",
    "nppes_stream_receipt",
    "cms_hospitals",
    "hud",
    "acs_population",
    "acs_income",
    "gazetteer",
    "rucc",
    "hrsa",
    "places_canonical",
    "geocoder_canonical",
    "adjacency_canonical",
    "county_topology",
    "state_topology",
}

PHASE2F_CHAIN_SCALAR_ROLES_R3 = {
    "provider_mapping",
    "hospital_mapping",
    "request_manifest",
    "d2ii_handoff_receipt",
    "places_derivation_handoff",
    "places_handoff_receipt",
    "places_source_acquisition_receipt",
    "places_raw",
    "census_attempts_manifest",
    "census_handoff_receipt",
    "hud_compatibility",
    "hud_handoff",
    "hud_handoff_receipt",
    "hud_source_acquisition_receipt",
    "hud_raw_xlsx",
    "tiger_raw_zip",
    "tiger_candidate_manifest",
    "tiger_manifest_approval",
    "gazetteer_raw_zip",
    "gazetteer_candidate_manifest",
    "gazetteer_extraction_receipt",
}
PHASE2F_CHAIN_SCALAR_ROLES = (
    PHASE2F_CHAIN_SCALAR_ROLES_R3
    - {
        "hud_compatibility", "hud_handoff", "hud_handoff_receipt",
        "hud_source_acquisition_receipt", "hud_raw_xlsx",
    }
    | {
        "hud_api_compatibility", "hud_api_handoff",
        "hud_api_handoff_receipt", "hud_api_acquisition_receipt",
        "hud_api_import_receipt", "hud_api_raw_json", "hud_api_excluded_rows",
    }
)
PHASE2F_CHAIN_LIST_ROLES = {
    "request_chunks",
    "census_attempt_metadata",
    "census_attempt_responses",
}
PHASE2F_ROLES = (
    PHASE2F_ANALYTICAL_ROLES
    | PHASE2F_CHAIN_SCALAR_ROLES
    | PHASE2F_CHAIN_LIST_ROLES
)


def _roles(bundle: FrozenBundle, *, require_chain: bool) -> dict[str, object]:
    value = bundle.expectation("source_roles", dict)
    required = PHASE2F_ANALYTICAL_ROLES
    if require_chain:
        method_lineage_id = bundle.expectation("method_lineage_id", str)
        if method_lineage_id != METHOD_LINEAGE_ID:
            raise IntegrityError("Phase-2F method lineage is not Amendment-03 R4")
        if bundle.source_tree_id == FAILED_R3_SOURCE_TREE_ID:
            raise IntegrityError("failed R3 source tree cannot enter the R4 build")
        if set(value) != PHASE2F_ROLES:
            raise IntegrityError("R4 Phase-2F requires the exact HUD API role set")
    if not require_chain and set(value) != required:
        raise IntegrityError("Phase-2F source_roles are incomplete or malformed")
    active_chain_roles = (
        set(value) - PHASE2F_ANALYTICAL_ROLES - PHASE2F_CHAIN_LIST_ROLES
        if require_chain else set()
    )
    scalar_roles = PHASE2F_ANALYTICAL_ROLES | (
        active_chain_roles if require_chain else set()
    )
    if any(not isinstance(value[role], str) for role in scalar_roles):
        raise IntegrityError("Phase-2F scalar source role is not a source ID")
    if require_chain:
        for role in PHASE2F_CHAIN_LIST_ROLES:
            items = value[role]
            if (
                not isinstance(items, list)
                or not items
                or (
                    role == "census_attempt_responses"
                    and any(item is not None and not isinstance(item, str) for item in items)
                )
                or (
                    role != "census_attempt_responses"
                    and any(not isinstance(item, str) for item in items)
                )
            ):
                raise IntegrityError(f"Phase-2F list source role is malformed: {role}")
        if len(value["census_attempt_metadata"]) != len(value["census_attempt_responses"]):
            raise IntegrityError("Census attempt metadata/response role lengths differ")
    source_ids = [value[role] for role in scalar_roles]
    if require_chain:
        source_ids.extend(value["request_chunks"])
        source_ids.extend(value["census_attempt_metadata"])
        source_ids.extend(
            item for item in value["census_attempt_responses"] if item is not None
        )
    if len(source_ids) != len(set(source_ids)):
        raise IntegrityError("Phase-2F source role IDs are reused")
    if require_chain and set(source_ids) != set(bundle.inputs):
        raise IntegrityError("Phase-2F source roles do not exhaust the frozen bundle inputs")
    bundle.require_sources(source_ids)
    if require_chain:
        for source_id in source_ids:
            bundle.validate_registered_schema(source_id)
    return dict(value)


def _load_single_json(bundle: FrozenBundle, source_id: str) -> dict[str, object]:
    value = _load_json_bytes(bundle.verify_file(source_id).read_bytes())
    if not isinstance(value, dict):
        raise IntegrityError(f"bundle JSON source is not an object: {source_id}")
    return value


def _validate_nppes_chain(bundle: FrozenBundle, roles: dict[str, str]) -> tuple[dict[str, str], ...]:
    receipt = _load_single_json(bundle, roles["nppes_stream_receipt"])
    _require_exact_keys(
        receipt,
        ("schema_version", "source_snapshot_id", "database_schema", "artifacts"),
        "NPPES stream receipt",
    )
    if receipt["schema_version"] != "atlas-v19-nppes-stream-receipt-1":
        raise IntegrityError("NPPES stream receipt schema mismatch")
    artifacts = receipt["artifacts"]
    expected_roles = {
        "nppes_selected": "nppes_selected",
        "nppes_dispositions": "nppes_dispositions",
        "nppes_audit": "nppes_audit",
    }
    if not isinstance(artifacts, dict) or set(artifacts) != set(expected_roles):
        raise IntegrityError("NPPES stream receipt artifact set mismatch")
    for artifact_id, role in expected_roles.items():
        identity = artifacts[artifact_id]
        if not isinstance(identity, dict) or tuple(identity) != ("bytes", "sha256"):
            raise IntegrityError("NPPES stream artifact identity schema mismatch")
        entry = bundle.inputs[roles[role]]
        if (identity["bytes"], identity["sha256"]) != (entry.bytes, entry.sha256):
            raise IntegrityError("NPPES stream receipt/bundle artifact mismatch")

    audit = _load_single_json(bundle, roles["nppes_audit"])
    _require_exact_keys(
        audit,
        (
            "schema_version", "source_snapshot_id", "raw_rows",
            "retained_unique_npis", "identical_duplicate_extra_rows",
            "selected_unique_npis", "pre_geography_disposition_counts",
        ),
        "NPPES audit",
    )
    if audit["raw_rows"] != audit["retained_unique_npis"] + audit["identical_duplicate_extra_rows"]:
        raise IntegrityError("NPPES audit raw/duplicate equation failed")
    rows = tuple(iter_selected_source_rows(bundle.verify_file(roles["nppes_selected"])))
    if len(rows) != audit["selected_unique_npis"]:
        raise IntegrityError("NPPES selected row count differs from frozen audit")
    with bundle.verify_file(roles["nppes_dispositions"]).open("rb") as handle:
        disposition_count = sum(1 for _ in handle)
    if disposition_count != audit["retained_unique_npis"]:
        raise IntegrityError("NPPES disposition row count differs from frozen audit")
    return rows


def load_phase2f_inputs(
    bundle: FrozenBundle,
    *,
    require_chain: bool = True,
) -> tuple[
    OfflineBuildInputs,
    BuildExpectations,
    bytes,
    tuple[dict[str, object], ...],
    bytes,
    bytes,
]:
    if bundle.phase != "2F":
        raise IntegrityError("offline build requires a Phase-2F bundle")
    roles = _roles(bundle, require_chain=require_chain)
    if not require_chain:
        for role in PHASE2F_ANALYTICAL_ROLES:
            source_id = roles[role]
            bundle.validate_registered_schema(source_id)
    universe_list = bundle.expectation("county_universe", list)
    if not all(isinstance(fips, str) for fips in universe_list):
        raise IntegrityError("county_universe must be a string array")
    universe = set(universe_list)
    if len(universe) != len(universe_list):
        raise IntegrityError("county_universe contains duplicates")
    expected_count = bundle.expectation("county_count", int)
    if len(universe) != expected_count:
        raise IntegrityError("approved county universe/count mismatch")

    if require_chain:
        # This must precede parse_gazetteer/OfflineBuildInputs construction:
        # HUD fallback and distance calculations may only see points rederived
        # byte-for-byte from the approved raw COUNTY-GAZ-2025 member.
        gazetteer_rows = verify_gazetteer_provenance(
            bundle,
            roles,
            county_universe=universe,
            expected_count=expected_count,
        )
    else:
        gazetteer_rows = list(bundle.iter_csv(roles["gazetteer"]))
    county_points = parse_gazetteer(gazetteer_rows, expected_count=expected_count)
    if set(county_points) != universe:
        raise IntegrityError("Gazetteer rows differ from approved county universe")
    names = {
        row["GEOID"]: (row["USPS"], row["NAME"])
        for row in gazetteer_rows
        if row["GEOID"] in universe
    }
    population_total = bundle.expectation("population_total", int)
    populations = parse_acs_population(
        bundle.iter_csv(roles["acs_population"]),
        universe,
        expected_total=population_total,
    )
    incomes = parse_acs_income(bundle.iter_csv(roles["acs_income"]), universe)
    rucc = parse_rucc(bundle.iter_csv(roles["rucc"]), universe)
    hpsa, _hpsa_audit = parse_hrsa_hpsa(bundle.iter_csv(roles["hrsa"]), universe)
    county_attributes = {
        fips: {
            "state": names[fips][0],
            "county": names[fips][1],
            "population": populations[fips],
            "median_hh_income": incomes[fips],
            "rucc_2023": rucc[fips],
            "hpsa_primary_care": hpsa[fips],
        }
        for fips in sorted(universe)
    }

    places_rows = tuple(
        PlacesCountyRow(**row)
        for row in bundle.iter_jsonl(roles["places_canonical"])
    )
    geocode_rows = tuple(
        CanonicalGeocode(**row)
        for row in bundle.iter_jsonl(roles["geocoder_canonical"])
    )
    nppes_rows = _validate_nppes_chain(bundle, roles)
    hospitals = tuple(bundle.iter_csv(roles["cms_hospitals"]))
    request_states: dict[str, str] = {}

    def register_request(address) -> None:
        if not address.direct_eligible:
            return
        prior = request_states.setdefault(address.address_id, address.state)
        if prior != address.state:
            raise IntegrityError("one geocoder request ID maps to multiple source states")

    for row in nppes_rows:
        disposition = classify_disposition(row)
        if disposition.bucket != "final_unmatched":
            raise IntegrityError("selected NPPES snapshot contains a pre-geography exclusion")
        register_request(disposition.address)
    for hospital in in_scope_hospitals(deduplicate_hospitals(hospitals)):
        register_request(hospital.address)
    geocodes = validate_canonical_geocodes(
        geocode_rows,
        expected_ids=set(request_states),
        request_states=request_states,
        county_universe=universe,
    )
    hud_rows = bundle.iter_csv(roles["hud"])
    if bundle.inputs[roles["hud"]].schema_id == "hud_zip_county_csv":
        # Historical R3 XLSX handoffs predate the API-only OTH/city QA fields.
        # Keep them readable without conflating their source identity with R4.
        hud_rows = (
            {
                **row,
                "OTH_RATIO": "0",
                "USPS_ZIP_PREF_CITY": "UNAVAILABLE_R3_XLSX",
            }
            for row in hud_rows
        )
    hud = tuple(parse_candidates(hud_rows, universe))
    frozen_inputs = bundle.frozen_inputs()
    input_manifest_sha256 = hashlib.sha256(
        canonical_manifest_bytes(list(frozen_inputs))
    ).hexdigest()
    inputs = OfflineBuildInputs(
        nppes_rows=nppes_rows,
        hospital_rows=hospitals,
        geocodes=geocodes,
        hud_candidates=hud,
        county_points=county_points,
        county_attributes=county_attributes,
        places_rows=places_rows,
        source_snapshot_id=input_manifest_sha256,
        hud_source_snapshot_id=bundle.inputs[roles["hud"]].sha256,
        gazetteer_source_snapshot_id=bundle.inputs[roles["gazetteer"]].sha256,
        method_lineage_id=(
            bundle.expectation("method_lineage_id", str)
            if require_chain
            else bundle.expectations.get("method_lineage_id", METHOD_LINEAGE_ID)
        ),
    )
    config = bundle.expectation("build_expectations", dict)
    required_keys = (
        "county_count", "population_total", "places_county_rows", "places_nonnull",
        "places_public_nulls", "places_snapshot_sha256", "input_manifest_sha256",
        "required_input_hashes", "method_lineage_id", "places_suppressed_fips",
    )
    _require_exact_keys(config, required_keys, "build_expectations")
    expectations = BuildExpectations(**config)
    if expectations.input_manifest_sha256 != input_manifest_sha256:
        raise IntegrityError("approved build input-manifest hash differs from bundle inputs")
    expected_hashes = {entry.source_id: entry.sha256 for entry in frozen_inputs}
    if dict(expectations.required_input_hashes) != expected_hashes:
        raise IntegrityError("approved build source hashes differ from bundle inputs")
    places_bytes = bundle.verify_file(roles["places_canonical"]).read_bytes()
    adjacency_rows = tuple(bundle.iter_jsonl(roles["adjacency_canonical"]))
    county_topology = bundle.verify_file(roles["county_topology"]).read_bytes()
    state_topology = bundle.verify_file(roles["state_topology"]).read_bytes()
    return (
        inputs,
        expectations,
        places_bytes,
        adjacency_rows,
        county_topology,
        state_topology,
    )
