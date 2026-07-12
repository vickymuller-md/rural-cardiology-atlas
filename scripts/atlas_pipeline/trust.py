"""External integrator trust-root verification for every frozen entrypoint."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import IntegrityError
from .control_receipts import (
    PHASE2F_CROSS_PHASE_ORIGINAL_IDS,
    ReceiptContext,
    load_control_receipts_manifest,
    validate_control_receipt,
)
from .source_tree import (
    SourceTreeIdentity,
    verify_imported_module_containment,
    verify_source_tree_manifest,
)
from .strict_json import (
    reject_control_sentinels,
    require_canonical_json,
    validate_safe_id,
)

TRUST_ROOT_SCHEMA_VERSION = "atlas-v19-integrator-trust-root-1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHASES = frozenset({"2B", "2C", "2D-I", "2D-II", "2E", "2F"})


@dataclass(frozen=True)
class RuntimeControl:
    phase: str
    control_id: str
    authorization_id: str
    trust_root_sha256: str
    source_tree: SourceTreeIdentity
    approved_bundle_sha256: str
    approved_authorization_sha256: str
    approved_review_config_sha256: str | None
    approved_control_receipts_manifest_sha256: str
    approved_storage_preflight_receipt_sha256: str | None
    imported_modules: tuple[str, ...]

    @property
    def source_tree_id(self) -> str:
        return self.source_tree.source_tree_id


def _read_regular(path: Path, label: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise IntegrityError(f"{label} must be a regular non-symlink file")
    return path.read_bytes()


def verify_entrypoint_authority(
    *,
    repo_root: Path,
    phase: str,
    source_tree_manifest_path: Path,
    integrator_control_path: Path,
    approved_control_sha256: str,
    bundle_manifest_path: Path,
    authorization_receipt_path: Path,
    control_receipts_manifest_path: Path,
    review_config_path: Path | None = None,
    storage_preflight_receipt_path: Path | None = None,
) -> RuntimeControl:
    """Verify independent approval before a Phase 2B--2F entrypoint runs.

    ``approved_control_sha256`` is intentionally a separate CLI/config value.
    Neither the candidate bundle nor its authorization receipt may choose it.
    """

    if phase not in _PHASES:
        raise IntegrityError("entrypoint phase is invalid")
    if _SHA256.fullmatch(approved_control_sha256) is None:
        raise IntegrityError("independently approved control SHA-256 is required")
    control_body = _read_regular(integrator_control_path, "integrator trust root")
    control_digest = hashlib.sha256(control_body).hexdigest()
    if control_digest != approved_control_sha256:
        raise IntegrityError("integrator trust root differs from the independently approved hash")
    control = require_canonical_json(control_body, label="integrator trust root")
    if not isinstance(control, dict) or tuple(control) != (
        "schema_version",
        "control_id",
        "phase",
        "source_tree_manifest_sha256",
        "bundle_manifest_sha256",
        "authorization_receipt_sha256",
        "review_config_sha256",
        "control_receipts_manifest_sha256",
        "storage_preflight_receipt_sha256",
        "approved_by",
        "approved_at_utc",
    ):
        raise IntegrityError("integrator trust-root keys/order mismatch")
    reject_control_sentinels(control, label="integrator trust root")
    validate_safe_id(control["control_id"], label="integrator control ID")
    if control["schema_version"] != TRUST_ROOT_SCHEMA_VERSION or control["phase"] != phase:
        raise IntegrityError("integrator trust root does not authorize this phase")
    for field in (
        "source_tree_manifest_sha256",
        "bundle_manifest_sha256",
        "authorization_receipt_sha256",
        "control_receipts_manifest_sha256",
    ):
        if not isinstance(control[field], str) or _SHA256.fullmatch(control[field]) is None:
            raise IntegrityError(f"integrator trust root has an invalid {field}")
    optional_rules = {
        "review_config_sha256": phase == "2F",
        "storage_preflight_receipt_sha256": phase in {"2C", "2D-II"},
    }
    for field, required in optional_rules.items():
        value = control[field]
        if required and (not isinstance(value, str) or _SHA256.fullmatch(value) is None):
            raise IntegrityError(f"integrator trust root requires {field} for {phase}")
        if not required and value is not None:
            raise IntegrityError(f"integrator trust root must set inapplicable {field} to null")
    if not isinstance(control["approved_by"], str) or not control["approved_by"].strip():
        raise IntegrityError("integrator approver is required")
    if (
        not isinstance(control["approved_at_utc"], str)
        or not control["approved_at_utc"].endswith("Z")
    ):
        raise IntegrityError("integrator approval must use a UTC Z timestamp")
    try:
        datetime.fromisoformat(control["approved_at_utc"][:-1] + "+00:00")
    except ValueError as exc:
        raise IntegrityError("integrator approval timestamp is invalid") from exc

    source_body = _read_regular(source_tree_manifest_path, "source-tree manifest")
    bundle_body = _read_regular(bundle_manifest_path, "bundle manifest")
    authorization_body = _read_regular(authorization_receipt_path, "authorization receipt")
    control_receipts_body = _read_regular(
        control_receipts_manifest_path, "control-receipts manifest"
    )
    observed = {
        "source_tree_manifest_sha256": hashlib.sha256(source_body).hexdigest(),
        "bundle_manifest_sha256": hashlib.sha256(bundle_body).hexdigest(),
        "authorization_receipt_sha256": hashlib.sha256(authorization_body).hexdigest(),
        "control_receipts_manifest_sha256": hashlib.sha256(
            control_receipts_body
        ).hexdigest(),
    }
    for field, digest in observed.items():
        if digest != control[field]:
            raise IntegrityError(f"candidate {field} is not independently approved")
    control_receipts_value = require_canonical_json(
        control_receipts_body, label="control-receipts manifest"
    )
    if control["review_config_sha256"] is not None:
        if review_config_path is None:
            raise IntegrityError("approved Phase-2F review config path is required")
        review_body = _read_regular(review_config_path, "Phase-2F review config")
        if hashlib.sha256(review_body).hexdigest() != control["review_config_sha256"]:
            raise IntegrityError("Phase-2F review config is not independently approved")
        review_value = require_canonical_json(review_body, label="Phase-2F review config")
        reject_control_sentinels(review_value, label="Phase-2F review config")
    elif review_config_path is not None:
        raise IntegrityError("review config was supplied to a phase that does not authorize one")
    storage_body: bytes | None = None
    if control["storage_preflight_receipt_sha256"] is not None:
        if storage_preflight_receipt_path is None:
            raise IntegrityError("approved storage-preflight receipt path is required")
        storage_body = _read_regular(
            storage_preflight_receipt_path, "storage-preflight receipt"
        )
        if hashlib.sha256(storage_body).hexdigest() != control["storage_preflight_receipt_sha256"]:
            raise IntegrityError("storage-preflight receipt is not independently approved")
        require_canonical_json(storage_body, label="storage-preflight receipt")
    elif storage_preflight_receipt_path is not None:
        raise IntegrityError("storage receipt was supplied to a phase that does not authorize one")

    authorization = require_canonical_json(
        authorization_body, label="entrypoint authorization receipt"
    )
    if not isinstance(authorization, dict) or tuple(authorization) != (
        "schema_version",
        "authorization_id",
        "phase",
        "status",
        "source_tree_id",
        "bundle_manifest_sha256",
        "integrator_control_id",
        "approved_by",
        "approved_at_utc",
    ):
        raise IntegrityError("entrypoint authorization receipt keys/order mismatch")
    reject_control_sentinels(authorization, label="entrypoint authorization receipt")
    if (
        authorization["schema_version"] != "atlas-v19-entrypoint-authorization-receipt-1"
        or authorization["phase"] != phase
        or authorization["status"] != f"AUTHORIZED_PHASE_{phase}"
        or authorization["source_tree_id"] != control["source_tree_manifest_sha256"]
        or authorization["bundle_manifest_sha256"] != control["bundle_manifest_sha256"]
        or authorization["integrator_control_id"] != control["control_id"]
    ):
        raise IntegrityError("entrypoint authorization receipt semantic binding mismatch")
    validate_safe_id(authorization["authorization_id"], label="authorization receipt ID")
    if not isinstance(authorization["approved_by"], str) or not authorization["approved_by"].strip():
        raise IntegrityError("authorization receipt approver is required")
    if not isinstance(authorization["approved_at_utc"], str) or not authorization["approved_at_utc"].endswith("Z"):
        raise IntegrityError("authorization receipt timestamp must be UTC Z")
    try:
        datetime.fromisoformat(authorization["approved_at_utc"][:-1] + "+00:00")
    except ValueError as exc:
        raise IntegrityError("authorization receipt timestamp is invalid") from exc

    if (
        not isinstance(control_receipts_value, dict)
        or tuple(control_receipts_value) != (
            "schema_version", "source_tree_id", "bundle_manifest_sha256",
            "authorization_id", "receipts",
        )
        or not isinstance(control_receipts_value["receipts"], list)
        or not control_receipts_value["receipts"]
    ):
        raise IntegrityError("control-receipts manifest must contain typed receipt pins")
    receipt_ids = {
        row.get("receipt_id")
        for row in control_receipts_value["receipts"]
        if isinstance(row, dict) and isinstance(row.get("receipt_id"), str)
    }
    if len(receipt_ids) != len(control_receipts_value["receipts"]):
        raise IntegrityError("control-receipts manifest IDs are malformed or duplicated")
    load_control_receipts_manifest(
        control_receipts_manifest_path,
        expected_manifest_sha256=control["control_receipts_manifest_sha256"],
        required_ids=receipt_ids,
        context=ReceiptContext(
            control["source_tree_manifest_sha256"],
            control["bundle_manifest_sha256"],
            authorization["authorization_id"],
        ),
        cross_phase_original_ids=(
            PHASE2F_CROSS_PHASE_ORIGINAL_IDS & receipt_ids
            if phase == "2F"
            else frozenset()
        ),
    )
    if storage_body is not None:
        storage_receipt = validate_control_receipt(
            "storage_preflight",
            storage_body,
            context=ReceiptContext(
                control["source_tree_manifest_sha256"],
                control["bundle_manifest_sha256"],
                authorization["authorization_id"],
            ),
        )
        if storage_receipt["evidence"]["integrator_control_id"] != control["control_id"]:
            raise IntegrityError("storage-preflight receipt does not bind the external control ID")

    source_tree = verify_source_tree_manifest(repo_root, source_body)
    if source_tree.source_tree_id != control["source_tree_manifest_sha256"]:
        raise IntegrityError("source_tree_id is not the approved canonical manifest hash")
    imported = verify_imported_module_containment(repo_root)
    return RuntimeControl(
        phase=phase,
        control_id=control["control_id"],
        authorization_id=authorization["authorization_id"],
        trust_root_sha256=control_digest,
        source_tree=source_tree,
        approved_bundle_sha256=control["bundle_manifest_sha256"],
        approved_authorization_sha256=control["authorization_receipt_sha256"],
        approved_review_config_sha256=control["review_config_sha256"],
        approved_control_receipts_manifest_sha256=control[
            "control_receipts_manifest_sha256"
        ],
        approved_storage_preflight_receipt_sha256=control[
            "storage_preflight_receipt_sha256"
        ],
        imported_modules=imported,
    )
