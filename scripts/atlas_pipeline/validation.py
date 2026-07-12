"""Deterministic manual-validation sampling and exact gate evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Mapping, Sequence

from .errors import ContractError, IntegrityError

_SAMPLE_PREFIX = "ATLAS-V19-MANUAL-20260709"
_DOUBLE_PREFIX = "ATLAS-V19-DOUBLE-20260709"

PROVIDER_STRATA_TARGETS = {
    "P_DIRECT_RURAL_NONBORDER": 15,
    "P_DIRECT_RURAL_BORDER": 15,
    "P_DIRECT_URBAN_NONBORDER": 15,
    "P_DIRECT_URBAN_BORDER": 15,
    "P_HUD_RURAL_NONBORDER": 15,
    "P_HUD_RURAL_BORDER": 15,
    "P_HUD_URBAN_NONBORDER": 15,
    "P_HUD_URBAN_BORDER": 15,
    "P_UNMATCHED_RURAL": 20,
    "P_UNMATCHED_URBAN": 20,
    "P_UNMATCHED_COMPLEX": 20,
}

HOSPITAL_ASSIGNED_CONTEXTS = (
    "RURAL_NONBORDER",
    "RURAL_BORDER",
    "URBAN_NONBORDER",
    "URBAN_BORDER",
)


def sample_key(
    entity_kind: str,
    stratum_id: str,
    entity_id: str,
    address_id: str,
    source_snapshot_id: str,
) -> str:
    if entity_kind not in {"P", "H"}:
        raise ContractError("entity_kind must be P or H")
    if not all("\0" not in value for value in (stratum_id, entity_id, address_id, source_snapshot_id)):
        raise ContractError("manual sample-key fields may not contain NUL")
    payload = "\0".join(
        (_SAMPLE_PREFIX, entity_kind, stratum_id, entity_id, address_id, source_snapshot_id)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def double_key(primary_sample_key: str) -> str:
    return hashlib.sha256(
        (_DOUBLE_PREFIX + "\0" + primary_sample_key).encode("utf-8")
    ).hexdigest()


def assigned_context(rucc: int, state_border: bool) -> str:
    if not 1 <= rucc <= 9:
        raise ContractError("RUCC must be 1..9")
    return ("RURAL" if rucc >= 4 else "URBAN") + (
        "_BORDER" if state_border else "_NONBORDER"
    )


def provider_stratum(
    method: str,
    *,
    rucc: int | None = None,
    state_border: bool | None = None,
    positive_candidate_ruccs: Sequence[int | None] = (),
    complex_unknown: bool = False,
) -> str:
    if method in {"DIRECT", "HUD"}:
        if rucc is None or state_border is None:
            raise ContractError("assigned provider context requires RUCC and border flag")
        return f"P_{method}_{assigned_context(rucc, state_border)}"
    if method != "UNMATCHED":
        raise ContractError(f"unknown provider method: {method}")
    if complex_unknown or not positive_candidate_ruccs or any(
        value is None or not 1 <= value <= 9 for value in positive_candidate_ruccs
    ):
        return "P_UNMATCHED_COMPLEX"
    if all(value >= 4 for value in positive_candidate_ruccs):
        return "P_UNMATCHED_RURAL"
    if all(value <= 3 for value in positive_candidate_ruccs):
        return "P_UNMATCHED_URBAN"
    return "P_UNMATCHED_COMPLEX"


@dataclass(frozen=True)
class SampleCandidate:
    entity_kind: str
    stratum_id: str
    entity_id: str
    address_id: str
    source_snapshot_id: str
    method: str
    context: str | None
    is_cah: bool = False

    @property
    def sample_key(self) -> str:
        return sample_key(
            self.entity_kind,
            self.stratum_id,
            self.entity_id,
            self.address_id,
            self.source_snapshot_id,
        )

    def validate(self) -> None:
        from .nppes import valid_npi

        if not re.fullmatch(r"A-[0-9a-f]{64}", self.address_id):
            raise ContractError("validation candidate address_id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_snapshot_id):
            raise ContractError("validation source_snapshot_id is invalid")
        if self.entity_kind == "P":
            if not valid_npi(self.entity_id) or self.stratum_id not in PROVIDER_STRATA_TARGETS:
                raise ContractError("provider validation identity/stratum is invalid")
            expected_method = (
                "DIRECT" if self.stratum_id.startswith("P_DIRECT_")
                else "HUD" if self.stratum_id.startswith("P_HUD_")
                else "UNMATCHED"
            )
            if self.method != expected_method:
                raise ContractError("provider validation method/stratum mismatch")
        elif self.entity_kind == "H":
            if not re.fullmatch(r"H-(?:[0-9]{6}|[0-9]{5}F)", self.entity_id):
                raise ContractError("hospital validation entity_id is invalid")
            if self.method == "UNMATCHED":
                if self.stratum_id != "H_UNMATCHED" or self.context is not None:
                    raise ContractError("hospital unmatched stratum is invalid")
            elif self.method in {"DIRECT", "HUD"}:
                if self.context not in HOSPITAL_ASSIGNED_CONTEXTS or self.stratum_id != f"H_{self.method}_{self.context}":
                    raise ContractError("hospital assigned method/context/stratum mismatch")
            else:
                raise ContractError("hospital validation method is invalid")
            if self.is_cah and self.entity_id.endswith("F"):
                raise ContractError("federal F-suffix validation candidate cannot be a CAH")
        else:
            raise ContractError("validation candidate entity kind is invalid")

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.sample_key, self.entity_id, self.address_id)


@dataclass(frozen=True)
class ProviderSample:
    selected: tuple[SampleCandidate, ...]
    target: int
    actual: int
    shortfalls: Mapping[str, int]


def select_provider_sample(candidates: Iterable[SampleCandidate]) -> ProviderSample:
    grouped: dict[str, list[SampleCandidate]] = {
        stratum: [] for stratum in PROVIDER_STRATA_TARGETS
    }
    for candidate in candidates:
        candidate.validate()
        if candidate.entity_kind != "P" or candidate.stratum_id not in grouped:
            raise ContractError(f"invalid provider sample candidate: {candidate}")
        grouped[candidate.stratum_id].append(candidate)
    selected: list[SampleCandidate] = []
    shortfalls: dict[str, int] = {}
    for stratum, target in PROVIDER_STRATA_TARGETS.items():
        ordered = sorted(grouped[stratum], key=lambda item: item.identity)
        selected.extend(ordered[:target])
        if len(ordered) < target:
            shortfalls[stratum] = target - len(ordered)
    return ProviderSample(
        tuple(sorted(selected, key=lambda item: item.identity)),
        sum(PROVIDER_STRATA_TARGETS.values()),
        len(selected),
        shortfalls,
    )


@dataclass(frozen=True)
class HospitalQuotas:
    direct: int
    hud: int
    unmatched: int
    target: int
    structural_shortfall: int


def hospital_quotas(n_direct: int, n_hud: int, n_unmatched: int) -> HospitalQuotas:
    if min(n_direct, n_hud, n_unmatched) < 0:
        raise ContractError("hospital candidate counts cannot be negative")
    u = min(10, n_unmatched)
    r = 10 - u
    assigned_target = 60 - u
    d0 = 30 + math.ceil(3 * r / 5)
    h0 = 20 + math.floor(2 * r / 5)
    if n_direct + n_hud < assigned_target:
        direct, hud = n_direct, n_hud
        target = direct + hud + u
        return HospitalQuotas(direct, hud, u, target, 60 - target)
    if n_direct < d0:
        direct, hud = n_direct, assigned_target - n_direct
    elif n_hud < h0:
        hud, direct = n_hud, assigned_target - n_hud
    else:
        direct, hud = d0, h0
    if direct > n_direct or hud > n_hud:
        raise IntegrityError("hospital quota calculation exceeded candidates")
    return HospitalQuotas(direct, hud, u, 60, 0)


def _method_increment(method: str) -> tuple[int, int, int]:
    if method == "DIRECT":
        return (1, 0, 0)
    if method == "HUD":
        return (0, 1, 0)
    if method == "UNMATCHED":
        return (0, 0, 1)
    raise ContractError(f"invalid hospital sample method: {method}")


@dataclass(frozen=True)
class HospitalSample:
    selected: tuple[SampleCandidate, ...]
    quotas: HospitalQuotas
    cah_target: int
    maximum_feasible_cah: int
    required_contexts: tuple[str, ...]


def select_hospital_sample(candidates: Iterable[SampleCandidate]) -> HospitalSample:
    items = sorted(candidates, key=lambda item: item.identity)
    for item in items:
        item.validate()
    if any(item.entity_kind != "H" for item in items):
        raise ContractError("hospital sample includes a non-hospital candidate")
    counts = Counter(item.method for item in items)
    quotas = hospital_quotas(counts["DIRECT"], counts["HUD"], counts["UNMATCHED"])
    quota_by_method = {
        "DIRECT": quotas.direct,
        "HUD": quotas.hud,
        "UNMATCHED": quotas.unmatched,
    }
    required_contexts = tuple(
        context
        for context in HOSPITAL_ASSIGNED_CONTEXTS
        if any(
            item.method in {"DIRECT", "HUD"}
            and quota_by_method[item.method] > 0
            and item.context == context
            for item in items
        )
    )
    context_bit = {context: 1 << index for index, context in enumerate(required_contexts)}
    full_mask = (1 << len(required_contexts)) - 1

    # Feasibility depends only on method, assigned context, and CAH status.
    # Keeping suffix category counts avoids the per-row, selection-vector DP
    # that becomes intractable for the ~5,432-row production hospital frame.
    category_counts: Counter[tuple[str, str | None, bool]] = Counter(
        (item.method, item.context, item.is_cah) for item in items
    )
    contexts_by_bit = tuple(required_contexts)

    def method_maximum_cah(
        remaining: Counter[tuple[str, str | None, bool]],
        method: str,
        quota: int,
        required_mask: int,
    ) -> int | None:
        local = {
            (context, is_cah): remaining[(method, context, is_cah)]
            for context in HOSPITAL_ASSIGNED_CONTEXTS
            for is_cah in (False, True)
        }
        if sum(local.values()) < quota or required_mask.bit_count() > quota:
            return None
        selected_cah = 0
        mandatory = 0
        for bit_index, context in enumerate(contexts_by_bit):
            if not required_mask & (1 << bit_index):
                continue
            mandatory += 1
            if local[(context, True)] > 0:
                local[(context, True)] -= 1
                selected_cah += 1
            elif local[(context, False)] > 0:
                local[(context, False)] -= 1
            else:
                return None
        slots = quota - mandatory
        if sum(local.values()) < slots:
            return None
        return selected_cah + min(
            slots,
            sum(local[(context, True)] for context in HOSPITAL_ASSIGNED_CONTEXTS),
        )

    def maximum_future_cah(
        remaining: Counter[tuple[str, str | None, bool]],
        needed: Mapping[str, int],
        missing_mask: int,
    ) -> int | None:
        unmatched_total = sum(
            remaining[("UNMATCHED", None, is_cah)] for is_cah in (False, True)
        )
        if unmatched_total < needed["UNMATCHED"]:
            return None
        unmatched_cah = min(
            needed["UNMATCHED"], remaining[("UNMATCHED", None, True)]
        )
        maximum: int | None = None
        for direct_mask in range(full_mask + 1):
            for hud_mask in range(full_mask + 1):
                if (direct_mask | hud_mask) & missing_mask != missing_mask:
                    continue
                direct_cah = method_maximum_cah(
                    remaining, "DIRECT", needed["DIRECT"], direct_mask
                )
                hud_cah = method_maximum_cah(
                    remaining, "HUD", needed["HUD"], hud_mask
                )
                if direct_cah is None or hud_cah is None:
                    continue
                value = direct_cah + hud_cah + unmatched_cah
                maximum = value if maximum is None else max(maximum, value)
        return maximum

    quota_by_method = {
        "DIRECT": quotas.direct,
        "HUD": quotas.hud,
        "UNMATCHED": quotas.unmatched,
    }
    maximum = maximum_future_cah(category_counts, quota_by_method, full_mask)
    if maximum is None:
        raise ContractError("no quota/context-feasible hospital sample")
    cah_target = min(maximum, 20)

    # All feasible samples have the same length. Scanning ascending identities
    # and including the earliest row whenever a feasible suffix remains yields
    # the exact lexicographically smallest ascending identity vector.
    selected_rows: list[SampleCandidate] = []
    selected_counts: Counter[str] = Counter()
    selected_mask = 0
    selected_cah = 0
    for item in items:
        category_counts[(item.method, item.context, item.is_cah)] -= 1
        if selected_counts[item.method] >= quota_by_method[item.method]:
            continue
        proposed_counts = selected_counts.copy()
        proposed_counts[item.method] += 1
        proposed_mask = selected_mask | context_bit.get(item.context or "", 0)
        proposed_cah = selected_cah + int(item.is_cah)
        needed = {
            method: quota_by_method[method] - proposed_counts[method]
            for method in quota_by_method
        }
        future_maximum = maximum_future_cah(
            category_counts, needed, full_mask & ~proposed_mask
        )
        if future_maximum is None or proposed_cah + future_maximum < cah_target:
            continue
        selected_rows.append(item)
        selected_counts = proposed_counts
        selected_mask = proposed_mask
        selected_cah = proposed_cah

    if (
        any(selected_counts[method] != quota for method, quota in quota_by_method.items())
        or selected_mask != full_mask
        or selected_cah < cah_target
    ):
        raise ContractError("no hospital sample satisfies C_target")
    selected = tuple(selected_rows)
    return HospitalSample(selected, quotas, cah_target, maximum, required_contexts)


def select_double_review(
    candidates: Iterable[SampleCandidate], *, hospital: bool
) -> tuple[SampleCandidate, ...]:
    cells: dict[str, list[SampleCandidate]] = {}
    for item in candidates:
        item.validate()
        if hospital:
            cell = f"H_{item.method}"
            if cell not in {"H_DIRECT", "H_HUD", "H_UNMATCHED"}:
                raise ContractError(f"invalid hospital double-review cell: {cell}")
        else:
            cell = item.stratum_id
            if cell not in PROVIDER_STRATA_TARGETS:
                raise ContractError(f"invalid provider double-review cell: {cell}")
        cells.setdefault(cell, []).append(item)
    result: list[SampleCandidate] = []
    for cell in sorted(cells):
        ordered = sorted(
            cells[cell], key=lambda item: (double_key(item.sample_key), item.entity_id, item.address_id)
        )
        result.extend(ordered[: math.ceil(0.20 * len(ordered))])
    return tuple(result)


@dataclass(frozen=True)
class CriterionGate:
    criterion: str
    passes: int
    denominator: int
    ratio: Fraction | None
    status: str


GATE_THRESHOLDS: dict[str, Fraction] = {
    "provider_identity_normalization_route": Fraction(1, 1),
    "provider_direct_county_state": Fraction(59, 60),
    "provider_direct_coordinate_plausibility": Fraction(57, 60),
    "provider_hud_rule": Fraction(1, 1),
    "provider_hud_proxy_plausibility": Fraction(57, 60),
    "provider_unmatched_unassignable": Fraction(57, 60),
    "hospital_identity_type_route": Fraction(1, 1),
    "hospital_assigned_county_state": Fraction(59, 60),
    "hospital_hud_rule": Fraction(1, 1),
    "hospital_unmatched_unassignable": Fraction(57, 60),
}


def evaluate_criterion(
    criterion: str,
    codes: Iterable[str],
    *,
    source_state_mismatches: int = 0,
) -> CriterionGate:
    if criterion not in GATE_THRESHOLDS:
        raise ContractError(f"unknown validation criterion: {criterion}")
    allowed = {"CORRECT", "INCORRECT", "UNVERIFIABLE", "NA"}
    values = list(codes)
    if any(value not in allowed for value in values):
        raise ContractError("invalid manual review code")
    applicable = [value for value in values if value != "NA"]
    if not applicable:
        return CriterionGate(criterion, 0, 0, None, "NA")
    passes = applicable.count("CORRECT")
    ratio = Fraction(passes, len(applicable))
    status = "PASS" if ratio >= GATE_THRESHOLDS[criterion] else "FAIL"
    if criterion in {"provider_direct_county_state", "hospital_assigned_county_state"} and source_state_mismatches:
        status = "FAIL"
    return CriterionGate(criterion, passes, len(applicable), ratio, status)


@dataclass(frozen=True)
class AgreementGate:
    raw_agreement: Fraction
    kappa: Fraction | None
    status: str


def agreement_gate(first: Sequence[str], second: Sequence[str]) -> AgreementGate:
    if len(first) != len(second) or not first:
        raise ContractError("paired nonempty double-review codes are required")
    controlled = {"CORRECT", "INCORRECT", "UNVERIFIABLE", "NA"}
    if any(value not in controlled for value in (*first, *second)):
        raise ContractError("double-review values must use controlled review codes")
    categories = sorted(set(first) | set(second))
    agreed = sum(a == b for a, b in zip(first, second))
    raw = Fraction(agreed, len(first))
    count_a = Counter(first)
    count_b = Counter(second)
    p_expected = sum(
        (
            Fraction(count_a[category], len(first))
            * Fraction(count_b[category], len(first))
            for category in categories
        ),
        Fraction(0, 1),
    )
    if p_expected == 1:
        return AgreementGate(raw, None, "PASS" if raw == 1 else "FAIL")
    kappa = (raw - p_expected) / (1 - p_expected)
    return AgreementGate(raw, kappa, "PASS" if kappa >= Fraction(4, 5) else "FAIL")


SAMPLING_FRAME_FIELDS = (
    "entity_kind",
    "stratum_id",
    "entity_id",
    "address_id",
    "source_snapshot_id",
    "sample_key",
    "method",
    "context",
    "is_cah",
)

WORKSHEET_FIELDS = (
    *SAMPLING_FRAME_FIELDS,
    "raw_address_fields",
    "normalized_address_fields",
    "route_reason",
    "source_state",
    "assigned_county_fips",
    "longitude",
    "latitude",
    "matched_address",
    "geocoder_outcome",
    "hud_candidates",
    "hospital_type",
    "source_is_cah",
    "reviewer_id",
    "identity_normalization_route_code",
    "county_state_code",
    "coordinate_or_hud_plausibility_code",
    "unmatched_rule_code",
    "review_notes",
)

DOUBLE_REVIEW_FIELDS = (
    *SAMPLING_FRAME_FIELDS,
    "double_review_cell",
    "double_key",
)


@dataclass(frozen=True)
class FrozenValidationArtifacts:
    sampling_frame_bytes: bytes
    sampling_frame_sha256: str
    blank_worksheet_bytes: bytes
    blank_worksheet_sha256: str


@dataclass(frozen=True)
class ReviewEvidence:
    raw_address_fields: tuple[str, str, str, str, str, str]
    normalized_address_fields: tuple[str, str, str, str, str, str]
    route_reason: str
    source_state: str
    assigned_county_fips: str | None
    longitude: str | None
    latitude: str | None
    matched_address: str | None
    geocoder_outcome: str | None
    hud_candidates: tuple[dict[str, object], ...]
    hospital_type: str | None
    source_is_cah: bool | None

    def as_row_fields(self) -> dict[str, object]:
        required_hud_keys = (
            "county_fips",
            "bus_ratio",
            "res_ratio",
            "tot_ratio",
            "oth_ratio",
            "same_state",
        )
        for candidate in self.hud_candidates:
            if tuple(candidate) != required_hud_keys:
                raise IntegrityError("manual-review HUD evidence schema/order mismatch")
        return {
            "raw_address_fields": list(self.raw_address_fields),
            "normalized_address_fields": list(self.normalized_address_fields),
            "route_reason": self.route_reason,
            "source_state": self.source_state,
            "assigned_county_fips": self.assigned_county_fips,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "matched_address": self.matched_address,
            "geocoder_outcome": self.geocoder_outcome,
            "hud_candidates": list(self.hud_candidates),
            "hospital_type": self.hospital_type,
            "source_is_cah": self.source_is_cah,
        }


def _canonical_rows(rows: list[dict[str, object]], fields: tuple[str, ...]) -> bytes:
    lines = []
    for row in rows:
        if tuple(row) != fields:
            raise IntegrityError("validation artifact schema/order mismatch")
        lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def _sample_base_row(item: SampleCandidate) -> dict[str, object]:
    item.validate()
    return {
        "entity_kind": item.entity_kind,
        "stratum_id": item.stratum_id,
        "entity_id": item.entity_id,
        "address_id": item.address_id,
        "source_snapshot_id": item.source_snapshot_id,
        "sample_key": item.sample_key,
        "method": item.method,
        "context": item.context,
        "is_cah": item.is_cah,
    }


def canonical_sample_bytes(candidates: Iterable[SampleCandidate]) -> bytes:
    """Serialize one frozen sample without review fields or aggregate results."""

    items = list(candidates)
    if len(set(items)) != len(items):
        raise IntegrityError("frozen sample contains duplicate rows")
    return _canonical_rows(
        [_sample_base_row(item) for item in sorted(items, key=lambda item: item.identity)],
        SAMPLING_FRAME_FIELDS,
    )


def canonical_double_review_bytes(candidates: Iterable[SampleCandidate]) -> bytes:
    """Bind the deterministic 20-percent double-review subset and cell keys."""

    items = list(candidates)
    if len(set(items)) != len(items):
        raise IntegrityError("double-review sample contains duplicate rows")
    rows = []
    for item in sorted(items, key=lambda candidate: candidate.identity):
        row = _sample_base_row(item)
        row.update(
            {
                "double_review_cell": (
                    item.stratum_id if item.entity_kind == "P" else f"H_{item.method}"
                ),
                "double_key": double_key(item.sample_key),
            }
        )
        rows.append(row)
    return _canonical_rows(rows, DOUBLE_REVIEW_FIELDS)


def freeze_validation_artifacts(
    complete_frame: Iterable[SampleCandidate],
    selected_sample: Iterable[SampleCandidate],
    evidence_by_sample_key: Mapping[str, ReviewEvidence],
) -> FrozenValidationArtifacts:
    frame_candidates = list(complete_frame)
    selected = list(selected_sample)
    for candidate in (*frame_candidates, *selected):
        candidate.validate()
    frame_keys = {(item.entity_kind, item.entity_id, item.address_id) for item in frame_candidates}
    if len(frame_keys) != len(frame_candidates):
        raise IntegrityError("complete validation frame contains duplicate entities/addresses")
    frame_sample_keys = {item.sample_key for item in frame_candidates}
    if len(frame_sample_keys) != len(frame_candidates):
        raise IntegrityError("complete validation frame contains duplicate sample keys")
    if len(set(selected)) != len(selected):
        raise IntegrityError("selected validation sample contains duplicate rows")
    frame_set = set(frame_candidates)
    if any(item not in frame_set for item in selected):
        raise IntegrityError("selected validation row is absent from complete frame")
    expected_evidence = {item.sample_key for item in selected}
    if set(evidence_by_sample_key) != expected_evidence:
        raise IntegrityError("manual-review evidence keys do not equal selected sample keys")

    frame_rows = [
        _sample_base_row(item)
        for item in sorted(frame_candidates, key=lambda item: item.identity)
    ]
    worksheet_rows = []
    for item in sorted(selected, key=lambda item: item.identity):
        row = _sample_base_row(item)
        row.update(evidence_by_sample_key[item.sample_key].as_row_fields())
        row.update(
            {
                "reviewer_id": "",
                "identity_normalization_route_code": "",
                "county_state_code": "",
                "coordinate_or_hud_plausibility_code": "",
                "unmatched_rule_code": "",
                "review_notes": "",
            }
        )
        worksheet_rows.append(row)
    frame_bytes = _canonical_rows(frame_rows, SAMPLING_FRAME_FIELDS)
    worksheet_bytes = _canonical_rows(worksheet_rows, WORKSHEET_FIELDS)
    return FrozenValidationArtifacts(
        frame_bytes,
        hashlib.sha256(frame_bytes).hexdigest(),
        worksheet_bytes,
        hashlib.sha256(worksheet_bytes).hexdigest(),
    )
