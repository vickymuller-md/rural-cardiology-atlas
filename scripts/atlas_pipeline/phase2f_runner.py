"""Network-denied Phase-2F bundle runner with mandatory review-config hook."""

from __future__ import annotations

import hashlib
import socket
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping

from .acquisition import canonical_manifest_bytes
from .build import OfflineBuildArtifacts, OfflineBuildInputs, ReleaseQualityGateError, build_release
from .bundle import (
    FrozenBundle,
    _load_json_bytes,
    _require_exact_keys,
    atomic_write,
    canonical_json_bytes,
    write_output_receipt,
)
from .errors import IntegrityError
from .control_receipts import (
    PHASE2F_CROSS_PHASE_ORIGINAL_IDS,
    ReceiptContext,
    load_control_receipts_manifest,
)
from .geoqa import (
    GeometryEngineIdentity,
    build_adjacency_receipt,
    build_point_qa_setup,
)
from .offline_bundle import (
    FAILED_R3_SOURCE_TREE_ID,
    METHOD_LINEAGE_ID,
    PHASE2F_ROLES,
    load_phase2f_inputs,
)
from .phase2f_review import (
    build_phase2f_review_artifacts,
    parse_adjacency_snapshot,
)
from .phase2f_chain import verify_phase2f_chain
from .trust import RuntimeControl
from .tiger_qa import load_tiger_qa_binding

REVIEW_SCHEMA_VERSION = "atlas-v19-phase2f-review-config-3"
VALIDATION_COMPLETION_SCHEMA_VERSION = "atlas-v19-r4-validation-completion-1"
LEGACY_R3_REVIEW_RECEIPT_IDS = frozenset(
    {
        "source_tree_freeze",
        "dependency_materialization",
        "network_denied",
        "input_materialization",
        "input_materialization_import",
        "nppes_transport",
        "nppes_transport_import",
        "nppes_zip_candidate",
        "nppes_zip_candidate_import",
        "nppes_container",
        "nppes_container_import",
        "adjacency_materialization",
        "tiger_qa_materialization",
        "topology_authorization",
        "storage_preflight",
        "d2ii_prepare_requests",
        "census_acquisition",
        "hud_acquisition",
        "places_derivation",
    }
)
MANDATORY_REVIEW_RECEIPT_IDS = (
    LEGACY_R3_REVIEW_RECEIPT_IDS - {"hud_acquisition"}
) | {"hud_api_acquisition"}
MANDATORY_R4_REVIEW_RECEIPT_IDS = MANDATORY_REVIEW_RECEIPT_IDS | {
    "predecessor_bridge_audit"
}


@dataclass(frozen=True)
class ReviewConfig:
    method_lineage_id: str
    validation_completion_schema_version: str
    independent_reviewer: str
    receipt_ids: tuple[str, ...]
    geometry_engine: GeometryEngineIdentity
    receipts: Mapping[str, Mapping[str, object]]

    @classmethod
    def load(
        cls,
        path: Path,
        bundle: FrozenBundle,
        runtime_control: RuntimeControl,
        control_receipts_manifest_path: Path,
    ) -> "ReviewConfig":
        if not path.is_file() or path.is_symlink():
            raise IntegrityError("review config must be a regular non-symlink file")
        body = path.read_bytes()
        if (
            runtime_control.phase != "2F"
            or runtime_control.source_tree_id != bundle.source_tree_id
            or runtime_control.authorization_id != bundle.authorization_id
            or runtime_control.approved_bundle_sha256 != bundle.manifest_sha256
            or runtime_control.approved_review_config_sha256 is None
            or hashlib.sha256(body).hexdigest()
            != runtime_control.approved_review_config_sha256
        ):
            raise IntegrityError("review config is not independently approved for this bundle")
        value = _load_json_bytes(body)
        if not isinstance(value, dict):
            raise IntegrityError("review config must be an object")
        if body != canonical_json_bytes(value):
            raise IntegrityError("review config is not canonical UTF-8/LF JSON")
        _require_exact_keys(
            value,
            (
                "schema_version", "authorization_id", "source_tree_id",
                "method_lineage_id", "validation_completion_schema_version",
                "bundle_manifest_sha256", "input_manifest_sha256",
                "independent_reviewer", "network_disabled", "geometry_engine",
                "control_receipts_manifest_sha256", "required_receipt_ids",
            ),
            "review config",
        )
        if (
            value["schema_version"] != REVIEW_SCHEMA_VERSION
            or value["authorization_id"] != bundle.authorization_id
            or value["source_tree_id"] != bundle.source_tree_id
            or value["source_tree_id"] == FAILED_R3_SOURCE_TREE_ID
            or value["method_lineage_id"] != METHOD_LINEAGE_ID
            or value["validation_completion_schema_version"]
            != VALIDATION_COMPLETION_SCHEMA_VERSION
            or bundle.expectations.get("method_lineage_id") != METHOD_LINEAGE_ID
            or value["bundle_manifest_sha256"] != bundle.manifest_sha256
            or value["network_disabled"] is not True
            or not isinstance(value["independent_reviewer"], str)
            or not value["independent_reviewer"].strip()
        ):
            raise IntegrityError("review config is not bound to the approved offline build")
        expected_input_manifest = hashlib.sha256(
            canonical_manifest_bytes(list(bundle.frozen_inputs()))
        ).hexdigest()
        if value["input_manifest_sha256"] != expected_input_manifest:
            raise IntegrityError("review config input-manifest identity mismatch")
        engine = value["geometry_engine"]
        if not isinstance(engine, dict):
            raise IntegrityError("review geometry engine must be an object")
        _require_exact_keys(
            engine,
            ("package", "version", "package_sha256", "algorithm"),
            "review geometry engine",
        )
        geometry_engine = GeometryEngineIdentity(**engine)
        geometry_engine.validate()
        receipt_ids = value["required_receipt_ids"]
        source_roles = bundle.expectations.get("source_roles", {})
        if not isinstance(source_roles, dict):
            raise IntegrityError("review source_roles expectation has the wrong type")
        if set(source_roles) != PHASE2F_ROLES:
            raise IntegrityError("ReviewConfig requires the exact R4 HUD API role set")
        mandatory_receipt_ids = MANDATORY_R4_REVIEW_RECEIPT_IDS
        if (
            not isinstance(receipt_ids, list)
            or receipt_ids != sorted(mandatory_receipt_ids)
        ):
            raise IntegrityError("review receipt IDs differ from the complete frozen set")
        if (
            value["control_receipts_manifest_sha256"]
            != runtime_control.approved_control_receipts_manifest_sha256
        ):
            raise IntegrityError("review config does not bind the independent receipt manifest")
        receipts = load_control_receipts_manifest(
            control_receipts_manifest_path,
            expected_manifest_sha256=runtime_control.approved_control_receipts_manifest_sha256,
            required_ids=mandatory_receipt_ids,
            context=ReceiptContext(
                bundle.source_tree_id, bundle.manifest_sha256, bundle.authorization_id
            ),
            cross_phase_original_ids=PHASE2F_CROSS_PHASE_ORIGINAL_IDS,
        )
        input_receipt = receipts["input_materialization_import"]["evidence"]
        if input_receipt["phase2f_input_manifest_sha256"] != expected_input_manifest:
            raise IntegrityError("input-materialization receipt differs from bundle inputs")
        dependency_evidence = receipts["dependency_materialization"]["evidence"]
        for lock in dependency_evidence["lockfiles"]:
            lock_path = runtime_control.source_tree.root.joinpath(
                *lock["path"].split("/")
            )
            if (
                not lock_path.is_file()
                or lock_path.is_symlink()
                or hashlib.sha256(lock_path.read_bytes()).hexdigest()
                != lock["sha256"]
            ):
                raise IntegrityError(f"dependency receipt lockfile drift: {lock['path']}")
        topology_evidence = receipts["topology_authorization"]["evidence"]
        topology_config = runtime_control.source_tree.root.joinpath(
            *topology_evidence["config_path"].split("/")
        )
        if (
            not topology_config.is_file()
            or topology_config.is_symlink()
            or hashlib.sha256(topology_config.read_bytes()).hexdigest()
            != topology_evidence["config_sha256"]
        ):
            raise IntegrityError("topology authorization config hash differs from source tree")
        return cls(
            METHOD_LINEAGE_ID,
            VALIDATION_COMPLETION_SCHEMA_VERSION,
            value["independent_reviewer"], tuple(receipt_ids), geometry_engine, receipts
        )


@contextmanager
def python_network_denied() -> Iterator[None]:
    originals = {
        "create_connection": socket.create_connection,
        "getaddrinfo": socket.getaddrinfo,
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "sendto": socket.socket.sendto,
    }

    def denied(*_args, **_kwargs):
        raise RuntimeError("Phase-2F Python network access is disabled")

    socket.create_connection = denied
    socket.getaddrinfo = denied
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.sendto = denied
    try:
        yield
    finally:
        socket.create_connection = originals["create_connection"]
        socket.getaddrinfo = originals["getaddrinfo"]
        socket.socket.connect = originals["connect"]
        socket.socket.connect_ex = originals["connect_ex"]
        socket.socket.sendto = originals["sendto"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _gate_rows(artifacts: OfflineBuildArtifacts) -> list[dict[str, object]]:
    return [
        {
            "name": gate.name,
            "numerator": gate.numerator,
            "denominator": gate.denominator,
            "ratio_numerator": gate.ratio.numerator,
            "ratio_denominator": gate.ratio.denominator,
            "operator": gate.operator,
            "threshold_numerator": gate.threshold.numerator,
            "threshold_denominator": gate.threshold.denominator,
            "passed": gate.passed,
        }
        for gate in artifacts.quality_gates
    ]


def _diagnostic_rows(artifacts: OfflineBuildArtifacts) -> list[dict[str, object]]:
    return [
        {
            "name": diagnostic.name,
            "numerator": diagnostic.numerator,
            "denominator": diagnostic.denominator,
            "ratio_numerator": diagnostic.ratio.numerator,
            "ratio_denominator": diagnostic.ratio.denominator,
        }
        for diagnostic in artifacts.geography_diagnostics
    ]


def _route_cross_tab_rows(rows, *, entity_kind: str) -> list[dict[str, object]]:
    counts = Counter(
        (
            row.direct_eligible,
            row.census_outcome,
            row.hud_outcome,
            row.final_disposition,
        )
        for row in rows
    )
    result = [
        {
            "direct_eligible": direct_eligible,
            "census_outcome": census_outcome,
            "hud_outcome": hud_outcome,
            "final_disposition": final_disposition,
            "count": count,
        }
        for (
            direct_eligible, census_outcome, hud_outcome, final_disposition
        ), count in sorted(counts.items())
    ]
    if sum(row["count"] for row in result) != len(rows):
        raise IntegrityError(f"{entity_kind} route cross-tab does not reconcile")
    return result


def _copy_hud_api_excluded_rows(
    bundle: FrozenBundle,
    directory: Path,
) -> Path:
    roles = bundle.expectation("source_roles", dict)
    if set(roles) != PHASE2F_ROLES:
        raise IntegrityError("excluded-row output requires the exact R4 role set")
    source_id = roles["hud_api_excluded_rows"]
    if bundle.inputs[source_id].schema_id != "hud_api_excluded_rows_json":
        raise IntegrityError("excluded-row output role has the wrong schema")
    path = directory / "hud_api_excluded_rows.json"
    atomic_write(path, bundle.verify_file(source_id).read_bytes())
    return path


def _write_gate_b_audit_artifacts(
    directory: Path,
    inputs: OfflineBuildInputs,
    artifacts: OfflineBuildArtifacts,
) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "waterfall_audit": directory / "waterfall_audit.json",
        "duplicate_audit": directory / "duplicate_audit.json",
        "route_cross_tab": directory / "route_cross_tab.jsonl",
        "provider_route_cross_tab": directory / "provider_route_cross_tab.json",
        "hospital_route_cross_tab": directory / "hospital_route_cross_tab.json",
        "unique_address_counts": directory / "unique_address_counts.json",
        "geocoder_report": directory / "geocoder_report.json",
        "geography_diagnostics": directory / "geography_diagnostics.json",
        "cah_coordinate_method_breakdown": directory / "cah_coordinate_method_breakdown.json",
    }
    atomic_write(
        paths["waterfall_audit"],
        canonical_json_bytes(
            {
                "terminal_counts": artifacts.waterfall.terminal_counts,
                "diagnostic_counts": artifacts.waterfall.diagnostic_counts,
                "d_geo": artifacts.waterfall.d_geo,
                "counted_providers": artifacts.waterfall.counted_providers,
            }
        ),
    )
    atomic_write(paths["duplicate_audit"], canonical_json_bytes(asdict(artifacts.duplicate_audit)))
    atomic_write(
        paths["route_cross_tab"],
        b"".join(canonical_json_bytes(asdict(row)) for row in artifacts.route_cross_tab),
    )
    atomic_write(
        paths["provider_route_cross_tab"],
        canonical_json_bytes(
            {
                "schema_version": "atlas-v19-route-cross-tab-1",
                "entity_kind": "provider",
                "entity_count": len(artifacts.route_cross_tab),
                "cross_tab": _route_cross_tab_rows(
                    artifacts.route_cross_tab, entity_kind="provider"
                ),
            }
        ),
    )
    atomic_write(
        paths["hospital_route_cross_tab"],
        canonical_json_bytes(
            {
                "schema_version": "atlas-v19-route-cross-tab-1",
                "entity_kind": "hospital",
                "entity_count": len(artifacts.hospital_locations),
                "cross_tab": _route_cross_tab_rows(
                    artifacts.hospital_locations, entity_kind="hospital"
                ),
            }
        ),
    )
    atomic_write(
        paths["unique_address_counts"],
        canonical_json_bytes(dict(sorted(artifacts.unique_address_counts.items()))),
    )
    geocoder_report = {
        "request_count": len(inputs.geocodes),
        "outcome_counts": dict(sorted(Counter(row.outcome for row in inputs.geocodes).items())),
        "match_indicator_counts": dict(
            sorted(Counter(row.match_indicator for row in inputs.geocodes).items())
        ),
        "match_type_counts": dict(sorted(Counter(row.match_type for row in inputs.geocodes).items())),
        "accepted_attempt_counts": dict(
            sorted((str(key), value) for key, value in Counter(
                row.accepted_attempt for row in inputs.geocodes
            ).items())
        ),
        "source_response_sha256": sorted(
            {row.source_response_sha256 for row in inputs.geocodes}
        ),
    }
    atomic_write(paths["geocoder_report"], canonical_json_bytes(geocoder_report))
    atomic_write(
        paths["geography_diagnostics"],
        canonical_json_bytes(
            {
                "schema_version": "atlas-v19-geography-diagnostics-1",
                "diagnostics": _diagnostic_rows(artifacts),
            }
        ),
    )
    distance_by_id = {row.facility_id: row for row in artifacts.cah.distances}
    cah_rows = []
    for hospital in sorted(
        (row for row in artifacts.hospital_locations if row.is_cah),
        key=lambda row: row.facility_id,
    ):
        distance = distance_by_id.get(hospital.facility_id)
        coordinate_source = (
            "DIRECT_MATCHED_ADDRESS"
            if hospital.assignment_method == "CENSUS_MATCH"
            else "GAZETTEER_COUNTY_INTERNAL_POINT"
            if hospital.assignment_method == "HUD_BUS_RATIO_SAME_STATE_COUNTY_INTERNAL_POINT"
            else None
        )
        cah_rows.append(
            {
                "facility_id": hospital.facility_id,
                "assigned": hospital.assigned,
                "assignment_method": hospital.assignment_method,
                "coordinate_source": coordinate_source,
                "county_fips": hospital.county_fips,
                "latitude": hospital.latitude,
                "longitude": hospital.longitude,
                "distance_raw_miles": None if distance is None else repr(distance.distance_raw),
                "gte_20_miles_raw": None if distance is None else distance.gte_20,
            }
        )
    atomic_write(
        paths["cah_coordinate_method_breakdown"],
        canonical_json_bytes(
            {
                "total_in_scope": artifacts.cah.total_in_scope,
                "evaluable": artifacts.cah.evaluable,
                "unmatched": artifacts.cah.unmatched,
                "gte_20": artifacts.cah.gte_20,
                "coordinate_method_counts": artifacts.cah.coordinate_method_counts,
                "facilities": cah_rows,
            }
        ),
    )
    return paths


def _persist_gate_rejection(
    output_dir: Path,
    *,
    bundle: FrozenBundle,
    inputs: OfflineBuildInputs,
    failure: ReleaseQualityGateError,
    completed_at_utc: str,
) -> Path:
    """Persist a complete, hash-bound rejection before propagating Gate failure."""

    artifacts = failure.artifacts
    if artifacts is None:
        raise IntegrityError("quality-gate failure omitted the mandatory audit artifacts")
    rejected_dir = output_dir / "rejected"
    audit_paths = _write_gate_b_audit_artifacts(rejected_dir / "audit", inputs, artifacts)
    if bundle.expectations.get("method_lineage_id") == METHOD_LINEAGE_ID:
        audit_paths["hud_api_excluded_rows"] = _copy_hud_api_excluded_rows(
            bundle, rejected_dir / "audit"
        )
    rejection_path = rejected_dir / "rejection_reason.json"
    atomic_write(
        rejection_path,
        canonical_json_bytes(
            {
                "schema_version": "atlas-v19-phase2f-rejection-1",
                "status": "REJECTED",
                "command": "build-offline",
                "bundle_manifest_sha256": bundle.manifest_sha256,
                "authorization_id": bundle.authorization_id,
                "source_tree_id": bundle.source_tree_id,
                **(
                    {"method_lineage_id": METHOD_LINEAGE_ID}
                    if bundle.expectations.get("method_lineage_id") == METHOD_LINEAGE_ID
                    else {}
                ),
                "completed_at_utc": completed_at_utc,
                "reason": str(failure),
                "quality_gates": _gate_rows(artifacts),
            }
        ),
    )
    return write_output_receipt(
        output_dir,
        command="build-offline-rejected",
        bundle=bundle,
        artifacts={**audit_paths, "rejection_reason": rejection_path},
        completed_at_utc=completed_at_utc,
    )


def run_phase2f(
    bundle: FrozenBundle,
    review: ReviewConfig,
    output_dir: Path,
    *,
    completed_at_utc: str | None = None,
) -> Path:
    if bundle.phase != "2F":
        raise IntegrityError("Phase-2F runner received the wrong bundle phase")
    if (
        review.method_lineage_id != METHOD_LINEAGE_ID
        or bundle.expectations.get("method_lineage_id") != METHOD_LINEAGE_ID
        or bundle.source_tree_id == FAILED_R3_SOURCE_TREE_ID
    ):
        raise IntegrityError("Phase-2F runner rejects missing/R3/mixed method lineage")
    output_dir.mkdir(parents=True, exist_ok=False)
    with python_network_denied():
        (
            inputs,
            expectations,
            places_bytes,
            adjacency_rows,
            county_topology,
            state_topology,
        ) = load_phase2f_inputs(bundle, require_chain=True)
        request_chain, census_provenance = verify_phase2f_chain(
            bundle, inputs, review.receipts
        )
        try:
            artifacts = build_release(
                inputs,
                expectations,
                places_snapshot_bytes=places_bytes,
                frozen_inputs=bundle.frozen_inputs(),
            )
        except ReleaseQualityGateError as failure:
            receipt = _persist_gate_rejection(
                output_dir,
                bundle=bundle,
                inputs=inputs,
                failure=failure,
                completed_at_utc=completed_at_utc or _now(),
            )
            failure.add_note(f"atomic rejection receipt: {receipt}")
            raise
        # A second independent bundle reload/build is mandatory.  The frozen
        # environment/source-tree receipts named in ReviewConfig bind the
        # surrounding clean-room identity; this comparison binds the outputs.
        (
            duplicate_inputs,
            duplicate_expectations,
            duplicate_places,
            duplicate_adjacency,
            duplicate_county_topology,
            duplicate_state_topology,
        ) = load_phase2f_inputs(bundle, require_chain=True)
        duplicate_request_chain, duplicate_census_provenance = verify_phase2f_chain(
            bundle, duplicate_inputs, review.receipts
        )
        if (
            request_chain != duplicate_request_chain
            or census_provenance != duplicate_census_provenance
        ):
            raise IntegrityError("duplicate D-II/Census chain verification differs")
        try:
            duplicate_artifacts = build_release(
                duplicate_inputs,
                duplicate_expectations,
                places_snapshot_bytes=duplicate_places,
                frozen_inputs=bundle.frozen_inputs(),
            )
        except ReleaseQualityGateError as failure:
            receipt = _persist_gate_rejection(
                output_dir,
                bundle=bundle,
                inputs=duplicate_inputs,
                failure=failure,
                completed_at_utc=completed_at_utc or _now(),
            )
            failure.add_note(f"atomic rejection receipt: {receipt}")
            raise
        if (
            adjacency_rows != duplicate_adjacency
            or county_topology != duplicate_county_topology
            or state_topology != duplicate_state_topology
        ):
            raise IntegrityError("duplicate Phase-2F static asset loads differ")

        roles = bundle.expectation("source_roles", dict)
        topology_contracts = {
            "county_topology": (
                1_349_047,
                "affdae57c1fe33dd5e64ccf1c735f83db830374d5dcff375231a667aa96aa4d5",
            ),
            "state_topology": (
                465_345,
                "05a7896dc0cd3be17813dbe7b571e74eafd690a6078a99698aa9a52df01b6bbe",
            ),
        }
        for role, (expected_bytes, expected_sha256) in topology_contracts.items():
            source_id = roles[role]
            entry = bundle.inputs[source_id]
            if (
                entry.schema_id != "topology_json"
                or entry.bytes != expected_bytes
                or entry.sha256 != expected_sha256
            ):
                raise IntegrityError(f"{role} differs from the preregistered display asset")

        universe = set(inputs.county_points)
        adjacency = parse_adjacency_snapshot(adjacency_rows, universe)
        adjacency_evidence = review.receipts["adjacency_materialization"]["evidence"]
        adjacency_source_sha256 = adjacency_evidence["source_sha256"]
        if (
            adjacency_evidence["source_id"] != "CENSUS-COUNTY-ADJ-2025"
            or adjacency_source_sha256
            != "8233d2aed342f62eb02bae9f9002ad1d8fc6bd82d739185eba8bad832bf8fc87"
            or adjacency_evidence["county_universe_sha256"]
            != hashlib.sha256(("\n".join(sorted(universe)) + "\n").encode("ascii")).hexdigest()
            or adjacency_evidence["border_count"] != 1_184
            or adjacency_evidence["border_list_sha256"]
            != "8391b5131fd5048537f9feb470bbb39e608f0ce62b716d4f1affe59879ab298f"
        ):
            raise IntegrityError("adjacency receipt differs from the preregistered source/universe")
        adjacency_receipt = build_adjacency_receipt(
            adjacency,
            universe,
            source_id="CENSUS-COUNTY-ADJ-2025",
            source_sha256=adjacency_source_sha256,
            expected_source_sha256="8233d2aed342f62eb02bae9f9002ad1d8fc6bd82d739185eba8bad832bf8fc87",
            expected_border_count=1_184,
            expected_border_sha256="8391b5131fd5048537f9feb470bbb39e608f0ce62b716d4f1affe59879ab298f",
        )
        state_borders = frozenset(
            primary
            for primary, neighbors in adjacency.items()
            if any(neighbor[:2] != primary[:2] for neighbor in neighbors)
        )
        for tiger_role in (
            "tiger_raw_zip", "tiger_candidate_manifest", "tiger_manifest_approval"
        ):
            if not isinstance(roles.get(tiger_role), str):
                raise IntegrityError(f"Phase-2F lacks required TIGER role: {tiger_role}")
        tiger_binding = load_tiger_qa_binding(
            bundle,
            raw_zip_source=roles["tiger_raw_zip"],
            candidate_manifest_source=roles["tiger_candidate_manifest"],
            approval_source=roles["tiger_manifest_approval"],
            county_universe=universe,
            geometry_engine=review.geometry_engine,
            receipt_evidence=review.receipts["tiger_qa_materialization"]["evidence"],
        )
        point_qa_setup = build_point_qa_setup(
            tiger_binding.polygon_source,
            review.geometry_engine,
            universe,
        )
        review_artifacts = build_phase2f_review_artifacts(
            inputs,
            artifacts,
            independent_reviewer=review.independent_reviewer,
            adjacency_receipt=adjacency_receipt,
            state_borders=state_borders,
            point_qa_setup=point_qa_setup,
        )
        duplicate_review = build_phase2f_review_artifacts(
            duplicate_inputs,
            duplicate_artifacts,
            independent_reviewer=review.independent_reviewer,
            adjacency_receipt=adjacency_receipt,
            state_borders=state_borders,
            point_qa_setup=point_qa_setup,
        )
        if (
            review_artifacts.validation.blank_worksheet_sha256
            != tiger_binding.point_qa_worksheet_sha256
            or duplicate_review.validation.blank_worksheet_sha256
            != tiger_binding.point_qa_worksheet_sha256
        ):
            raise IntegrityError(
                "generated point-QA worksheet differs from the approved TIGER receipt"
            )
        deterministic_pairs = {
            "provider_locations": (
                artifacts.provider_locations_bytes,
                duplicate_artifacts.provider_locations_bytes,
            ),
            "hospital_locations": (
                artifacts.hospital_locations_bytes,
                duplicate_artifacts.hospital_locations_bytes,
            ),
            "counties": (artifacts.counties_json, duplicate_artifacts.counties_json),
            "summary": (artifacts.summary_json, duplicate_artifacts.summary_json),
            "hospital_county_name_qa": (
                artifacts.hospital_county_name_qa_bytes,
                duplicate_artifacts.hospital_county_name_qa_bytes,
            ),
            "sensitivity_report": (
                review_artifacts.sensitivity.canonical_bytes,
                duplicate_review.sensitivity.canonical_bytes,
            ),
            "sampling_frame": (
                review_artifacts.validation.sampling_frame_bytes,
                duplicate_review.validation.sampling_frame_bytes,
            ),
            "blank_worksheet": (
                review_artifacts.validation.blank_worksheet_bytes,
                duplicate_review.validation.blank_worksheet_bytes,
            ),
            "validation_receipt": (
                review_artifacts.validation_receipt_bytes,
                duplicate_review.validation_receipt_bytes,
            ),
            "county_topology": (county_topology, duplicate_county_topology),
            "state_topology": (state_topology, duplicate_state_topology),
        }
        differing = sorted(name for name, pair in deterministic_pairs.items() if pair[0] != pair[1])
        if differing:
            raise IntegrityError(f"duplicate clean-room output mismatch: {differing}")
    analytical_dir = output_dir / "analytical"
    pipeline_dir = output_dir / "pipeline"
    app_dir = output_dir / "app"
    for directory in (analytical_dir, pipeline_dir, app_dir):
        directory.mkdir()
    paths = {
        "entity_address_mapping": analytical_dir / "entity_address_mapping.jsonl",
        "provider_locations": analytical_dir / "provider_locations_canonical.jsonl",
        "hospital_locations": analytical_dir / "hospital_locations_canonical.jsonl",
        "hospital_county_name_qa": analytical_dir / "hospital_county_name_qa.json",
        "pipeline_counties": pipeline_dir / "counties.json",
        "app_counties": app_dir / "counties.json",
        "pipeline_summary": pipeline_dir / "summary.json",
        "app_summary": app_dir / "summary.json",
        "app_county_universe": app_dir / "county-universe.ids.txt",
        "app_county_topology": app_dir / "topology" / "counties-2025-5m.topo.json",
        "app_state_topology": app_dir / "topology" / "states-2025-5m.topo.json",
    }
    atomic_write(paths["entity_address_mapping"], artifacts.entity_address_mapping_bytes)
    atomic_write(paths["provider_locations"], artifacts.provider_locations_bytes)
    atomic_write(paths["hospital_locations"], artifacts.hospital_locations_bytes)
    atomic_write(paths["hospital_county_name_qa"], artifacts.hospital_county_name_qa_bytes)
    atomic_write(paths["pipeline_counties"], artifacts.counties_json)
    atomic_write(paths["app_counties"], artifacts.counties_json)
    atomic_write(paths["pipeline_summary"], artifacts.summary_json)
    atomic_write(paths["app_summary"], artifacts.summary_json)
    county_universe_bytes = (
        "\n".join(sorted(inputs.county_points)) + "\n"
    ).encode("ascii")
    if hashlib.sha256(county_universe_bytes).hexdigest() != (
        "f2651ec6a9375476e3ff09efb4c2a699cd5690ffd3f0c982aeacad967c21c241"
    ):
        raise IntegrityError("Phase-2F county-universe list differs from the preregistered set")
    atomic_write(paths["app_county_universe"], county_universe_bytes)
    atomic_write(paths["app_county_topology"], county_topology)
    atomic_write(paths["app_state_topology"], state_topology)

    release_files = (
        ("counties.json", paths["app_counties"]),
        ("county-universe.ids.txt", paths["app_county_universe"]),
        ("summary.json", paths["app_summary"]),
        ("topology/counties-2025-5m.topo.json", paths["app_county_topology"]),
        ("topology/states-2025-5m.topo.json", paths["app_state_topology"]),
    )
    release_manifest = app_dir / "release-manifest.json"
    atomic_write(
        release_manifest,
        canonical_json_bytes(
            {
                "schema_version": 1,
                "source_tree_id": bundle.source_tree_id,
                "files": [
                    {
                        "path": relative,
                        "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for relative, path in release_files
                ],
            }
        ),
    )
    paths["app_release_manifest"] = release_manifest

    review_dir = analytical_dir / "review"
    sensitivity_dir = analytical_dir / "sensitivity"
    review_dir.mkdir()
    sensitivity_dir.mkdir()
    sensitivity_report_path = sensitivity_dir / "report.json"
    atomic_write(sensitivity_report_path, review_artifacts.sensitivity.canonical_bytes)
    paths["sensitivity_report"] = sensitivity_report_path
    for name, body in sorted(review_artifacts.sensitivity.table_bytes.items()):
        path = sensitivity_dir / f"{name.lower()}.jsonl"
        atomic_write(path, body)
        paths[f"sensitivity_{name.lower()}"] = path
    validation_paths = {
        "validation_sampling_frame": review_dir / "sampling_frame.jsonl",
        "validation_selected_sample": review_dir / "selected_sample.jsonl",
        "validation_blank_worksheet": review_dir / "blank_worksheet.jsonl",
        "validation_double_review_sample": review_dir / "double_review_sample.jsonl",
        "validation_adjacency_receipt": review_dir / "adjacency_receipt.json",
        "validation_point_qa_setup": review_dir / "point_qa_setup.json",
        "validation_receipt": review_dir / "validation_receipt.json",
    }
    atomic_write(
        validation_paths["validation_sampling_frame"],
        review_artifacts.validation.sampling_frame_bytes,
    )
    atomic_write(
        validation_paths["validation_selected_sample"],
        review_artifacts.selected_sample_bytes,
    )
    atomic_write(
        validation_paths["validation_blank_worksheet"],
        review_artifacts.validation.blank_worksheet_bytes,
    )
    atomic_write(
        validation_paths["validation_double_review_sample"],
        review_artifacts.double_review_sample_bytes,
    )
    atomic_write(
        validation_paths["validation_adjacency_receipt"],
        review_artifacts.adjacency_receipt.canonical_bytes,
    )
    atomic_write(
        validation_paths["validation_point_qa_setup"],
        review_artifacts.point_qa_setup.canonical_bytes,
    )
    atomic_write(
        validation_paths["validation_receipt"],
        review_artifacts.validation_receipt_bytes,
    )
    paths.update(validation_paths)
    paths.update(_write_gate_b_audit_artifacts(analytical_dir / "audit", inputs, artifacts))
    paths["hud_api_excluded_rows"] = _copy_hud_api_excluded_rows(
        bundle, analytical_dir / "audit"
    )
    gate_rows = _gate_rows(artifacts)
    audit_path = analytical_dir / "build_audit.json"
    atomic_write(
        audit_path,
        canonical_json_bytes(
            {
                "schema_version": "atlas-v19-phase2f-build-audit-1",
                "bundle_manifest_sha256": bundle.manifest_sha256,
                "method_lineage_id": METHOD_LINEAGE_ID,
                "independent_reviewer": review.independent_reviewer,
                "required_receipt_ids": list(review.receipt_ids),
                "provider_locations_sha256": artifacts.provider_locations_sha256,
                "hospital_locations_sha256": artifacts.hospital_locations_sha256,
                "entity_address_mapping_sha256": artifacts.entity_address_mapping_sha256,
                "hospital_county_name_qa_sha256": artifacts.hospital_county_name_qa_sha256,
                "provider_hash_used_for_counts": artifacts.provider_hash_used_for_counts,
                "provider_hash_used_for_distances": artifacts.provider_hash_used_for_distances,
                "hospital_hash_used_for_counts": artifacts.hospital_hash_used_for_counts,
                "hospital_hash_used_for_cah_distances": artifacts.hospital_hash_used_for_cah_distances,
                "sensitivity_report_sha256": review_artifacts.sensitivity.sha256,
                "validation_receipt_sha256": review_artifacts.validation_receipt_sha256,
                "adjacency_receipt_sha256": review_artifacts.adjacency_receipt.sha256,
                "point_qa_setup_sha256": review_artifacts.point_qa_setup.sha256,
                "county_universe_sha256": hashlib.sha256(county_universe_bytes).hexdigest(),
                "app_release_manifest_sha256": hashlib.sha256(
                    release_manifest.read_bytes()
                ).hexdigest(),
                "duplicate_clean_room_reload_count": 2,
                "duplicate_clean_room_output_hashes": {
                    name: hashlib.sha256(pair[0]).hexdigest()
                    for name, pair in sorted(deterministic_pairs.items())
                },
                "manual_review_status": "REQUIRED_BEFORE_GATE_B",
                "validation_completion": {
                    "state": "PENDING_LATER_STATE",
                    "schema_version": VALIDATION_COMPLETION_SCHEMA_VERSION,
                    "method_lineage_id": METHOD_LINEAGE_ID,
                    "selected_sample_sha256": hashlib.sha256(
                        review_artifacts.selected_sample_bytes
                    ).hexdigest(),
                    "blank_worksheet_sha256": hashlib.sha256(
                        review_artifacts.validation.blank_worksheet_bytes
                    ).hexdigest(),
                    "double_review_sample_sha256": hashlib.sha256(
                        review_artifacts.double_review_sample_bytes
                    ).hexdigest(),
                    "global_assigned_state_mismatches": 0,
                },
                "quality_gates": gate_rows,
                "geography_diagnostics": _diagnostic_rows(artifacts),
            }
        ),
    )
    paths["build_audit"] = audit_path
    return write_output_receipt(
        output_dir,
        command="build-offline",
        bundle=bundle,
        artifacts=paths,
        completed_at_utc=completed_at_utc or _now(),
    )
