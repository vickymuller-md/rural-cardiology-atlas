from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path

from atlas_pipeline.acquisition import (
    ZipCandidateManifest,
    canonical_zip_candidate_manifest_bytes,
    ZipMemberIdentity,
)
from atlas_pipeline.bundle import canonical_json_bytes
from atlas_pipeline.contracts import STATE_FIPS
from atlas_pipeline.errors import IntegrityError
from atlas_pipeline.gazetteer_provenance import (
    CANONICAL_COLUMNS,
    CANONICAL_TRANSFORM_ID,
    EXTRACTION_RECEIPT_SCHEMA,
    GazetteerExtractionReceipt,
    SELECTED_MEMBER_FILENAME,
    SOURCE_ID,
    _candidate_from_body,
    _member_body,
    _receipt_from_body,
    derive_canonical_gazetteer,
    require_exact_canonical_derivation,
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


def synthetic_rows(count: int = 3_144) -> tuple[set[str], list[str]]:
    states = sorted(STATE_FIPS.items(), key=lambda item: item[1])
    universe: set[str] = set()
    rows: list[str] = []
    for state, prefix in states:
        for county_code in range(1, 1_000):
            fips = f"{prefix}{county_code:03d}"
            universe.add(fips)
            latitude = 25 + len(universe) / 100_000
            longitude = -124 + len(universe) / 100_000
            rows.append(
                f"{state}|{fips}|Synthetic {fips}|{latitude:.8f}|{longitude:.8f}"
            )
            if len(rows) == count:
                return universe, rows
    raise AssertionError("synthetic state/FIPS generator exhausted")


def member_bytes(rows: list[str]) -> bytes:
    return (
        "|".join(CANONICAL_COLUMNS) + "\n" + "\n".join(rows) + "\n"
    ).encode("utf-8")


class GazetteerProvenanceTests(unittest.TestCase):
    def test_gazetteer_predecessor_receipt_is_exactly_pinned(self):
        context = PredecessorBridgeContext(
            "a" * 64,
            R4_METHOD_LINEAGE_ID,
            R4_INPUT_MANIFEST_SHA256,
            R4_INPUT_COUNT,
        )
        artifact_id = "R4B-GAZETTEER-EXTRACTION-RECEIPT"
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
                observed_source_tree_id="b" * 64,
                artifact_id=artifact_id,
                artifact_sha256=artifact_sha256,
            )

    def test_changed_coordinate_with_same_3144_fips_set_fails_exact_derivation(self):
        universe, rows = synthetic_rows()
        approved_member = member_bytes(rows)
        approved_canonical = derive_canonical_gazetteer(
            approved_member, county_universe=universe, expected_count=3_144
        )

        canonical_lines = approved_canonical.decode("utf-8").splitlines()
        changed = list(canonical_lines[1:])
        fields = changed[1_517].split(",")
        fields[3] = "40.12345678"
        changed[1_517] = ",".join(fields)
        changed_canonical = (
            canonical_lines[0] + "\n" + "\n".join(changed) + "\n"
        ).encode("utf-8")

        # A rehashed processed table changed no identifier and preserved all
        # 3,144 rows; the raw approved member remains unchanged.
        self.assertEqual(
            {line.split("|")[1] for line in rows},
            {line.split(",")[1] for line in changed},
        )
        with self.assertRaisesRegex(
            IntegrityError, "differs byte-for-byte from the frozen raw ZIP derivation"
        ):
            require_exact_canonical_derivation(
                approved_member,
                changed_canonical,
                county_universe=universe,
                expected_count=3_144,
            )

    def test_candidate_receipt_crc_and_selected_member_are_exactly_bound(self):
        universe, rows = synthetic_rows(2)
        payload = member_bytes(rows)
        archive_buffer = io.BytesIO()
        info = zipfile.ZipInfo(
            SELECTED_MEMBER_FILENAME, date_time=(2026, 1, 1, 0, 0, 0)
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr(info, payload)
        archive_body = archive_buffer.getvalue()

        with self.subTest("candidate parser"):
            with zipfile.ZipFile(io.BytesIO(archive_body)) as archive:
                observed = archive.getinfo(SELECTED_MEMBER_FILENAME)
                member = ZipMemberIdentity(
                    observed.filename,
                    observed.compress_size,
                    observed.file_size,
                    observed.CRC,
                    observed.compress_type,
                    observed.flag_bits,
                    observed.external_attr,
                    observed.is_dir(),
                )
            candidate = ZipCandidateManifest(
                SOURCE_ID,
                len(archive_body),
                hashlib.sha256(archive_body).hexdigest(),
                (member,),
            )
            candidate_body = canonical_zip_candidate_manifest_bytes(candidate)
            self.assertEqual(_candidate_from_body(candidate_body), candidate)

        canonical = derive_canonical_gazetteer(
            payload, county_universe=universe, expected_count=2
        )
        receipt = GazetteerExtractionReceipt(
            EXTRACTION_RECEIPT_SCHEMA,
            "APPROVED_CRC_VERIFIED_CANONICAL",
            hashlib.sha256(b"synthetic source tree").hexdigest(),
            "SYNTHETIC-GAZETTEER-AUTHORIZATION",
            SOURCE_ID,
            len(archive_body),
            hashlib.sha256(archive_body).hexdigest(),
            hashlib.sha256(candidate_body).hexdigest(),
            "SYNTHETIC-GAZETTEER-EXTRACTION-APPROVAL",
            "Independent Synthetic Integrator",
            "2026-07-10T00:00:00Z",
            SELECTED_MEMBER_FILENAME,
            hashlib.sha256(canonical_json_bytes(asdict(member))).hexdigest(),
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            "|",
            CANONICAL_TRANSFORM_ID,
            list(CANONICAL_COLUMNS),
            len(canonical),
            hashlib.sha256(canonical).hexdigest(),
        )
        receipt_body = canonical_json_bytes(asdict(receipt))
        self.assertEqual(_receipt_from_body(receipt_body), receipt)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gazetteer.zip"
            path.write_bytes(archive_body)
            self.assertEqual(_member_body(path, candidate, receipt), payload)
            wrong = GazetteerExtractionReceipt(
                **{
                    **asdict(receipt),
                    "selected_member_sha256": hashlib.sha256(
                        b"different approved member"
                    ).hexdigest(),
                }
            )
            with self.assertRaisesRegex(IntegrityError, "bytes/CRC/SHA-256"):
                _member_body(path, candidate, wrong)


if __name__ == "__main__":
    unittest.main()
