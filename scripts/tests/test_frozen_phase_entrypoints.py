from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

from atlas_pipeline.acquisition import (
    FrozenInput,
    PendingDigestInput,
    ZIP_APPROVAL_TRUST_ROOT_SCHEMA_VERSION,
    ZipManifestApproval,
    authorize_zip_approval,
    build_zip_candidate_manifest,
    canonical_zip_candidate_manifest_bytes,
    canonical_zip_manifest_approval_bytes,
    zip_member_central_metadata_sha256,
)
from atlas_pipeline.bundle import FrozenBundle
from atlas_pipeline.errors import AcquisitionError, ContractError, IntegrityError
from atlas_pipeline.hud_surface import (
    CURRENT_CT_PLANNING_REGIONS,
    build_hud_handoff,
)
from atlas_pipeline.geoqa import sorted_fips_bytes
from atlas_pipeline.materialization import (
    MATERIALIZATION_SCHEMA_VERSION,
    NPPES_TRANSPORT_SCHEMA_VERSION,
    MaterializationManifest,
    NppesTransportPlan,
    StreamingResponse,
    _acquire_pending_candidate,
    _load_zip_candidate,
    acquire_nppes_candidate,
    acquire_known_hash,
    approve_nppes_container,
    import_known_hash,
    inspect_nppes_container,
    validate_stream_acquisition_receipt,
)
from atlas_pipeline.places import PLACES_REQUIRED_FIELDS
from atlas_pipeline.pipeline_cli import acquire_nppes_main
from atlas_pipeline.places_derivation import derive_places_county
from atlas_pipeline.source_registry import (
    HUD_HEADERS,
    SOURCE_POLICIES,
    ReleaseValidationContext,
    resolve_release_validators,
)
from atlas_pipeline.strict_json import canonical_json_bytes
from tests.bundle_helpers import write_bundle


SOURCE_TREE_ID = hashlib.sha256(b"frozen synthetic source tree identity").hexdigest()
PLAN_SHA256 = hashlib.sha256(b"externally approved synthetic acquisition plan").hexdigest()
AUTHORIZATION_ID = "SYNTHETIC-AUTHORIZATION-1"
STORAGE_PREFLIGHT_SHA256 = hashlib.sha256(
    b"externally approved synthetic storage preflight"
).hexdigest()


class TrackingBody(io.BytesIO):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("streaming transport attempted an unbounded read")
        return super().read(size)


class FakeTransport:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        final_url: str | None = None,
    ) -> None:
        self.tracker = TrackingBody(body)
        self.status = status
        self.headers = headers or {
            "Content-Type": "application/zip",
            "Content-Encoding": "identity",
            "Content-Length": str(len(body)),
        }
        self.final_url = final_url
        self.calls = 0

    @contextmanager
    def get(self, url: str, headers: dict[str, str]):
        self.calls += 1
        response = StreamingResponse(
            self.status,
            self.headers,
            self.tracker,
            url,
            self.final_url or url,
            (),
            "GET",
            tuple(headers.items()),
            "2026-07-10T12:00:00Z",
            "2026-07-10T12:00:01Z",
        )
        yield response


def synthetic_materialization_manifest(
    phase: str,
    sources: dict[str, FrozenInput],
    *,
    expectations: dict[str, object] | None = None,
) -> MaterializationManifest:
    body = canonical_json_bytes({"phase": phase, "synthetic": True})
    return MaterializationManifest(
        Path("synthetic-materialization.json"),
        phase,
        SOURCE_TREE_ID,
        expectations or {},
        sources,
        body,
        hashlib.sha256(body).hexdigest(),
    )


def write_zip(path: Path, members: dict[str, bytes]) -> bytes:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path.read_bytes()


def zip_approval(candidate, path: Path) -> ZipManifestApproval:
    selected = (
        next(
            member
            for member in candidate.members
            if not member.is_directory and member.filename.startswith("npidata_pfile")
        )
        if candidate.source_id == "NPPES-202606-V2"
        else None
    )
    approval = ZipManifestApproval(
        candidate.source_id,
        candidate.archive_sha256,
        hashlib.sha256(canonical_zip_candidate_manifest_bytes(candidate)).hexdigest(),
        "SYNTHETIC-INDEPENDENT-ZIP-APPROVAL",
        "Independent Synthetic Integrator",
        "2026-07-10T12:30:00Z",
        selected.filename if selected is not None else None,
        zip_member_central_metadata_sha256(selected) if selected is not None else None,
    )
    path.write_bytes(canonical_json_bytes(asdict(approval)))
    return approval


def trusted_zip_approval(
    candidate,
    approval_path: Path,
    trust_root_path: Path,
    *,
    source_tree_id: str = SOURCE_TREE_ID,
    bundle_manifest_sha256: str = PLAN_SHA256,
    authorization_id: str = AUTHORIZATION_ID,
):
    approval = zip_approval(candidate, approval_path)
    approval_body = canonical_zip_manifest_approval_bytes(approval)
    trust_body, trust_pin = zip_approval_trust_root(
        candidate,
        approval_body,
        trust_root_path,
        source_tree_id=source_tree_id,
        bundle_manifest_sha256=bundle_manifest_sha256,
        authorization_id=authorization_id,
    )
    trusted = authorize_zip_approval(
        candidate,
        approval_body,
        trust_body,
        independently_approved_trust_root_sha256=trust_pin,
        source_tree_id=source_tree_id,
        bundle_manifest_sha256=bundle_manifest_sha256,
        authorization_id=authorization_id,
    )
    return trusted, trust_pin


def zip_approval_trust_root(
    candidate,
    approval_body: bytes,
    trust_root_path: Path,
    *,
    source_tree_id: str = SOURCE_TREE_ID,
    bundle_manifest_sha256: str = PLAN_SHA256,
    authorization_id: str = AUTHORIZATION_ID,
) -> tuple[bytes, str]:
    approval = json.loads(approval_body)
    trust_body = canonical_json_bytes(
        {
            "schema_version": ZIP_APPROVAL_TRUST_ROOT_SCHEMA_VERSION,
            "trust_root_id": "SYNTHETIC-INDEPENDENT-ZIP-TRUST",
            "source_tree_id": source_tree_id,
            "bundle_manifest_sha256": bundle_manifest_sha256,
            "authorization_id": authorization_id,
            "source_id": candidate.source_id,
            "archive_sha256": candidate.archive_sha256,
            "candidate_manifest_sha256": hashlib.sha256(
                canonical_zip_candidate_manifest_bytes(candidate)
            ).hexdigest(),
            "member_approval_sha256": hashlib.sha256(approval_body).hexdigest(),
            "approved_by": "Independent Synthetic Integrator",
            "approved_at_utc": "2026-07-10T12:31:00Z",
            "selected_member_filename": approval["selected_member_filename"],
            "selected_member_central_metadata_sha256": approval[
                "selected_member_central_metadata_sha256"
            ],
        }
    )
    trust_root_path.write_bytes(trust_body)
    trust_pin = hashlib.sha256(trust_body).hexdigest()
    return trust_body, trust_pin


class FrozenMaterializationTests(unittest.TestCase):
    @staticmethod
    def _spec_for(source_id: str) -> FrozenInput:
        policy = SOURCE_POLICIES[source_id]
        return FrozenInput(
            source_id=policy.source_id,
            release=policy.release,
            url=policy.url,
            filename=policy.filename,
            bytes=policy.bytes,
            sha256=policy.sha256,
            allowed_hosts=(urlparse(policy.url).hostname,),
            artifact_type=policy.artifact_type,
            validator_ids=policy.validator_ids,
            content_types=policy.content_types,
            magic_hex=policy.magic_hex,
        )

    def test_acs_pipe_and_rucc_long_form_release_contracts(self):
        fixtures = {
            "ACS-POP-2024-5Y": (
                b"GEO_ID|B01003_E001|B01003_M001\n0500000US01001|100|1\n",
                b"GEO_ID,B01003_E001,B01003_M001\n0500000US01001,100,1\n",
            ),
            "ACS-INCOME-2024-5Y": (
                b"GEO_ID|B19013_E001|B19013_M001\n0500000US01001|50000|2\n",
                b"GEO_ID,B19013_E001,B19013_M001\n0500000US01001,50000,2\n",
            ),
            "RUCC-2023": (
                b"FIPS,State,County_Name,Attribute,Value\n"
                b"35013,NM,Do\xf1a Ana County,Population_2020,219561\n"
                b"35013,NM,Do\xf1a Ana County,RUCC_2023,2\n"
                b"35013,NM,Do\xf1a Ana County,Description,Metro\n",
                b"FIPS,RUCC_2023\n01001,2\n",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for source_id, (valid, wrong_dialect) in fixtures.items():
                with self.subTest(source_id=source_id):
                    validator = resolve_release_validators(self._spec_for(source_id))[0]
                    path = root / SOURCE_POLICIES[source_id].filename
                    path.write_bytes(valid)
                    validator(path)
                    path.write_bytes(wrong_dialect)
                    with self.assertRaises(IntegrityError):
                        validator(path)

    def test_stable_places_metadata_and_hrsa_trailing_header_contracts(self):
        metadata = {
            "id": "swc5-untb",
            "name": "PLACES: Local Data for Better Health, County Data, 2025 release",
            "attribution": (
                "Centers for Disease Control and Prevention, National Center for "
                "Chronic Disease Prevention and Health Promotion, Division of Population Health"
            ),
            "provenance": "OFFICIAL",
            "license": "Public Domain",
            "domain": "data.cdc.gov",
            "dataUpdatedAt": "2025-12-04T10:35:06+0000",
            "metadataUpdatedAt": "2025-12-05T18:12:42+0000",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            places_path = root / "places.json"
            places_path.write_text(json.dumps(metadata), encoding="utf-8")
            places_validators = resolve_release_validators(
                self._spec_for("PLACES-META-2025-20251205-STABLE")
            )
            for validator in places_validators:
                validator(places_path)
            places_path.write_text(
                json.dumps({**metadata, "provenance": "COMMUNITY"}), encoding="utf-8"
            )
            with self.assertRaises(IntegrityError):
                for validator in places_validators:
                    validator(places_path)

            hrsa_path = root / "hrsa.csv"
            output = io.StringIO(newline="")
            nonblank_header = (
                "Common State County FIPS Code",
                "HPSA Status",
                *(f"Field {index:02d}" for index in range(1, 64)),
            )
            writer = csv.writer(output, lineterminator="\r\n")
            writer.writerow((*nonblank_header, ""))
            writer.writerow(("01001", "Designated", *("x" for _ in range(63))))
            hrsa_path.write_text(output.getvalue(), encoding="utf-8", newline="")
            hrsa_validator = resolve_release_validators(
                self._spec_for("HRSA-HPSA-WAREHOUSE-20260710")
            )[0]
            hrsa_validator(hrsa_path)
            hrsa_path.write_text(
                output.getvalue().replace(",\r\n", ",x\r\n", 1),
                encoding="utf-8",
                newline="",
            )
            with self.assertRaises(IntegrityError):
                hrsa_validator(hrsa_path)

    def test_phase2b_manifest_requires_full_internal_registry_and_rejects_noop(self):
        from atlas_pipeline.materialization import PHASE2B_SOURCE_IDS

        rows = []
        for source_id in sorted(PHASE2B_SOURCE_IDS):
            policy = SOURCE_POLICIES[source_id]
            rows.append(
                {
                    "source_id": source_id,
                    "release": policy.release,
                    "url": policy.url,
                    "filename": policy.filename,
                    "bytes": policy.bytes,
                    "sha256": policy.sha256,
                    "allowed_hosts": [urlparse(policy.url).hostname],
                    "artifact_type": policy.artifact_type,
                    "validator_ids": list(policy.validator_ids),
                    "content_types": list(policy.content_types),
                    "magic_hex": policy.magic_hex,
                }
            )
        manifest = {
            "schema_version": MATERIALIZATION_SCHEMA_VERSION,
            "phase": "2B",
            "source_tree_id": SOURCE_TREE_ID,
            "expectations": {},
            "sources": rows,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_bytes(canonical_json_bytes(manifest))
            loaded = MaterializationManifest.load(path, required_phase="2B")
            self.assertEqual(set(loaded.sources), PHASE2B_SOURCE_IDS)
            drifted = json.loads(path.read_text())
            drifted["sources"][0]["validator_ids"] = ["noop"]
            path.write_bytes(canonical_json_bytes(drifted))
            with self.assertRaises(IntegrityError):
                MaterializationManifest.load(path, required_phase="2B")
            drifted["sources"] = drifted["sources"][:-1]
            path.write_bytes(canonical_json_bytes(drifted))
            with self.assertRaises(IntegrityError):
                MaterializationManifest.load(path, required_phase="2B")

    def test_known_hash_local_import_uses_immutable_validator_and_no_callback_surface(self):
        body = b"id,value\n1,ok\n"
        spec = FrozenInput(
            "SYNTHETIC-IMMUTABLE-CSV",
            "fixture-v1",
            "https://example.test/synthetic.csv",
            "synthetic.csv",
            len(body),
            hashlib.sha256(body).hexdigest(),
            ("example.test",),
            "CSV",
            ("synthetic-immutable-csv",),
            ("text/csv",),
        )
        manifest = synthetic_materialization_manifest("2B", {spec.source_id: spec})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            source.write_bytes(body)
            result = import_known_hash(manifest, spec.source_id, source, root / "store")
            self.assertEqual(result.final_path.read_bytes(), body)
            validate_stream_acquisition_receipt(result.receipt_path.read_bytes(), spec)
            cached = import_known_hash(
                manifest, spec.source_id, source, root / "store"
            )
            validate_stream_acquisition_receipt(cached.receipt_path.read_bytes(), spec)
            cache_receipt = json.loads(cached.receipt_path.read_text())
            self.assertEqual(cache_receipt["receipt_kind"], "cache_validation")
            cache_receipt["request_method"] = "LOCAL_IMPORT"
            with self.assertRaises(IntegrityError):
                validate_stream_acquisition_receipt(
                    canonical_json_bytes(cache_receipt), spec
                )
            bad = root / "bad.csv"
            bad.write_bytes(b"id,value\n1,ok\n1,duplicate\n")
            bad_spec = FrozenInput(
                spec.source_id, spec.release, spec.url, spec.filename,
                bad.stat().st_size, hashlib.sha256(bad.read_bytes()).hexdigest(),
                spec.allowed_hosts, spec.artifact_type, spec.validator_ids,
                spec.content_types,
            )
            bad_manifest = synthetic_materialization_manifest(
                "2B", {bad_spec.source_id: bad_spec}
            )
            with self.assertRaises(IntegrityError):
                import_known_hash(
                    bad_manifest, bad_spec.source_id, bad, root / "bad-store"
                )
            with self.assertRaises(TypeError):
                resolve_release_validators(spec, callback=lambda _path: None)

    def test_stream_receipt_validator_binds_exact_request_response_and_final_bytes(self):
        body = b"id,value\n1,ok\n"
        spec = FrozenInput(
            "SYNTHETIC-IMMUTABLE-CSV", "fixture-v1",
            "https://example.test/synthetic.csv", "synthetic.csv",
            len(body), hashlib.sha256(body).hexdigest(), ("example.test",),
            "CSV", ("synthetic-immutable-csv",), ("text/csv",),
        )
        manifest = synthetic_materialization_manifest("2B", {spec.source_id: spec})
        transport = FakeTransport(
            body,
            headers={
                "Content-Type": "text/csv",
                "Content-Encoding": "identity",
                "Content-Length": str(len(body)),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = acquire_known_hash(
                manifest, spec.source_id, Path(tmp), transport=transport
            )
            receipt = result.receipt_path.read_bytes()
            validate_stream_acquisition_receipt(receipt, spec)
            drifted = json.loads(receipt)
            drifted["final_sha256"] = hashlib.sha256(
                b"drifted-materialized-content"
            ).hexdigest()
            with self.assertRaises(IntegrityError):
                validate_stream_acquisition_receipt(canonical_json_bytes(drifted), spec)
            with self.assertRaises(IntegrityError):
                validate_stream_acquisition_receipt(
                    receipt.replace(b'"response_bytes":14', b'"response_bytes":NaN'),
                    spec,
                )

    def test_known_hash_206_and_416_receipts_bind_segment_and_assembled_artifact(self):
        body = b"id,value\n1,synthetic\n"
        spec = FrozenInput(
            "SYNTHETIC-IMMUTABLE-CSV", "fixture-v1",
            "https://example.test/synthetic.csv", "synthetic.csv",
            len(body), hashlib.sha256(body).hexdigest(), ("example.test",),
            "CSV", ("synthetic-immutable-csv",), ("text/csv",),
        )
        manifest = synthetic_materialization_manifest("2B", {spec.source_id: spec})
        split = 7
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            part = spec.content_path(root).with_name(spec.filename + ".part")
            part.parent.mkdir(parents=True)
            part.write_bytes(body[:split])
            transport = FakeTransport(
                body[split:],
                status=206,
                headers={
                    "Content-Type": "text/csv",
                    "Content-Encoding": "identity",
                    "Content-Length": str(len(body) - split),
                    "Content-Range": f"bytes {split}-{len(body) - 1}/{len(body)}",
                },
            )
            result = acquire_known_hash(
                manifest, spec.source_id, root, transport=transport
            )
            receipt = result.receipt_path.read_bytes()
            parsed = validate_stream_acquisition_receipt(
                receipt, spec, final_path=result.final_path
            )
            self.assertEqual(parsed["http_status"], 206)
            self.assertEqual(parsed["range_request"], f"bytes={split}-")
            self.assertEqual(parsed["segment_bytes"], len(body) - split)
            self.assertEqual(parsed["final_sha256"], spec.sha256)
            tampered = json.loads(receipt)
            drifted_hash = hashlib.sha256(b"different response segment").hexdigest()
            tampered["segment_sha256"] = drifted_hash
            tampered["response_sha256"] = drifted_hash
            with self.assertRaisesRegex(IntegrityError, "assembled artifact range"):
                validate_stream_acquisition_receipt(
                    canonical_json_bytes(tampered),
                    spec,
                    final_path=result.final_path,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            part = spec.content_path(root).with_name(spec.filename + ".part")
            part.parent.mkdir(parents=True)
            part.write_bytes(body)
            transport = FakeTransport(
                b"",
                status=416,
                headers={
                    "Content-Type": "text/csv",
                    "Content-Encoding": "identity",
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{len(body)}",
                },
            )
            result = acquire_known_hash(
                manifest, spec.source_id, root, transport=transport
            )
            receipt = result.receipt_path.read_bytes()
            parsed = validate_stream_acquisition_receipt(
                receipt, spec, final_path=result.final_path
            )
            self.assertEqual(parsed["http_status"], 416)
            self.assertEqual(parsed["segment_bytes"], 0)
            self.assertEqual(
                parsed["segment_sha256"], hashlib.sha256(b"").hexdigest()
            )
            with self.assertRaisesRegex(IntegrityError, "assembled artifact path"):
                validate_stream_acquisition_receipt(receipt, spec)

    def test_authenticated_hud_receipt_binds_only_a_redacted_credential_digest(self):
        body = b"synthetic approved HUD XLSX bytes"
        policy = SOURCE_POLICIES["HUD-ZIP-COUNTY-2025Q4"]
        spec = FrozenInput(
            policy.source_id,
            policy.release,
            policy.url,
            policy.filename,
            len(body),
            hashlib.sha256(body).hexdigest(),
            (urlparse(policy.url).hostname,),
            policy.artifact_type,
            policy.validator_ids,
            policy.content_types,
            policy.magic_hex,
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
                [
                    "Authorization-Digest",
                    "sha256:" + hashlib.sha256(b"Bearer synthetic-secret").hexdigest(),
                ],
            ],
            "range_request": None,
            "started_at_utc": "2026-07-10T00:00:00Z",
            "ended_at_utc": "2026-07-10T00:00:01Z",
            "redirect_chain": [],
            "final_url": spec.url,
            "http_status": 200,
            "content_type": policy.content_types[0],
            "content_encoding": "identity",
            "content_range": None,
            "segment_start": 0,
            "segment_end": len(body) - 1,
            "representation_total_bytes": len(body),
            "segment_bytes": len(body),
            "segment_sha256": spec.sha256,
            "response_bytes": len(body),
            "response_sha256": spec.sha256,
            "result_state": "complete",
            "rejection_reason": None,
            "final_bytes": len(body),
            "final_sha256": spec.sha256,
        }
        validate_stream_acquisition_receipt(canonical_json_bytes(receipt), spec)
        receipt["request_headers"][1] = [
            "Authorization", "Bearer synthetic-secret"
        ]
        with self.assertRaisesRegex(IntegrityError, "redacted authorization digest"):
            validate_stream_acquisition_receipt(canonical_json_bytes(receipt), spec)


class NppesTransportAndContainerTests(unittest.TestCase):
    def candidate_spec(self, body: bytes) -> PendingDigestInput:
        return PendingDigestInput(
            "SYNTHETIC-NPPES-TRANSPORT",
            "fixture-v1",
            "https://example.test/nppes.zip",
            "nppes.zip",
            len(body),
            ("example.test",),
        )

    def test_public_phase2c_plan_freezes_exact_june_2026_v2_identity(self):
        policy = SOURCE_POLICIES["NPPES-202606-V2"]
        plan = {
            "schema_version": NPPES_TRANSPORT_SCHEMA_VERSION,
            "phase": "2C",
            "source_tree_id": SOURCE_TREE_ID,
            "source": {
                "source_id": policy.source_id,
                "release": policy.release,
                "url": policy.url,
                "filename": policy.filename,
                "bytes": policy.bytes,
                "allowed_hosts": ["download.cms.gov"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nppes-plan.json"
            path.write_bytes(canonical_json_bytes(plan))
            loaded = NppesTransportPlan.load(path)
            self.assertEqual(loaded.spec.bytes, 1_137_408_535)
            drifted = json.loads(path.read_text())
            drifted["source"]["filename"] = "weekly.zip"
            path.write_bytes(canonical_json_bytes(drifted))
            with self.assertRaises(IntegrityError):
                NppesTransportPlan.load(path)

    def test_phase2c_streams_one_identity_200_atomically_without_zip_open(self):
        body = b"PK\x03\x04" + bytes(range(128))
        transport = FakeTransport(body)
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            zipfile, "ZipFile", side_effect=AssertionError("container opened in Phase 2C")
        ):
            result = _acquire_pending_candidate(
                self.candidate_spec(body),
                Path(tmp),
                transport=transport,
                source_tree_id=SOURCE_TREE_ID,
                plan_sha256=PLAN_SHA256,
                authorization_id=AUTHORIZATION_ID,
                storage_preflight_receipt_sha256=STORAGE_PREFLIGHT_SHA256,
                chunk_size=17,
            )
            self.assertEqual(result.candidate_path.read_bytes(), body)
            self.assertEqual(result.candidate_sha256, hashlib.sha256(body).hexdigest())
            self.assertEqual(transport.calls, 1)
            self.assertTrue(all(size == 17 for size in transport.tracker.read_sizes))
            final = json.loads(result.final_receipt_path.read_text())
            self.assertFalse(final["evidence"]["container_opened"])
            self.assertEqual(final["evidence"]["completed_transfer_count"], 1)
            self.assertEqual(
                final["evidence"]["storage_preflight_receipt_sha256"],
                STORAGE_PREFLIGHT_SHA256,
            )

    def test_phase2c_low_current_space_rejects_before_transport_construction(self):
        minimum = 25 * 1024**3
        policy = SOURCE_POLICIES["NPPES-202606-V2"]
        plan_value = {
            "schema_version": NPPES_TRANSPORT_SCHEMA_VERSION,
            "phase": "2C",
            "source_tree_id": SOURCE_TREE_ID,
            "source": {
                "source_id": policy.source_id,
                "release": policy.release,
                "url": policy.url,
                "filename": policy.filename,
                "bytes": policy.bytes,
                "allowed_hosts": ["download.cms.gov"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_parent = root / "output"
            output_parent.mkdir()
            plan_path = root / "phase2c-plan.json"
            plan_path.write_bytes(canonical_json_bytes(plan_value))
            plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            control_id = "SYNTHETIC-PHASE2C-STORAGE-CONTROL"
            st_dev = os.stat(output_parent).st_dev
            filesystem_identity = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "work_root": str(root.resolve()),
                        "output_parent": str(output_parent.resolve()),
                        "device": "synthetic-volume",
                        "st_dev": st_dev,
                    }
                )
            ).hexdigest()
            storage_value = {
                "schema_version": "atlas-v19-storage-preflight-receipt-1",
                "receipt_id": "storage_preflight",
                "status": "approved",
                "source_tree_id": SOURCE_TREE_ID,
                "bundle_manifest_sha256": plan_sha256,
                "authorization_id": AUTHORIZATION_ID,
                "command": "approve-storage-preflight",
                "completed_at_utc": "2026-07-10T00:00:01Z",
                "evidence": {
                    "work_root": str(root.resolve()),
                    "output_parent": str(output_parent.resolve()),
                    "device": "synthetic-volume",
                    "st_dev": st_dev,
                    "checked_at_utc": "2026-07-10T00:00:00Z",
                    "free_bytes": minimum + 1,
                    "worst_case_bytes": minimum,
                    "minimum_required_bytes": minimum,
                    "filesystem_identity_sha256": filesystem_identity,
                    "integrator_control_id": control_id,
                    "approved_by": "Independent Synthetic Integrator",
                    "approved": True,
                },
            }
            storage_path = root / "storage.json"
            storage_path.write_bytes(canonical_json_bytes(storage_value))
            runtime = SimpleNamespace(
                source_tree_id=SOURCE_TREE_ID,
                approved_bundle_sha256=plan_sha256,
                authorization_id=AUTHORIZATION_ID,
                control_id=control_id,
                approved_storage_preflight_receipt_sha256=hashlib.sha256(
                    storage_path.read_bytes()
                ).hexdigest(),
            )
            transport_calls = 0

            def transport_factory():
                nonlocal transport_calls
                transport_calls += 1
                raise AssertionError("transport constructed before storage gate")

            argv = [
                "--plan", str(plan_path),
                "--output", str(output_parent / "nppes"),
                "--execute-live",
                "--repo-root", str(root),
                "--source-tree-manifest", str(root / "source-tree.json"),
                "--integrator-control", str(root / "control.json"),
                "--approved-control-sha256", STORAGE_PREFLIGHT_SHA256,
                "--authorization-receipt", str(root / "authorization.json"),
                "--control-receipts-manifest", str(root / "receipts.json"),
                "--storage-preflight-receipt", str(storage_path),
            ]
            with patch(
                "atlas_pipeline.pipeline_cli._verify_authority",
                return_value=runtime,
            ), self.assertRaisesRegex(IntegrityError, "free-space"):
                acquire_nppes_main(
                    argv,
                    transport_factory=transport_factory,
                    disk_usage=lambda _path: SimpleNamespace(free=minimum - 1),
                    stat=os.stat,
                )
            self.assertEqual(transport_calls, 0)

    def test_phase2c_rejects_resume_non200_encoding_length_and_truncation(self):
        body = b"PK\x03\x04synthetic"
        cases = (
            FakeTransport(body, status=206),
            FakeTransport(body, headers={
                "Content-Type": "application/zip",
                "Content-Encoding": "gzip",
                "Content-Length": str(len(body)),
            }),
            FakeTransport(body, headers={
                "Content-Type": "application/zip",
                "Content-Encoding": "identity",
                "Content-Length": str(len(body) + 1),
            }),
            FakeTransport(body[:-1], headers={
                "Content-Type": "application/zip",
                "Content-Encoding": "identity",
                "Content-Length": str(len(body)),
            }),
        )
        for transport in cases:
            with self.subTest(status=transport.status, headers=transport.headers), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises((AcquisitionError, IntegrityError)):
                    _acquire_pending_candidate(
                        self.candidate_spec(body),
                        Path(tmp),
                        transport=transport,
                        source_tree_id=SOURCE_TREE_ID,
                        plan_sha256=PLAN_SHA256,
                        authorization_id=AUTHORIZATION_ID,
                        storage_preflight_receipt_sha256=STORAGE_PREFLIGHT_SHA256,
                        chunk_size=4,
                    )
                self.assertFalse(any(Path(tmp).rglob("*.candidate")))
                self.assertFalse(any(Path(tmp).rglob("*.part")))

        wrong_release_spec = PendingDigestInput(
            "NPPES-202606-V2",
            "2026-06",
            SOURCE_POLICIES["NPPES-202606-V2"].url,
            SOURCE_POLICIES["NPPES-202606-V2"].filename,
            10,
            ("download.cms.gov",),
        )
        transport = FakeTransport(b"PK" + b"x" * 8)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(IntegrityError):
            acquire_nppes_candidate(
                wrong_release_spec,
                Path(tmp),
                transport=transport,
                source_tree_id=SOURCE_TREE_ID,
                plan_sha256=PLAN_SHA256,
                authorization_id=AUTHORIZATION_ID,
                storage_preflight_receipt_sha256=STORAGE_PREFLIGHT_SHA256,
            )
        self.assertEqual(transport.calls, 0)

    def test_phase2di_central_manifest_precedes_approval_and_crc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "nppes.zip"
            body = write_zip(
                archive,
                {
                    "npidata_pfile_fixture.csv": b"NPI,Entity Type Code\n123,1\n",
                    "other.txt": b"synthetic",
                },
            )
            spec = FrozenInput(
                "NPPES-202606-V2", "2026-06",
                SOURCE_POLICIES["NPPES-202606-V2"].url,
                SOURCE_POLICIES["NPPES-202606-V2"].filename,
                len(body), hashlib.sha256(body).hexdigest(),
                ("download.cms.gov",), "ZIP", ("nppes-zip-central",),
                ("application/zip", "application/octet-stream"), "504b",
            )
            manifest = synthetic_materialization_manifest(
                "2D-I", {spec.source_id: spec}
            )
            with patch.object(
                zipfile.ZipFile, "open", side_effect=AssertionError("payload opened preapproval")
            ):
                candidate_path, receipt_path = inspect_nppes_container(
                    manifest,
                    archive,
                    root / "candidate-output",
                    authorization_id=AUTHORIZATION_ID,
                )
            candidate = _load_zip_candidate(candidate_path)
            selected_identity = next(
                member
                for member in candidate.members
                if member.filename == "npidata_pfile_fixture.csv"
            )
            self.assertFalse(json.loads(receipt_path.read_text())["evidence"]["payload_opened"])
            wrong = ZipManifestApproval(
                candidate.source_id,
                candidate.archive_sha256,
                hashlib.sha256(b"wrong-nppes-candidate-manifest").hexdigest(),
                "WRONG-APPROVAL",
                "reviewer",
                "2026-07-10T12:30:00Z",
                selected_identity.filename,
                zip_member_central_metadata_sha256(selected_identity),
            )
            wrong_path = root / "wrong-approval.json"
            wrong_body = canonical_json_bytes(asdict(wrong))
            wrong_path.write_bytes(wrong_body)
            wrong_trust_path = root / "wrong-trust-root.json"
            _wrong_trust_body, wrong_trust_pin = zip_approval_trust_root(
                candidate,
                wrong_body,
                wrong_trust_path,
                source_tree_id=manifest.source_tree_id,
                bundle_manifest_sha256=manifest.sha256,
            )
            with patch.object(
                zipfile.ZipFile, "open", side_effect=AssertionError("bad approval bypassed")
            ), self.assertRaises(IntegrityError):
                approve_nppes_container(
                    manifest, archive, candidate_path, wrong_path,
                    wrong_trust_path, root / "bad-crc",
                    independently_approved_trust_root_sha256=wrong_trust_pin,
                    authorization_id=AUTHORIZATION_ID,
                    selected_member_filename="npidata_pfile_fixture.csv",
                )
            approval_path = root / "approval.json"
            trust_root_path = root / "approval-trust-root.json"
            _trusted, trust_pin = trusted_zip_approval(
                candidate,
                approval_path,
                trust_root_path,
                source_tree_id=manifest.source_tree_id,
                bundle_manifest_sha256=manifest.sha256,
            )
            wrong_metadata_approval = ZipManifestApproval(
                candidate.source_id,
                candidate.archive_sha256,
                hashlib.sha256(
                    canonical_zip_candidate_manifest_bytes(candidate)
                ).hexdigest(),
                "WRONG-CENTRAL-METADATA-APPROVAL",
                "Independent Synthetic Integrator",
                "2026-07-10T12:30:00Z",
                selected_identity.filename,
                hashlib.sha256(b"changed selected central metadata").hexdigest(),
            )
            wrong_metadata_path = root / "wrong-metadata-approval.json"
            wrong_metadata_body = canonical_zip_manifest_approval_bytes(
                wrong_metadata_approval
            )
            wrong_metadata_path.write_bytes(wrong_metadata_body)
            wrong_metadata_trust_path = root / "wrong-metadata-trust.json"
            _, wrong_metadata_pin = zip_approval_trust_root(
                candidate,
                wrong_metadata_body,
                wrong_metadata_trust_path,
                source_tree_id=manifest.source_tree_id,
                bundle_manifest_sha256=manifest.sha256,
            )
            with patch.object(
                zipfile.ZipFile,
                "open",
                side_effect=AssertionError("changed central metadata opened payload"),
            ), self.assertRaisesRegex(IntegrityError, "central metadata"):
                approve_nppes_container(
                    manifest,
                    archive,
                    candidate_path,
                    wrong_metadata_path,
                    wrong_metadata_trust_path,
                    root / "wrong-metadata-rejected",
                    independently_approved_trust_root_sha256=wrong_metadata_pin,
                    authorization_id=AUTHORIZATION_ID,
                    selected_member_filename=selected_identity.filename,
                )
            with patch.object(
                zipfile.ZipFile,
                "open",
                side_effect=AssertionError("self-authored approval opened payload"),
            ), self.assertRaisesRegex(IntegrityError, "independently supplied pin"):
                approve_nppes_container(
                    manifest,
                    archive,
                    candidate_path,
                    approval_path,
                    trust_root_path,
                    root / "self-authored-rejected",
                    independently_approved_trust_root_sha256=hashlib.sha256(
                        b"separately frozen integrator control"
                    ).hexdigest(),
                    authorization_id=AUTHORIZATION_ID,
                    selected_member_filename="npidata_pfile_fixture.csv",
                )
            with patch.object(
                zipfile.ZipFile,
                "open",
                side_effect=AssertionError("unsafe selection opened payload"),
            ), self.assertRaisesRegex(IntegrityError, "root ZIP filename"):
                approve_nppes_container(
                    manifest,
                    archive,
                    candidate_path,
                    approval_path,
                    trust_root_path,
                    root / "unsafe-selection-rejected",
                    independently_approved_trust_root_sha256=trust_pin,
                    authorization_id=AUTHORIZATION_ID,
                    selected_member_filename="nested/npidata_pfile_fixture.csv",
                )
            with patch.object(
                zipfile.ZipFile,
                "open",
                side_effect=AssertionError("unapproved root member opened payload"),
            ), self.assertRaisesRegex(
                IntegrityError, "externally trusted approval"
            ):
                approve_nppes_container(
                    manifest,
                    archive,
                    candidate_path,
                    approval_path,
                    trust_root_path,
                    root / "other-root-selection-rejected",
                    independently_approved_trust_root_sha256=trust_pin,
                    authorization_id=AUTHORIZATION_ID,
                    selected_member_filename="other.txt",
                )
            verified_receipt = approve_nppes_container(
                manifest,
                archive,
                candidate_path,
                approval_path,
                trust_root_path,
                root / "verified",
                independently_approved_trust_root_sha256=trust_pin,
                authorization_id=AUTHORIZATION_ID,
                selected_member_filename="npidata_pfile_fixture.csv",
            )
            value = json.loads(verified_receipt.read_text())
            self.assertTrue(value["evidence"]["crc_verified"])
            self.assertFalse(value["evidence"]["analytical_rows_parsed"])
            self.assertEqual(
                value["evidence"]["approval_trust_root_sha256"], trust_pin
            )
            self.assertEqual(
                value["evidence"]["selected_member_filename"],
                "npidata_pfile_fixture.csv",
            )
            self.assertEqual(len(value["evidence"]["selected_member_sha256"]), 64)
            self.assertEqual(
                value["evidence"]["selected_member_bytes"],
                selected_identity.uncompressed_bytes,
            )
            self.assertEqual(
                value["evidence"]["selected_member_central_metadata_sha256"],
                zip_member_central_metadata_sha256(selected_identity),
            )


def places_row(locationid: str, *, value: str = "5.0", suppressed: bool = False):
    row = {field: "" for field in PLACES_REQUIRED_FIELDS}
    row.update(
        {
            "locationid": locationid,
            "stateabbr": "US" if locationid == "59" else "AL",
            "locationname": "United States" if locationid == "59" else f"Area {locationid}",
            "year": "2023",
            "datasource": "BRFSS",
            "category": "Health Outcomes",
            "measure": "Coronary heart disease among adults",
            "measureid": "CHD",
            "data_value_unit": "%",
            "data_value_type": "Age-adjusted prevalence",
            "datavaluetypeid": "AgeAdjPrv",
            "data_value": "" if suppressed else value,
            "low_confidence_limit": "" if suppressed else "4.0",
            "high_confidence_limit": "" if suppressed else "6.0",
            "data_value_footnote": "Population less than 50" if suppressed else "",
        }
    )
    return row


class PlacesDerivationEntrypointTests(unittest.TestCase):
    def test_exact_national_exclusion_2957_2956_suppression_and_receipt(self):
        universe = [f"{value:05d}" for value in range(1, 3_145) if value != 59]
        universe.append("48301")
        universe.sort()
        county_ids = [fips for fips in universe if fips != "48301"][:2_956]
        county_ids.append("48301")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=PLACES_REQUIRED_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(places_row("59", value="5.3"))
        for fips in county_ids:
            writer.writerow(places_row(fips, suppressed=fips == "48301"))
        raw = output.getvalue().encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_bundle(
                root / "bundle",
                phase="2D-II",
                expectations={
                    "places_raw_source": "PLACES-RAW",
                    "places_source_acquisition_receipt": "PLACES-ACQUISITION",
                    "county_universe": universe,
                },
                files=[
                    ("PLACES-RAW", "places_raw_csv", "places.csv", raw, "text/csv"),
                    (
                        "PLACES-ACQUISITION", "build_receipt_json", "places-acquisition.json",
                        canonical_json_bytes({"schema_version": "synthetic-source-acquisition-1", "state": "complete"}),
                        "application/json",
                    ),
                ],
            )
            bundle = FrozenBundle.load(root / "bundle", required_phase="2D-II")
            receipt = derive_places_county(
                bundle,
                root / "derived",
                completed_at_utc="2026-07-10T13:00:00Z",
            )
            evidence = json.loads(receipt.read_text())["evidence"]
            self.assertEqual(
                (evidence["raw_county_rows"], evidence["retained_rows"], evidence["suppressed_rows"], evidence["national_rows_excluded"]),
                (2_957, 2_956, 1, 1),
            )
            snapshot = (root / "derived" / "places_chd_county_2025.jsonl").read_bytes()
            self.assertNotIn(b'"fips":"00059"', snapshot)
            self.assertEqual(snapshot.count(b"\n"), 2_957)
            drifted = raw.replace(b"\n59,US,", b"\n58,US,", 1)
            write_bundle(
                root / "bad-bundle",
                phase="2D-II",
                expectations={
                    "places_raw_source": "PLACES-RAW",
                    "places_source_acquisition_receipt": "PLACES-ACQUISITION",
                    "county_universe": universe,
                },
                files=[
                    ("PLACES-RAW", "places_raw_csv", "places.csv", drifted, "text/csv"),
                    (
                        "PLACES-ACQUISITION", "build_receipt_json", "places-acquisition.json",
                        canonical_json_bytes({"schema_version": "synthetic-source-acquisition-1", "state": "complete"}),
                        "application/json",
                    ),
                ],
            )
            with self.assertRaises(ContractError):
                derive_places_county(
                    FrozenBundle.load(root / "bad-bundle", required_phase="2D-II"),
                    root / "bad-derived",
                )


def county_universe() -> list[str]:
    result = set(CURRENT_CT_PLANNING_REGIONS)
    state_prefixes = sorted(
        {
            policy
            for policy in __import__("atlas_pipeline.contracts", fromlist=["STATE_FIPS"]).STATE_FIPS.values()
            if policy != "09"
        }
    )
    for state in state_prefixes:
        for suffix in range(1, 1_000):
            result.add(f"{state}{suffix:03d}")
            if len(result) == 3_144:
                return sorted(result)
    raise AssertionError("could not construct synthetic county universe")


def write_hud_workbook(path: Path, rows: list[tuple[object, ...]], *, headers=HUD_HEADERS) -> None:
    all_rows = [tuple(headers), *rows]

    def column(index: int) -> str:
        return chr(ord("A") + index)

    xml_rows = []
    for row_number, values in enumerate(all_rows, start=1):
        cells = []
        for index, value in enumerate(values):
            reference = f"{column(index)}{row_number}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            else:
                cells.append(
                    f'<c r="{reference}" t="inlineStr"><is><t>{html.escape(str(value))}</t></is></c>'
                )
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    members = {
        "[Content_Types].xml": (
            b'<?xml version="1.0"?><Types>'
            b'<Override PartName="/xl/workbook.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            b'<Override PartName="/xl/worksheets/sheet1.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            b'</Types>'
        ),
        "_rels/.rels": (
            b'<?xml version="1.0"?><Relationships>'
            b'<Relationship Id="rIdOffice" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            b'Target="xl/workbook.xml"/>'
            b'</Relationships>'
        ),
        "xl/workbook.xml": (
            b'<?xml version="1.0"?><workbook '
            b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            b'<sheets><sheet name="ZIP_COUNTY_122025" sheetId="1" r:id="rId1"/></sheets>'
            b'</workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            b'<?xml version="1.0"?><Relationships>'
            b'<Relationship Id="rId1" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            b'Target="worksheets/sheet1.xml"/>'
            b'</Relationships>'
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0"?><worksheet><sheetData>'
            + "".join(xml_rows)
            + "</sheetData></worksheet>"
        ).encode("utf-8"),
    }
    write_zip(path, members)


class HudXlsxAndHandoffTests(unittest.TestCase):
    def context_for(self, path: Path):
        body = path.read_bytes()
        spec = FrozenInput(
            "HUD-ZIP-COUNTY-2025Q4",
            "2025-Q4",
            SOURCE_POLICIES["HUD-ZIP-COUNTY-2025Q4"].url,
            "ZIP_COUNTY_122025.xlsx",
            len(body),
            hashlib.sha256(body).hexdigest(),
            ("www.huduser.gov",),
            "XLSX",
            ("hud-2025q4-xlsx",),
            SOURCE_POLICIES["HUD-ZIP-COUNTY-2025Q4"].content_types,
            "504b",
        )
        candidate = build_zip_candidate_manifest(path, spec)
        approval_path = path.with_name(path.stem + "-approval.json")
        trust_path = path.with_name(path.stem + "-approval-trust-root.json")
        approval, _trust_pin = trusted_zip_approval(
            candidate, approval_path, trust_path
        )
        return spec, ReleaseValidationContext(
            candidate, approval, ("ZIP_COUNTY_122025",)
        )

    def test_hud_xlsx_self_authored_approval_fails_before_ooxml_payload_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "ZIP_COUNTY_122025.xlsx"
            write_hud_workbook(
                workbook, [(1001, 1001, 0.75, 0.70, 0.72, "AL")]
            )
            _spec, context = self.context_for(workbook)
            trusted = context.manifest_approval
            assert trusted is not None
            candidate = context.candidate_manifest
            assert candidate is not None
            approval_body = canonical_zip_manifest_approval_bytes(
                trusted.approval
            )
            trust_body = canonical_json_bytes(asdict(trusted.trust_root))
            with patch.object(
                zipfile.ZipFile,
                "open",
                side_effect=AssertionError("HUD OOXML opened before external approval"),
            ), self.assertRaisesRegex(IntegrityError, "independently supplied pin"):
                authorize_zip_approval(
                    candidate,
                    approval_body,
                    trust_body,
                    independently_approved_trust_root_sha256=hashlib.sha256(
                        b"separately frozen HUD integrator control"
                    ).hexdigest(),
                    source_tree_id=trusted.trust_root.source_tree_id,
                    bundle_manifest_sha256=(
                        trusted.trust_root.bundle_manifest_sha256
                    ),
                    authorization_id=trusted.trust_root.authorization_id,
                )

    def test_hud_xlsx_full_schema_types_unique_key_and_2025_handoff(self):
        universe = county_universe()
        universe_body = canonical_json_bytes(universe)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "ZIP_COUNTY_122025.xlsx"
            write_hud_workbook(
                workbook,
                [
                    (1001, 1001, 0.75, 0.70, 0.72, "AL"),
                    *[
                        (6000 + index, county, 0.80, 0.75, 0.77, "CT")
                        for index, county in enumerate(
                            sorted(CURRENT_CT_PLANNING_REGIONS), start=1
                        )
                    ],
                ],
            )
            spec, context = self.context_for(workbook)
            expectations = {
                    "county_universe_sha256": hashlib.sha256(
                        sorted_fips_bytes(universe)
                    ).hexdigest(),
                    "county_count": 3_144,
                    "required_sheet_name": "ZIP_COUNTY_122025",
            }
            plan_value = {
                "schema_version": MATERIALIZATION_SCHEMA_VERSION,
                "phase": "2E",
                "source_tree_id": SOURCE_TREE_ID,
                "expectations": expectations,
                "sources": [
                    {
                        "source_id": spec.source_id,
                        "release": spec.release,
                        "url": spec.url,
                        "filename": spec.filename,
                        "bytes": spec.bytes,
                        "sha256": spec.sha256,
                        "allowed_hosts": list(spec.allowed_hosts),
                        "artifact_type": spec.artifact_type,
                        "validator_ids": list(spec.validator_ids),
                        "content_types": list(spec.content_types),
                        "magic_hex": spec.magic_hex,
                    }
                ],
            }
            plan_path = root / "hud-plan.json"
            plan_path.write_bytes(canonical_json_bytes(plan_value))
            manifest = MaterializationManifest.load(plan_path, required_phase="2E")
            drifted_plan = json.loads(plan_path.read_text())
            drifted_plan["sources"][0]["filename"] = "different.xlsx"
            plan_path.write_bytes(canonical_json_bytes(drifted_plan))
            with self.assertRaises(IntegrityError):
                MaterializationManifest.load(plan_path, required_phase="2E")
            universe_path = root / "county-universe.json"
            universe_path.write_bytes(universe_body)
            imported = import_known_hash(
                manifest,
                spec.source_id,
                workbook,
                root / "store",
                context=context,
            )
            receipt = build_hud_handoff(
                manifest,
                imported.final_path,
                universe_path,
                context,
                imported.receipt_path,
                root / "handoff",
                authorization_id=AUTHORIZATION_ID,
                completed_at_utc="2026-07-10T14:00:00Z",
            )
            self.assertEqual(json.loads(receipt.read_text())["evidence"]["release_year"], 2025)
            csv_body = (root / "handoff" / "hud_zip_county_2025q4.csv").read_text()
            self.assertIn("01001,01001,0.75,0.7,0.72,AL", csv_body)
            compatibility = json.loads((root / "handoff" / "hud_compatibility.json").read_text())
            self.assertTrue(compatibility["compatible"])
            self.assertEqual(compatibility["obsolete_connecticut_count"], 0)
            self.assertEqual(
                set(compatibility["connecticut_fips"]), CURRENT_CT_PLANNING_REGIONS
            )

            obsolete = root / "obsolete-ct.xlsx"
            write_hud_workbook(
                obsolete,
                [("06001", "09001", 0.75, 0.70, 0.72, "CT")],
            )
            obsolete_spec, obsolete_context = self.context_for(obsolete)
            obsolete_manifest = synthetic_materialization_manifest(
                "2E",
                {obsolete_spec.source_id: obsolete_spec},
                expectations=manifest.expectations,
            )
            obsolete_import = import_known_hash(
                obsolete_manifest,
                obsolete_spec.source_id,
                obsolete,
                root / "obsolete-store",
                context=obsolete_context,
            )
            with self.assertRaises(ContractError):
                build_hud_handoff(
                    obsolete_manifest,
                    obsolete_import.final_path,
                    universe_path,
                    obsolete_context,
                    obsolete_import.receipt_path,
                    root / "obsolete-handoff",
                    authorization_id=AUTHORIZATION_ID,
                )

    def test_hud_xlsx_rejects_wrong_headers_header_only_and_normalized_duplicates(self):
        cases = (
            ("wrong-header", [(1001, 1001, 0.5, 0.5, 0.5, "AL")], ("BAD",) + HUD_HEADERS[1:]),
            ("header-only", [], HUD_HEADERS),
            ("normalized-duplicate", [("01001", "01001", 0.5, 0.5, 0.5, "AL"), (1001, 1001, 0.4, 0.4, 0.4, "AL")], HUD_HEADERS),
        )
        for name, rows, headers in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ZIP_COUNTY_122025.xlsx"
                write_hud_workbook(path, rows, headers=headers)
                spec, context = self.context_for(path)
                with self.assertRaises(IntegrityError):
                    resolve_release_validators(spec, context=context)[0](path)


if __name__ == "__main__":
    unittest.main()
