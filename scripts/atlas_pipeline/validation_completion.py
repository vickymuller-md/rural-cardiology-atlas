"""Pure, fail-closed completion of the frozen Amendment-03 manual review."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Mapping

from .errors import ContractError, IntegrityError
from .validation import (
    DOUBLE_REVIEW_FIELDS,
    SAMPLING_FRAME_FIELDS,
    WORKSHEET_FIELDS,
    agreement_gate,
    evaluate_criterion,
)

REVIEW_CODE_FIELDS = (
    "identity_normalization_route_code",
    "county_state_code",
    "coordinate_or_hud_plausibility_code",
    "unmatched_rule_code",
)
REVIEW_MUTABLE_FIELDS = (
    "reviewer_id", *REVIEW_CODE_FIELDS, "review_notes",
)
ADJUDICATION_FIELDS = (
    "sample_key", "criterion_field", "adjudicated_code", "adjudicator_id", "notes",
)
ALLOWED_CODES = frozenset({"CORRECT", "INCORRECT", "UNVERIFIABLE", "NA"})


@dataclass(frozen=True)
class ValidationCompletion:
    status: str
    receipt_bytes: bytes
    sha256: str


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError("validation JSON contains duplicate object keys")
        result[key] = value
    return result


def _jsonl(body: bytes, fields: tuple[str, ...], label: str) -> list[dict[str, object]]:
    if body and not body.endswith(b"\n"):
        raise IntegrityError(f"{label} is not LF-terminated")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityError(f"{label} is not UTF-8") from exc
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line, object_pairs_hook=_pairs)
        except (json.JSONDecodeError, ValueError) as exc:
            raise IntegrityError(f"{label} contains invalid JSON") from exc
        if not isinstance(row, dict) or tuple(row) != fields:
            raise IntegrityError(f"{label} schema/order mismatch")
        if json.dumps(row, ensure_ascii=False, separators=(",", ":")) != line:
            raise IntegrityError(f"{label} is not canonical JSONL")
        rows.append(row)
    return rows


def _require_hash(body: bytes, expected: str, label: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ContractError(f"{label} expected hash is invalid")
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected:
        raise IntegrityError(f"{label} hash mismatch")
    return actual


def _by_sample(rows: list[dict[str, object]], label: str) -> dict[str, dict[str, object]]:
    result = {}
    for row in rows:
        key = row.get("sample_key")
        if not isinstance(key, str) or key in result:
            raise IntegrityError(f"{label} sample identity is invalid or duplicated")
        result[key] = row
    return result


def _applicable(row: Mapping[str, object], field: str) -> bool:
    method = row["method"]
    if field == "identity_normalization_route_code":
        return True
    if field == "county_state_code":
        return method in {"DIRECT", "HUD"}
    if field == "coordinate_or_hud_plausibility_code":
        return method in {"DIRECT", "HUD"}
    if field == "unmatched_rule_code":
        return method == "UNMATCHED"
    raise IntegrityError("unknown review field")


def _validate_completed_row(row: Mapping[str, object]) -> None:
    reviewer = row["reviewer_id"]
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ContractError("completed worksheet reviewer_id is required")
    if not isinstance(row["review_notes"], str):
        raise ContractError("completed worksheet review_notes must be text")
    for field in REVIEW_CODE_FIELDS:
        code = row[field]
        if code not in ALLOWED_CODES:
            raise ContractError("completed worksheet has an invalid review code")
        if _applicable(row, field) == (code == "NA"):
            raise ContractError("worksheet applicable/NA code contract failed")


def _criterion_codes(rows: list[dict[str, object]]) -> dict[str, list[str]]:
    result = {
        "provider_identity_normalization_route": [],
        "provider_direct_county_state": [],
        "provider_direct_coordinate_plausibility": [],
        "provider_hud_rule": [],
        "provider_hud_proxy_plausibility": [],
        "provider_unmatched_unassignable": [],
        "hospital_identity_type_route": [],
        "hospital_assigned_county_state": [],
        "hospital_hud_rule": [],
        "hospital_unmatched_unassignable": [],
    }
    for row in rows:
        kind, method = row["entity_kind"], row["method"]
        prefix = "provider" if kind == "P" else "hospital"
        result[f"{prefix}_identity_normalization_route" if kind == "P" else "hospital_identity_type_route"].append(
            row["identity_normalization_route_code"]
        )
        if kind == "P" and method == "DIRECT":
            result["provider_direct_county_state"].append(row["county_state_code"])
            result["provider_direct_coordinate_plausibility"].append(row["coordinate_or_hud_plausibility_code"])
        elif kind == "P" and method == "HUD":
            result["provider_hud_rule"].append(row["identity_normalization_route_code"])
            result["provider_hud_proxy_plausibility"].append(row["coordinate_or_hud_plausibility_code"])
        elif kind == "P" and method == "UNMATCHED":
            result["provider_unmatched_unassignable"].append(row["unmatched_rule_code"])
        elif kind == "H" and method in {"DIRECT", "HUD"}:
            result["hospital_assigned_county_state"].append(row["county_state_code"])
            if method == "HUD":
                result["hospital_hud_rule"].append(row["identity_normalization_route_code"])
        elif kind == "H" and method == "UNMATCHED":
            result["hospital_unmatched_unassignable"].append(row["unmatched_rule_code"])
        else:
            raise IntegrityError("worksheet entity kind/method is invalid")
    return result


def _fraction(value: Fraction | None) -> dict[str, int] | None:
    return None if value is None else {
        "numerator": value.numerator, "denominator": value.denominator,
    }


def complete_validation(
    *,
    selected_sample_bytes: bytes,
    blank_worksheet_bytes: bytes,
    completed_primary_worksheet_bytes: bytes,
    double_review_sample_bytes: bytes,
    completed_secondary_worksheet_bytes: bytes,
    adjudication_bytes: bytes,
    expected_selected_sample_sha256: str,
    expected_blank_worksheet_sha256: str,
    expected_double_review_sample_sha256: str,
    global_assigned_state_mismatches: int,
) -> ValidationCompletion:
    """Validate both reviews and return a deterministic PASS/FAIL receipt."""

    _require_hash(selected_sample_bytes, expected_selected_sample_sha256, "selected sample")
    _require_hash(blank_worksheet_bytes, expected_blank_worksheet_sha256, "blank worksheet")
    _require_hash(double_review_sample_bytes, expected_double_review_sample_sha256, "double sample")
    if type(global_assigned_state_mismatches) is not int or global_assigned_state_mismatches < 0:
        raise ContractError("global state-mismatch count must be a nonnegative integer")

    selected = _by_sample(_jsonl(selected_sample_bytes, SAMPLING_FRAME_FIELDS, "selected sample"), "selected sample")
    blank = _by_sample(_jsonl(blank_worksheet_bytes, WORKSHEET_FIELDS, "blank worksheet"), "blank worksheet")
    primary = _by_sample(_jsonl(completed_primary_worksheet_bytes, WORKSHEET_FIELDS, "primary worksheet"), "primary worksheet")
    double_rows = _by_sample(_jsonl(double_review_sample_bytes, DOUBLE_REVIEW_FIELDS, "double sample"), "double sample")
    secondary = _by_sample(_jsonl(completed_secondary_worksheet_bytes, WORKSHEET_FIELDS, "secondary worksheet"), "secondary worksheet")
    if set(selected) != set(blank) or set(primary) != set(blank) or not set(double_rows).issubset(selected) or set(secondary) != set(double_rows):
        raise IntegrityError("validation sample/worksheet coverage mismatch")
    immutable = tuple(field for field in WORKSHEET_FIELDS if field not in REVIEW_MUTABLE_FIELDS)
    for key in blank:
        if any(blank[key][field] != selected[key][field] for field in SAMPLING_FRAME_FIELDS):
            raise IntegrityError("blank worksheet identity differs from selected sample")
        if any(primary[key][field] != blank[key][field] for field in immutable):
            raise IntegrityError("primary worksheet mutated frozen evidence")
        _validate_completed_row(primary[key])
    for key in secondary:
        if any(secondary[key][field] != blank[key][field] for field in immutable):
            raise IntegrityError("secondary worksheet mutated frozen evidence")
        _validate_completed_row(secondary[key])
    primary_reviewers = {row["reviewer_id"] for row in primary.values()}
    secondary_reviewers = {row["reviewer_id"] for row in secondary.values()}
    if len(primary_reviewers) != 1 or len(secondary_reviewers) != 1 or primary_reviewers == secondary_reviewers:
        raise ContractError("primary/secondary reviewers must be distinct single identities")

    disagreement_keys = {
        (key, field)
        for key in secondary
        for field in REVIEW_CODE_FIELDS
        if primary[key][field] != secondary[key][field]
    }
    adjudications = _jsonl(adjudication_bytes, ADJUDICATION_FIELDS, "adjudication")
    adjudication_map = {}
    adjudicator_ids = set()
    for row in adjudications:
        key = (row["sample_key"], row["criterion_field"])
        if key in adjudication_map or key not in disagreement_keys:
            raise IntegrityError("adjudication does not exactly address a disagreement")
        if row["adjudicated_code"] not in ALLOWED_CODES or not isinstance(row["notes"], str):
            raise ContractError("adjudication code/notes are invalid")
        if _applicable(primary[row["sample_key"]], row["criterion_field"]) == (
            row["adjudicated_code"] == "NA"
        ):
            raise ContractError("adjudication applicable/NA code contract failed")
        adjudicator = row["adjudicator_id"]
        if not isinstance(adjudicator, str) or not adjudicator.strip():
            raise ContractError("adjudicator identity is required")
        adjudicator_ids.add(adjudicator)
        adjudication_map[key] = row
    if set(adjudication_map) != disagreement_keys:
        raise IntegrityError("every double-review disagreement requires adjudication")
    if adjudicator_ids & (primary_reviewers | secondary_reviewers) or len(adjudicator_ids) > 1:
        raise ContractError("adjudicator must be one independent identity")

    final_rows = [dict(primary[key]) for key in sorted(primary)]
    final_by_key = {row["sample_key"]: row for row in final_rows}
    for (key, field), row in adjudication_map.items():
        final_by_key[key][field] = row["adjudicated_code"]
    for row in final_rows:
        _validate_completed_row(row)
    criterion_gates = [
        evaluate_criterion(name, codes, source_state_mismatches=global_assigned_state_mismatches)
        for name, codes in sorted(_criterion_codes(final_rows).items())
    ]
    ordered_double = sorted(secondary)
    agreement = agreement_gate(
        [primary[key][field] for key in ordered_double for field in REVIEW_CODE_FIELDS],
        [secondary[key][field] for key in ordered_double for field in REVIEW_CODE_FIELDS],
    )
    status = "PASS" if (
        global_assigned_state_mismatches == 0
        and agreement.status == "PASS"
        and all(gate.status in {"PASS", "NA"} for gate in criterion_gates)
    ) else "FAIL"
    receipt = {
        "schema_version": "atlas-v19-r4-validation-completion-1",
        "status": status,
        "input_sha256": {
            "selected_sample": hashlib.sha256(selected_sample_bytes).hexdigest(),
            "blank_worksheet": hashlib.sha256(blank_worksheet_bytes).hexdigest(),
            "completed_primary": hashlib.sha256(completed_primary_worksheet_bytes).hexdigest(),
            "double_review_sample": hashlib.sha256(double_review_sample_bytes).hexdigest(),
            "completed_secondary": hashlib.sha256(completed_secondary_worksheet_bytes).hexdigest(),
            "adjudication": hashlib.sha256(adjudication_bytes).hexdigest(),
        },
        "reviewers": {
            "primary": next(iter(primary_reviewers)),
            "secondary": next(iter(secondary_reviewers)),
            "adjudicator": None if not adjudicator_ids else next(iter(adjudicator_ids)),
        },
        "selected_rows": len(primary),
        "double_review_rows": len(secondary),
        "disagreements": len(disagreement_keys),
        "global_assigned_state_mismatches": global_assigned_state_mismatches,
        "agreement": {
            "raw": _fraction(agreement.raw_agreement),
            "kappa": _fraction(agreement.kappa),
            "status": agreement.status,
        },
        "criteria": [
            {
                **asdict(gate),
                "ratio": _fraction(gate.ratio),
            }
            for gate in criterion_gates
        ],
    }
    body = (json.dumps(receipt, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    return ValidationCompletion(status, body, hashlib.sha256(body).hexdigest())
