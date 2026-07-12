from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from atlas_pipeline.acquisition import (
    ArtifactValidator,
    CacheIdentity,
    FrozenInput,
    HttpResponse,
    PendingDigestInput,
    TrustedZipApproval,
    ZIP_APPROVAL_TRUST_ROOT_SCHEMA_VERSION,
    ZipManifestApproval,
    ZipMemberIdentity,
    apply_http_response,
    apply_pending_digest_response,
    authorize_zip_approval,
    build_zip_candidate_manifest,
    canonical_manifest_bytes,
    canonical_receipt_bytes,
    canonical_request_headers,
    canonical_zip_candidate_manifest_bytes,
    canonical_zip_manifest_approval_bytes,
    csv_artifact_validator,
    execute_frozen_response,
    execute_pending_digest_response,
    json_artifact_validator,
    open_verified_zip_member,
    persist_receipt_atomic,
    validate_cache_identity,
    validate_zip_container,
    verify_zip_after_approval,
    xlsx_artifact_validator,
    zip_artifact_validator,
)
from atlas_pipeline.errors import AcquisitionError, IntegrityError
from atlas_pipeline.strict_json import canonical_json_bytes


ZIP_SOURCE_TREE_ID = hashlib.sha256(b"synthetic ZIP source tree").hexdigest()
ZIP_BUNDLE_ID = hashlib.sha256(b"synthetic ZIP bundle").hexdigest()
ZIP_AUTHORIZATION_ID = "SYNTHETIC-ZIP-AUTHORIZATION"


def trusted_zip_approval(candidate, approval: ZipManifestApproval | None = None):
    approval = approval or ZipManifestApproval(
        candidate.source_id,
        candidate.archive_sha256,
        hashlib.sha256(
            canonical_zip_candidate_manifest_bytes(candidate)
        ).hexdigest(),
        "SYNTHETIC-APPROVAL-1",
        "independent-integrator",
        "2026-07-10T12:30:00Z",
    )
    approval_body = canonical_zip_manifest_approval_bytes(approval)
    trust_body = canonical_json_bytes(
        {
            "schema_version": ZIP_APPROVAL_TRUST_ROOT_SCHEMA_VERSION,
            "trust_root_id": "SYNTHETIC-ZIP-TRUST-ROOT",
            "source_tree_id": ZIP_SOURCE_TREE_ID,
            "bundle_manifest_sha256": ZIP_BUNDLE_ID,
            "authorization_id": ZIP_AUTHORIZATION_ID,
            "source_id": candidate.source_id,
            "archive_sha256": candidate.archive_sha256,
            "candidate_manifest_sha256": hashlib.sha256(
                canonical_zip_candidate_manifest_bytes(candidate)
            ).hexdigest(),
            "member_approval_sha256": hashlib.sha256(approval_body).hexdigest(),
            "approved_by": "independent-integrator",
            "approved_at_utc": "2026-07-10T12:31:00Z",
            "selected_member_filename": None,
            "selected_member_central_metadata_sha256": None,
        }
    )
    return authorize_zip_approval(
        candidate,
        approval_body,
        trust_body,
        independently_approved_trust_root_sha256=hashlib.sha256(
            trust_body
        ).hexdigest(),
        source_tree_id=ZIP_SOURCE_TREE_ID,
        bundle_manifest_sha256=ZIP_BUNDLE_ID,
        authorization_id=ZIP_AUTHORIZATION_ID,
    )


def require_nonempty(path: Path) -> None:
    if path.stat().st_size == 0:
        raise IntegrityError("synthetic binary is empty")


class _AcquisitionHelpers:
    URL = "https://example.test/source.bin"
    START = "2026-07-10T12:00:00Z"
    END = "2026-07-10T12:00:01Z"

    def spec(self, body: bytes, **overrides) -> FrozenInput:
        values = {
            "source_id": "SYNTHETIC-1",
            "release": "fixture",
            "url": self.URL,
            "filename": "source.bin",
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "allowed_hosts": ("example.test",),
            "artifact_type": "BINARY",
            "validator_ids": ("binary-nonempty",),
            "content_types": ("application/octet-stream",),
        }
        values.update(overrides)
        return FrozenInput(**values)

    def response(
        self,
        status: int,
        body: bytes,
        *,
        range_start: int | None = None,
        requested_url: str | None = None,
        final_url: str | None = None,
        redirect_chain: tuple[str, ...] = (),
        method: str = "GET",
        request_headers: tuple[tuple[str, str], ...] | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        **headers: str,
    ) -> HttpResponse:
        merged = {
            "Content-Type": "application/octet-stream",
            "Content-Encoding": "identity",
        }
        merged.update(headers)
        actual_headers = request_headers or tuple(
            canonical_request_headers(range_start=range_start).items()
        )
        return HttpResponse(
            status,
            merged,
            body,
            requested_url or self.URL,
            final_url or self.URL,
            redirect_chain,
            method,
            actual_headers,
            started_at or self.START,
            ended_at or self.END,
        )

    @property
    def registry(self) -> dict[str, ArtifactValidator]:
        validator = ArtifactValidator(
            "binary-nonempty", ("BINARY",), require_nonempty
        )
        return {validator.validator_id: validator}


class AcquisitionStateMachineTests(_AcquisitionHelpers, unittest.TestCase):

    def test_new_200_promotes_only_after_size_hash_and_receipt_binding(self):
        body = b"complete synthetic bytes"
        with tempfile.TemporaryDirectory() as tmp:
            actual_request_headers = (
                ("Accept-Encoding", "identity"),
                ("X-Synthetic-Trace", "trace-1"),
            )
            result = apply_http_response(
                Path(tmp),
                self.spec(body),
                self.response(
                    200,
                    body,
                    request_headers=actual_request_headers,
                    redirect_chain=(self.URL,),
                ),
                range_requested=False,
            )
            self.assertEqual(result.state, "complete")
            self.assertEqual(result.final_path.read_bytes(), body)
            self.assertEqual(canonical_request_headers(), {"Accept-Encoding": "identity"})
            self.assertEqual(result.receipt.request_method, "GET")
            self.assertEqual(result.receipt.request_headers, actual_request_headers)
            self.assertEqual(result.receipt.redirect_chain, (self.URL,))
            self.assertEqual(result.receipt.started_at_utc, self.START)
            self.assertEqual(result.receipt.ended_at_utc, self.END)
            self.assertEqual(result.receipt.content_encoding, "identity")
            self.assertEqual(result.receipt.final_bytes, len(body))
            self.assertEqual(result.receipt.final_sha256, hashlib.sha256(body).hexdigest())
            self.assertEqual(
                (result.receipt.segment_start, result.receipt.segment_end),
                (0, len(body) - 1),
            )

    def test_completed_response_wrong_hash_and_longer_partial_are_deleted(self):
        body = b"right"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self.spec(body)
            with self.assertRaises(IntegrityError):
                apply_http_response(
                    root, spec, self.response(200, b"wrong"), range_requested=False
                )
            part = spec.content_path(root).with_name("source.bin.part")
            self.assertFalse(part.exists())
            with self.assertRaises(IntegrityError):
                apply_http_response(
                    root, spec, self.response(200, body + b"x"), range_requested=False
                )
            self.assertFalse(part.exists())

    def test_corrupt_final_wrong_size_or_right_size_wrong_hash_is_removed_then_recoverable(self):
        body = b"right"
        corruptions = (b"too-long", b"wrung")
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                spec = self.spec(body)
                final = spec.content_path(root)
                final.parent.mkdir(parents=True)
                final.write_bytes(corrupt)
                with self.assertRaises(IntegrityError):
                    apply_http_response(
                        root, spec, self.response(200, body), range_requested=False
                    )
                self.assertFalse(final.exists())
                recovered = apply_http_response(
                    root, spec, self.response(200, body), range_requested=False
                )
                self.assertEqual(recovered.final_path.read_bytes(), body)

    def test_final_and_partial_symlinks_are_rejected_without_following_target(self):
        body = b"right"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self.spec(body)
            final = spec.content_path(root)
            final.parent.mkdir(parents=True)
            target = root / "outside-target"
            target.write_bytes(body)
            final.symlink_to(target)
            with self.assertRaises(IntegrityError):
                apply_http_response(
                    root, spec, self.response(200, body), range_requested=False
                )
            self.assertFalse(final.exists())
            self.assertEqual(target.read_bytes(), body)

            part = final.with_name(final.name + ".part")
            part.symlink_to(target)
            with self.assertRaises(IntegrityError):
                apply_http_response(
                    root, spec, self.response(200, body), range_requested=False
                )
            self.assertFalse(part.exists())
            self.assertEqual(target.read_bytes(), body)

    def test_truncated_partial_and_ignored_range_200_restart(self):
        body = b"abcdefghij"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self.spec(body)
            first = apply_http_response(
                root, spec, self.response(200, body[:3]), range_requested=False
            )
            self.assertEqual((first.state, first.partial_bytes), ("partial", 3))
            self.assertEqual(first.receipt.local_bytes_after, 3)
            complete = apply_http_response(
                root,
                spec,
                self.response(200, body, range_start=3),
                range_requested=True,
            )
            self.assertEqual(complete.final_path.read_bytes(), body)

    def test_valid_and_invalid_206_bind_segment(self):
        body = b"abcdefghij"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self.spec(body)
            apply_http_response(
                root, spec, self.response(200, body[:4]), range_requested=False
            )
            result = apply_http_response(
                root,
                spec,
                self.response(
                    206,
                    body[4:],
                    range_start=4,
                    **{"Content-Range": "bytes 4-9/10"},
                ),
                range_requested=True,
            )
            self.assertEqual(result.state, "complete")
            self.assertEqual(
                (
                    result.receipt.segment_start,
                    result.receipt.segment_end,
                    result.receipt.representation_total_bytes,
                ),
                (4, 9, 10),
            )
        for content_range in (
            "bytes 3-9/10",
            "bytes 4-9/11",
            "bytes 4-10/10",
            "bad",
        ):
            with self.subTest(content_range=content_range), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                spec = self.spec(body)
                apply_http_response(
                    root, spec, self.response(200, body[:4]), range_requested=False
                )
                with self.assertRaises(AcquisitionError):
                    apply_http_response(
                        root,
                        spec,
                        self.response(
                            206,
                            body[4:],
                            range_start=4,
                            **{"Content-Range": content_range},
                        ),
                        range_requested=True,
                    )

    def test_valid_and_invalid_416(self):
        body = b"abcdefghij"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self.spec(body)
            part = spec.content_path(root).with_name("source.bin.part")
            part.parent.mkdir(parents=True)
            part.write_bytes(body)
            result = apply_http_response(
                root,
                spec,
                self.response(
                    416,
                    b"",
                    range_start=10,
                    **{"Content-Range": "bytes */10"},
                ),
                range_requested=True,
            )
            self.assertEqual(result.state, "complete")
            self.assertEqual(result.receipt.representation_total_bytes, 10)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self.spec(body)
            part = spec.content_path(root).with_name("source.bin.part")
            part.parent.mkdir(parents=True)
            part.write_bytes(b"bad")
            with self.assertRaises(AcquisitionError):
                apply_http_response(
                    root,
                    spec,
                    self.response(
                        416,
                        b"",
                        range_start=3,
                        **{"Content-Range": "bytes */10"},
                    ),
                    range_requested=True,
                )
            self.assertFalse(part.exists())

    def test_stale_identity_url_drift_and_exact_range_request(self):
        body = b"bytes"
        spec = self.spec(body)
        validate_cache_identity(
            CacheIdentity(spec.source_id, spec.url, spec.bytes, spec.sha256), spec
        )
        for identity in (
            CacheIdentity(spec.source_id, "https://example.test/other.bin", spec.bytes, spec.sha256),
            CacheIdentity(
                spec.source_id,
                spec.url,
                spec.bytes,
                hashlib.sha256(b"stale-cache-content").hexdigest(),
            ),
            CacheIdentity(spec.source_id, spec.url, spec.bytes + 1, spec.sha256),
        ):
            with self.assertRaises(IntegrityError):
                validate_cache_identity(identity, spec)
        drift = self.response(200, body, final_url="https://example.test/other.bin")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(AcquisitionError):
            apply_http_response(Path(tmp), spec, drift, range_requested=False)
        missing_range = self.response(206, body)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(AcquisitionError):
            apply_http_response(Path(tmp), spec, missing_range, range_requested=True)

    def test_encoding_content_type_response_provenance_and_manifest_fail_closed(self):
        body = b"bytes"
        spec = self.spec(body)
        encoded = self.response(200, body, **{"Content-Encoding": "gzip"})
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(AcquisitionError):
            apply_http_response(Path(tmp), spec, encoded, range_requested=False)
        wrong_type = self.response(200, body, **{"Content-Type": "text/html"})
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(AcquisitionError):
            apply_http_response(Path(tmp), spec, wrong_type, range_requested=False)
        for invalid in (
            self.response(200, body, method="POST"),
            self.response(200, body, started_at="2026-07-10T08:00:00-04:00"),
            self.response(
                200,
                body,
                request_headers=(("Accept-Encoding", "gzip"),),
            ),
            self.response(200, body, **{"Content-Length": str(len(body) + 1)}),
        ):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(AcquisitionError):
                apply_http_response(Path(tmp), spec, invalid, range_requested=False)
        manifest = canonical_manifest_bytes([spec])
        self.assertTrue(manifest.endswith(b"\n"))
        self.assertIn(b'"artifact_type":"BINARY"', manifest)
        with self.assertRaises(IntegrityError):
            canonical_manifest_bytes([spec, spec])
        with self.assertRaises(IntegrityError):
            self.spec(body, artifact_type=None).validate(release_mode=True)
        with self.assertRaises(IntegrityError):
            self.spec(body, validator_ids=()).validate(release_mode=True)
        with self.assertRaises(IntegrityError):
            self.spec(body, bytes=0).validate()

    def test_release_executor_requires_named_validator_and_persists_success_or_rejection(self):
        import _common

        self.assertNotIn("apply_http_response", _common.__all__)
        self.assertNotIn("ArtifactValidator", _common.__all__)
        self.assertIn("execute_frozen_response", _common.__all__)
        body = b"id,value\n1,ok\n"
        spec = self.spec(
            body,
            source_id="SYNTHETIC-IMMUTABLE-CSV",
            release="fixture-v1",
            url="https://example.test/synthetic.csv",
            filename="synthetic.csv",
            artifact_type="CSV",
            validator_ids=("synthetic-immutable-csv",),
            content_types=("text/csv",),
        )

        def response(**overrides):
            return self.response(
                200,
                body,
                requested_url=spec.url,
                final_url=spec.url,
                **{"Content-Type": "text/csv"},
                **overrides,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = execute_frozen_response(
                root,
                spec,
                response(),
                range_requested=False,
            )
            self.assertIsNotNone(result.receipt_path)
            self.assertEqual(result.receipt_path.read_bytes(), canonical_receipt_bytes(result.receipt))
            self.assertEqual(persist_receipt_atomic(root, result.receipt), result.receipt_path)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unknown = self.spec(
                body,
                source_id="UNREGISTERED-SOURCE",
                release="fixture-v1",
                url="https://example.test/synthetic.csv",
                filename="synthetic.csv",
                artifact_type="CSV",
                validator_ids=("synthetic-immutable-csv",),
                content_types=("text/csv",),
            )
            with self.assertRaises(IntegrityError):
                execute_frozen_response(
                    root,
                    unknown,
                    response(),
                    range_requested=False,
                )
            receipts = list((root / "receipts" / unknown.source_id).glob("*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text())["result_state"], "rejected")
        for invalid_response in (
            response(**{"Content-Encoding": "gzip"}),
            response(request_headers=(("X-Trace", "missing-identity"),)),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaises(AcquisitionError):
                    execute_frozen_response(
                        root,
                        spec,
                        invalid_response,
                        range_requested=False,
                    )
                receipts = list((root / "receipts" / spec.source_id).glob("*.json"))
                self.assertEqual(len(receipts), 1)
                rejected = json.loads(receipts[0].read_text(encoding="utf-8"))
                self.assertEqual(rejected["result_state"], "rejected")
                self.assertTrue(rejected["rejection_reason"])


class PendingDigestTests(_AcquisitionHelpers, unittest.TestCase):
    def test_pending_digest_accepts_only_single_complete_200_and_persists_receipt(self):
        body = b"candidate NPPES bytes are never opened"
        spec = PendingDigestInput(
            "NPPES-202606-V2",
            "2026-06",
            self.URL,
            "nppes.zip",
            len(body),
            ("example.test",),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = execute_pending_digest_response(
                root,
                spec,
                self.response(200, body),
                range_requested=False,
            )
            self.assertEqual(result.state, "candidate_complete")
            self.assertEqual(result.candidate_sha256, hashlib.sha256(body).hexdigest())
            self.assertEqual(result.receipt.final_sha256, result.candidate_sha256)
            self.assertTrue(result.receipt_path.is_file())
            self.assertIn("candidate-digest", str(result.candidate_path))
            self.assertFalse((root / "sha256" / result.candidate_sha256).exists())
            repeated = apply_pending_digest_response(
                root, spec, self.response(200, body), range_requested=False
            )
            self.assertEqual(repeated.candidate_sha256, result.candidate_sha256)

    def test_pending_digest_rejects_partial_200_206_416_range_and_redirect(self):
        body = b"candidate bytes"
        spec = PendingDigestInput(
            "NPPES-TEST", "fixture", self.URL, "nppes.zip", len(body), ("example.test",)
        )
        cases = (
            (self.response(200, body[:-1]), False),
            (
                self.response(
                    206,
                    body[3:],
                    range_start=3,
                    **{"Content-Range": f"bytes 3-{len(body)-1}/{len(body)}"},
                ),
                True,
            ),
            (
                self.response(
                    416,
                    b"",
                    range_start=len(body),
                    **{"Content-Range": f"bytes */{len(body)}"},
                ),
                True,
            ),
            (self.response(200, body, range_start=0), True),
            (
                self.response(
                    200,
                    body,
                    redirect_chain=("https://evil.test/hop",),
                ),
                False,
            ),
        )
        for response, ranged in cases:
            with self.subTest(status=response.status, ranged=ranged), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                candidate = spec.candidate_path(root)
                part = candidate.with_name(candidate.name + ".part")
                part.parent.mkdir(parents=True)
                part.write_bytes(b"stale")
                with self.assertRaises((AcquisitionError, IntegrityError)):
                    apply_pending_digest_response(
                        root, spec, response, range_requested=ranged
                    )
                self.assertFalse(part.exists())

    def test_pending_digest_same_identity_changed_complete_body_fails(self):
        body = b"candidate bytes"
        changed = b"candidate bytez"
        spec = PendingDigestInput(
            "NPPES-TEST", "fixture", self.URL, "nppes.zip", len(body), ("example.test",)
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apply_pending_digest_response(
                root, spec, self.response(200, body), range_requested=False
            )
            with self.assertRaises(IntegrityError):
                apply_pending_digest_response(
                    root, spec, self.response(200, changed), range_requested=False
                )

    def test_pending_digest_executor_persists_rejected_response_receipt(self):
        body = b"candidate bytes"
        spec = PendingDigestInput(
            "NPPES-TEST", "fixture", self.URL, "nppes.zip", len(body), ("example.test",)
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(IntegrityError):
                execute_pending_digest_response(
                    root,
                    spec,
                    self.response(200, body[:-1]),
                    range_requested=False,
                )
            receipts = list((root / "receipts" / spec.source_id).glob("*.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertEqual(receipt["result_state"], "rejected")
            self.assertEqual(receipt["response_bytes"], len(body) - 1)


class ArtifactAndArchiveValidationTests(_AcquisitionHelpers, unittest.TestCase):
    def write_zip(self, root: Path, members: dict[str, bytes]) -> tuple[Path, bytes]:
        path = root / "fixture.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, body in members.items():
                archive.writestr(name, body)
        return path, path.read_bytes()

    def zip_spec(self, body: bytes, *, artifact_type: str = "ZIP") -> FrozenInput:
        validator = "xlsx-approved" if artifact_type == "XLSX" else "zip-approved"
        suffix = "xlsx" if artifact_type == "XLSX" else "zip"
        return self.spec(
            body,
            filename=f"fixture.{suffix}",
            artifact_type=artifact_type,
            validator_ids=(validator,),
            magic_hex="504b",
        )

    def approval(self, candidate):
        return trusted_zip_approval(candidate)

    def test_zip_central_manifest_never_decompresses_before_approval_then_streams_crc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, body = self.write_zip(root, {"fixture.csv": b"id,value\n1,ok\n"})
            spec = self.zip_spec(body)
            with patch.object(
                zipfile.ZipFile, "open", side_effect=AssertionError("preapproval decompression")
            ), patch.object(
                zipfile.ZipFile, "testzip", side_effect=AssertionError("testzip forbidden")
            ):
                candidate = build_zip_candidate_manifest(path, spec)
            self.assertEqual(candidate.members[0].filename, "fixture.csv")
            self.assertEqual(candidate.members[0].compression_method, zipfile.ZIP_DEFLATED)

            wrong = ZipManifestApproval(
                candidate.source_id,
                candidate.archive_sha256,
                hashlib.sha256(b"wrong-zip-candidate-manifest").hexdigest(),
                "WRONG",
                "reviewer",
                "2026-07-10T12:30:00Z",
            )
            with patch.object(
                zipfile.ZipFile, "open", side_effect=AssertionError("approval was bypassed")
            ):
                with self.assertRaises(IntegrityError):
                    trusted_zip_approval(candidate, wrong)

            with patch.object(
                zipfile.ZipFile, "testzip", side_effect=AssertionError("testzip forbidden")
            ):
                verified = verify_zip_after_approval(path, candidate, self.approval(candidate))
            with open_verified_zip_member(verified, "fixture.csv") as handle:
                self.assertEqual(handle.read(), b"id,value\n1,ok\n")
            with self.assertRaises(IntegrityError):
                validate_zip_container(path, candidate.members)

    def test_self_authored_zip_and_xlsx_approval_fails_without_external_pin_before_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, body = self.write_zip(root, {"fixture.csv": b"id,value\n1,ok\n"})
            candidate = build_zip_candidate_manifest(path, self.zip_spec(body))
            approval = ZipManifestApproval(
                candidate.source_id,
                candidate.archive_sha256,
                hashlib.sha256(
                    canonical_zip_candidate_manifest_bytes(candidate)
                ).hexdigest(),
                "SELF-AUTHORED-APPROVAL",
                "candidate-author",
                "2026-07-10T12:30:00Z",
            )
            approval_body = canonical_zip_manifest_approval_bytes(approval)
            self_authored_trust = canonical_json_bytes(
                {
                    "schema_version": ZIP_APPROVAL_TRUST_ROOT_SCHEMA_VERSION,
                    "trust_root_id": "SELF-AUTHORED-TRUST",
                    "source_tree_id": ZIP_SOURCE_TREE_ID,
                    "bundle_manifest_sha256": ZIP_BUNDLE_ID,
                    "authorization_id": ZIP_AUTHORIZATION_ID,
                    "source_id": candidate.source_id,
                    "archive_sha256": candidate.archive_sha256,
                    "candidate_manifest_sha256": hashlib.sha256(
                        canonical_zip_candidate_manifest_bytes(candidate)
                    ).hexdigest(),
                    "member_approval_sha256": hashlib.sha256(
                        approval_body
                    ).hexdigest(),
                    "approved_by": "candidate-author",
                    "approved_at_utc": "2026-07-10T12:31:00Z",
                    "selected_member_filename": None,
                    "selected_member_central_metadata_sha256": None,
                }
            )
            independent_pin = hashlib.sha256(
                b"separately frozen integrator ZIP approval control"
            ).hexdigest()
            with patch.object(
                zipfile.ZipFile,
                "open",
                side_effect=AssertionError("payload opened before external approval"),
            ), self.assertRaisesRegex(IntegrityError, "independently supplied pin"):
                authorize_zip_approval(
                    candidate,
                    approval_body,
                    self_authored_trust,
                    independently_approved_trust_root_sha256=independent_pin,
                    source_tree_id=ZIP_SOURCE_TREE_ID,
                    bundle_manifest_sha256=ZIP_BUNDLE_ID,
                    authorization_id=ZIP_AUTHORIZATION_ID,
                )
            parsed_trust = trusted_zip_approval(candidate).trust_root
            forged = TrustedZipApproval(
                approval,
                parsed_trust,
                hashlib.sha256(self_authored_trust).hexdigest(),
                object(),
            )
            with patch.object(
                zipfile.ZipFile,
                "open",
                side_effect=AssertionError("forged trusted object opened payload"),
            ), self.assertRaisesRegex(IntegrityError, "external-pin verifier"):
                verify_zip_after_approval(path, candidate, forged)

    def test_zip_rejects_unsafe_duplicate_symlink_flags_and_compression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unsafe, body = self.write_zip(root, {"../escape.csv": b"x"})
            with self.assertRaises(IntegrityError):
                build_zip_candidate_manifest(unsafe, self.zip_spec(body))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "duplicate.zip"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("same.csv", b"one")
                    archive.writestr("same.csv", b"two")
            body = path.read_bytes()
            with self.assertRaises(IntegrityError):
                build_zip_candidate_manifest(path, self.zip_spec(body))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "symlink.zip"
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(info, "target")
            body = path.read_bytes()
            with self.assertRaises(IntegrityError):
                build_zip_candidate_manifest(path, self.zip_spec(body))

        for member in (
            ZipMemberIdentity("safe.csv", 1, 1, 0, 99),
            ZipMemberIdentity("safe.csv", 1, 1, 0, zipfile.ZIP_STORED, 1),
            ZipMemberIdentity("C:/escape.csv", 1, 1, 0),
        ):
            with self.assertRaises(IntegrityError):
                member.validate()

    def test_strict_csv_full_parse_schema_width_nonempty_and_unique(self):
        validator = csv_artifact_validator(
            "synthetic-csv",
            required_fields=("id", "value"),
            unique_fields=("id",),
            expected_header=("id", "value"),
        )
        bodies = {
            "valid": b"id,value\n1,a\n2,b\n",
            "empty": b"id,value\n",
            "duplicate_header": b"id,id\n1,a\n",
            "bad_width": b"id,value\n1,a,extra\n",
            "duplicate_key": b"id,value\n1,a\n1,b\n",
            "missing": b"id,other\n1,a\n",
        }
        for name, body in bodies.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "fixture.csv"
                path.write_bytes(body)
                if name == "valid":
                    validator(path)
                else:
                    with self.assertRaises(IntegrityError):
                        validator(path)

    def test_strict_json_complete_duplicate_keys_schema_and_unique_rows(self):
        validator = json_artifact_validator(
            "synthetic-json",
            top_level="array",
            required_fields=("id", "value"),
            unique_field="id",
        )
        bodies = {
            "valid": b'[{"id":"1","value":2}]',
            "duplicate_object_key": b'[{"id":"1","id":"2","value":2}]',
            "duplicate_row": b'[{"id":"1","value":2},{"id":"1","value":3}]',
            "nan": b'[{"id":"1","value":NaN}]',
            "trailing": b'[{"id":"1","value":2}] junk',
            "empty": b"[]",
        }
        for name, body in bodies.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "fixture.json"
                path.write_bytes(body)
                if name == "valid":
                    validator(path)
                else:
                    with self.assertRaises(IntegrityError):
                        validator(path)

    def test_zip_and_xlsx_specific_validators_require_approved_manifest(self):
        workbook_xml = (
            b'<?xml version="1.0"?>'
            b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            b'<sheets><sheet name="ZIP_COUNTY" sheetId="1" r:id="rId1"/></sheets></workbook>'
        )
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
            "xl/workbook.xml": workbook_xml,
            "xl/_rels/workbook.xml.rels": (
                b'<?xml version="1.0"?><Relationships>'
                b'<Relationship Id="rId1" '
                b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                b'Target="worksheets/sheet1.xml"/>'
                b'</Relationships>'
            ),
            "xl/worksheets/sheet1.xml": (
                b'<?xml version="1.0"?><worksheet><sheetData>'
                b'<row r="1"><c r="A1" t="inlineStr"><is><t>HEADER</t></is></c></row>'
                b'<row r="2"><c r="A2" t="inlineStr"><is><t>VALUE</t></is></c></row>'
                b'</sheetData></worksheet>'
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, body = self.write_zip(root, members)
            zip_spec = self.zip_spec(body)
            candidate = build_zip_candidate_manifest(path, zip_spec)
            approval = self.approval(candidate)
            zip_artifact_validator("zip-approved", candidate, approval)(path)

            xlsx_spec = self.zip_spec(body, artifact_type="XLSX")
            xlsx_candidate = build_zip_candidate_manifest(path, xlsx_spec)
            xlsx_approval = self.approval(xlsx_candidate)
            xlsx_artifact_validator(
                "xlsx-approved",
                xlsx_candidate,
                xlsx_approval,
                required_sheet_names=("ZIP_COUNTY",),
            )(path)
            with self.assertRaises(IntegrityError):
                xlsx_artifact_validator(
                    "xlsx-approved",
                    xlsx_candidate,
                    xlsx_approval,
                    required_sheet_names=("MISSING",),
                )(path)


if __name__ == "__main__":
    unittest.main()
