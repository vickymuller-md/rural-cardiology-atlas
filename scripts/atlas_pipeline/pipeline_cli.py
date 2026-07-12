"""Externally authorized frozen Phase-2B through Phase-2F command routing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .bundle import (
    FrozenBundle,
    atomic_write,
    verify_output_receipt,
    write_output_receipt,
)
from .acquisition import authorize_zip_approval, canonical_manifest_bytes
from .clean_room import CleanRoomInputs, orchestrate_clean_rooms
from .control_receipts import ReceiptContext, validate_control_receipt
from .contracts import METHOD_LINEAGE_ID
from .errors import AcquisitionError, IntegrityError
from .hud_surface import build_hud_api_handoff, build_hud_handoff
from .live_geocoder import LiveApproval, RequestsCensusTransport, acquire_census
from .materialization import (
    MaterializationManifest,
    NppesTransportPlan,
    RequestsStreamingTransport,
    _load_zip_candidate,
    acquire_known_hash,
    acquire_nppes_candidate,
    approve_nppes_container,
    finalize_known_hash_materialization,
    import_known_hash,
    inspect_nppes_container,
)
from .phase2f_runner import ReviewConfig, run_phase2f
from .places_derivation import derive_places_county
from .request_prep import prepare_requests
from .source_registry import ReleaseValidationContext
from .trust import RuntimeControl, verify_entrypoint_authority
from .strict_json import (
    canonical_json_bytes,
    require_canonical_json,
    resolve_regular_contained,
    validate_safe_relative_posix,
)
from .validation_completion import complete_validation


def _add_authority_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_storage: bool = False,
) -> None:
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--source-tree-manifest", type=Path, required=True)
    parser.add_argument("--integrator-control", type=Path, required=True)
    parser.add_argument("--approved-control-sha256", required=True)
    parser.add_argument("--authorization-receipt", type=Path, required=True)
    parser.add_argument("--control-receipts-manifest", type=Path, required=True)
    if include_storage:
        parser.add_argument("--storage-preflight-receipt", type=Path, required=True)


def _verify_authority(
    args: argparse.Namespace,
    *,
    phase: str,
    bundle_manifest_path: Path,
    review_config_path: Path | None = None,
) -> RuntimeControl:
    runtime = verify_entrypoint_authority(
        repo_root=args.repo_root,
        phase=phase,
        source_tree_manifest_path=args.source_tree_manifest,
        integrator_control_path=args.integrator_control,
        approved_control_sha256=args.approved_control_sha256,
        bundle_manifest_path=bundle_manifest_path,
        authorization_receipt_path=args.authorization_receipt,
        control_receipts_manifest_path=args.control_receipts_manifest,
        review_config_path=review_config_path,
        storage_preflight_receipt_path=getattr(
            args, "storage_preflight_receipt", None
        ),
    )
    if hashlib.sha256(bundle_manifest_path.read_bytes()).hexdigest() != runtime.approved_bundle_sha256:
        raise IntegrityError("runtime authority/bundle hash mismatch")
    return runtime


def _require_runtime_binding(
    runtime: RuntimeControl,
    *,
    source_tree_id: str,
    manifest_sha256: str,
) -> None:
    if (
        runtime.source_tree_id != source_tree_id
        or runtime.approved_bundle_sha256 != manifest_sha256
    ):
        raise IntegrityError("entrypoint inputs are not bound to the approved runtime control")


def _validate_storage_preflight(
    path: Path,
    *,
    source_tree_id: str,
    bundle_manifest_sha256: str,
    authorization_id: str,
    runtime: RuntimeControl,
    output_dir: Path,
    disk_usage=shutil.disk_usage,
    stat=os.stat,
) -> None:
    body = path.read_bytes()
    value = validate_control_receipt(
        "storage_preflight",
        body,
        context=ReceiptContext(
            source_tree_id, bundle_manifest_sha256, authorization_id
        ),
    )
    if runtime.authorization_id != authorization_id:
        raise IntegrityError("storage preflight/bundle authorization IDs differ")
    evidence = value["evidence"]
    output_parent = output_dir.absolute().parent.resolve(strict=True)
    approved_parent = Path(evidence["output_parent"]).resolve(strict=True)
    work_root = Path(evidence["work_root"]).resolve(strict=True)
    try:
        output_parent.relative_to(work_root)
    except ValueError as exc:
        raise IntegrityError("current output parent escapes approved storage work root") from exc
    observed_stat = stat(output_parent)
    observed_free = disk_usage(output_parent).free
    filesystem_identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "work_root": str(work_root),
                "output_parent": str(output_parent),
                "device": evidence["device"],
                "st_dev": observed_stat.st_dev,
            }
        )
    ).hexdigest()
    if (
        output_parent != approved_parent
        or observed_stat.st_dev != evidence["st_dev"]
        or observed_free < evidence["worst_case_bytes"]
        or filesystem_identity != evidence["filesystem_identity_sha256"]
        or evidence["integrator_control_id"] != runtime.control_id
        or hashlib.sha256(path.read_bytes()).hexdigest()
        != runtime.approved_storage_preflight_receipt_sha256
    ):
        raise IntegrityError("current storage device/path/free-space differs from approval")


def _load_bundle_after_authority(
    args: argparse.Namespace,
    *,
    phase: str,
    review_config_path: Path | None = None,
) -> tuple[FrozenBundle, RuntimeControl]:
    manifest_path = args.bundle / "bundle.json"
    runtime = _verify_authority(
        args,
        phase=phase,
        bundle_manifest_path=manifest_path,
        review_config_path=review_config_path,
    )
    bundle = FrozenBundle.load(args.bundle, required_phase=phase)
    _require_runtime_binding(
        runtime,
        source_tree_id=bundle.source_tree_id,
        manifest_sha256=bundle.manifest_sha256,
    )
    if runtime.authorization_id != bundle.authorization_id:
        raise IntegrityError("bundle authorization ID is not externally approved")
    return bundle, runtime


def materialize_main(
    argv: Sequence[str] | None = None,
    *,
    default_source_ids: tuple[str, ...] | None = None,
) -> None:
    parser = argparse.ArgumentParser(prog="materialize-known-hash")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument(
        "--import-source",
        action="append",
        default=[],
        metavar="SOURCE_ID=PATH",
    )
    parser.add_argument("--execute-live", action="store_true")
    _add_authority_arguments(parser)
    args = parser.parse_args(argv)
    runtime = _verify_authority(
        args, phase="2B", bundle_manifest_path=args.manifest
    )
    manifest = MaterializationManifest.load(args.manifest, required_phase="2B")
    _require_runtime_binding(
        runtime,
        source_tree_id=manifest.source_tree_id,
        manifest_sha256=manifest.sha256,
    )
    selected = tuple(args.source_id) or default_source_ids or tuple(sorted(manifest.sources))
    if len(selected) != len(set(selected)) or not set(selected) <= set(manifest.sources):
        raise IntegrityError("materialization source selection is invalid/duplicated")
    imports: dict[str, Path] = {}
    for value in args.import_source:
        if "=" not in value:
            raise IntegrityError("--import-source must be SOURCE_ID=PATH")
        source_id, raw_path = value.split("=", 1)
        if source_id in imports or source_id not in selected:
            raise IntegrityError("import source ID is duplicated or unselected")
        imports[source_id] = Path(raw_path)
    if args.execute_live == bool(imports):
        raise IntegrityError("choose exactly one of --execute-live or local --import-source")
    if imports and set(imports) != set(selected):
        raise IntegrityError("every selected local source requires one --import-source")
    transport = RequestsStreamingTransport() if args.execute_live else None
    for source_id in sorted(selected):
        if transport is None:
            import_known_hash(
                manifest, source_id, imports[source_id], args.output
            )
        else:
            acquire_known_hash(
                manifest, source_id, args.output, transport=transport
            )
    if set(selected) == set(manifest.sources):
        finalize_known_hash_materialization(
            manifest,
            args.output,
            authorization_id=runtime.authorization_id,
        )


def acquire_nppes_main(
    argv: Sequence[str] | None = None,
    *,
    transport_factory: Callable[[], RequestsStreamingTransport] = RequestsStreamingTransport,
    disk_usage=shutil.disk_usage,
    stat=os.stat,
) -> None:
    parser = argparse.ArgumentParser(prog="acquire-nppes-transport")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute-live", action="store_true", required=True)
    _add_authority_arguments(parser, include_storage=True)
    args = parser.parse_args(argv)
    if not args.execute_live:
        raise AcquisitionError("Phase-2C NPPES transport requires --execute-live")
    runtime = _verify_authority(
        args, phase="2C", bundle_manifest_path=args.plan
    )
    plan = NppesTransportPlan.load(args.plan)
    _require_runtime_binding(
        runtime,
        source_tree_id=plan.source_tree_id,
        manifest_sha256=plan.sha256,
    )
    _validate_storage_preflight(
        args.storage_preflight_receipt,
        source_tree_id=plan.source_tree_id,
        bundle_manifest_sha256=plan.sha256,
        authorization_id=runtime.authorization_id,
        runtime=runtime,
        output_dir=args.output,
        disk_usage=disk_usage,
        stat=stat,
    )
    storage_preflight_sha256 = hashlib.sha256(
        args.storage_preflight_receipt.read_bytes()
    ).hexdigest()
    acquire_nppes_candidate(
        plan.spec,
        args.output,
        transport=transport_factory(),
        source_tree_id=runtime.source_tree_id,
        plan_sha256=plan.sha256,
        authorization_id=runtime.authorization_id,
        storage_preflight_receipt_sha256=storage_preflight_sha256,
    )


def inspect_container_main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="inspect-nppes-container")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    _add_authority_arguments(parser)
    args = parser.parse_args(argv)
    runtime = _verify_authority(
        args, phase="2D-I", bundle_manifest_path=args.manifest
    )
    manifest = MaterializationManifest.load(args.manifest, required_phase="2D-I")
    _require_runtime_binding(
        runtime,
        source_tree_id=manifest.source_tree_id,
        manifest_sha256=manifest.sha256,
    )
    inspect_nppes_container(
        manifest,
        args.archive,
        args.output,
        authorization_id=runtime.authorization_id,
    )


def approve_container_main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="verify-nppes-container")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--member-approval", type=Path, required=True)
    parser.add_argument("--approval-trust-root", type=Path, required=True)
    parser.add_argument(
        "--approved-approval-trust-root-sha256", required=True
    )
    parser.add_argument("--selected-member", required=True)
    parser.add_argument("--output", type=Path, required=True)
    _add_authority_arguments(parser)
    args = parser.parse_args(argv)
    runtime = _verify_authority(
        args, phase="2D-I", bundle_manifest_path=args.manifest
    )
    manifest = MaterializationManifest.load(args.manifest, required_phase="2D-I")
    _require_runtime_binding(
        runtime,
        source_tree_id=manifest.source_tree_id,
        manifest_sha256=manifest.sha256,
    )
    approve_nppes_container(
        manifest,
        args.archive,
        args.candidate_manifest,
        args.member_approval,
        args.approval_trust_root,
        args.output,
        independently_approved_trust_root_sha256=(
            args.approved_approval_trust_root_sha256
        ),
        authorization_id=runtime.authorization_id,
        selected_member_filename=args.selected_member,
    )


def prepare_main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="prepare-requests")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-rows", type=int, default=10_000)
    _add_authority_arguments(parser, include_storage=True)
    args = parser.parse_args(argv)
    bundle, runtime = _load_bundle_after_authority(args, phase="2D-II")
    _validate_storage_preflight(
        args.storage_preflight_receipt,
        source_tree_id=bundle.source_tree_id,
        bundle_manifest_sha256=bundle.manifest_sha256,
        authorization_id=bundle.authorization_id,
        runtime=runtime,
        output_dir=args.output,
    )
    prepare_requests(
        bundle,
        args.output,
        chunk_rows=args.chunk_rows,
        storage_preflight_receipt_path=args.storage_preflight_receipt,
    )


def derive_places_main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="derive-places-county")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    _add_authority_arguments(parser, include_storage=True)
    args = parser.parse_args(argv)
    bundle, _runtime = _load_bundle_after_authority(args, phase="2D-II")
    derive_places_county(bundle, args.output)


def acquire_main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="acquire-census")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--approval-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute-live", action="store_true", required=True)
    _add_authority_arguments(parser)
    args = parser.parse_args(argv)
    if not args.execute_live:
        raise AcquisitionError("live Census acquisition requires --execute-live")
    bundle, _runtime = _load_bundle_after_authority(args, phase="2E")
    approval = LiveApproval.load(args.approval_receipt, bundle)
    acquire_census(
        bundle,
        approval,
        args.output,
        transport=RequestsCensusTransport(),
    )


def hud_main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="acquire-hud")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--member-approval", type=Path)
    parser.add_argument("--approval-trust-root", type=Path)
    parser.add_argument(
        "--approved-approval-trust-root-sha256"
    )
    parser.add_argument("--acquisition-receipt", type=Path)
    parser.add_argument("--county-universe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--import-file", type=Path)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--credential-env", default="HUD_API_TOKEN")
    _add_authority_arguments(parser)
    args = parser.parse_args(argv)
    runtime = _verify_authority(
        args, phase="2E", bundle_manifest_path=args.manifest
    )
    manifest = MaterializationManifest.load(args.manifest, required_phase="2E")
    _require_runtime_binding(
        runtime,
        source_tree_id=manifest.source_tree_id,
        manifest_sha256=manifest.sha256,
    )
    if "HUD-ZIP-COUNTY-API-2025Q4" in manifest.sources:
        if (
            args.execute_live
            or args.import_file is None
            or args.acquisition_receipt is None
            or any(
                value is not None
                for value in (
                    args.candidate_manifest, args.member_approval,
                    args.approval_trust_root,
                    args.approved_approval_trust_root_sha256,
                )
            )
        ):
            raise IntegrityError(
                "R4 HUD API requires frozen --import-file and --acquisition-receipt only"
            )
        materialization_root = args.output / "materialized"
        result = import_known_hash(
            manifest,
            "HUD-ZIP-COUNTY-API-2025Q4",
            args.import_file,
            materialization_root,
        )
        build_hud_api_handoff(
            manifest,
            result.final_path,
            args.county_universe,
            args.acquisition_receipt,
            result.receipt_path,
            args.output / "handoff",
            authorization_id=runtime.authorization_id,
        )
        return
    if any(
        value is None
        for value in (
            args.candidate_manifest, args.member_approval,
            args.approval_trust_root,
            args.approved_approval_trust_root_sha256,
        )
    ) or args.acquisition_receipt is not None:
        raise IntegrityError("R3 HUD XLSX requires the approved OOXML trust inputs")
    candidate = _load_zip_candidate(args.candidate_manifest)
    hud_spec = manifest.sources["HUD-ZIP-COUNTY-2025Q4"]
    if (
        candidate.source_id != hud_spec.source_id
        or candidate.archive_bytes != hud_spec.bytes
        or candidate.archive_sha256 != hud_spec.sha256
    ):
        raise IntegrityError("HUD candidate manifest differs from the frozen source")
    for path, label in (
        (args.member_approval, "HUD member approval"),
        (args.approval_trust_root, "HUD approval trust root"),
    ):
        if not path.is_file() or path.is_symlink():
            raise IntegrityError(f"{label} must be a regular non-symlink file")
    approval = authorize_zip_approval(
        candidate,
        args.member_approval.read_bytes(),
        args.approval_trust_root.read_bytes(),
        independently_approved_trust_root_sha256=(
            args.approved_approval_trust_root_sha256
        ),
        source_tree_id=manifest.source_tree_id,
        bundle_manifest_sha256=manifest.sha256,
        authorization_id=runtime.authorization_id,
    )
    context = ReleaseValidationContext(
        candidate,
        approval,
        (manifest.expectations["required_sheet_name"],),
    )
    if args.execute_live == (args.import_file is not None):
        raise IntegrityError("choose exactly one authenticated HUD live/import mode")
    materialization_root = args.output / "materialized"
    if args.import_file is not None:
        result = import_known_hash(
            manifest,
            "HUD-ZIP-COUNTY-2025Q4",
            args.import_file,
            materialization_root,
            context=context,
        )
    else:
        credential = os.environ.get(args.credential_env)
        if not credential:
            raise AcquisitionError("HUD authenticated acquisition credential is absent")
        result = acquire_known_hash(
            manifest,
            "HUD-ZIP-COUNTY-2025Q4",
            materialization_root,
            transport=RequestsStreamingTransport(
                authorization_header=f"Bearer {credential}"
            ),
            context=context,
        )
    build_hud_handoff(
        manifest,
        result.final_path,
        args.county_universe,
        context,
        result.receipt_path,
        args.output / "handoff",
        authorization_id=runtime.authorization_id,
    )


def build_main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="build")
    parser.add_argument("--offline", action="store_true", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--review-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    _add_authority_arguments(parser)
    args = parser.parse_args(argv)
    if not args.offline:
        raise IntegrityError("Phase-2F build requires --offline")
    bundle, runtime = _load_bundle_after_authority(
        args, phase="2F", review_config_path=args.review_config
    )
    review = ReviewConfig.load(
        args.review_config,
        bundle,
        runtime,
        args.control_receipts_manifest,
    )
    run_phase2f(
        bundle,
        review,
        args.output,
        completed_at_utc=os.environ.get("ATLAS_PHASE2F_COMPLETED_AT_UTC"),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def validation_complete_main(argv: Sequence[str] | None = None) -> None:
    """Complete the frozen later-state manual review without rerunning Phase 2F."""

    parser = argparse.ArgumentParser(prog="validation-complete")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--review-config", type=Path, required=True)
    parser.add_argument("--phase2f-output-root", type=Path, required=True)
    parser.add_argument("--phase2f-output-receipt", type=Path, required=True)
    parser.add_argument("--completed-primary", type=Path, required=True)
    parser.add_argument("--completed-secondary", type=Path, required=True)
    parser.add_argument("--adjudication", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    _add_authority_arguments(parser)
    args = parser.parse_args(argv)

    bundle, runtime = _load_bundle_after_authority(
        args, phase="2F", review_config_path=args.review_config
    )
    review = ReviewConfig.load(
        args.review_config,
        bundle,
        runtime,
        args.control_receipts_manifest,
    )
    if (
        bundle.expectations.get("method_lineage_id") != METHOD_LINEAGE_ID
        or review.method_lineage_id != METHOD_LINEAGE_ID
    ):
        raise IntegrityError("validation completion rejects a non-R4 method lineage")

    unverified_receipt = require_canonical_json(
        args.phase2f_output_receipt.read_bytes(), label="Phase-2F output receipt"
    )
    if not isinstance(unverified_receipt, dict) or not isinstance(
        unverified_receipt.get("artifacts"), list
    ):
        raise IntegrityError("Phase-2F output receipt artifact list is absent")
    required_artifact_ids = tuple(
        row.get("artifact_id") if isinstance(row, dict) else None
        for row in unverified_receipt["artifacts"]
    )
    if any(not isinstance(item, str) for item in required_artifact_ids):
        raise IntegrityError("Phase-2F output receipt artifact ID is invalid")
    receipt = verify_output_receipt(
        args.phase2f_output_receipt,
        bundle=bundle,
        artifact_root=args.phase2f_output_root,
        required_artifacts=required_artifact_ids,
        expected_command="build-offline",
    )
    artifact_rows = {row["artifact_id"]: row for row in receipt["artifacts"]}
    needed = {
        "build_audit",
        "validation_selected_sample",
        "validation_blank_worksheet",
        "validation_double_review_sample",
    }
    if not needed <= set(artifact_rows):
        raise IntegrityError("Phase-2F output receipt lacks validation artifacts")

    def phase2f_artifact(artifact_id: str) -> Path:
        return resolve_regular_contained(
            args.phase2f_output_root.resolve(strict=True),
            validate_safe_relative_posix(
                artifact_rows[artifact_id]["relative_path"],
                label=f"Phase-2F {artifact_id} path",
            ),
            label=f"Phase-2F {artifact_id}",
        )

    build_audit_path = phase2f_artifact("build_audit")
    build_audit = require_canonical_json(
        build_audit_path.read_bytes(), label="Phase-2F build audit"
    )
    if not isinstance(build_audit, dict):
        raise IntegrityError("Phase-2F build audit must be an object")
    pending = build_audit.get("validation_completion")
    if (
        build_audit.get("method_lineage_id") != METHOD_LINEAGE_ID
        or build_audit.get("manual_review_status") != "REQUIRED_BEFORE_GATE_B"
        or not isinstance(pending, dict)
        or pending.get("state") != "PENDING_LATER_STATE"
        or pending.get("schema_version")
        != review.validation_completion_schema_version
        or pending.get("method_lineage_id") != METHOD_LINEAGE_ID
        or pending.get("global_assigned_state_mismatches") != 0
    ):
        raise IntegrityError("Phase-2F build audit lacks the frozen R4 validation state")

    selected_path = phase2f_artifact("validation_selected_sample")
    blank_path = phase2f_artifact("validation_blank_worksheet")
    double_path = phase2f_artifact("validation_double_review_sample")
    selected_bytes = selected_path.read_bytes()
    blank_bytes = blank_path.read_bytes()
    double_bytes = double_path.read_bytes()
    expected_hashes = {
        "selected_sample_sha256": hashlib.sha256(selected_bytes).hexdigest(),
        "blank_worksheet_sha256": hashlib.sha256(blank_bytes).hexdigest(),
        "double_review_sample_sha256": hashlib.sha256(double_bytes).hexdigest(),
    }
    if any(pending.get(key) != value for key, value in expected_hashes.items()):
        raise IntegrityError("validation inputs differ from the Phase-2F build audit")

    external_paths = (
        (args.completed_primary, "completed primary worksheet"),
        (args.completed_secondary, "completed secondary worksheet"),
        (args.adjudication, "validation adjudication"),
    )
    for path, label in external_paths:
        if not path.is_file() or path.is_symlink():
            raise IntegrityError(f"{label} must be a regular non-symlink file")
    primary_bytes = args.completed_primary.read_bytes()
    secondary_bytes = args.completed_secondary.read_bytes()
    adjudication_bytes = args.adjudication.read_bytes()

    completion = complete_validation(
        selected_sample_bytes=selected_bytes,
        blank_worksheet_bytes=blank_bytes,
        completed_primary_worksheet_bytes=primary_bytes,
        double_review_sample_bytes=double_bytes,
        completed_secondary_worksheet_bytes=secondary_bytes,
        adjudication_bytes=adjudication_bytes,
        expected_selected_sample_sha256=expected_hashes["selected_sample_sha256"],
        expected_blank_worksheet_sha256=expected_hashes["blank_worksheet_sha256"],
        expected_double_review_sample_sha256=expected_hashes[
            "double_review_sample_sha256"
        ],
        global_assigned_state_mismatches=0,
    )

    args.output.mkdir(parents=True, exist_ok=False)
    completion_path = args.output / "validation_completion.json"
    atomic_write(completion_path, completion.receipt_bytes)
    completed_at = os.environ.get("ATLAS_VALIDATION_COMPLETED_AT_UTC") or _utc_now()
    evidence = {
        "method_lineage_id": METHOD_LINEAGE_ID,
        "phase2f_output_receipt_sha256": hashlib.sha256(
            args.phase2f_output_receipt.read_bytes()
        ).hexdigest(),
        "review_config_sha256": hashlib.sha256(
            args.review_config.read_bytes()
        ).hexdigest(),
        "selected_sample_sha256": expected_hashes["selected_sample_sha256"],
        "blank_worksheet_sha256": expected_hashes["blank_worksheet_sha256"],
        "completed_primary_sha256": hashlib.sha256(primary_bytes).hexdigest(),
        "double_review_sample_sha256": expected_hashes[
            "double_review_sample_sha256"
        ],
        "completed_secondary_sha256": hashlib.sha256(secondary_bytes).hexdigest(),
        "adjudication_sha256": hashlib.sha256(adjudication_bytes).hexdigest(),
        "global_assigned_state_mismatches": 0,
        "validation_status": completion.status,
        "validation_completion_receipt_sha256": completion.sha256,
    }
    control = {
        "schema_version": "atlas-v19-validation-completion-receipt-1",
        "receipt_id": "validation_completion",
        "status": "complete",
        "source_tree_id": bundle.source_tree_id,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "authorization_id": bundle.authorization_id,
        "command": "validation-complete",
        "completed_at_utc": completed_at,
        "evidence": evidence,
    }
    control_body = canonical_json_bytes(control)
    validate_control_receipt(
        "validation_completion",
        control_body,
        context=ReceiptContext(
            bundle.source_tree_id, bundle.manifest_sha256, bundle.authorization_id
        ),
    )
    control_path = args.output / "validation_completion_control_receipt.json"
    atomic_write(control_path, control_body)
    write_output_receipt(
        args.output,
        command="validation-complete",
        bundle=bundle,
        artifacts={
            "validation_completion": completion_path,
            "validation_completion_control_receipt": control_path,
        },
        completed_at_utc=completed_at,
    )
    if completion.status != "PASS":
        raise IntegrityError("manual validation failed; completion receipts preserved")


def clean_room_main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="clean-room")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--review-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python-executable", type=Path, required=True)
    parser.add_argument("--uv-executable", type=Path, required=True)
    parser.add_argument("--node-executable", type=Path, required=True)
    parser.add_argument("--npm-executable", type=Path, required=True)
    parser.add_argument("--environment-receipt", type=Path, required=True)
    parser.add_argument("--network-denied-receipt", type=Path, required=True)
    _add_authority_arguments(parser)
    args = parser.parse_args(argv)
    bundle, runtime = _load_bundle_after_authority(
        args, phase="2F", review_config_path=args.review_config
    )
    review = ReviewConfig.load(
        args.review_config,
        bundle,
        runtime,
        args.control_receipts_manifest,
    )
    for path, label in (
        (args.environment_receipt, "dependency-materialization receipt"),
        (args.network_denied_receipt, "network-denial receipt"),
    ):
        if not path.is_file() or path.is_symlink():
            raise IntegrityError(f"clean-room {label} must be a regular file")
    environment_body = args.environment_receipt.read_bytes()
    network_body = args.network_denied_receipt.read_bytes()
    if (
        hashlib.sha256(environment_body).hexdigest()
        != review.receipts["topology_authorization"]["evidence"][
            "dependency_receipt_sha256"
        ]
        or hashlib.sha256(network_body).hexdigest()
        != review.receipts["dependency_materialization"]["evidence"][
            "network_denied_receipt_sha256"
        ]
    ):
        raise IntegrityError("clean-room receipt files differ from approved receipt chain")
    input_manifest_sha256 = hashlib.sha256(
        canonical_manifest_bytes(list(bundle.frozen_inputs()))
    ).hexdigest()
    orchestrate_clean_rooms(
        CleanRoomInputs(
            repo_root=args.repo_root,
            python_executable=args.python_executable,
            uv_executable=args.uv_executable,
            node_executable=args.node_executable,
            npm_executable=args.npm_executable,
            bundle_root=args.bundle,
            review_config=args.review_config,
            source_tree_manifest=args.source_tree_manifest,
            integrator_control=args.integrator_control,
            approved_control_sha256=args.approved_control_sha256,
            authorization_receipt=args.authorization_receipt,
            control_receipts_manifest=args.control_receipts_manifest,
            runtime_control=runtime,
            context=ReceiptContext(
                bundle.source_tree_id, bundle.manifest_sha256, bundle.authorization_id
            ),
            input_manifest_sha256=input_manifest_sha256,
            environment_receipt_bytes=environment_body,
            network_denied_receipt_bytes=network_body,
        ),
        args.output,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="atlas-v19")
    commands = {
        "materialize-known-hash": materialize_main,
        "acquire-nppes-transport": acquire_nppes_main,
        "inspect-nppes-container": inspect_container_main,
        "verify-nppes-container": approve_container_main,
        "prepare-requests": prepare_main,
        "derive-places-county": derive_places_main,
        "acquire-census": acquire_main,
        "acquire-hud": hud_main,
        "build": build_main,
        "validation-complete": validation_complete_main,
        "clean-room": clean_room_main,
    }
    parser.add_argument("command", choices=tuple(commands))
    args, remainder = parser.parse_known_args(argv)
    commands[args.command](remainder)
