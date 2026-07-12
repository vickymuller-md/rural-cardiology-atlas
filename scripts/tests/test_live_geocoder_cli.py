from __future__ import annotations

import csv
import hashlib
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atlas_pipeline.addresses import EntityAddressMapping, normalize_address
from atlas_pipeline.acquisition import canonical_manifest_bytes
from atlas_pipeline.bundle import FrozenBundle, canonical_json_bytes
from atlas_pipeline.contracts import (
    CENSUS_BENCHMARK_ID,
    CENSUS_BENCHMARK_NAME,
    CENSUS_GEOCODER_ENDPOINT,
    CENSUS_VINTAGE_ID,
    CENSUS_VINTAGE_NAME,
)
from atlas_pipeline.geocoder import GeocoderRequest, request_csv_bytes
from atlas_pipeline.geocoder_provenance import verify_census_provenance
from atlas_pipeline.errors import IntegrityError
from atlas_pipeline.live_geocoder import (
    APPROVAL_SCHEMA_VERSION,
    LiveApproval,
    TransportResponse,
    acquire_census,
)
from atlas_pipeline.pipeline_cli import acquire_main, build_main
from atlas_pipeline.offline_bundle import METHOD_LINEAGE_ID, PHASE2F_ROLES
from atlas_pipeline.phase2f_runner import (
    MANDATORY_R4_REVIEW_RECEIPT_IDS,
    REVIEW_SCHEMA_VERSION,
    ReviewConfig,
    VALIDATION_COMPLETION_SCHEMA_VERSION,
    python_network_denied,
)
from atlas_pipeline.predecessor_bridge import (
    PREDECESSOR_ARTIFACT_SHA256,
    R4B_SOURCE_TREE_ID,
    R4_INPUT_COUNT,
    R4_INPUT_MANIFEST_SHA256,
    R4_METHOD_LINEAGE_ID,
    PredecessorBridgeContext,
    require_predecessor_hash,
)
from atlas_pipeline.request_verification import verify_frozen_request_chain
from atlas_pipeline.source_tree import SourceTreeIdentity
from atlas_pipeline.trust import RuntimeControl
from tests.bundle_helpers import write_bundle
from tests.synthetic import make_npi


class FakeTransport:
    def __init__(self, response: TransportResponse) -> None:
        self.response = response
        self.calls = 0

    def execute(self, _request) -> TransportResponse:
        self.calls += 1
        return self.response


class SequenceTransport:
    def __init__(self, responses: list[TransportResponse]) -> None:
        self.responses = list(responses)
        self.requests = []

    def execute(self, request) -> TransportResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def mapping_bytes(mapping: EntityAddressMapping) -> bytes:
    return canonical_json_bytes(mapping.__dict__)


class LiveGeocoderTests(unittest.TestCase):
    def test_census_predecessor_role_is_exactly_pinned(self):
        context = PredecessorBridgeContext(
            "a" * 64,
            R4_METHOD_LINEAGE_ID,
            R4_INPUT_MANIFEST_SHA256,
            R4_INPUT_COUNT,
        )
        artifact_id = "R4B-CENSUS-HANDOFF-RECEIPT"
        artifact_sha256 = PREDECESSOR_ARTIFACT_SHA256[artifact_id]
        require_predecessor_hash(
            context,
            observed_source_tree_id=R4B_SOURCE_TREE_ID,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha256,
        )
        with self.assertRaises(IntegrityError):
            require_predecessor_hash(
                context,
                observed_source_tree_id=R4B_SOURCE_TREE_ID,
                artifact_id=artifact_id,
                artifact_sha256=artifact_sha256[:-1]
                + format(int(artifact_sha256[-1], 16) ^ 1, "x"),
            )

    def make_bundle(self, root: Path, *, tamper_request: bool = False):
        address = normalize_address("100 TEST ST", "", "CITY", "AL", "01001", "US")
        request = request_csv_bytes(
            [GeocoderRequest(address.address_id, address.submitted_street, address.city, address.state, address.zip5)]
        )
        if tamper_request:
            request = request.replace(b"100 TEST ST", b"999 ALTERED ST")
        manifest = canonical_json_bytes(
            [
                {
                    "filename": "geocoder_request_0001.csv",
                    "first_id": address.address_id,
                    "last_id": address.address_id,
                    "row_count": 1,
                    "byte_count": len(request),
                    "sha256": hashlib.sha256(request).hexdigest(),
                }
            ]
        )
        mapping = EntityAddressMapping(
            "P", make_npi("600000001"), address.address_id, address.street1,
            address.street2, address.city, address.state, address.zip5,
            address.country, address.classification, True, True,
        )
        hospital_mapping = EntityAddressMapping(
            "H", "H-000001", address.address_id, address.street1,
            address.street2, address.city, address.state, address.zip5,
            address.country, address.classification, True, True,
        )
        expectations = {
            "source_roles": {
                "request_manifest": "REQUEST-MANIFEST",
                "provider_mapping": "PROVIDER-MAPPING",
                "hospital_mapping": "HOSPITAL-MAPPING",
                "request_chunks": ["REQUEST-0001"],
            },
            "county_universe": ["01001"],
        }
        write_bundle(
            root,
            phase="2E",
            expectations=expectations,
            files=[
                ("REQUEST-MANIFEST", "request_manifest_json", "request_manifest.json", manifest, "application/json"),
                ("PROVIDER-MAPPING", "entity_address_mapping_jsonl", "providers.jsonl", mapping_bytes(mapping), "application/jsonl"),
                ("HOSPITAL-MAPPING", "entity_address_mapping_jsonl", "hospitals.jsonl", mapping_bytes(hospital_mapping), "application/jsonl"),
                ("REQUEST-0001", "geocoder_request_csv", "geocoder_request_0001.csv", request, "text/csv"),
            ],
        )
        bundle = FrozenBundle.load(root, required_phase="2E")
        approval_value = {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "phase": "2E",
            "authorization_id": bundle.authorization_id,
            "bundle_manifest_sha256": bundle.manifest_sha256,
            "request_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "endpoint": CENSUS_GEOCODER_ENDPOINT,
            "benchmark_id": CENSUS_BENCHMARK_ID,
            "benchmark_name": CENSUS_BENCHMARK_NAME,
            "vintage_id": CENSUS_VINTAGE_ID,
            "vintage_name": CENSUS_VINTAGE_NAME,
            "network_allowed": True,
            "approved_at_utc": "2026-07-10T00:00:00Z",
        }
        approval_path = root.parent / "approval.json"
        approval_path.write_bytes(canonical_json_bytes(approval_value))
        return bundle, approval_path, address

    def test_approved_synthetic_transport_accepts_first_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, approval_path, address = self.make_bundle(root / "bundle")
            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(
                [
                    address.address_id, "input", "Match", "Exact", "100 TEST ST",
                    "-86.000000,32.000000", "1", "L", "01", "001", "000100", "1000",
                ]
            )
            transport = FakeTransport(
                TransportResponse(
                    200,
                    {"Content-Type": "text/csv", "Content-Encoding": "identity"},
                    output.getvalue().encode(),
                    CENSUS_GEOCODER_ENDPOINT,
                    CENSUS_GEOCODER_ENDPOINT,
                )
            )
            approval = LiveApproval.load(approval_path, bundle)
            receipt = acquire_census(
                bundle,
                approval,
                root / "output",
                transport=transport,
                sleep=lambda _seconds: self.fail("valid Match must not retry"),
                now=lambda: "2026-07-10T00:00:01Z",
            )
            self.assertEqual(transport.calls, 1)
            self.assertTrue(receipt.is_file())
            snapshot = (root / "output" / "geocoder_canonical.jsonl").read_text()
            self.assertIn('"outcome":"Match"', snapshot)

    def test_official_text_plain_short_no_match_representation_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, approval_path, address = self.make_bundle(root / "bundle")
            body = f"{address.address_id},input,No_Match\n".encode()
            transport = FakeTransport(
                TransportResponse(
                    200,
                    {
                        "Content-Type": "text/plain",
                        "Content-Disposition": "attachment; filename=GeocodeResults.csv",
                        "Content-Encoding": "identity",
                    },
                    body,
                    CENSUS_GEOCODER_ENDPOINT,
                    CENSUS_GEOCODER_ENDPOINT,
                )
            )
            receipt = acquire_census(
                bundle,
                LiveApproval.load(approval_path, bundle),
                root / "output",
                transport=transport,
                sleep=lambda _seconds: self.fail("approved representation must not retry"),
                now=lambda: "2026-07-10T00:00:01Z",
            )
            self.assertTrue(receipt.is_file())
            self.assertEqual(transport.calls, 1)
            snapshot = (root / "output" / "geocoder_canonical.jsonl").read_text()
            self.assertIn('"outcome":"No_Match"', snapshot)

    def test_cli_gates_fail_before_transport_or_bundle_loading(self):
        with self.assertRaises(SystemExit):
            acquire_main(
                [
                    "--bundle", "/missing", "--approval-receipt", "/missing",
                    "--output", "/missing",
                ]
            )

    def test_census_reconstructs_chunks_from_mappings_and_rejects_consistent_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, approval_path, _address = self.make_bundle(
                root / "bundle", tamper_request=True
            )
            approval = LiveApproval.load(approval_path, bundle)
            transport = FakeTransport(
                TransportResponse(
                    200,
                    {"Content-Type": "text/csv"},
                    b"",
                    CENSUS_GEOCODER_ENDPOINT,
                    CENSUS_GEOCODER_ENDPOINT,
                )
            )
            with self.assertRaisesRegex(IntegrityError, "reconstructed exact bytes"):
                acquire_census(
                    bundle,
                    approval,
                    root / "output",
                    transport=transport,
                    sleep=lambda _seconds: None,
                )
            self.assertEqual(transport.calls, 0)

    def test_every_census_attempt_records_prepared_provenance_and_integrity_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, approval_path, address = self.make_bundle(root / "bundle")
            output = io.StringIO(newline="")
            csv.writer(output, lineterminator="\n").writerow(
                [
                    address.address_id, "input", "No_Match", "", "", "", "", "",
                    "", "", "", "",
                ]
            )
            body = output.getvalue().encode()
            transport = SequenceTransport(
                [
                    TransportResponse(
                        200,
                        {"Content-Type": "application/json"},
                        body,
                        CENSUS_GEOCODER_ENDPOINT,
                        CENSUS_GEOCODER_ENDPOINT,
                    ),
                    TransportResponse(
                        200,
                        {"Content-Type": "text/csv", "Content-Encoding": "identity"},
                        body,
                        CENSUS_GEOCODER_ENDPOINT,
                        CENSUS_GEOCODER_ENDPOINT,
                    ),
                ]
            )
            receipt = acquire_census(
                bundle,
                LiveApproval.load(approval_path, bundle),
                root / "output",
                transport=transport,
                sleep=lambda _seconds: None,
            )
            self.assertTrue(receipt.is_file())
            first = json.loads(
                (root / "output" / "chunk_0001" / "attempt_1.json").read_text()
            )
            second = json.loads(
                (root / "output" / "chunk_0001" / "attempt_2.json").read_text()
            )
            self.assertIn("Content-Type is not CSV", first["integrity_error"])
            self.assertIsNone(second["integrity_error"])
            self.assertEqual(first["request_method"], "POST")
            self.assertEqual(first["requested_url"], CENSUS_GEOCODER_ENDPOINT)
            self.assertEqual(first["final_url"], CENSUS_GEOCODER_ENDPOINT)
            self.assertEqual(first["benchmark_id"], CENSUS_BENCHMARK_ID)
            self.assertEqual(first["vintage_id"], CENSUS_VINTAGE_ID)
            self.assertRegex(first["prepared_body_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(len(transport.requests), 2)
            self.assertEqual(
                transport.requests[0].body_sha256,
                transport.requests[1].body_sha256,
            )
        with self.assertRaises(SystemExit):
            build_main(
                [
                    "--bundle", "/missing", "--review-config", "/missing",
                    "--output", "/missing",
                ]
            )

    def test_phase2f_revalidates_every_attempt_and_rejects_self_labeled_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            phase2e, approval_path, address = self.make_bundle(root / "phase2e")
            csv_output = io.StringIO(newline="")
            csv.writer(csv_output, lineterminator="\n").writerow(
                [
                    address.address_id, "input", "No_Match", "", "", "", "", "",
                    "", "", "", "",
                ]
            )
            acquire_census(
                phase2e,
                LiveApproval.load(approval_path, phase2e),
                root / "acquired",
                transport=FakeTransport(
                    TransportResponse(
                        200,
                        {"Content-Type": "text/csv", "Content-Encoding": "identity"},
                        csv_output.getvalue().encode(),
                        CENSUS_GEOCODER_ENDPOINT,
                        CENSUS_GEOCODER_ENDPOINT,
                    )
                ),
                sleep=lambda _seconds: None,
            )

            source_roles = phase2e.expectation("source_roles", dict)
            base_files = [
                (
                    source_id,
                    phase2e.inputs[source_id].schema_id,
                    Path(phase2e.inputs[source_id].relative_path).name,
                    phase2e.verify_file(source_id).read_bytes(),
                    phase2e.inputs[source_id].content_type,
                )
                for source_id in (
                    source_roles["request_manifest"],
                    source_roles["provider_mapping"],
                    source_roles["hospital_mapping"],
                    *source_roles["request_chunks"],
                )
            ]

            def phase2f_bundle(
                target: Path, *, self_label: bool, canonical_tamper: bool
            ) -> FrozenBundle:
                metadata = json.loads(
                    (root / "acquired/chunk_0001/attempt_1.json").read_bytes()
                )
                if self_label:
                    metadata["integrity_error"] = "self-authored retry/acceptance label"
                metadata_body = canonical_json_bytes(metadata)
                raw_body = (root / "acquired/chunk_0001/attempt_1.response").read_bytes()
                attempts = json.loads((root / "acquired/attempts_manifest.json").read_bytes())
                attempts[0]["metadata_bytes"] = len(metadata_body)
                attempts[0]["metadata_sha256"] = hashlib.sha256(metadata_body).hexdigest()
                attempts_body = canonical_json_bytes(attempts)
                acquisition = json.loads(
                    (root / "acquired/census_handoff_receipt.json").read_bytes()
                )
                canonical_body = (root / "acquired/geocoder_canonical.jsonl").read_bytes()
                if canonical_tamper:
                    canonical_body = canonical_body.replace(
                        b'"outcome":"No_Match"', b'"outcome":"Tie"'
                    )
                acquisition["evidence"]["attempts_manifest_sha256"] = hashlib.sha256(
                    attempts_body
                ).hexdigest()
                acquisition["evidence"]["canonical_snapshot_sha256"] = hashlib.sha256(
                    canonical_body
                ).hexdigest()
                acquisition_body = canonical_json_bytes(acquisition)
                files = base_files + [
                    ("CENSUS-ATTEMPTS", "geocoder_attempts_manifest_json", "attempts_manifest.json", attempts_body, "application/json"),
                    ("CENSUS-META-1", "geocoder_attempt_json", "attempt_1.json", metadata_body, "application/json"),
                    ("CENSUS-RAW-1", "geocoder_response_bytes", "attempt_1.response", raw_body, "text/csv"),
                    ("CENSUS-CANONICAL", "geocoder_canonical_jsonl", "geocoder_canonical.jsonl", canonical_body, "application/jsonl"),
                    ("CENSUS-ACQUISITION", "census_handoff_receipt_json", "census_handoff_receipt.json", acquisition_body, "application/json"),
                ]
                write_bundle(target, phase="2F", expectations={}, files=files)
                return FrozenBundle.load(target, required_phase="2F")

            for self_label, canonical_tamper in (
                (False, False), (True, False), (False, True)
            ):
                bundle = phase2f_bundle(
                    root
                    / (
                        "self-labeled"
                        if self_label
                        else "canonical-tampered"
                        if canonical_tamper
                        else "verified"
                    ),
                    self_label=self_label,
                    canonical_tamper=canonical_tamper,
                )
                request_chain = verify_frozen_request_chain(
                    bundle,
                    provider_mapping_source=source_roles["provider_mapping"],
                    hospital_mapping_source=source_roles["hospital_mapping"],
                    request_manifest_source=source_roles["request_manifest"],
                    request_chunk_sources=source_roles["request_chunks"],
                )
                attempts_hash = hashlib.sha256(
                    bundle.verify_file("CENSUS-ATTEMPTS").read_bytes()
                ).hexdigest()
                canonical_hash = hashlib.sha256(
                    bundle.verify_file("CENSUS-CANONICAL").read_bytes()
                ).hexdigest()
                evidence = {
                    "phase2e_bundle_manifest_sha256": phase2e.manifest_sha256,
                    "request_manifest_sha256": request_chain.manifest_sha256,
                    "attempts_manifest_sha256": attempts_hash,
                    "canonical_snapshot_sha256": canonical_hash,
                    "geocoder_acquisition_receipt_sha256": hashlib.sha256(
                        bundle.verify_file("CENSUS-ACQUISITION").read_bytes()
                    ).hexdigest(),
                    "benchmark_id": int(CENSUS_BENCHMARK_ID),
                    "vintage_id": int(CENSUS_VINTAGE_ID),
                    "attempt_count": 1,
                }
                call = lambda: verify_census_provenance(
                    bundle,
                    request_chain=request_chain,
                    attempts_manifest_source="CENSUS-ATTEMPTS",
                    attempt_metadata_sources=["CENSUS-META-1"],
                    attempt_response_sources=["CENSUS-RAW-1"],
                    acquisition_receipt_source="CENSUS-ACQUISITION",
                    canonical_snapshot_source="CENSUS-CANONICAL",
                    control_receipt_evidence=evidence,
                    county_universe={"01001"},
                )
                if self_label:
                    with self.assertRaisesRegex(IntegrityError, "objectively rederived"):
                        call()
                elif canonical_tamper:
                    with self.assertRaisesRegex(IntegrityError, "differs from accepted attempts"):
                        call()
                else:
                    verified = call()
                    self.assertEqual(verified.accepted_by_chunk[1].attempt, 1)

    def test_review_config_has_no_bundle_hash_cycle_and_binds_complete_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt_ids = sorted(MANDATORY_R4_REVIEW_RECEIPT_IDS)
            self.assertIn("predecessor_bridge_audit", receipt_ids)
            write_bundle(
                root / "bundle",
                phase="2F",
                expectations={
                    "method_lineage_id": METHOD_LINEAGE_ID,
                    "source_roles": {role: "SYNTHETIC" for role in sorted(PHASE2F_ROLES)},
                },
                files=[
                    (
                        "SYNTHETIC-INPUT",
                        "nppes_audit_json",
                        "synthetic-input.json",
                        canonical_json_bytes({"synthetic": "input"}),
                        "application/json",
                    )
                ],
            )
            bundle = FrozenBundle.load(root / "bundle", required_phase="2F")
            input_manifest_sha256 = hashlib.sha256(
                canonical_manifest_bytes(list(bundle.frozen_inputs()))
            ).hexdigest()
            config_value = {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "authorization_id": bundle.authorization_id,
                "source_tree_id": bundle.source_tree_id,
                "method_lineage_id": METHOD_LINEAGE_ID,
                "validation_completion_schema_version": VALIDATION_COMPLETION_SCHEMA_VERSION,
                "bundle_manifest_sha256": bundle.manifest_sha256,
                "input_manifest_sha256": input_manifest_sha256,
                "independent_reviewer": "Independent Synthetic Reviewer",
                "network_disabled": True,
                "geometry_engine": {
                    "package": "synthetic-geometry",
                    "version": "1.0.0",
                    "package_sha256": hashlib.sha256(
                        b"synthetic geometry package"
                    ).hexdigest(),
                    "algorithm": "covers",
                },
                "control_receipts_manifest_sha256": hashlib.sha256(
                    b"independently approved control receipts manifest"
                ).hexdigest(),
                "required_receipt_ids": receipt_ids,
            }
            self.assertNotIn("review_config_sha256", bundle.expectations)
            config_path = root / "review.json"
            config_path.write_bytes(canonical_json_bytes(config_value))
            source_body = canonical_json_bytes({"synthetic": "source tree"})
            repo_root = Path(__file__).resolve().parents[2]
            runtime = RuntimeControl(
                phase="2F",
                control_id="synthetic-control",
                authorization_id=bundle.authorization_id,
                trust_root_sha256=hashlib.sha256(b"external trust root").hexdigest(),
                source_tree=SourceTreeIdentity(
                    repo_root,
                    source_body,
                    bundle.source_tree_id,
                    ("scripts/atlas_pipeline/synthetic.py",),
                ),
                approved_bundle_sha256=bundle.manifest_sha256,
                approved_authorization_sha256=hashlib.sha256(
                    b"external authorization"
                ).hexdigest(),
                approved_review_config_sha256=hashlib.sha256(
                    config_path.read_bytes()
                ).hexdigest(),
                approved_control_receipts_manifest_sha256=config_value[
                    "control_receipts_manifest_sha256"
                ],
                approved_storage_preflight_receipt_sha256=None,
                imported_modules=("atlas_pipeline.phase2f_runner",),
            )
            receipts = {
                receipt_id: {
                    "evidence": (
                        {"phase2f_input_manifest_sha256": input_manifest_sha256}
                        if receipt_id == "input_materialization_import"
                        else {
                            "lockfiles": [
                                {
                                    "path": path,
                                    "sha256": hashlib.sha256(
                                        (repo_root / path).read_bytes()
                                    ).hexdigest(),
                                }
                                for path in (
                                    "app/package-lock.json",
                                    "scripts/uv.lock",
                                    "tooling/topology/package-lock.json",
                                )
                            ]
                        }
                        if receipt_id == "dependency_materialization"
                        else {
                            "config_path": "tooling/topology/topology-contract.json",
                            "config_sha256": hashlib.sha256(
                                (
                                    repo_root
                                    / "tooling/topology/topology-contract.json"
                                ).read_bytes()
                            ).hexdigest(),
                        }
                        if receipt_id == "topology_authorization"
                        else {}
                    )
                }
                for receipt_id in receipt_ids
            }
            manifest_path = root / "control-receipts.json"
            manifest_path.write_bytes(canonical_json_bytes({"synthetic": "manifest"}))
            with patch(
                "atlas_pipeline.phase2f_runner.load_control_receipts_manifest",
                return_value=receipts,
            ):
                config = ReviewConfig.load(
                    config_path, bundle, runtime, manifest_path
                )
            self.assertEqual(config.receipt_ids, tuple(receipt_ids))
            self.assertEqual(config.method_lineage_id, METHOD_LINEAGE_ID)
            self.assertEqual(
                config.validation_completion_schema_version,
                VALIDATION_COMPLETION_SCHEMA_VERSION,
            )
            self.assertEqual(config.geometry_engine.algorithm, "covers")
            config_path.write_bytes(config_path.read_bytes() + b" ")
            with self.assertRaises(IntegrityError):
                ReviewConfig.load(config_path, bundle, runtime, manifest_path)

    def test_phase2f_network_guard_blocks_high_and_low_level_socket_paths(self):
        original = socket.create_connection
        with python_network_denied():
            with self.assertRaisesRegex(RuntimeError, "network access is disabled"):
                socket.create_connection(("127.0.0.1", 9))
            with socket.socket() as raw:
                with self.assertRaisesRegex(RuntimeError, "network access is disabled"):
                    raw.connect(("127.0.0.1", 9))
        self.assertIs(socket.create_connection, original)


if __name__ == "__main__":
    unittest.main()
