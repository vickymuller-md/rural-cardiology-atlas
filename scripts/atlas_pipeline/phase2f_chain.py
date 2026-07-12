"""Mandatory D-II -> E -> F provenance-chain verification."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from .acquisition import FrozenInput, canonical_manifest_bytes
from .build import OfflineBuildInputs
from .bundle import FrozenBundle, _load_json_bytes, _require_exact_keys, canonical_json_bytes
from .control_receipts import ReceiptContext, validate_control_receipt
from .errors import IntegrityError
from .geocoder_provenance import VerifiedCensusProvenance, verify_census_provenance
from .geoqa import sorted_fips_bytes
from .hospitals import deduplicate_hospitals, in_scope_hospitals
from .hud_surface import (
    HUD_API_REQUEST_PARAMETERS,
    _validate_hud_api_acquisition_receipt,
    canonicalize_hud_api,
)
from .materialization import validate_stream_acquisition_receipt
from .predecessor_bridge import (
    require_bundle_predecessor_hash,
    require_same_tree_or_predecessor_body,
)
from .request_verification import (
    VerifiedRequestChain,
    verify_frozen_mappings_from_sources,
    verify_frozen_request_chain,
)
from .source_registry import SOURCE_POLICIES, validate_source_identity
from .strict_json import require_canonical_json


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _original_receipt(
    bundle: FrozenBundle,
    source_id: str,
    *,
    receipt_id: str,
    schema_id: str,
) -> tuple[Mapping[str, object], bytes]:
    entry = bundle.inputs[source_id]
    if entry.schema_id != schema_id:
        raise IntegrityError(f"{receipt_id} bundle role has the wrong schema")
    body = bundle.verify_file(source_id).read_bytes()
    value = require_canonical_json(body, label=f"{receipt_id} handoff")
    if not isinstance(value, dict):
        raise IntegrityError(f"{receipt_id} handoff must be an object")
    require_same_tree_or_predecessor_body(
        bundle,
        observed_source_tree_id=value.get("source_tree_id"),
        artifact_id=source_id,
        body=body,
    )
    parsed = validate_control_receipt(
        receipt_id,
        body,
        context=ReceiptContext(
            value.get("source_tree_id"),
            value.get("bundle_manifest_sha256"),
            value.get("authorization_id"),
        ),
    )
    return parsed, body


def _require_external_import(
    original: Mapping[str, object],
    original_body: bytes,
    external: Mapping[str, object],
    *,
    original_hash_field: str,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    original_evidence = original["evidence"]
    external_evidence = external["evidence"]
    if not isinstance(original_evidence, dict) or not isinstance(external_evidence, dict):
        raise IntegrityError("handoff/control receipt evidence must be objects")
    if external_evidence.get(original_hash_field) != hashlib.sha256(original_body).hexdigest():
        raise IntegrityError("verified-import receipt does not pin the original handoff")
    for key, value in original_evidence.items():
        if external_evidence.get(key) != value:
            raise IntegrityError(f"verified-import receipt differs from handoff: {key}")
    return original_evidence, external_evidence


def _source_spec(source_id: str, *, bytes_: int, sha256: str) -> FrozenInput:
    policy = SOURCE_POLICIES[source_id]
    host = urlparse(policy.url).hostname
    if host is None:
        raise IntegrityError("immutable source policy lacks a host")
    spec = FrozenInput(
        source_id,
        policy.release,
        policy.url,
        policy.filename,
        bytes_,
        sha256,
        (host,),
        policy.artifact_type,
        policy.validator_ids,
        policy.content_types,
        policy.magic_hex,
    )
    validate_source_identity(spec)
    return spec


def _validate_source_receipt(
    bundle: FrozenBundle,
    source_id: str,
    *,
    raw_source_id: str,
    raw_schema_id: str,
    expected_hash: str,
    spec: FrozenInput,
) -> None:
    entry = bundle.inputs[source_id]
    if entry.schema_id != "source_acquisition_receipt_json":
        raise IntegrityError("source-acquisition role has the wrong schema")
    body = bundle.verify_file(source_id).read_bytes()
    if hashlib.sha256(body).hexdigest() != expected_hash:
        raise IntegrityError("source-acquisition receipt hash differs from handoff")
    if raw_source_id == source_id:
        raise IntegrityError("source-acquisition receipt cannot also be the raw artifact")
    try:
        raw_entry = bundle.inputs[raw_source_id]
    except KeyError as exc:
        raise IntegrityError("source-acquisition raw role is absent from the bundle") from exc
    if raw_source_id != spec.source_id or raw_entry.source_id != spec.source_id:
        raise IntegrityError("source-acquisition raw role has the wrong source ID")
    if raw_entry.schema_id != raw_schema_id:
        raise IntegrityError("source-acquisition raw role has the wrong schema")
    if (
        raw_entry.release != spec.release
        or raw_entry.url != spec.url
        or Path(raw_entry.relative_path).name != spec.filename
        or raw_entry.bytes != spec.bytes
        or raw_entry.sha256 != spec.sha256
        or raw_entry.content_type.split(";", 1)[0] not in spec.content_types
    ):
        raise IntegrityError("source-acquisition raw role identity/pins differ from source policy")
    validate_stream_acquisition_receipt(
        body,
        spec,
        final_path=bundle.verify_file(raw_source_id),
    )


def _verify_prior_phase_controls(
    bundle: FrozenBundle,
    controls: Mapping[str, Mapping[str, object]],
) -> Mapping[str, object]:
    """Bind original Phase-2B/2C/2D-I receipts to their Phase-2F imports."""

    required = {
        "input_materialization",
        "input_materialization_import",
        "nppes_transport",
        "nppes_transport_import",
        "nppes_zip_candidate",
        "nppes_zip_candidate_import",
        "nppes_container",
        "nppes_container_import",
        "storage_preflight",
        "d2ii_prepare_requests",
    }
    if not required <= set(controls):
        raise IntegrityError("Phase-2F lacks the complete prior-phase receipt/import chain")
    original_input = controls["input_materialization"]
    imported_input = controls["input_materialization_import"]["evidence"]
    expected_input_manifest = hashlib.sha256(
        canonical_manifest_bytes(list(bundle.frozen_inputs()))
    ).hexdigest()
    registry_sha256 = _sha(Path(__file__).with_name("source_registry.py"))
    original_ids = (
        "input_materialization",
        "nppes_transport",
        "nppes_zip_candidate",
        "nppes_container",
        "storage_preflight",
    )
    predecessor_mode = any(
        controls[receipt_id]["source_tree_id"] != bundle.source_tree_id
        for receipt_id in original_ids
    )
    if predecessor_mode:
        predecessor_rows = (
            (
                "input_materialization",
                controls["input_materialization_import"]["evidence"][
                    "input_materialization_receipt_sha256"
                ],
            ),
            (
                "nppes_transport",
                controls["nppes_transport_import"]["evidence"][
                    "nppes_transport_receipt_sha256"
                ],
            ),
            (
                "nppes_zip_candidate",
                controls["nppes_zip_candidate_import"]["evidence"][
                    "nppes_zip_candidate_receipt_sha256"
                ],
            ),
            (
                "nppes_container",
                controls["nppes_container_import"]["evidence"][
                    "nppes_container_receipt_sha256"
                ],
            ),
            (
                "storage_preflight",
                controls["d2ii_prepare_requests"]["evidence"][
                    "storage_preflight_receipt_sha256"
                ],
            ),
        )
        for artifact_id, artifact_sha256 in predecessor_rows:
            require_bundle_predecessor_hash(
                bundle,
                observed_source_tree_id=controls[artifact_id]["source_tree_id"],
                artifact_id=artifact_id,
                artifact_sha256=artifact_sha256,
            )
    if (
        original_input["evidence"]["input_manifest_sha256"]
        != original_input["bundle_manifest_sha256"]
        or imported_input["phase2f_input_manifest_sha256"]
        != expected_input_manifest
        or original_input["evidence"]["validator_registry_sha256"]
        != registry_sha256
        or imported_input["validator_registry_sha256"] != registry_sha256
    ):
        raise IntegrityError("Phase-2B/Phase-2F input-materialization chain mismatch")

    transport = controls["nppes_transport"]["evidence"]
    candidate = controls["nppes_zip_candidate"]["evidence"]
    container = controls["nppes_container"]["evidence"]
    if (
        transport["candidate_sha256"] != candidate["archive_sha256"]
        or transport["candidate_sha256"] != container["archive_sha256"]
        or candidate["candidate_manifest_sha256"]
        != container["candidate_manifest_sha256"]
    ):
        raise IntegrityError("prior-phase NPPES receipt chain is discontinuous")
    return {
        "nppes_archive_sha256": container["archive_sha256"],
        "nppes_candidate_manifest_sha256": container[
            "candidate_manifest_sha256"
        ],
        "nppes_approval_receipt_sha256": container["approval_receipt_sha256"],
        "nppes_approval_trust_root_sha256": container[
            "approval_trust_root_sha256"
        ],
        "nppes_selected_member_filename": container[
            "selected_member_filename"
        ],
        "nppes_selected_member_sha256": container["selected_member_sha256"],
    }


def _verify_d2ii(
    bundle: FrozenBundle,
    roles: Mapping[str, object],
    inputs: OfflineBuildInputs,
    external: Mapping[str, object],
    nppes_provenance: Mapping[str, object],
) -> VerifiedRequestChain:
    expected_schemas = {
        roles["provider_mapping"]: "entity_address_mapping_jsonl",
        roles["hospital_mapping"]: "entity_address_mapping_jsonl",
        roles["request_manifest"]: "request_manifest_json",
        roles["nppes_audit"]: "nppes_audit_json",
        roles["nppes_dispositions"]: "nppes_dispositions_jsonl",
        **{
            source_id: "geocoder_request_csv"
            for source_id in roles["request_chunks"]
        },
    }
    for source_id, schema_id in expected_schemas.items():
        if bundle.inputs[source_id].schema_id != schema_id:
            raise IntegrityError(f"D-II role has the wrong schema: {source_id}")
    original, original_body = _original_receipt(
        bundle,
        roles["d2ii_handoff_receipt"],
        receipt_id="d2ii_handoff",
        schema_id="d2ii_handoff_receipt_json",
    )
    original_evidence, external_evidence = _require_external_import(
        original,
        original_body,
        external,
        original_hash_field="d2ii_handoff_receipt_sha256",
    )
    if (
        original_evidence["phase2dii_bundle_manifest_sha256"]
        != original["bundle_manifest_sha256"]
    ):
        raise IntegrityError("D-II handoff does not bind its producing bundle")
    nppes_audit = require_canonical_json(
        bundle.verify_file(roles["nppes_audit"]).read_bytes(), label="NPPES audit"
    )
    nppes_stream = require_canonical_json(
        bundle.verify_file(roles["nppes_stream_receipt"]).read_bytes(),
        label="NPPES stream receipt",
    )
    if (
        not isinstance(nppes_audit, dict)
        or not isinstance(nppes_stream, dict)
        or nppes_audit.get("source_snapshot_id")
        != original_evidence["phase2dii_bundle_manifest_sha256"]
        or nppes_stream.get("source_snapshot_id")
        != original_evidence["phase2dii_bundle_manifest_sha256"]
    ):
        raise IntegrityError("NPPES audit/stream receipt do not bind the D-II bundle")
    for field in (
        "nppes_archive_sha256",
        "nppes_candidate_manifest_sha256",
        "nppes_approval_receipt_sha256",
        "nppes_approval_trust_root_sha256",
        "nppes_selected_member_filename",
        "nppes_selected_member_sha256",
    ):
        if (
            original_evidence[field] != nppes_provenance.get(field)
            or external_evidence[field] != nppes_provenance.get(field)
        ):
            raise IntegrityError(
                f"D-II NPPES selected-member provenance mismatch: {field}"
            )
    hospitals = in_scope_hospitals(deduplicate_hospitals(inputs.hospital_rows))
    verify_frozen_mappings_from_sources(
        bundle,
        nppes_rows=inputs.nppes_rows,
        hospitals=hospitals,
        provider_mapping_source=roles["provider_mapping"],
        hospital_mapping_source=roles["hospital_mapping"],
    )
    request_chain = verify_frozen_request_chain(
        bundle,
        provider_mapping_source=roles["provider_mapping"],
        hospital_mapping_source=roles["hospital_mapping"],
        request_manifest_source=roles["request_manifest"],
        request_chunk_sources=roles["request_chunks"],
    )
    chunk_rows = [
        {
            "filename": chunk.filename,
            "bytes": chunk.byte_count,
            "sha256": chunk.sha256,
        }
        for chunk in request_chain.chunks
    ]
    artifact_hashes = {
        "provider_mapping_sha256": _sha(bundle.verify_file(roles["provider_mapping"])),
        "hospital_mapping_sha256": _sha(bundle.verify_file(roles["hospital_mapping"])),
        "nppes_audit_sha256": _sha(bundle.verify_file(roles["nppes_audit"])),
        "nppes_dispositions_sha256": _sha(
            bundle.verify_file(roles["nppes_dispositions"])
        ),
        "request_manifest_sha256": request_chain.manifest_sha256,
        "request_chunks_sha256": hashlib.sha256(
            canonical_json_bytes(chunk_rows)
        ).hexdigest(),
    }
    for key, observed in artifact_hashes.items():
        if original_evidence[key] != observed or external_evidence[key] != observed:
            raise IntegrityError(f"D-II handoff artifact mismatch: {key}")
    if (
        original_evidence["request_count"] != len(request_chain.requests)
        or original_evidence["chunk_count"] != len(request_chain.chunks)
    ):
        raise IntegrityError("D-II handoff request/chunk counts differ from reconstruction")
    return request_chain


def _verify_places(
    bundle: FrozenBundle,
    roles: Mapping[str, object],
    inputs: OfflineBuildInputs,
    external: Mapping[str, object],
) -> None:
    original, original_body = _original_receipt(
        bundle,
        roles["places_handoff_receipt"],
        receipt_id="places_handoff",
        schema_id="places_handoff_receipt_json",
    )
    evidence, _external_evidence = _require_external_import(
        original,
        original_body,
        external,
        original_hash_field="places_handoff_receipt_sha256",
    )
    if evidence["phase2dii_bundle_manifest_sha256"] != original["bundle_manifest_sha256"]:
        raise IntegrityError("PLACES handoff does not bind its producing bundle")
    canonical_path = bundle.verify_file(roles["places_canonical"])
    if _sha(canonical_path) != evidence["canonical_table_sha256"]:
        raise IntegrityError("PLACES canonical table differs from handoff")
    universe_hash = hashlib.sha256(sorted_fips_bytes(inputs.county_points)).hexdigest()
    if evidence["county_universe_sha256"] != universe_hash:
        raise IntegrityError("PLACES handoff universe differs from Gazetteer")
    if bundle.inputs[roles["places_derivation_handoff"]].schema_id != "places_derivation_handoff_json":
        raise IntegrityError("PLACES data handoff role has the wrong schema")
    handoff_body = bundle.verify_file(roles["places_derivation_handoff"]).read_bytes()
    handoff = require_canonical_json(handoff_body, label="PLACES derivation handoff")
    if not isinstance(handoff, dict):
        raise IntegrityError("PLACES derivation handoff must be an object")
    _require_exact_keys(
        handoff,
        (
            "schema_version", "source_tree_id", "bundle_manifest_sha256",
            "raw_source_id", "raw_source_bytes", "raw_source_sha256",
            "raw_total_rows", "national_rows_excluded", "county_rows",
            "nonnull_rows", "suppressed_rows", "suppressed_fips",
            "county_universe_sha256", "canonical_bytes", "canonical_sha256",
        ),
        "PLACES derivation handoff",
    )
    require_same_tree_or_predecessor_body(
        bundle,
        observed_source_tree_id=handoff["source_tree_id"],
        artifact_id=roles["places_derivation_handoff"],
        body=handoff_body,
    )
    if (
        handoff["schema_version"] != "atlas-v19-places-derivation-handoff-1"
        or handoff["bundle_manifest_sha256"]
        != evidence["phase2dii_bundle_manifest_sha256"]
        or handoff["raw_source_id"] != "PLACES-CHD-RAW-2025"
        or handoff["raw_source_sha256"] != evidence["raw_source_sha256"]
        or handoff["raw_total_rows"]
        != evidence["raw_county_rows"] + evidence["national_rows_excluded"]
        or handoff["national_rows_excluded"] != evidence["national_rows_excluded"]
        or handoff["county_rows"] != evidence["raw_county_rows"]
        or handoff["nonnull_rows"] != evidence["retained_rows"]
        or handoff["suppressed_rows"] != evidence["suppressed_rows"]
        or handoff["suppressed_fips"] != "48301"
        or handoff["county_universe_sha256"] != universe_hash
        or handoff["canonical_bytes"] != canonical_path.stat().st_size
        or handoff["canonical_sha256"] != evidence["canonical_table_sha256"]
    ):
        raise IntegrityError("PLACES derivation handoff semantic mismatch")
    spec = _source_spec(
        "PLACES-CHD-RAW-2025",
        bytes_=handoff["raw_source_bytes"],
        sha256=handoff["raw_source_sha256"],
    )
    _validate_source_receipt(
        bundle,
        roles["places_source_acquisition_receipt"],
        raw_source_id=roles["places_raw"],
        raw_schema_id="places_raw_csv",
        expected_hash=evidence["source_acquisition_receipt_sha256"],
        spec=spec,
    )


def _verify_hud(
    bundle: FrozenBundle,
    roles: Mapping[str, object],
    inputs: OfflineBuildInputs,
    external: Mapping[str, object],
) -> None:
    original, original_body = _original_receipt(
        bundle,
        roles["hud_handoff_receipt"],
        receipt_id="hud_handoff",
        schema_id="hud_handoff_receipt_json",
    )
    evidence, _external_evidence = _require_external_import(
        original,
        original_body,
        external,
        original_hash_field="hud_handoff_receipt_sha256",
    )
    if evidence["phase2e_bundle_manifest_sha256"] != original["bundle_manifest_sha256"]:
        raise IntegrityError("HUD handoff does not bind its producing bundle")
    hud_path = bundle.verify_file(roles["hud"])
    if _sha(hud_path) != evidence["canonical_csv_sha256"]:
        raise IntegrityError("HUD canonical CSV differs from handoff")
    if (
        bundle.inputs[roles["hud_compatibility"]].schema_id != "hud_compatibility_json"
        or bundle.inputs[roles["hud_handoff"]].schema_id != "hud_data_handoff_json"
    ):
        raise IntegrityError("HUD compatibility/data handoff roles have wrong schemas")
    compatibility_path = bundle.verify_file(roles["hud_compatibility"])
    data_handoff_path = bundle.verify_file(roles["hud_handoff"])
    if (
        _sha(compatibility_path) != evidence["compatibility_receipt_sha256"]
        or _sha(data_handoff_path) != evidence["hud_data_handoff_sha256"]
    ):
        raise IntegrityError("HUD compatibility/data handoff hashes differ")
    compatibility = require_canonical_json(
        compatibility_path.read_bytes(), label="HUD compatibility"
    )
    if not isinstance(compatibility, dict):
        raise IntegrityError("HUD compatibility must be an object")
    _require_exact_keys(
        compatibility,
        (
            "schema_version", "release", "county_universe_sha256",
            "county_universe_count", "hud_row_count", "hud_county_count",
            "outside_universe_count", "obsolete_connecticut_count",
            "connecticut_fips", "state_basis", "compatible",
        ),
        "HUD compatibility",
    )
    universe_hash = hashlib.sha256(sorted_fips_bytes(inputs.county_points)).hexdigest()
    current_ct = [
        "09110", "09120", "09130", "09140", "09150",
        "09160", "09170", "09180", "09190",
    ]
    if (
        compatibility["schema_version"] != "atlas-v19-hud-compatibility-1"
        or compatibility["release"] != "2025-Q4"
        or compatibility["county_universe_sha256"] != universe_hash
        or compatibility["county_universe_count"] != len(inputs.county_points)
        or type(compatibility["hud_row_count"]) is not int
        or compatibility["hud_row_count"] <= 0
        or type(compatibility["hud_county_count"]) is not int
        or not 1 <= compatibility["hud_county_count"] <= len(inputs.county_points)
        or compatibility["outside_universe_count"] != 0
        or compatibility["obsolete_connecticut_count"] != 0
        or compatibility["connecticut_fips"] != current_ct
        or compatibility["state_basis"] != "CT_PLANNING_REGIONS_2022_PLUS"
        or compatibility["compatible"] is not True
    ):
        raise IntegrityError("HUD 2025/Connecticut compatibility contract failed")
    handoff = require_canonical_json(data_handoff_path.read_bytes(), label="HUD data handoff")
    if not isinstance(handoff, dict):
        raise IntegrityError("HUD data handoff must be an object")
    _require_exact_keys(
        handoff,
        (
            "schema_version", "source_tree_id", "materialization_manifest_sha256",
            "source_id", "release", "url", "filename", "raw_xlsx_bytes",
            "raw_xlsx_sha256", "candidate_manifest_sha256",
            "manifest_approval_id", "required_sheet_name", "canonical_csv_bytes",
            "canonical_csv_sha256", "compatibility_sha256", "handoff_state",
        ),
        "HUD data handoff",
    )
    if (
        handoff.get("schema_version") != "atlas-v19-hud-handoff-1"
        or handoff.get("source_tree_id") != bundle.source_tree_id
        or handoff.get("materialization_manifest_sha256")
        != evidence["phase2e_bundle_manifest_sha256"]
        or handoff.get("source_id") != "HUD-ZIP-COUNTY-2025Q4"
        or handoff.get("release") != "2025-Q4"
        or handoff.get("url") != evidence["url"]
        or handoff.get("filename") != evidence["filename"]
        or handoff.get("raw_xlsx_sha256") != evidence["raw_xlsx_sha256"]
        or handoff.get("canonical_csv_bytes") != hud_path.stat().st_size
        or handoff.get("canonical_csv_sha256") != evidence["canonical_csv_sha256"]
        or handoff.get("compatibility_sha256")
        != evidence["compatibility_receipt_sha256"]
        or handoff.get("handoff_state") != "APPROVED_FROZEN_HUD_INPUT"
    ):
        raise IntegrityError("HUD data handoff semantic mismatch")
    spec = _source_spec(
        "HUD-ZIP-COUNTY-2025Q4",
        bytes_=handoff["raw_xlsx_bytes"],
        sha256=handoff["raw_xlsx_sha256"],
    )
    _validate_source_receipt(
        bundle,
        roles["hud_source_acquisition_receipt"],
        raw_source_id=roles["hud_raw_xlsx"],
        raw_schema_id="hud_raw_xlsx",
        expected_hash=evidence["source_acquisition_receipt_sha256"],
        spec=spec,
    )


def _verify_hud_api(
    bundle: FrozenBundle,
    roles: Mapping[str, object],
    inputs: OfflineBuildInputs,
    external: Mapping[str, object],
) -> None:
    original, original_body = _original_receipt(
        bundle,
        roles["hud_api_handoff_receipt"],
        receipt_id="hud_api_handoff",
        schema_id="hud_api_handoff_receipt_json",
    )
    evidence, _external_evidence = _require_external_import(
        original,
        original_body,
        external,
        original_hash_field="hud_api_handoff_receipt_sha256",
    )
    if evidence["phase2e_bundle_manifest_sha256"] != original["bundle_manifest_sha256"]:
        raise IntegrityError("HUD API handoff does not bind its producing bundle")

    hud_path = bundle.verify_file(roles["hud"])
    if (
        bundle.inputs[roles["hud"]].schema_id != "hud_api_zip_county_csv"
        or _sha(hud_path) != evidence["canonical_csv_sha256"]
    ):
        raise IntegrityError("HUD API canonical CSV schema/hash differs from handoff")
    if len(inputs.hud_candidates) != evidence["in_scope_row_count"]:
        raise IntegrityError("HUD API canonical candidate count differs from handoff")
    if (
        bundle.inputs[roles["hud_api_compatibility"]].schema_id
        != "hud_api_compatibility_json"
        or bundle.inputs[roles["hud_api_handoff"]].schema_id
        != "hud_api_data_handoff_json"
        or bundle.inputs[roles["hud_api_acquisition_receipt"]].schema_id
        != "hud_api_acquisition_receipt_json"
        or bundle.inputs[roles["hud_api_import_receipt"]].schema_id
        != "source_acquisition_receipt_json"
        or bundle.inputs[roles["hud_api_raw_json"]].schema_id
        != "hud_api_raw_json"
        or bundle.inputs[roles["hud_api_excluded_rows"]].schema_id
        != "hud_api_excluded_rows_json"
    ):
        raise IntegrityError("HUD API provenance roles use the wrong schemas")

    compatibility_path = bundle.verify_file(roles["hud_api_compatibility"])
    handoff_path = bundle.verify_file(roles["hud_api_handoff"])
    acquisition_receipt_path = bundle.verify_file(
        roles["hud_api_acquisition_receipt"]
    )
    raw_path = bundle.verify_file(roles["hud_api_raw_json"])
    excluded_path = bundle.verify_file(roles["hud_api_excluded_rows"])
    if (
        _sha(compatibility_path) != evidence["compatibility_receipt_sha256"]
        or _sha(handoff_path) != evidence["hud_api_data_handoff_sha256"]
        or _sha(acquisition_receipt_path)
        != evidence["frozen_acquisition_receipt_sha256"]
        or _sha(raw_path) != evidence["raw_json_sha256"]
        or excluded_path.stat().st_size != evidence["excluded_rows_bytes"]
        or _sha(excluded_path) != evidence["excluded_rows_sha256"]
    ):
        raise IntegrityError("HUD API provenance hashes differ from handoff")
    rederived_csv, rederived_rows, rederived_excluded, rederived_audit = canonicalize_hud_api(
        raw_path,
        county_universe=set(inputs.county_points),
    )
    if (
        rederived_csv != hud_path.read_bytes()
        or rederived_excluded != excluded_path.read_bytes()
        or len(rederived_rows) != len(inputs.hud_candidates)
    ):
        raise IntegrityError("HUD API canonical CSV is not byte-rederived from raw JSON")

    compatibility = require_canonical_json(
        compatibility_path.read_bytes(), label="HUD API compatibility"
    )
    if not isinstance(compatibility, dict):
        raise IntegrityError("HUD API compatibility must be an object")
    _require_exact_keys(
        compatibility,
        (
            "schema_version", "release", "county_universe_sha256",
            "county_universe_count", "raw_row_count", "in_scope_row_count",
            "in_scope_county_count", "excluded_territory_row_count",
            "excluded_territory_geoid_count", "excluded_special_row_count",
            "excluded_special_geoid_count", "excluded_rows_bytes",
            "excluded_rows_sha256",
            "territory_state_prefix_counts", "special_pair_counts",
            "invalid_unknown_geography_count", "missing_universe_count",
            "obsolete_connecticut_count", "connecticut_fips", "state_basis",
            "schema_checks_passed", "ratio_checks_passed",
            "metadata_checks_passed", "unique_pair_check_passed",
            "geography_checks_passed", "compatible",
        ),
        "HUD API compatibility",
    )
    universe_hash = hashlib.sha256(sorted_fips_bytes(inputs.county_points)).hexdigest()
    current_ct = [
        "09110", "09120", "09130", "09140", "09150",
        "09160", "09170", "09180", "09190",
    ]
    if (
        compatibility["schema_version"] != "atlas-v19-hud-api-compatibility-1"
        or compatibility["release"] != "2025-Q4"
        or compatibility["county_universe_sha256"] != universe_hash
        or compatibility["county_universe_count"] != len(inputs.county_points)
        or compatibility["raw_row_count"] != evidence["result_row_count"]
        or compatibility["in_scope_row_count"] != len(inputs.hud_candidates)
        or compatibility["in_scope_county_count"] != len(inputs.county_points)
        or compatibility["in_scope_row_count"]
        + compatibility["excluded_territory_row_count"]
        + compatibility["excluded_special_row_count"]
        != compatibility["raw_row_count"]
        or compatibility["excluded_rows_sha256"] != evidence["excluded_rows_sha256"]
        or compatibility["excluded_rows_bytes"] != evidence["excluded_rows_bytes"]
        or compatibility["raw_row_count"] != rederived_audit["raw_row_count"]
        or compatibility["in_scope_row_count"]
        != rederived_audit["in_scope_row_count"]
        or compatibility["in_scope_county_count"]
        != rederived_audit["in_scope_county_count"]
        or compatibility["excluded_territory_row_count"]
        != rederived_audit["excluded_territory_row_count"]
        or compatibility["excluded_territory_geoid_count"]
        != rederived_audit["excluded_territory_geoid_count"]
        or compatibility["excluded_special_row_count"]
        != rederived_audit["excluded_special_row_count"]
        or compatibility["excluded_special_geoid_count"]
        != rederived_audit["excluded_special_geoid_count"]
        or compatibility["excluded_rows_sha256"]
        != rederived_audit["excluded_rows_sha256"]
        or compatibility["excluded_rows_bytes"]
        != rederived_audit["excluded_rows_bytes"]
        or compatibility["territory_state_prefix_counts"]
        != rederived_audit["territory_state_prefix_counts"]
        or compatibility["special_pair_counts"]
        != rederived_audit["special_pair_counts"]
        or compatibility["invalid_unknown_geography_count"] != 0
        or compatibility["missing_universe_count"] != 0
        or compatibility["obsolete_connecticut_count"] != 0
        or compatibility["connecticut_fips"] != current_ct
        or compatibility["state_basis"] != "CT_PLANNING_REGIONS_2022_PLUS"
        or any(
            compatibility[field] is not True
            for field in (
                "schema_checks_passed", "ratio_checks_passed",
                "metadata_checks_passed", "unique_pair_check_passed",
                "geography_checks_passed", "compatible",
            )
        )
    ):
        raise IntegrityError("HUD API compatibility/accounting contract failed")

    handoff_body = handoff_path.read_bytes()
    handoff = require_canonical_json(handoff_body, label="HUD API data handoff")
    if not isinstance(handoff, dict):
        raise IntegrityError("HUD API data handoff must be an object")
    _require_exact_keys(
        handoff,
        (
            "schema_version", "source_tree_id", "materialization_manifest_sha256",
            "source_id", "release", "url", "filename", "request_parameters",
            "request_parameters_sha256", "raw_json_bytes", "raw_json_sha256",
            "frozen_acquisition_receipt_sha256", "import_receipt_sha256",
            "parser_sha256", "county_universe_sha256", "result_row_count",
            "canonical_csv_bytes", "canonical_csv_sha256",
            "excluded_rows_bytes", "excluded_rows_sha256",
            "compatibility_sha256", "handoff_state",
        ),
        "HUD API data handoff",
    )
    require_same_tree_or_predecessor_body(
        bundle,
        observed_source_tree_id=handoff["source_tree_id"],
        artifact_id=roles["hud_api_handoff"],
        body=handoff_body,
    )
    request_parameters_sha256 = hashlib.sha256(
        canonical_json_bytes(HUD_API_REQUEST_PARAMETERS)
    ).hexdigest()
    if (
        handoff["schema_version"] != "atlas-v19-hud-api-handoff-1"
        or handoff["materialization_manifest_sha256"]
        != evidence["phase2e_bundle_manifest_sha256"]
        or handoff["source_id"] != "HUD-ZIP-COUNTY-API-2025Q4"
        or handoff["release"] != "2025-Q4"
        or handoff["url"] != evidence["url"]
        or handoff["filename"] != evidence["filename"]
        or handoff["request_parameters"] != HUD_API_REQUEST_PARAMETERS
        or handoff["request_parameters_sha256"] != request_parameters_sha256
        or handoff["request_parameters_sha256"] != evidence["request_parameters_sha256"]
        or handoff["raw_json_bytes"] != raw_path.stat().st_size
        or handoff["raw_json_sha256"] != evidence["raw_json_sha256"]
        or handoff["frozen_acquisition_receipt_sha256"]
        != evidence["frozen_acquisition_receipt_sha256"]
        or handoff["import_receipt_sha256"] != evidence["import_receipt_sha256"]
        or handoff["parser_sha256"] != _sha(Path(__file__).with_name("hud_surface.py"))
        or handoff["county_universe_sha256"] != universe_hash
        or handoff["result_row_count"] != evidence["result_row_count"]
        or handoff["canonical_csv_bytes"] != hud_path.stat().st_size
        or handoff["canonical_csv_sha256"] != evidence["canonical_csv_sha256"]
        or handoff["excluded_rows_bytes"] != excluded_path.stat().st_size
        or handoff["excluded_rows_bytes"] != evidence["excluded_rows_bytes"]
        or handoff["excluded_rows_sha256"] != _sha(excluded_path)
        or handoff["excluded_rows_sha256"] != evidence["excluded_rows_sha256"]
        or handoff["compatibility_sha256"] != evidence["compatibility_receipt_sha256"]
        or handoff["handoff_state"] != "APPROVED_FROZEN_HUD_API_INPUT"
    ):
        raise IntegrityError("HUD API data handoff semantic mismatch")

    _validate_hud_api_acquisition_receipt(
        acquisition_receipt_path,
        expected_sha256=evidence["frozen_acquisition_receipt_sha256"],
        raw_bytes=raw_path.stat().st_size,
        raw_sha256=_sha(raw_path),
    )
    spec = _source_spec(
        "HUD-ZIP-COUNTY-API-2025Q4",
        bytes_=raw_path.stat().st_size,
        sha256=_sha(raw_path),
    )
    _validate_source_receipt(
        bundle,
        roles["hud_api_import_receipt"],
        raw_source_id=roles["hud_api_raw_json"],
        raw_schema_id="hud_api_raw_json",
        expected_hash=evidence["import_receipt_sha256"],
        spec=spec,
    )


def verify_phase2f_chain(
    bundle: FrozenBundle,
    inputs: OfflineBuildInputs,
    control_receipts: Mapping[str, Mapping[str, object]],
) -> tuple[VerifiedRequestChain, VerifiedCensusProvenance]:
    """Verify every imported D-II/E handoff before analytical computation."""

    roles = bundle.expectation("source_roles", dict)
    legacy_hud_roles = {
        "hud_compatibility", "hud_handoff", "hud_handoff_receipt",
        "hud_source_acquisition_receipt", "hud_raw_xlsx",
    }
    if (
        legacy_hud_roles & set(roles)
        or "hud_api_handoff" not in roles
        or "hud_api_acquisition" not in control_receipts
        or "hud_acquisition" in control_receipts
    ):
        raise IntegrityError("R4 provenance chain rejects R3/mixed HUD lineage")
    nppes_provenance = _verify_prior_phase_controls(bundle, control_receipts)
    request_chain = _verify_d2ii(
        bundle,
        roles,
        inputs,
        control_receipts["d2ii_prepare_requests"],
        nppes_provenance,
    )
    if {row.request_id for row in inputs.geocodes} != {
        request.address_id for request in request_chain.requests
    }:
        raise IntegrityError("canonical Census IDs differ from reconstructed D-II requests")
    census = verify_census_provenance(
        bundle,
        request_chain=request_chain,
        attempts_manifest_source=roles["census_attempts_manifest"],
        attempt_metadata_sources=roles["census_attempt_metadata"],
        attempt_response_sources=roles["census_attempt_responses"],
        acquisition_receipt_source=roles["census_handoff_receipt"],
        canonical_snapshot_source=roles["geocoder_canonical"],
        control_receipt_evidence=control_receipts["census_acquisition"]["evidence"],
        county_universe=set(inputs.county_points),
    )
    _verify_places(bundle, roles, inputs, control_receipts["places_derivation"])
    _verify_hud_api(
        bundle, roles, inputs, control_receipts["hud_api_acquisition"]
    )
    return request_chain, census
