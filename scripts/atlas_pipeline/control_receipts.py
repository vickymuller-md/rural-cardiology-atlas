"""Receipt-ID-specific schemas for the frozen Phase 2B--2F chain."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from .errors import IntegrityError
from .predecessor_bridge import (
    AMENDMENT04_ADDENDUM01_SHA256,
    AMENDMENT04_SHA256,
    PREDECESSOR_ARTIFACT_SHA256,
    PREDECESSOR_NEGATIVE_MATRIX_SHA256,
    PREDECESSOR_VERIFICATION_PATHS,
    R4B_SOURCE_TREE_ID,
    R4_INPUT_COUNT,
    R4_INPUT_MANIFEST_SHA256,
    R4_METHOD_LINEAGE_ID,
    require_provisional_control_predecessor,
)
from .strict_json import (
    canonical_json_bytes,
    reject_control_sentinels,
    require_canonical_json,
    resolve_regular_contained,
    validate_safe_id,
    validate_safe_relative_posix,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# These receipts are produced by earlier frozen phases and are intentionally
# carried verbatim into the independently hash-pinned Phase-2F control
# manifest.  They must retain their producing bundle/authorization context;
# pretending that they were emitted under the Phase-2F context would sever the
# provenance chain.  The source tree remains identical across all phases.
PHASE2F_CROSS_PHASE_ORIGINAL_IDS = frozenset(
    {
        "input_materialization",
        "nppes_transport",
        "nppes_zip_candidate",
        "nppes_container",
        "storage_preflight",
    }
)


@dataclass(frozen=True)
class ReceiptContext:
    source_tree_id: str
    bundle_manifest_sha256: str
    authorization_id: str

    def validate(self) -> None:
        for label, value in (
            ("source_tree_id", self.source_tree_id),
            ("bundle_manifest_sha256", self.bundle_manifest_sha256),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise IntegrityError(f"receipt context {label} is invalid")
        validate_safe_id(self.authorization_id, label="receipt authorization ID")


@dataclass(frozen=True)
class ReceiptSpec:
    status: str
    command: str
    evidence_keys: tuple[str, ...]
    validate_evidence: Callable[[Mapping[str, object]], None]


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise IntegrityError(f"{label} must be a lowercase SHA-256")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise IntegrityError(f"{label} must be nonblank single-line text")
    return value


def _require_positive_int(value: object, label: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise IntegrityError(f"{label} must be an integer in the allowed range")
    return value


def _require_utc(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IntegrityError(f"{label} must be a UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise IntegrityError(f"{label} is invalid") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise IntegrityError(f"{label} is not UTC")
    return value


def _hash_fields(*fields: str):
    def validate(evidence: Mapping[str, object]) -> None:
        for field in fields:
            _require_sha(evidence[field], field)

    return validate


def _source_tree(e: Mapping[str, object]) -> None:
    from .source_tree import SOURCE_TREE_POLICY_SHA256

    _require_sha(e["source_tree_manifest_sha256"], "source tree manifest")
    _require_sha(e["allowlist_policy_sha256"], "allowlist policy")
    if e["allowlist_policy_sha256"] != SOURCE_TREE_POLICY_SHA256:
        raise IntegrityError("source-tree receipt uses an unknown allowlist policy")
    _require_positive_int(e["file_count"], "source-tree file_count")
    if e["symlink_count"] != 0 or e["protected_files_excluded"] is not True or e["ignored_outputs_excluded"] is not True:
        raise IntegrityError("source-tree freeze did not exclude symlinks/protected/output files")


def _network(e: Mapping[str, object]) -> None:
    _require_text(e["mechanism"], "network-denial mechanism")
    _require_sha(e["policy_sha256"], "network-denial policy")
    _require_text(e["probe_command"], "network-denial probe command")
    if type(e["probe_exit_code"]) is not int or e["probe_exit_code"] == 0:
        raise IntegrityError("network-denial probe must fail under OS-level isolation")
    if _require_utc(e["ended_at_utc"], "network probe end") < _require_utc(e["started_at_utc"], "network probe start"):
        raise IntegrityError("network-denial receipt timestamps are reversed")


def _locks(value: object) -> None:
    if not isinstance(value, list) or len(value) != 3:
        raise IntegrityError("dependency receipt must bind exactly three lockfiles")
    expected = ["app/package-lock.json", "scripts/uv.lock", "tooling/topology/package-lock.json"]
    paths = []
    for row in value:
        if not isinstance(row, dict) or tuple(row) != ("path", "sha256"):
            raise IntegrityError("dependency lockfile pin schema mismatch")
        paths.append(validate_safe_relative_posix(row["path"], label="lockfile path").as_posix())
        _require_sha(row["sha256"], "lockfile hash")
    if sorted(paths) != expected:
        raise IntegrityError("dependency receipt lockfile set is incomplete")


def _dependency(e: Mapping[str, object]) -> None:
    _locks(e["lockfiles"])
    for field in ("cache_or_image_sha256", "network_denied_receipt_sha256"):
        _require_sha(e[field], field)
    for field in (
        "operating_system", "platform", "architecture", "python_version",
        "uv_version", "node_version", "npm_version",
    ):
        _require_text(e[field], field)
    if e["install_command"] != "npm ci --offline --ignore-scripts" or e["install_exit_code"] != 0:
        raise IntegrityError("dependency materialization command/result is not frozen")


def _input_materialization(e: Mapping[str, object]) -> None:
    for field in ("input_manifest_sha256", "materialization_root_sha256", "validator_registry_sha256"):
        _require_sha(e[field], field)
    _require_positive_int(e["artifact_count"], "materialized artifact count")


def _input_materialization_import(e: Mapping[str, object]) -> None:
    for field in (
        "phase2b_bundle_manifest_sha256",
        "input_materialization_receipt_sha256",
        "phase2f_input_manifest_sha256",
        "materialization_root_sha256",
        "validator_registry_sha256",
    ):
        _require_sha(e[field], field)
    validate_safe_id(
        e["phase2b_authorization_id"], label="Phase-2B authorization ID"
    )
    _require_positive_int(e["artifact_count"], "materialized artifact count")


def _source_attempt(e: Mapping[str, object]) -> None:
    validate_safe_id(e["source_id"], label="acquisition source ID")
    validate_safe_id(e["schema_id"], label="acquisition schema ID")
    if not _require_text(e["url"], "acquisition URL").startswith("https://"):
        raise IntegrityError("source acquisition URL must be HTTPS")
    if e["method"] != "GET" or e["response_status"] != 200 or e["content_encoding"] != "identity":
        raise IntegrityError("source acquisition attempt is not a complete identity GET/200")
    headers = e["request_headers"]
    if not isinstance(headers, list) or not headers:
        raise IntegrityError("source acquisition request headers are absent")
    normalized_headers = []
    for row in headers:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not all(isinstance(item, str) and item for item in row)
        ):
            raise IntegrityError("source acquisition request headers are malformed")
        normalized_headers.append((row[0].lower(), row[1]))
    if len({name for name, _value in normalized_headers}) != len(normalized_headers):
        raise IntegrityError("source acquisition request headers are duplicated")
    if ("accept-encoding", "identity") not in normalized_headers:
        raise IntegrityError("source acquisition did not request identity on-wire bytes")
    _require_utc(e["started_at_utc"], "source acquisition start")
    _require_utc(e["ended_at_utc"], "source acquisition end")
    _require_positive_int(e["response_bytes"], "source acquisition bytes")
    _require_sha(e["response_sha256"], "source acquisition hash")
    validate_safe_relative_posix(e["promoted_relative_path"], label="promoted source path")


def _nppes_transport(e: Mapping[str, object]) -> None:
    if not _require_text(e["url"], "NPPES URL").startswith("https://"):
        raise IntegrityError("NPPES URL must be HTTPS")
    filename = validate_safe_relative_posix(e["filename"], label="NPPES filename")
    if len(filename.parts) != 1:
        raise IntegrityError("NPPES filename must not contain directories")
    _require_positive_int(e["expected_bytes"], "NPPES expected bytes")
    _require_sha(e["candidate_sha256"], "NPPES candidate hash")
    _require_sha(e["attempt_receipt_sha256"], "NPPES attempt receipt hash")
    _require_sha(
        e["storage_preflight_receipt_sha256"],
        "NPPES storage-preflight receipt hash",
    )
    if (
        e["response_status"] != 200
        or e["content_encoding"] != "identity"
        or e["container_opened"] is not False
        or e["completed_transfer_count"] != 1
    ):
        raise IntegrityError("NPPES transport did not stop after one identity 200 candidate")


def _nppes_transport_import(e: Mapping[str, object]) -> None:
    for field in (
        "phase2c_bundle_manifest_sha256",
        "nppes_transport_receipt_sha256",
    ):
        _require_sha(e[field], field)
    validate_safe_id(e["phase2c_authorization_id"], label="Phase-2C authorization ID")
    _nppes_transport(e)


def _nppes_candidate(e: Mapping[str, object]) -> None:
    for field in ("archive_sha256", "candidate_manifest_sha256"):
        _require_sha(e[field], field)
    _require_positive_int(e["member_count"], "NPPES ZIP candidate member count")
    if e["payload_opened"] is not False or e["analytical_rows_parsed"] is not False:
        raise IntegrityError("NPPES candidate gate opened payload before approval")


def _nppes_candidate_import(e: Mapping[str, object]) -> None:
    for field in (
        "phase2di_bundle_manifest_sha256",
        "nppes_zip_candidate_receipt_sha256",
    ):
        _require_sha(e[field], field)
    validate_safe_id(
        e["phase2di_authorization_id"], label="Phase-2D-I authorization ID"
    )
    _nppes_candidate(e)


def _nppes_container(e: Mapping[str, object]) -> None:
    for field in (
        "archive_sha256", "candidate_manifest_sha256", "approval_receipt_sha256",
        "approval_trust_root_sha256", "selected_member_central_metadata_sha256",
        "selected_member_sha256",
    ):
        _require_sha(e[field], field)
    validate_safe_id(e["approval_id"], label="NPPES ZIP approval ID")
    selected = validate_safe_relative_posix(
        e["selected_member_filename"], label="NPPES selected member filename"
    )
    if len(selected.parts) != 1:
        raise IntegrityError("NPPES selected member must be a root ZIP filename")
    _require_positive_int(e["selected_member_bytes"], "NPPES selected member bytes")
    _require_positive_int(e["member_count"], "NPPES ZIP member count")
    if e["crc_verified"] is not True or e["analytical_rows_parsed"] is not False:
        raise IntegrityError("NPPES approved CRC gate did not stop before analytical parse")


def _nppes_container_import(e: Mapping[str, object]) -> None:
    for field in (
        "phase2di_bundle_manifest_sha256",
        "nppes_container_receipt_sha256",
    ):
        _require_sha(e[field], field)
    validate_safe_id(
        e["phase2di_authorization_id"], label="Phase-2D-I authorization ID"
    )
    _nppes_container(e)


def _adjacency(e: Mapping[str, object]) -> None:
    if e["source_id"] != "CENSUS-COUNTY-ADJ-2025":
        raise IntegrityError("adjacency source ID is not preregistered")
    for field in ("source_sha256", "county_universe_sha256", "border_list_sha256"):
        _require_sha(e[field], field)
    _require_positive_int(e["border_count"], "adjacency border count")


def _tiger(e: Mapping[str, object]) -> None:
    for field in (
        "raw_zip_sha256", "member_manifest_sha256", "selected_member_set_sha256",
        "county_fips_set_sha256", "geometry_engine_sha256", "loader_sha256",
        "point_qa_worksheet_sha256",
    ):
        _require_sha(e[field], field)
    if e["resolution"] != "FULL_RESOLUTION":
        raise IntegrityError("TIGER point QA must use full-resolution geometry")


def _topology(e: Mapping[str, object]) -> None:
    if validate_safe_relative_posix(e["config_path"], label="topology config path").as_posix() != "tooling/topology/topology-contract.json":
        raise IntegrityError("topology config path is not frozen")
    for field in (
        "config_sha256", "dependency_receipt_sha256", "extracted_members_manifest_sha256",
        "integrator_trust_root_sha256",
    ):
        _require_sha(e[field], field)


def _d2ii(e: Mapping[str, object]) -> None:
    for field in (
        "phase2dii_bundle_manifest_sha256", "d2ii_handoff_receipt_sha256",
        "nppes_archive_sha256", "nppes_candidate_manifest_sha256",
        "nppes_approval_receipt_sha256", "nppes_approval_trust_root_sha256",
        "nppes_selected_member_sha256",
        "provider_mapping_sha256", "hospital_mapping_sha256", "nppes_audit_sha256",
        "nppes_dispositions_sha256", "request_manifest_sha256", "request_chunks_sha256",
        "storage_preflight_receipt_sha256",
    ):
        _require_sha(e[field], field)
    selected_member = validate_safe_relative_posix(
        e["nppes_selected_member_filename"], label="NPPES selected member filename"
    )
    if len(selected_member.parts) != 1:
        raise IntegrityError("NPPES selected member must be a root ZIP filename")
    _require_positive_int(e["request_count"], "Census request count")
    _require_positive_int(e["chunk_count"], "Census chunk count")


def _d2ii_handoff(e: Mapping[str, object]) -> None:
    for field in (
        "phase2dii_bundle_manifest_sha256", "nppes_archive_sha256",
        "nppes_candidate_manifest_sha256", "nppes_approval_receipt_sha256",
        "nppes_approval_trust_root_sha256",
        "nppes_selected_member_sha256", "provider_mapping_sha256",
        "hospital_mapping_sha256", "nppes_audit_sha256",
        "nppes_dispositions_sha256", "request_manifest_sha256",
        "request_chunks_sha256", "storage_preflight_receipt_sha256",
    ):
        _require_sha(e[field], field)
    selected_member = validate_safe_relative_posix(
        e["nppes_selected_member_filename"], label="NPPES selected member filename"
    )
    if len(selected_member.parts) != 1:
        raise IntegrityError("NPPES selected member must be a root ZIP filename")
    _require_positive_int(e["request_count"], "Census request count")
    _require_positive_int(e["chunk_count"], "Census chunk count")


def _census(e: Mapping[str, object]) -> None:
    for field in (
        "phase2e_bundle_manifest_sha256", "request_manifest_sha256",
        "attempts_manifest_sha256", "canonical_snapshot_sha256",
        "geocoder_acquisition_receipt_sha256",
    ):
        _require_sha(e[field], field)
    if type(e["benchmark_id"]) is not int or type(e["vintage_id"]) is not int:
        raise IntegrityError("Census benchmark/vintage IDs must be integers")
    _require_positive_int(e["attempt_count"], "Census attempt count")


def _census_handoff(e: Mapping[str, object]) -> None:
    for field in (
        "phase2e_bundle_manifest_sha256", "request_manifest_sha256",
        "attempts_manifest_sha256", "canonical_snapshot_sha256",
    ):
        _require_sha(e[field], field)
    if type(e["benchmark_id"]) is not int or type(e["vintage_id"]) is not int:
        raise IntegrityError("Census benchmark/vintage IDs must be integers")
    _require_positive_int(e["attempt_count"], "Census attempt count")


def _hud(e: Mapping[str, object]) -> None:
    url = _require_text(e["url"], "HUD URL")
    if not url.startswith("https://"):
        raise IntegrityError("HUD URL must be HTTPS")
    validate_safe_relative_posix(e["filename"], label="HUD filename")
    for field in (
        "phase2e_bundle_manifest_sha256", "source_acquisition_receipt_sha256",
        "raw_xlsx_sha256", "canonical_csv_sha256", "compatibility_receipt_sha256",
        "hud_data_handoff_sha256", "hud_handoff_receipt_sha256",
    ):
        _require_sha(e[field], field)
    if e["release_year"] != 2025 or e["state_basis"] != "CT_PLANNING_REGIONS_2022_PLUS":
        raise IntegrityError("HUD release is incompatible with the frozen county universe")


def _hud_handoff(e: Mapping[str, object]) -> None:
    url = _require_text(e["url"], "HUD URL")
    if not url.startswith("https://"):
        raise IntegrityError("HUD URL must be HTTPS")
    validate_safe_relative_posix(e["filename"], label="HUD filename")
    for field in (
        "phase2e_bundle_manifest_sha256", "source_acquisition_receipt_sha256",
        "raw_xlsx_sha256", "canonical_csv_sha256", "compatibility_receipt_sha256",
        "hud_data_handoff_sha256",
    ):
        _require_sha(e[field], field)
    if e["release_year"] != 2025 or e["state_basis"] != "CT_PLANNING_REGIONS_2022_PLUS":
        raise IntegrityError("HUD release is incompatible with the frozen county universe")


def _hud_api_common(e: Mapping[str, object]) -> None:
    url = _require_text(e["url"], "HUD API URL")
    if url != "https://www.huduser.gov/hudapi/public/usps":
        raise IntegrityError("HUD API URL differs from Amendment 03")
    if e["filename"] != "HUD_USPS_ZIP_COUNTY_2025Q4_API_RAW.json":
        raise IntegrityError("HUD API filename differs from Amendment 03")
    validate_safe_relative_posix(e["filename"], label="HUD API filename")
    for field in (
        "phase2e_bundle_manifest_sha256", "request_parameters_sha256",
        "frozen_acquisition_receipt_sha256", "import_receipt_sha256",
        "raw_json_sha256", "canonical_csv_sha256", "county_universe_sha256",
        "excluded_rows_sha256", "compatibility_receipt_sha256",
        "hud_api_data_handoff_sha256",
    ):
        _require_sha(e[field], field)
    if (
        e["frozen_acquisition_receipt_sha256"]
        != "013d18d4019534e28bb2306d0d0fd1afce6b9b911e8a37da7d0489405a2d6782"
        or e["raw_json_sha256"]
        != "72f7c78d1439b6b38c0ebc5a163765c6c0e90caae1bc921e59a0a4a03b99cda4"
        or e["result_row_count"] != 54_571
        or type(e["in_scope_row_count"]) is not int
        or not 1 <= e["in_scope_row_count"] <= e["result_row_count"]
        or type(e["excluded_row_count"]) is not int
        or e["excluded_row_count"] < 0
        or type(e["excluded_rows_bytes"]) is not int
        or e["excluded_rows_bytes"] <= 0
        or e["in_scope_row_count"] + e["excluded_row_count"]
        != e["result_row_count"]
        or e["release_year"] != 2025
        or e["state_basis"] != "CT_PLANNING_REGIONS_2022_PLUS"
    ):
        raise IntegrityError("HUD API release/accounting is incompatible with Amendment 03")


def _hud_api_handoff(e: Mapping[str, object]) -> None:
    _hud_api_common(e)


def _hud_api(e: Mapping[str, object]) -> None:
    _hud_api_common(e)
    _require_sha(e["hud_api_handoff_receipt_sha256"], "HUD API handoff receipt hash")


def _places(e: Mapping[str, object]) -> None:
    for field in (
        "phase2dii_bundle_manifest_sha256", "source_acquisition_receipt_sha256",
        "places_handoff_receipt_sha256", "raw_source_sha256",
        "canonical_table_sha256", "county_universe_sha256",
    ):
        _require_sha(e[field], field)
    if e["raw_county_rows"] != 2_957 or e["retained_rows"] != 2_956 or e["suppressed_rows"] != 1 or e["national_rows_excluded"] != 1:
        raise IntegrityError("PLACES derivation counts differ from the preregistered contract")


def _places_handoff(e: Mapping[str, object]) -> None:
    for field in (
        "phase2dii_bundle_manifest_sha256", "source_acquisition_receipt_sha256",
        "raw_source_sha256", "canonical_table_sha256", "county_universe_sha256",
    ):
        _require_sha(e[field], field)
    if e["raw_county_rows"] != 2_957 or e["retained_rows"] != 2_956 or e["suppressed_rows"] != 1 or e["national_rows_excluded"] != 1:
        raise IntegrityError("PLACES derivation counts differ from the preregistered contract")


def _free_space(e: Mapping[str, object]) -> None:
    work_root = _require_text(e["work_root"], "storage work root")
    if not work_root.startswith("/") or "/../" in f"{work_root}/":
        raise IntegrityError("storage work root must be an absolute normalized path")
    output_parent = _require_text(e["output_parent"], "storage output parent")
    if (
        "/../" in f"{output_parent}/"
        or "//" in output_parent
        or not output_parent.startswith(f"{work_root.rstrip('/')}/")
    ):
        raise IntegrityError("storage output parent must be contained by the approved work root")
    _require_text(e["device"], "storage device")
    _require_positive_int(e["st_dev"], "storage st_dev")
    _require_utc(e["checked_at_utc"], "storage check time")
    _require_positive_int(e["free_bytes"], "free bytes")
    _require_positive_int(e["worst_case_bytes"], "worst-case bytes")
    _require_positive_int(e["minimum_required_bytes"], "minimum required bytes")
    _require_sha(e["filesystem_identity_sha256"], "filesystem identity")
    validate_safe_id(e["integrator_control_id"], label="storage integrator control ID")
    _require_text(e["approved_by"], "storage approver")
    minimum = 25 * 1024**3
    if (
        e["minimum_required_bytes"] != minimum
        or e["worst_case_bytes"] < minimum
        or e["free_bytes"] < e["worst_case_bytes"]
        or e["approved"] is not True
    ):
        raise IntegrityError("NPPES free-space approval is insufficient")


def _clean_room(e: Mapping[str, object]) -> None:
    for field in (
        "input_manifest_sha256", "environment_receipt_sha256", "network_denied_receipt_sha256",
        "run_a_receipt_sha256", "run_b_receipt_sha256", "comparison_receipt_sha256",
    ):
        _require_sha(e[field], field)
    if e["process_count"] != 2 or e["empty_root_count"] != 2:
        raise IntegrityError("clean-room orchestration did not use two separate processes/roots")


def _comparison(e: Mapping[str, object]) -> None:
    for field in ("run_a_receipt_sha256", "run_b_receipt_sha256", "compared_artifacts_sha256"):
        _require_sha(e[field], field)
    _require_positive_int(e["artifact_count"], "clean-room compared artifact count")
    if e["equal"] is not True:
        raise IntegrityError("clean-room comparison did not match")


def _validation_completion(e: Mapping[str, object]) -> None:
    if e["method_lineage_id"] != "ATLAS-V19-AMENDMENT03-R4":
        raise IntegrityError("validation completion uses the wrong method lineage")
    for field in (
        "phase2f_output_receipt_sha256", "review_config_sha256",
        "selected_sample_sha256", "blank_worksheet_sha256",
        "completed_primary_sha256", "double_review_sample_sha256",
        "completed_secondary_sha256", "adjudication_sha256",
        "validation_completion_receipt_sha256",
    ):
        _require_sha(e[field], field)
    if (
        e["validation_status"] not in {"PASS", "FAIL"}
        or type(e["global_assigned_state_mismatches"]) is not int
        or e["global_assigned_state_mismatches"] < 0
    ):
        raise IntegrityError("validation-completion result fields are invalid")


def _predecessor_bridge_audit(e: Mapping[str, object]) -> None:
    for field in (
        "amendment04_sha256",
        "amendment04_addendum01_sha256",
        "active_source_tree_id",
        "input_manifest_sha256",
        "predecessor_source_tree_id",
        "predecessor_allowlist_sha256",
        "integration_harness_sha256",
        "negative_matrix_sha256",
        "encounter_ledger_sha256",
    ):
        _require_sha(e[field], field)
    if (
        type(e["input_count"]) is not int
        or type(e["request_count"]) is not int
        or type(e["census_attempt_count"]) is not int
        or type(e["unlisted_encounter_count"]) is not int
        or
        e["amendment04_sha256"] != AMENDMENT04_SHA256
        or e["amendment04_addendum01_sha256"]
        != AMENDMENT04_ADDENDUM01_SHA256
        or e["method_lineage_id"] != R4_METHOD_LINEAGE_ID
        or e["input_manifest_sha256"] != R4_INPUT_MANIFEST_SHA256
        or e["input_count"] != R4_INPUT_COUNT
        or e["predecessor_source_tree_id"] != R4B_SOURCE_TREE_ID
        or e["negative_matrix_sha256"] != PREDECESSOR_NEGATIVE_MATRIX_SHA256
        or e["request_count"] != 22635
        or e["census_attempt_count"] != 5
        or e["negative_matrix_status"] != "PASS"
        or e["load_phase2f_inputs_passed"] is not True
        or e["verify_phase2f_chain_passed"] is not True
        or e["unlisted_encounter_count"] != 0
        or e["network_accessed"] is not False
    ):
        raise IntegrityError("predecessor bridge audit constants/status differ")

    expected_ids = sorted(PREDECESSOR_ARTIFACT_SHA256)
    predecessor_rows = e["predecessor_rows"]
    if not isinstance(predecessor_rows, list) or len(predecessor_rows) != 12:
        raise IntegrityError("predecessor bridge audit must contain twelve rows")
    observed_ids: list[str] = []
    allowlist_rows: list[dict[str, str]] = []
    for row in predecessor_rows:
        if not isinstance(row, dict) or tuple(row) != (
            "artifact_id", "sha256", "body_sha256", "body_verified"
        ):
            raise IntegrityError("predecessor bridge row keys/order mismatch")
        artifact_id = row["artifact_id"]
        if not isinstance(artifact_id, str):
            raise IntegrityError("predecessor bridge artifact ID must be text")
        expected_sha256 = PREDECESSOR_ARTIFACT_SHA256.get(artifact_id)
        if (
            expected_sha256 is None
            or row["sha256"] != expected_sha256
            or row["body_sha256"] != expected_sha256
            or row["body_verified"] is not True
        ):
            raise IntegrityError("predecessor bridge row is not an exact frozen pair")
        observed_ids.append(artifact_id)
        allowlist_rows.append({"artifact_id": artifact_id, "sha256": expected_sha256})
    if observed_ids != expected_ids:
        raise IntegrityError("predecessor bridge rows are unsorted, duplicated or incomplete")
    allowlist = {
        "schema_version": "atlas-v19-predecessor-allowlist-1",
        "rows": allowlist_rows,
    }
    if hashlib.sha256(canonical_json_bytes(allowlist)).hexdigest() != e["predecessor_allowlist_sha256"]:
        raise IntegrityError("predecessor allowlist hash differs from its canonical rows")

    ledger = e["encounter_ledger"]
    if not isinstance(ledger, dict) or tuple(ledger) != ("schema_version", "rows"):
        raise IntegrityError("predecessor encounter ledger keys/order mismatch")
    if ledger["schema_version"] != "atlas-v19-predecessor-encounter-ledger-1":
        raise IntegrityError("predecessor encounter ledger schema mismatch")
    ledger_rows = ledger["rows"]
    if not isinstance(ledger_rows, list) or len(ledger_rows) != 12:
        raise IntegrityError("predecessor encounter ledger must contain twelve rows")
    ledger_ids: list[str] = []
    for row in ledger_rows:
        if not isinstance(row, dict) or tuple(row) != (
            "artifact_id", "sha256", "encounter_count", "verification_path"
        ):
            raise IntegrityError("predecessor encounter row keys/order mismatch")
        artifact_id = row["artifact_id"]
        if (
            not isinstance(artifact_id, str)
            or row["sha256"] != PREDECESSOR_ARTIFACT_SHA256.get(artifact_id)
            or type(row["encounter_count"]) is not int
            or row["encounter_count"] != 1
            or row["verification_path"] != PREDECESSOR_VERIFICATION_PATHS.get(artifact_id)
        ):
            raise IntegrityError("predecessor encounter row differs from the frozen path")
        ledger_ids.append(artifact_id)
    if ledger_ids != expected_ids:
        raise IntegrityError("predecessor encounter rows are unsorted, duplicated or incomplete")
    if hashlib.sha256(canonical_json_bytes(ledger)).hexdigest() != e["encounter_ledger_sha256"]:
        raise IntegrityError("predecessor encounter ledger hash mismatch")


SPECS: Mapping[str, ReceiptSpec] = {
    "source_tree_freeze": ReceiptSpec("frozen_verified", "verify-source-tree", ("source_tree_manifest_sha256", "file_count", "allowlist_policy_sha256", "symlink_count", "protected_files_excluded", "ignored_outputs_excluded"), _source_tree),
    "dependency_materialization": ReceiptSpec("verified_frozen", "materialize-dependencies", ("lockfiles", "cache_or_image_sha256", "operating_system", "platform", "architecture", "python_version", "uv_version", "node_version", "npm_version", "install_command", "install_exit_code", "network_denied_receipt_sha256"), _dependency),
    "network_denied": ReceiptSpec("verified_os_level", "verify-network-denial", ("mechanism", "policy_sha256", "probe_command", "probe_exit_code", "started_at_utc", "ended_at_utc"), _network),
    "input_materialization": ReceiptSpec("verified_frozen", "materialize-inputs", ("input_manifest_sha256", "artifact_count", "materialization_root_sha256", "validator_registry_sha256"), _input_materialization),
    "input_materialization_import": ReceiptSpec("verified_import", "verify-input-materialization", ("phase2b_bundle_manifest_sha256", "phase2b_authorization_id", "input_materialization_receipt_sha256", "phase2f_input_manifest_sha256", "artifact_count", "materialization_root_sha256", "validator_registry_sha256"), _input_materialization_import),
    "source_acquisition_attempt": ReceiptSpec("complete", "acquire-known-hash-source", ("source_id", "schema_id", "url", "method", "request_headers", "started_at_utc", "ended_at_utc", "response_status", "content_encoding", "response_bytes", "response_sha256", "promoted_relative_path"), _source_attempt),
    "nppes_transport": ReceiptSpec("candidate_complete_stopped_before_container_open", "acquire-nppes-transport", ("url", "filename", "expected_bytes", "candidate_sha256", "attempt_receipt_sha256", "storage_preflight_receipt_sha256", "response_status", "content_encoding", "container_opened", "completed_transfer_count"), _nppes_transport),
    "nppes_transport_import": ReceiptSpec("verified_import", "verify-nppes-transport", ("phase2c_bundle_manifest_sha256", "phase2c_authorization_id", "nppes_transport_receipt_sha256", "url", "filename", "expected_bytes", "candidate_sha256", "attempt_receipt_sha256", "storage_preflight_receipt_sha256", "response_status", "content_encoding", "container_opened", "completed_transfer_count"), _nppes_transport_import),
    "nppes_zip_candidate": ReceiptSpec("candidate_manifest_complete_stopped_before_payload", "inspect-nppes-container", ("archive_sha256", "candidate_manifest_sha256", "member_count", "payload_opened", "analytical_rows_parsed"), _nppes_candidate),
    "nppes_zip_candidate_import": ReceiptSpec("verified_import", "verify-nppes-zip-candidate", ("phase2di_bundle_manifest_sha256", "phase2di_authorization_id", "nppes_zip_candidate_receipt_sha256", "archive_sha256", "candidate_manifest_sha256", "member_count", "payload_opened", "analytical_rows_parsed"), _nppes_candidate_import),
    "nppes_container": ReceiptSpec("crc_verified_stopped_before_analytical_parse", "verify-nppes-container", ("archive_sha256", "candidate_manifest_sha256", "approval_id", "approval_receipt_sha256", "approval_trust_root_sha256", "selected_member_filename", "selected_member_central_metadata_sha256", "selected_member_bytes", "selected_member_sha256", "member_count", "crc_verified", "analytical_rows_parsed"), _nppes_container),
    "nppes_container_import": ReceiptSpec("verified_import", "verify-nppes-container-import", ("phase2di_bundle_manifest_sha256", "phase2di_authorization_id", "nppes_container_receipt_sha256", "archive_sha256", "candidate_manifest_sha256", "approval_id", "approval_receipt_sha256", "approval_trust_root_sha256", "selected_member_filename", "selected_member_central_metadata_sha256", "selected_member_bytes", "selected_member_sha256", "member_count", "crc_verified", "analytical_rows_parsed"), _nppes_container_import),
    "adjacency_materialization": ReceiptSpec("verified_frozen", "materialize-adjacency", ("source_id", "source_sha256", "county_universe_sha256", "border_count", "border_list_sha256"), _adjacency),
    "tiger_qa_materialization": ReceiptSpec("verified_frozen", "materialize-tiger-qa", ("raw_zip_sha256", "member_manifest_sha256", "selected_member_set_sha256", "county_fips_set_sha256", "resolution", "geometry_engine_sha256", "loader_sha256", "point_qa_worksheet_sha256"), _tiger),
    "topology_authorization": ReceiptSpec("authorized_phase_2b_by_integrator", "authorize-topology", ("config_path", "config_sha256", "dependency_receipt_sha256", "extracted_members_manifest_sha256", "integrator_trust_root_sha256"), _topology),
    "storage_preflight": ReceiptSpec("approved", "approve-storage-preflight", ("work_root", "output_parent", "device", "st_dev", "checked_at_utc", "free_bytes", "worst_case_bytes", "minimum_required_bytes", "filesystem_identity_sha256", "integrator_control_id", "approved_by", "approved"), _free_space),
    "d2ii_handoff": ReceiptSpec("complete", "prepare-requests", ("phase2dii_bundle_manifest_sha256", "nppes_archive_sha256", "nppes_candidate_manifest_sha256", "nppes_approval_receipt_sha256", "nppes_approval_trust_root_sha256", "nppes_selected_member_filename", "nppes_selected_member_sha256", "provider_mapping_sha256", "hospital_mapping_sha256", "nppes_audit_sha256", "nppes_dispositions_sha256", "request_manifest_sha256", "request_chunks_sha256", "request_count", "chunk_count", "storage_preflight_receipt_sha256"), _d2ii_handoff),
    "d2ii_prepare_requests": ReceiptSpec("verified_import", "verify-d2ii-handoff", ("phase2dii_bundle_manifest_sha256", "d2ii_handoff_receipt_sha256", "nppes_archive_sha256", "nppes_candidate_manifest_sha256", "nppes_approval_receipt_sha256", "nppes_approval_trust_root_sha256", "nppes_selected_member_filename", "nppes_selected_member_sha256", "provider_mapping_sha256", "hospital_mapping_sha256", "nppes_audit_sha256", "nppes_dispositions_sha256", "request_manifest_sha256", "request_chunks_sha256", "request_count", "chunk_count", "storage_preflight_receipt_sha256"), _d2ii),
    "census_handoff": ReceiptSpec("complete", "acquire-census", ("phase2e_bundle_manifest_sha256", "request_manifest_sha256", "attempts_manifest_sha256", "canonical_snapshot_sha256", "benchmark_id", "vintage_id", "attempt_count"), _census_handoff),
    "census_acquisition": ReceiptSpec("verified_import", "verify-census-handoff", ("phase2e_bundle_manifest_sha256", "request_manifest_sha256", "attempts_manifest_sha256", "canonical_snapshot_sha256", "geocoder_acquisition_receipt_sha256", "benchmark_id", "vintage_id", "attempt_count"), _census),
    "hud_handoff": ReceiptSpec("complete", "acquire-hud", ("phase2e_bundle_manifest_sha256", "url", "filename", "source_acquisition_receipt_sha256", "raw_xlsx_sha256", "canonical_csv_sha256", "release_year", "state_basis", "compatibility_receipt_sha256", "hud_data_handoff_sha256"), _hud_handoff),
    "hud_acquisition": ReceiptSpec("verified_import", "verify-hud-handoff", ("phase2e_bundle_manifest_sha256", "url", "filename", "source_acquisition_receipt_sha256", "raw_xlsx_sha256", "canonical_csv_sha256", "release_year", "state_basis", "compatibility_receipt_sha256", "hud_data_handoff_sha256", "hud_handoff_receipt_sha256"), _hud),
    "hud_api_handoff": ReceiptSpec("complete", "import-hud-api", ("phase2e_bundle_manifest_sha256", "url", "filename", "request_parameters_sha256", "frozen_acquisition_receipt_sha256", "import_receipt_sha256", "raw_json_sha256", "canonical_csv_sha256", "county_universe_sha256", "result_row_count", "in_scope_row_count", "excluded_row_count", "excluded_rows_bytes", "excluded_rows_sha256", "release_year", "state_basis", "compatibility_receipt_sha256", "hud_api_data_handoff_sha256"), _hud_api_handoff),
    "hud_api_acquisition": ReceiptSpec("verified_import", "verify-hud-api-handoff", ("phase2e_bundle_manifest_sha256", "url", "filename", "request_parameters_sha256", "frozen_acquisition_receipt_sha256", "import_receipt_sha256", "raw_json_sha256", "canonical_csv_sha256", "county_universe_sha256", "result_row_count", "in_scope_row_count", "excluded_row_count", "excluded_rows_bytes", "excluded_rows_sha256", "release_year", "state_basis", "compatibility_receipt_sha256", "hud_api_data_handoff_sha256", "hud_api_handoff_receipt_sha256"), _hud_api),
    "places_handoff": ReceiptSpec("complete", "derive-places-county", ("phase2dii_bundle_manifest_sha256", "source_acquisition_receipt_sha256", "raw_source_sha256", "canonical_table_sha256", "county_universe_sha256", "raw_county_rows", "retained_rows", "suppressed_rows", "national_rows_excluded"), _places_handoff),
    "places_derivation": ReceiptSpec("verified_import", "verify-places-handoff", ("phase2dii_bundle_manifest_sha256", "source_acquisition_receipt_sha256", "places_handoff_receipt_sha256", "raw_source_sha256", "canonical_table_sha256", "county_universe_sha256", "raw_county_rows", "retained_rows", "suppressed_rows", "national_rows_excluded"), _places),
    "phase2f_clean_room": ReceiptSpec("complete", "run-clean-room", ("input_manifest_sha256", "environment_receipt_sha256", "network_denied_receipt_sha256", "run_a_receipt_sha256", "run_b_receipt_sha256", "comparison_receipt_sha256", "process_count", "empty_root_count"), _clean_room),
    "phase2f_comparison": ReceiptSpec("equal", "compare-clean-room", ("run_a_receipt_sha256", "run_b_receipt_sha256", "compared_artifacts_sha256", "artifact_count", "equal"), _comparison),
    "validation_completion": ReceiptSpec("complete", "validation-complete", ("method_lineage_id", "phase2f_output_receipt_sha256", "review_config_sha256", "selected_sample_sha256", "blank_worksheet_sha256", "completed_primary_sha256", "double_review_sample_sha256", "completed_secondary_sha256", "adjudication_sha256", "global_assigned_state_mismatches", "validation_status", "validation_completion_receipt_sha256"), _validation_completion),
    "predecessor_bridge_audit": ReceiptSpec("verified", "verify-predecessor-bridge", ("amendment04_sha256", "amendment04_addendum01_sha256", "active_source_tree_id", "method_lineage_id", "input_manifest_sha256", "input_count", "predecessor_source_tree_id", "predecessor_rows", "predecessor_allowlist_sha256", "integration_harness_sha256", "negative_matrix_sha256", "negative_matrix_status", "load_phase2f_inputs_passed", "verify_phase2f_chain_passed", "request_count", "census_attempt_count", "encounter_ledger", "encounter_ledger_sha256", "unlisted_encounter_count", "network_accessed"), _predecessor_bridge_audit),
}


def validate_control_receipt(
    receipt_id: str,
    body: bytes,
    *,
    context: ReceiptContext,
) -> Mapping[str, object]:
    context.validate()
    try:
        spec = SPECS[receipt_id]
    except KeyError as exc:
        raise IntegrityError(f"unregistered receipt ID: {receipt_id}") from exc
    value = require_canonical_json(body, label=f"{receipt_id} receipt")
    if not isinstance(value, dict) or tuple(value) != (
        "schema_version", "receipt_id", "status", "source_tree_id",
        "bundle_manifest_sha256", "authorization_id", "command",
        "completed_at_utc", "evidence",
    ):
        raise IntegrityError(f"{receipt_id} receipt keys/order mismatch")
    reject_control_sentinels(value, label=f"{receipt_id} receipt")
    if (
        value["schema_version"] != f"atlas-v19-{receipt_id.replace('_', '-')}-receipt-1"
        or value["receipt_id"] != receipt_id
        or value["status"] != spec.status
        or value["source_tree_id"] != context.source_tree_id
        or value["bundle_manifest_sha256"] != context.bundle_manifest_sha256
        or value["authorization_id"] != context.authorization_id
        or value["command"] != spec.command
    ):
        raise IntegrityError(f"{receipt_id} semantic binding mismatch")
    _require_utc(value["completed_at_utc"], f"{receipt_id} completion")
    evidence = value["evidence"]
    if not isinstance(evidence, dict) or tuple(evidence) != spec.evidence_keys:
        raise IntegrityError(f"{receipt_id} evidence keys/order mismatch")
    spec.validate_evidence(evidence)
    if (
        receipt_id == "predecessor_bridge_audit"
        and evidence["active_source_tree_id"] != value["source_tree_id"]
    ):
        raise IntegrityError("predecessor bridge active tree differs from receipt envelope")
    if (
        receipt_id == "source_tree_freeze"
        and evidence["source_tree_manifest_sha256"] != context.source_tree_id
    ):
        raise IntegrityError("source-tree receipt does not bind its canonical source_tree_id")
    return value


def validate_receipt_set(
    receipts: Mapping[str, bytes],
    *,
    required_ids: set[str] | frozenset[str],
    context: ReceiptContext,
    cross_phase_original_ids: set[str] | frozenset[str] = frozenset(),
) -> Mapping[str, Mapping[str, object]]:
    if set(receipts) != set(required_ids):
        raise IntegrityError("receipt ID set does not equal the required phase set")
    if not set(cross_phase_original_ids) <= set(required_ids):
        raise IntegrityError("cross-phase receipt IDs are absent from the required set")
    digests = [hashlib.sha256(body).hexdigest() for body in receipts.values()]
    if len(digests) != len(set(digests)):
        raise IntegrityError("one generic receipt was reused for distinct receipt IDs")
    parsed: dict[str, Mapping[str, object]] = {}
    for receipt_id, body in receipts.items():
        receipt_context = context
        if receipt_id in cross_phase_original_ids:
            original = require_canonical_json(
                body, label=f"{receipt_id} cross-phase original receipt"
            )
            if not isinstance(original, dict):
                raise IntegrityError("cross-phase original receipt must be an object")
            original_context = ReceiptContext(
                original.get("source_tree_id"),
                original.get("bundle_manifest_sha256"),
                original.get("authorization_id"),
            )
            original_context.validate()
            if original_context.source_tree_id != context.source_tree_id:
                require_provisional_control_predecessor(
                    active_source_tree_id=context.source_tree_id,
                    observed_source_tree_id=original_context.source_tree_id,
                    artifact_id=receipt_id,
                    body=body,
                )
            if (
                original_context.bundle_manifest_sha256
                == context.bundle_manifest_sha256
                or original_context.authorization_id == context.authorization_id
            ):
                raise IntegrityError(
                    "cross-phase original receipt was recontextualized as Phase 2F"
                )
            receipt_context = original_context
        parsed[receipt_id] = validate_control_receipt(
            receipt_id, body, context=receipt_context
        )
    if "input_materialization" in parsed:
        receipt = parsed["input_materialization"]
        if (
            receipt["evidence"]["input_manifest_sha256"]
            != receipt["bundle_manifest_sha256"]
        ):
            raise IntegrityError(
                "input-materialization receipt does not bind its producing manifest"
            )
    if {"input_materialization", "input_materialization_import"}.issubset(parsed):
        original = parsed["input_materialization"]
        imported = parsed["input_materialization_import"]["evidence"]
        original_evidence = original["evidence"]
        if (
            imported["phase2b_bundle_manifest_sha256"]
            != original["bundle_manifest_sha256"]
            or imported["phase2b_authorization_id"] != original["authorization_id"]
            or imported["input_materialization_receipt_sha256"]
            != hashlib.sha256(receipts["input_materialization"]).hexdigest()
            or any(
                imported[field] != original_evidence[field]
                for field in (
                    "artifact_count",
                    "materialization_root_sha256",
                    "validator_registry_sha256",
                )
            )
        ):
            raise IntegrityError(
                "Phase-2F input-materialization import does not bind the Phase-2B original"
            )
    if "network_denied" in parsed and "dependency_materialization" in parsed:
        expected = hashlib.sha256(receipts["network_denied"]).hexdigest()
        actual = parsed["dependency_materialization"]["evidence"]["network_denied_receipt_sha256"]
        if actual != expected:
            raise IntegrityError("dependency receipt does not bind the distinct network-denial receipt")
    if "dependency_materialization" in parsed and "topology_authorization" in parsed:
        expected = hashlib.sha256(receipts["dependency_materialization"]).hexdigest()
        actual = parsed["topology_authorization"]["evidence"]["dependency_receipt_sha256"]
        if actual != expected:
            raise IntegrityError("topology authorization does not bind the dependency receipt")
    d2ii_receipt_id = next(
        (receipt_id for receipt_id in ("d2ii_handoff", "d2ii_prepare_requests") if receipt_id in parsed),
        None,
    )
    if "storage_preflight" in parsed and d2ii_receipt_id is not None:
        expected = hashlib.sha256(receipts["storage_preflight"]).hexdigest()
        actual = parsed[d2ii_receipt_id]["evidence"]["storage_preflight_receipt_sha256"]
        if actual != expected:
            raise IntegrityError("D-II receipt does not bind the free-space approval")
    if {"nppes_transport", "nppes_zip_candidate", "nppes_container"}.issubset(parsed):
        transport_hash = parsed["nppes_transport"]["evidence"]["candidate_sha256"]
        candidate = parsed["nppes_zip_candidate"]["evidence"]
        container = parsed["nppes_container"]["evidence"]
        candidate_receipt = parsed["nppes_zip_candidate"]
        container_receipt = parsed["nppes_container"]
        if (
            candidate_receipt["bundle_manifest_sha256"]
            != container_receipt["bundle_manifest_sha256"]
            or candidate_receipt["authorization_id"]
            != container_receipt["authorization_id"]
        ):
            raise IntegrityError(
                "NPPES candidate/CRC receipts were produced under different authority"
            )
        if transport_hash != candidate["archive_sha256"] or transport_hash != container["archive_sha256"]:
            raise IntegrityError("NPPES transport/container archive hashes do not form one chain")
        if candidate["candidate_manifest_sha256"] != container["candidate_manifest_sha256"]:
            raise IntegrityError("NPPES candidate/CRC receipts bind different member manifests")
    nppes_imports = (
        (
            "nppes_transport",
            "nppes_transport_import",
            "phase2c_bundle_manifest_sha256",
            "phase2c_authorization_id",
            "nppes_transport_receipt_sha256",
        ),
        (
            "nppes_zip_candidate",
            "nppes_zip_candidate_import",
            "phase2di_bundle_manifest_sha256",
            "phase2di_authorization_id",
            "nppes_zip_candidate_receipt_sha256",
        ),
        (
            "nppes_container",
            "nppes_container_import",
            "phase2di_bundle_manifest_sha256",
            "phase2di_authorization_id",
            "nppes_container_receipt_sha256",
        ),
    )
    for original_id, import_id, bundle_field, authorization_field, hash_field in nppes_imports:
        if import_id not in parsed:
            continue
        if original_id not in parsed:
            raise IntegrityError(f"NPPES verified-import pair is incomplete: {original_id}")
        original = parsed[original_id]
        imported = parsed[import_id]["evidence"]
        original_evidence = original["evidence"]
        if (
            imported[bundle_field] != original["bundle_manifest_sha256"]
            or imported[authorization_field] != original["authorization_id"]
            or imported[hash_field] != hashlib.sha256(receipts[original_id]).hexdigest()
            or any(
                imported.get(field) != value
                for field, value in original_evidence.items()
            )
        ):
            raise IntegrityError(
                f"{import_id} does not bind the exact prior-phase receipt"
            )
    if "d2ii_prepare_requests" in parsed and "census_acquisition" in parsed:
        prepared = parsed["d2ii_prepare_requests"]["evidence"]["request_manifest_sha256"]
        acquired = parsed["census_acquisition"]["evidence"]["request_manifest_sha256"]
        if prepared != acquired:
            raise IntegrityError("Census acquisition did not consume the frozen D-II request manifest")
    if "storage_preflight" in parsed and "d2ii_prepare_requests" in parsed:
        storage = parsed["storage_preflight"]
        d2ii = parsed["d2ii_prepare_requests"]["evidence"]
        if storage["bundle_manifest_sha256"] != d2ii["phase2dii_bundle_manifest_sha256"]:
            raise IntegrityError(
                "storage preflight was not issued for the producing D-II bundle"
            )
    return parsed


def load_control_receipts_manifest(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    required_ids: set[str] | frozenset[str],
    context: ReceiptContext,
    cross_phase_original_ids: set[str] | frozenset[str] = frozenset(),
) -> Mapping[str, Mapping[str, object]]:
    """Load exact pinned receipt files and validate their ID-specific schemas."""

    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise IntegrityError("control-receipts manifest must be a regular file")
    body = manifest_path.read_bytes()
    if _require_sha(expected_manifest_sha256, "approved receipt-manifest hash") != hashlib.sha256(body).hexdigest():
        raise IntegrityError("control-receipts manifest is not independently approved")
    value = require_canonical_json(body, label="control-receipts manifest")
    reject_control_sentinels(value, label="control-receipts manifest")
    if not isinstance(value, dict) or tuple(value) != (
        "schema_version",
        "source_tree_id",
        "bundle_manifest_sha256",
        "authorization_id",
        "receipts",
    ):
        raise IntegrityError("control-receipts manifest keys/order mismatch")
    if (
        value["schema_version"] != "atlas-v19-control-receipts-manifest-1"
        or value["source_tree_id"] != context.source_tree_id
        or value["bundle_manifest_sha256"] != context.bundle_manifest_sha256
        or value["authorization_id"] != context.authorization_id
        or not isinstance(value["receipts"], list)
    ):
        raise IntegrityError("control-receipts manifest semantic binding mismatch")
    root = manifest_path.parent.resolve(strict=True)
    receipt_bodies: dict[str, bytes] = {}
    seen_paths: set[str] = set()
    for row in value["receipts"]:
        if not isinstance(row, dict) or tuple(row) != (
            "receipt_id", "relative_path", "bytes", "sha256"
        ):
            raise IntegrityError("control receipt pin schema mismatch")
        receipt_id = validate_safe_id(row["receipt_id"], label="control receipt ID")
        if receipt_id not in SPECS or receipt_id in receipt_bodies:
            raise IntegrityError("control receipt ID is unregistered or duplicated")
        relative = validate_safe_relative_posix(
            row["relative_path"], label="control receipt relative path"
        )
        if relative.as_posix() in seen_paths:
            raise IntegrityError("control receipt path is duplicated")
        seen_paths.add(relative.as_posix())
        if type(row["bytes"]) is not int or row["bytes"] <= 0:
            raise IntegrityError("control receipt byte count must be positive")
        _require_sha(row["sha256"], "control receipt hash")
        receipt_path = resolve_regular_contained(root, relative, label="control receipt")
        receipt_body = receipt_path.read_bytes()
        if len(receipt_body) != row["bytes"] or hashlib.sha256(receipt_body).hexdigest() != row["sha256"]:
            raise IntegrityError(f"control receipt bytes/hash mismatch: {receipt_id}")
        receipt_bodies[receipt_id] = receipt_body
    return validate_receipt_set(
        receipt_bodies,
        required_ids=required_ids,
        context=context,
        cross_phase_original_ids=cross_phase_original_ids,
    )
