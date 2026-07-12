"""Exact R4B-to-R4C predecessor bridge for frozen provenance artifacts.

The bridge is deliberately narrow: it recognizes only the twelve Amendment-04
role/hash pairs and only while validating the exact 54-input R4 manifest.  It
does not rewrite, recontextualize, or create an analytical input.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Protocol

from .acquisition import canonical_manifest_bytes
from .errors import IntegrityError

R4B_SOURCE_TREE_ID = (
    "b912ccaba6f7d629edb64dbdbe9dd57e10ccc79ac7fbec87c13ec984a2503d29"
)
R3_SOURCE_TREE_ID = (
    "d1cd35bb5001c6c5d400e848651fe67506b35c38f60fd88b30839e4ed2c7fa97"
)
R4_METHOD_LINEAGE_ID = "ATLAS-V19-AMENDMENT03-R4"
R4_INPUT_MANIFEST_SHA256 = (
    "40c1e605f3c334495e12bbe30ecdde837c37fa8853b7e053474c7fa7698758f3"
)
R4_INPUT_COUNT = 54
AMENDMENT04_SHA256 = (
    "72a063d41449757332f0df5c612293b7558bfdfa1bc8a0cac932bfa6b619a8bc"
)
AMENDMENT04_ADDENDUM01_SHA256 = (
    "1a4cfaf1744f8975de999e44e77f8dde4c037539989806aeb08f5bc035eb51b8"
)
PREDECESSOR_NEGATIVE_MATRIX_SHA256 = (
    "76b5f9cbbe8541bb69764f6e368f05a468d7df528fd8c60b6f1d6bcf060b6cd2"
)

PREDECESSOR_ARTIFACT_SHA256: Mapping[str, str] = MappingProxyType({
    "input_materialization": "61b4c14b1a2c047f7fdfbe56432bbede2c6bac1bf83eb99be13752b3fbd23699",
    "nppes_transport": "9645e6a8f3c6a045f0b6dd1b4ed901f7fe502bba030c41641a172d9351239c50",
    "nppes_zip_candidate": "35ee563e5bb0969b62c999e711ac55af68e27effa4735e81bc5ba67db48260ef",
    "nppes_container": "edf01a84e56f17de19801dad0a78e69044d220f4941323f4add37fe555db8009",
    "storage_preflight": "3ec6a8be7315f415fa3de8dcd146fa1291c34512e0ec9785743fe4759be66a1f",
    "R4B-CENSUS-HANDOFF-RECEIPT": "15fb2110eb64f906a853b56b4cd01500ea3507089b3614f49b79ef664888df63",
    "R4B-D2II-HANDOFF-RECEIPT": "e54b514ecde3c28748c8de38f6435af6e0de211a908bcc1022242250fec9d8ed",
    "R4B-GAZETTEER-EXTRACTION-RECEIPT": "c1959f621731420a792d5aa7170758a8436ff465bde481495cc0ca734bb28452",
    "R4B-HUD-API-DATA-HANDOFF": "6246a7b859aa431cdb547837ca2b4cbff35e2f815c4c97f6bc66a5543dd49336",
    "R4B-HUD-API-HANDOFF-RECEIPT": "5e225315f6955aa4e36c13bbaf750ce64df54182e8b0d23b97895689c02cf9c9",
    "R4B-PLACES-DERIVATION-HANDOFF": "aef45c6afde5c8876ba905829b5c047cbc979155c23c91bfcde1f673961d0a6b",
    "R4B-PLACES-HANDOFF-RECEIPT": "5db2d7b5e232046f1a76db0eb7bd259d69c4d057b45717b006b7447851283dff",
})

PREDECESSOR_VERIFICATION_PATHS: Mapping[str, str] = MappingProxyType({
    "R4B-CENSUS-HANDOFF-RECEIPT": "geocoder_provenance.verify_census_provenance:census_handoff",
    "R4B-D2II-HANDOFF-RECEIPT": "phase2f_chain._original_receipt:d2ii_handoff",
    "R4B-GAZETTEER-EXTRACTION-RECEIPT": "gazetteer_provenance.verify_gazetteer_provenance:gazetteer_extraction_receipt",
    "R4B-HUD-API-DATA-HANDOFF": "phase2f_chain._verify_hud_api:hud_api_data_handoff",
    "R4B-HUD-API-HANDOFF-RECEIPT": "phase2f_chain._original_receipt:hud_api_handoff",
    "R4B-PLACES-DERIVATION-HANDOFF": "phase2f_chain._verify_places:places_derivation_handoff",
    "R4B-PLACES-HANDOFF-RECEIPT": "phase2f_chain._original_receipt:places_handoff",
    "input_materialization": "control_receipts.validate_receipt_set:input_materialization|phase2f_chain._verify_prior_phase_controls:input_materialization",
    "nppes_container": "control_receipts.validate_receipt_set:nppes_container|phase2f_chain._verify_prior_phase_controls:nppes_container",
    "nppes_transport": "control_receipts.validate_receipt_set:nppes_transport|phase2f_chain._verify_prior_phase_controls:nppes_transport",
    "nppes_zip_candidate": "control_receipts.validate_receipt_set:nppes_zip_candidate|phase2f_chain._verify_prior_phase_controls:nppes_zip_candidate",
    "storage_preflight": "control_receipts.validate_receipt_set:storage_preflight|phase2f_chain._verify_prior_phase_controls:storage_preflight",
})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _BridgeBundle(Protocol):
    source_tree_id: str
    expectations: Mapping[str, object]

    def frozen_inputs(self): ...


@dataclass(frozen=True)
class PredecessorBridgeContext:
    active_source_tree_id: str
    method_lineage_id: str
    input_manifest_sha256: str
    input_count: int

    def validate(self) -> None:
        if (
            _SHA256.fullmatch(self.active_source_tree_id) is None
            or self.active_source_tree_id in {R4B_SOURCE_TREE_ID, R3_SOURCE_TREE_ID}
        ):
            raise IntegrityError("predecessor bridge requires a distinct R4 successor tree")
        if self.method_lineage_id != R4_METHOD_LINEAGE_ID:
            raise IntegrityError("predecessor bridge rejects non-R4 method lineage")
        if (
            self.input_manifest_sha256 != R4_INPUT_MANIFEST_SHA256
            or self.input_count != R4_INPUT_COUNT
        ):
            raise IntegrityError("predecessor bridge requires the exact 54-input R4 manifest")


def bridge_context_for_bundle(bundle: _BridgeBundle) -> PredecessorBridgeContext:
    frozen_inputs = list(bundle.frozen_inputs())
    context = PredecessorBridgeContext(
        active_source_tree_id=bundle.source_tree_id,
        method_lineage_id=str(bundle.expectations.get("method_lineage_id", "")),
        input_manifest_sha256=hashlib.sha256(
            canonical_manifest_bytes(frozen_inputs)
        ).hexdigest(),
        input_count=len(frozen_inputs),
    )
    context.validate()
    return context


def require_predecessor_hash(
    context: PredecessorBridgeContext,
    *,
    observed_source_tree_id: object,
    artifact_id: object,
    artifact_sha256: object,
) -> None:
    """Accept exactly one allowlisted historical R4B role/hash pair."""

    context.validate()
    if observed_source_tree_id != R4B_SOURCE_TREE_ID:
        raise IntegrityError("predecessor artifact is not from the exact R4B tree")
    if not isinstance(artifact_id, str) or artifact_id not in PREDECESSOR_ARTIFACT_SHA256:
        raise IntegrityError("predecessor artifact ID is not allowlisted")
    if artifact_sha256 != PREDECESSOR_ARTIFACT_SHA256[artifact_id]:
        raise IntegrityError("predecessor artifact hash differs from Amendment 04")


def require_predecessor_body(
    context: PredecessorBridgeContext,
    *,
    observed_source_tree_id: object,
    artifact_id: object,
    body: bytes,
) -> None:
    require_predecessor_hash(
        context,
        observed_source_tree_id=observed_source_tree_id,
        artifact_id=artifact_id,
        artifact_sha256=hashlib.sha256(body).hexdigest(),
    )


def require_bundle_predecessor_body(
    bundle: _BridgeBundle,
    *,
    observed_source_tree_id: object,
    artifact_id: object,
    body: bytes,
) -> None:
    require_predecessor_body(
        bridge_context_for_bundle(bundle),
        observed_source_tree_id=observed_source_tree_id,
        artifact_id=artifact_id,
        body=body,
    )


def require_bundle_predecessor_hash(
    bundle: _BridgeBundle,
    *,
    observed_source_tree_id: object,
    artifact_id: object,
    artifact_sha256: object,
) -> None:
    require_predecessor_hash(
        bridge_context_for_bundle(bundle),
        observed_source_tree_id=observed_source_tree_id,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
    )


def require_same_tree_or_predecessor_body(
    bundle: _BridgeBundle,
    *,
    observed_source_tree_id: object,
    artifact_id: object,
    body: bytes,
) -> bool:
    if observed_source_tree_id == bundle.source_tree_id:
        return False
    require_bundle_predecessor_body(
        bundle,
        observed_source_tree_id=observed_source_tree_id,
        artifact_id=artifact_id,
        body=body,
    )
    return True


def require_provisional_control_predecessor(
    *,
    active_source_tree_id: str,
    observed_source_tree_id: object,
    artifact_id: str,
    body: bytes,
) -> None:
    """Receipt-layer recognition; exact bundle validation completes later."""

    if (
        _SHA256.fullmatch(active_source_tree_id) is None
        or active_source_tree_id in {R4B_SOURCE_TREE_ID, R3_SOURCE_TREE_ID}
        or observed_source_tree_id != R4B_SOURCE_TREE_ID
        or artifact_id not in {
            "input_materialization",
            "nppes_transport",
            "nppes_zip_candidate",
            "nppes_container",
            "storage_preflight",
        }
        or hashlib.sha256(body).hexdigest()
        != PREDECESSOR_ARTIFACT_SHA256.get(artifact_id)
    ):
        raise IntegrityError("cross-phase control receipt is not an exact R4B predecessor")
