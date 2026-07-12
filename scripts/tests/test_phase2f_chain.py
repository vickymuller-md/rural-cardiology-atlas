from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from atlas_pipeline.addresses import EntityAddressMapping
from atlas_pipeline.acquisition import FrozenInput
from atlas_pipeline.build import OfflineBuildInputs
from atlas_pipeline.bundle import FrozenBundle, canonical_json_bytes
from atlas_pipeline.errors import IntegrityError
from atlas_pipeline.geocoder import chunk_requests, request_from_address, request_manifest_bytes
from atlas_pipeline.hospitals import deduplicate_hospitals, in_scope_hospitals
from atlas_pipeline.nppes import classify_disposition
from atlas_pipeline.offline_bundle import (
    FAILED_R3_SOURCE_TREE_ID,
    METHOD_LINEAGE_ID,
    PHASE2F_ANALYTICAL_ROLES,
    PHASE2F_CHAIN_LIST_ROLES,
    PHASE2F_CHAIN_SCALAR_ROLES,
    PHASE2F_CHAIN_SCALAR_ROLES_R3,
    PHASE2F_ROLES,
    _roles,
)
from atlas_pipeline.phase2f_chain import _validate_source_receipt, _verify_d2ii
from atlas_pipeline.predecessor_bridge import (
    PREDECESSOR_ARTIFACT_SHA256,
    R3_SOURCE_TREE_ID,
    R4B_SOURCE_TREE_ID,
    R4_INPUT_COUNT,
    R4_INPUT_MANIFEST_SHA256,
    R4_METHOD_LINEAGE_ID,
    PredecessorBridgeContext,
    require_predecessor_hash,
)
from tests.bundle_helpers import write_bundle
from tests.synthetic import hospital_row, make_npi, nppes_row


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class Phase2FChainTests(unittest.TestCase):
    @staticmethod
    def lineage_bundle(source_tree_id: str, expectations: dict[str, object]) -> FrozenBundle:
        return FrozenBundle(
            root=Path("."), phase="2F", authorization_id="SYNTHETIC-AUTH",
            approved_at_utc="2026-07-11T00:00:00Z", source_tree_id=source_tree_id,
            expectations=expectations, inputs={}, manifest_bytes=b"{}\n",
            manifest_sha256=sha(b"{}\n"),
        )

    def test_amendment04_predecessor_bridge_exact_allowlist_and_negative_matrix(self):
        context = PredecessorBridgeContext(
            "a" * 64,
            R4_METHOD_LINEAGE_ID,
            R4_INPUT_MANIFEST_SHA256,
            R4_INPUT_COUNT,
        )
        self.assertEqual(len(PREDECESSOR_ARTIFACT_SHA256), 12)
        self.assertEqual(
            len(set(PREDECESSOR_ARTIFACT_SHA256.values())),
            len(PREDECESSOR_ARTIFACT_SHA256),
        )
        for artifact_id, artifact_sha256 in PREDECESSOR_ARTIFACT_SHA256.items():
            with self.subTest(artifact_id=artifact_id, case="positive"):
                require_predecessor_hash(
                    context,
                    observed_source_tree_id=R4B_SOURCE_TREE_ID,
                    artifact_id=artifact_id,
                    artifact_sha256=artifact_sha256,
                )
            changed = artifact_sha256[:-1] + format(int(artifact_sha256[-1], 16) ^ 1, "x")
            with self.subTest(artifact_id=artifact_id, case="one-bit-hash"), self.assertRaises(IntegrityError):
                require_predecessor_hash(
                    context,
                    observed_source_tree_id=R4B_SOURCE_TREE_ID,
                    artifact_id=artifact_id,
                    artifact_sha256=changed,
                )

        first_id, first_hash = next(iter(PREDECESSOR_ARTIFACT_SHA256.items()))
        other_id = next(
            artifact_id
            for artifact_id in PREDECESSOR_ARTIFACT_SHA256
            if artifact_id != first_id
        )
        failures = (
            (context, "b" * 64, first_id, first_hash),
            (context, R3_SOURCE_TREE_ID, first_id, first_hash),
            (context, R4B_SOURCE_TREE_ID, other_id, first_hash),
            (context, R4B_SOURCE_TREE_ID, "R4B-GENERIC-ARTIFACT", first_hash),
            (
                PredecessorBridgeContext(
                    R4B_SOURCE_TREE_ID,
                    R4_METHOD_LINEAGE_ID,
                    R4_INPUT_MANIFEST_SHA256,
                    R4_INPUT_COUNT,
                ),
                R4B_SOURCE_TREE_ID,
                first_id,
                first_hash,
            ),
            (
                PredecessorBridgeContext(
                    R3_SOURCE_TREE_ID,
                    R4_METHOD_LINEAGE_ID,
                    R4_INPUT_MANIFEST_SHA256,
                    R4_INPUT_COUNT,
                ),
                R4B_SOURCE_TREE_ID,
                first_id,
                first_hash,
            ),
            (
                PredecessorBridgeContext(
                    "a" * 64,
                    "ATLAS-V19-R3",
                    R4_INPUT_MANIFEST_SHA256,
                    R4_INPUT_COUNT,
                ),
                R4B_SOURCE_TREE_ID,
                first_id,
                first_hash,
            ),
            (
                PredecessorBridgeContext(
                    "a" * 64,
                    R4_METHOD_LINEAGE_ID,
                    "c" * 64,
                    R4_INPUT_COUNT,
                ),
                R4B_SOURCE_TREE_ID,
                first_id,
                first_hash,
            ),
            (
                PredecessorBridgeContext(
                    "a" * 64,
                    R4_METHOD_LINEAGE_ID,
                    R4_INPUT_MANIFEST_SHA256,
                    R4_INPUT_COUNT + 1,
                ),
                R4B_SOURCE_TREE_ID,
                first_id,
                first_hash,
            ),
        )
        for index, (bad_context, observed_tree, artifact_id, artifact_hash) in enumerate(failures):
            with self.subTest(case=f"negative-{index}"), self.assertRaises(IntegrityError):
                require_predecessor_hash(
                    bad_context,
                    observed_source_tree_id=observed_tree,
                    artifact_id=artifact_id,
                    artifact_sha256=artifact_hash,
                )

    def test_r4_entrypoint_rejects_r3_missing_and_mixed_lineage_before_build(self):
        r3_roles = (
            PHASE2F_ANALYTICAL_ROLES
            | PHASE2F_CHAIN_SCALAR_ROLES_R3
            | PHASE2F_CHAIN_LIST_ROLES
        )
        cases = (
            (
                "r3-roles",
                "a" * 64,
                {
                    "method_lineage_id": METHOD_LINEAGE_ID,
                    "source_roles": {role: f"SRC-{index}" for index, role in enumerate(sorted(r3_roles))},
                },
                "exact HUD API role set",
            ),
            (
                "missing-lineage", "a" * 64,
                {"source_roles": {role: f"SRC-{index}" for index, role in enumerate(sorted(PHASE2F_ROLES))}},
                "missing approved expectation",
            ),
            (
                "r3-tree", FAILED_R3_SOURCE_TREE_ID,
                {
                    "method_lineage_id": METHOD_LINEAGE_ID,
                    "source_roles": {role: f"SRC-{index}" for index, role in enumerate(sorted(PHASE2F_ROLES))},
                },
                "failed R3 source tree",
            ),
            (
                "mixed-lineage", "a" * 64,
                {
                    "method_lineage_id": "ATLAS-V19-R3",
                    "source_roles": {role: f"SRC-{index}" for index, role in enumerate(sorted(PHASE2F_ROLES))},
                },
                "not Amendment-03 R4",
            ),
        )
        for label, tree, expectations, message in cases:
            with self.subTest(label=label), self.assertRaisesRegex(IntegrityError, message):
                _roles(self.lineage_bundle(tree, expectations), require_chain=True)

    def test_phase2f_chain_role_set_names_both_raw_artifacts(self):
        self.assertTrue(
            {"places_raw", "hud_api_raw_json", "hud_api_acquisition_receipt"}
            <= PHASE2F_CHAIN_SCALAR_ROLES
        )

    def test_phase2f_rehashes_206_segment_from_the_frozen_assembled_artifact(self):
        body = b"synthetic frozen source bytes"
        split = 9
        spec = FrozenInput(
            "SYNTHETIC-PHASE2F-SOURCE", "synthetic-fixture",
            "https://example.test/source.csv", "source.csv",
            len(body), sha(body), ("example.test",), "CSV", ("fixture",),
            ("text/csv",),
        )
        segment = body[split:]
        receipt = {
            "schema_version": "atlas-v19-stream-acquisition-receipt-2",
            "receipt_kind": "attempt",
            "source_id": spec.source_id,
            "release": spec.release,
            "request_method": "GET",
            "requested_url": spec.url,
            "request_headers": [
                ["Accept-Encoding", "identity"], ["Range", f"bytes={split}-"]
            ],
            "range_request": f"bytes={split}-",
            "started_at_utc": "2026-07-10T00:00:00Z",
            "ended_at_utc": "2026-07-10T00:00:01Z",
            "redirect_chain": [],
            "final_url": spec.url,
            "http_status": 206,
            "content_type": "text/csv",
            "content_encoding": "identity",
            "content_range": f"bytes {split}-{len(body) - 1}/{len(body)}",
            "segment_start": split,
            "segment_end": len(body) - 1,
            "representation_total_bytes": len(body),
            "segment_bytes": len(segment),
            "segment_sha256": sha(segment),
            "response_bytes": len(segment),
            "response_sha256": sha(segment),
            "result_state": "complete",
            "rejection_reason": None,
            "final_bytes": len(body),
            "final_sha256": sha(body),
        }
        receipt_body = canonical_json_bytes(receipt)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            write_bundle(
                root,
                phase="2F",
                expectations={},
                files=[
                    (
                        spec.source_id, "places_raw_csv", spec.filename,
                        body, "text/csv",
                    ),
                    (
                        "RECEIPT", "source_acquisition_receipt_json",
                        "receipt.json", receipt_body, "application/json",
                    ),
                ],
            )
            bundle = FrozenBundle.load(root)
            _validate_source_receipt(
                bundle,
                "RECEIPT",
                raw_source_id=spec.source_id,
                raw_schema_id="places_raw_csv",
                expected_hash=sha(receipt_body),
                spec=spec,
            )
            drifted = dict(receipt)
            drifted_hash = sha(b"forged segment")
            drifted["segment_sha256"] = drifted_hash
            drifted["response_sha256"] = drifted_hash
            drifted_body = canonical_json_bytes(drifted)
            bad_root = Path(temporary) / "bad-bundle"
            write_bundle(
                bad_root,
                phase="2F",
                expectations={},
                files=[
                    (
                        spec.source_id, "places_raw_csv", spec.filename,
                        body, "text/csv",
                    ),
                    (
                        "RECEIPT", "source_acquisition_receipt_json",
                        "receipt.json", drifted_body, "application/json",
                    ),
                ],
            )
            with self.assertRaisesRegex(IntegrityError, "assembled artifact range"):
                _validate_source_receipt(
                    FrozenBundle.load(bad_root),
                    "RECEIPT",
                    raw_source_id=spec.source_id,
                    raw_schema_id="places_raw_csv",
                    expected_hash=sha(drifted_body),
                    spec=spec,
                )

    def test_phase2f_source_receipt_uses_named_raw_role_not_hash_search(self):
        body = b"synthetic frozen source bytes"
        wrong = b"different but explicitly selected bytes"
        spec = FrozenInput(
            "SYNTHETIC-PHASE2F-SOURCE", "synthetic-fixture",
            "https://example.test/source.csv", "source.csv",
            len(body), sha(body), ("example.test",), "CSV", ("fixture",),
            ("text/csv",),
        )
        receipt = {
            "schema_version": "atlas-v19-stream-acquisition-receipt-2",
            "receipt_kind": "cache_validation",
            "source_id": spec.source_id,
            "release": spec.release,
            "request_method": "NONE",
            "requested_url": spec.url,
            "request_headers": [],
            "range_request": None,
            "started_at_utc": "2026-07-10T00:00:00Z",
            "ended_at_utc": "2026-07-10T00:00:01Z",
            "redirect_chain": [],
            "final_url": spec.url,
            "http_status": 0,
            "content_type": "text/csv",
            "content_encoding": "identity",
            "content_range": None,
            "segment_start": None,
            "segment_end": None,
            "representation_total_bytes": None,
            "segment_bytes": 0,
            "segment_sha256": sha(b""),
            "response_bytes": 0,
            "response_sha256": sha(b""),
            "result_state": "already_valid",
            "rejection_reason": None,
            "final_bytes": len(body),
            "final_sha256": sha(body),
        }
        receipt_body = canonical_json_bytes(receipt)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            write_bundle(
                root,
                phase="2F",
                expectations={},
                files=[
                    (
                        spec.source_id, "places_raw_csv", spec.filename,
                        body, "text/csv",
                    ),
                    (
                        "WRONG-RAW-ROLE", "places_raw_csv", "wrong.csv",
                        wrong, "text/csv",
                    ),
                    (
                        "RECEIPT", "source_acquisition_receipt_json",
                        "receipt.json", receipt_body, "application/json",
                    ),
                ],
            )
            bundle = FrozenBundle.load(root)
            _validate_source_receipt(
                bundle,
                "RECEIPT",
                raw_source_id=spec.source_id,
                raw_schema_id="places_raw_csv",
                expected_hash=sha(receipt_body),
                spec=spec,
            )
            with self.assertRaisesRegex(IntegrityError, "wrong source ID"):
                _validate_source_receipt(
                    bundle,
                    "RECEIPT",
                    raw_source_id="WRONG-RAW-ROLE",
                    raw_schema_id="places_raw_csv",
                    expected_hash=sha(receipt_body),
                    spec=spec,
                )

    def test_phase2f_validates_416_completion_against_named_raw_artifact(self):
        body = b"synthetic already-complete source bytes"
        spec = FrozenInput(
            "SYNTHETIC-PHASE2F-SOURCE", "synthetic-fixture",
            "https://example.test/source.csv", "source.csv",
            len(body), sha(body), ("example.test",), "CSV", ("fixture",),
            ("text/csv",),
        )
        receipt = {
            "schema_version": "atlas-v19-stream-acquisition-receipt-2",
            "receipt_kind": "attempt",
            "source_id": spec.source_id,
            "release": spec.release,
            "request_method": "GET",
            "requested_url": spec.url,
            "request_headers": [
                ["Accept-Encoding", "identity"],
                ["Range", f"bytes={len(body)}-"],
            ],
            "range_request": f"bytes={len(body)}-",
            "started_at_utc": "2026-07-10T00:00:00Z",
            "ended_at_utc": "2026-07-10T00:00:01Z",
            "redirect_chain": [],
            "final_url": spec.url,
            "http_status": 416,
            "content_type": "text/csv",
            "content_encoding": "identity",
            "content_range": f"bytes */{len(body)}",
            "segment_start": None,
            "segment_end": None,
            "representation_total_bytes": len(body),
            "segment_bytes": 0,
            "segment_sha256": sha(b""),
            "response_bytes": 0,
            "response_sha256": sha(b""),
            "result_state": "complete",
            "rejection_reason": None,
            "final_bytes": len(body),
            "final_sha256": sha(body),
        }
        receipt_body = canonical_json_bytes(receipt)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            write_bundle(
                root,
                phase="2F",
                expectations={},
                files=[
                    (
                        spec.source_id, "places_raw_csv", spec.filename,
                        body, "text/csv",
                    ),
                    (
                        "RECEIPT", "source_acquisition_receipt_json",
                        "receipt.json", receipt_body, "application/json",
                    ),
                ],
            )
            _validate_source_receipt(
                FrozenBundle.load(root),
                "RECEIPT",
                raw_source_id=spec.source_id,
                raw_schema_id="places_raw_csv",
                expected_hash=sha(receipt_body),
                spec=spec,
            )

    def test_phase2f_source_receipt_rejects_wrong_nominal_raw_schema(self):
        body = b"synthetic frozen source bytes"
        spec = FrozenInput(
            "SYNTHETIC-PHASE2F-SOURCE", "synthetic-fixture",
            "https://example.test/source.xlsx", "source.xlsx",
            len(body), sha(body), ("example.test",), "XLSX", ("fixture",),
            ("application/octet-stream",), "504b",
        )
        receipt = {
            "schema_version": "atlas-v19-stream-acquisition-receipt-2",
            "receipt_kind": "cache_validation",
            "source_id": spec.source_id,
            "release": spec.release,
            "request_method": "NONE",
            "requested_url": spec.url,
            "request_headers": [],
            "range_request": None,
            "started_at_utc": "2026-07-10T00:00:00Z",
            "ended_at_utc": "2026-07-10T00:00:01Z",
            "redirect_chain": [],
            "final_url": spec.url,
            "http_status": 0,
            "content_type": "application/octet-stream",
            "content_encoding": "identity",
            "content_range": None,
            "segment_start": None,
            "segment_end": None,
            "representation_total_bytes": None,
            "segment_bytes": 0,
            "segment_sha256": sha(b""),
            "response_bytes": 0,
            "response_sha256": sha(b""),
            "result_state": "already_valid",
            "rejection_reason": None,
            "final_bytes": len(body),
            "final_sha256": sha(body),
        }
        receipt_body = canonical_json_bytes(receipt)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            write_bundle(
                root,
                phase="2F",
                expectations={},
                files=[
                    (
                        spec.source_id, "places_raw_csv", spec.filename,
                        body, "application/octet-stream",
                    ),
                    (
                        "RECEIPT", "source_acquisition_receipt_json",
                        "receipt.json", receipt_body, "application/json",
                    ),
                ],
            )
            with self.assertRaisesRegex(IntegrityError, "wrong schema"):
                _validate_source_receipt(
                    FrozenBundle.load(root),
                    "RECEIPT",
                    raw_source_id=spec.source_id,
                    raw_schema_id="hud_raw_xlsx",
                    expected_hash=sha(receipt_body),
                    spec=spec,
                )

    def test_d2ii_rederives_mappings_requests_and_rejects_fully_rehashed_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_bundle(
                root / "identity",
                phase="2F",
                expectations={},
                files=[
                    (
                        "IDENTITY",
                        "nppes_audit_json",
                        "identity.json",
                        canonical_json_bytes({"synthetic": "identity"}),
                        "application/json",
                    )
                ],
            )
            source_tree_id = FrozenBundle.load(root / "identity").source_tree_id
            provider_source = nppes_row(make_npi("710000001"))
            provider = classify_disposition(provider_source)
            hospital_source = hospital_row(
                "000001",
                **{
                    "Address": provider.address.street1,
                    "City/Town": provider.address.city,
                    "State": provider.address.state,
                    "ZIP Code": provider.address.zip5,
                },
            )
            hospital = in_scope_hospitals(
                deduplicate_hospitals((hospital_source,))
            )[0]
            provider_mapping = EntityAddressMapping(
                "P", provider.npi, provider.address.address_id,
                provider.address.street1, provider.address.street2,
                provider.address.city, provider.address.state,
                provider.address.zip5, provider.address.country,
                provider.address.classification, provider.address.direct_eligible,
                provider.address.hud_eligible,
            )
            hospital_mapping = EntityAddressMapping(
                "H", hospital.entity_id, hospital.address.address_id,
                hospital.address.street1, hospital.address.street2,
                hospital.address.city, hospital.address.state,
                hospital.address.zip5, hospital.address.country,
                hospital.address.classification, hospital.address.direct_eligible,
                hospital.address.hud_eligible,
            )
            requests = {
                provider_mapping.address_id: request_from_address(provider.address),
                hospital_mapping.address_id: request_from_address(hospital.address),
            }
            chunks = tuple(chunk_requests(requests.values()))
            manifest = request_manifest_bytes(chunks)
            phase2dii_bundle = sha(b"independently approved synthetic D-II bundle")
            audit = canonical_json_bytes(
                {
                    "source_snapshot_id": phase2dii_bundle,
                    "synthetic": "NPPES audit",
                }
            )
            stream_receipt = canonical_json_bytes(
                {
                    "source_snapshot_id": phase2dii_bundle,
                    "synthetic": "NPPES stream receipt",
                }
            )
            dispositions = canonical_json_bytes(
                {
                    "schema_version": "atlas-v19-nppes-dispositions-1",
                    "npi": provider.npi,
                    "pre_geography_disposition": "pre_geography_eligible",
                    "diagnostics": [],
                    "address_id": provider.address.address_id,
                    "direct_eligible": True,
                    "hud_eligible": True,
                }
            )
            storage_receipt = sha(b"independently approved synthetic storage receipt")

            def build(target: Path, *, tamper: bool):
                provider_value = asdict(provider_mapping)
                if tamper:
                    provider_value["entity_id"] = make_npi("710000002")
                provider_body = canonical_json_bytes(provider_value)
                hospital_body = canonical_json_bytes(asdict(hospital_mapping))
                chunk_rows = [
                    {
                        "filename": chunk.filename,
                        "bytes": chunk.byte_count,
                        "sha256": chunk.sha256,
                    }
                    for chunk in chunks
                ]
                evidence = {
                    "phase2dii_bundle_manifest_sha256": phase2dii_bundle,
                    "nppes_archive_sha256": sha(b"synthetic NPPES archive"),
                    "nppes_candidate_manifest_sha256": sha(b"synthetic candidate manifest"),
                    "nppes_approval_receipt_sha256": sha(b"synthetic member approval"),
                    "nppes_approval_trust_root_sha256": sha(b"synthetic approval trust root"),
                    "nppes_selected_member_filename": "npidata_pfile_synthetic.csv",
                    "nppes_selected_member_sha256": sha(b"synthetic selected member"),
                    "provider_mapping_sha256": sha(provider_body),
                    "hospital_mapping_sha256": sha(hospital_body),
                    "nppes_audit_sha256": sha(audit),
                    "nppes_dispositions_sha256": sha(dispositions),
                    "request_manifest_sha256": sha(manifest),
                    "request_chunks_sha256": sha(canonical_json_bytes(chunk_rows)),
                    "request_count": len(requests),
                    "chunk_count": len(chunks),
                    "storage_preflight_receipt_sha256": storage_receipt,
                }
                handoff = canonical_json_bytes(
                    {
                        "schema_version": "atlas-v19-d2ii-handoff-receipt-1",
                        "receipt_id": "d2ii_handoff",
                        "status": "complete",
                        "source_tree_id": source_tree_id,
                        "bundle_manifest_sha256": phase2dii_bundle,
                        "authorization_id": "SYNTHETIC-D2II-AUTHORIZATION",
                        "command": "prepare-requests",
                        "completed_at_utc": "2026-07-10T00:00:00Z",
                        "evidence": evidence,
                    }
                )
                files = [
                    ("PROVIDER-MAPPING", "entity_address_mapping_jsonl", "providers.jsonl", provider_body, "application/jsonl"),
                    ("HOSPITAL-MAPPING", "entity_address_mapping_jsonl", "hospitals.jsonl", hospital_body, "application/jsonl"),
                    ("REQUEST-MANIFEST", "request_manifest_json", "request_manifest.json", manifest, "application/json"),
                    ("NPPES-AUDIT", "nppes_audit_json", "nppes_audit.json", audit, "application/json"),
                    ("NPPES-DISPOSITIONS", "nppes_dispositions_jsonl", "nppes_dispositions.jsonl", dispositions, "application/jsonl"),
                    ("NPPES-STREAM", "build_receipt_json", "nppes_stream_receipt.json", stream_receipt, "application/json"),
                    ("D2II-HANDOFF", "d2ii_handoff_receipt_json", "d2ii_handoff_receipt.json", handoff, "application/json"),
                ]
                for index, chunk in enumerate(chunks, start=1):
                    files.append(
                        (
                            f"REQUEST-{index:04d}", "geocoder_request_csv",
                            chunk.filename, chunk.body, "text/csv",
                        )
                    )
                roles = {
                    "provider_mapping": "PROVIDER-MAPPING",
                    "hospital_mapping": "HOSPITAL-MAPPING",
                    "request_manifest": "REQUEST-MANIFEST",
                    "request_chunks": [f"REQUEST-{index:04d}" for index in range(1, len(chunks) + 1)],
                    "nppes_audit": "NPPES-AUDIT",
                    "nppes_dispositions": "NPPES-DISPOSITIONS",
                    "nppes_stream_receipt": "NPPES-STREAM",
                    "d2ii_handoff_receipt": "D2II-HANDOFF",
                }
                write_bundle(target, phase="2F", expectations={}, files=files)
                external_evidence = {
                    "phase2dii_bundle_manifest_sha256": phase2dii_bundle,
                    "d2ii_handoff_receipt_sha256": sha(handoff),
                    **{key: value for key, value in evidence.items() if key != "phase2dii_bundle_manifest_sha256"},
                }
                return FrozenBundle.load(target), roles, {
                    "evidence": external_evidence
                }

            inputs = OfflineBuildInputs(
                (provider_source,),
                (hospital_source,),
                (),
                (),
                {},
                {},
                (),
                sha(b"synthetic source snapshot"),
                sha(b"synthetic HUD snapshot"),
                sha(b"synthetic Gazetteer snapshot"),
                METHOD_LINEAGE_ID,
            )
            good, roles, external = build(root / "good", tamper=False)
            nppes_provenance = {
                key: external["evidence"][key]
                for key in (
                    "nppes_archive_sha256",
                    "nppes_candidate_manifest_sha256",
                    "nppes_approval_receipt_sha256",
                    "nppes_approval_trust_root_sha256",
                    "nppes_selected_member_filename",
                    "nppes_selected_member_sha256",
                )
            }
            verified = _verify_d2ii(
                good, roles, inputs, external, nppes_provenance
            )
            self.assertEqual(len(verified.requests), len(requests))
            selected_member_tamper = dict(nppes_provenance)
            selected_member_tamper["nppes_selected_member_sha256"] = sha(
                b"different selected member"
            )
            with self.assertRaisesRegex(
                IntegrityError, "selected-member provenance mismatch"
            ):
                _verify_d2ii(
                    good, roles, inputs, external, selected_member_tamper
                )

            bad, roles, external = build(root / "tampered", tamper=True)
            with self.assertRaisesRegex(IntegrityError, "provider mapping differs"):
                _verify_d2ii(bad, roles, inputs, external, nppes_provenance)


if __name__ == "__main__":
    unittest.main()
