from __future__ import annotations

import hashlib
import struct
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path

from atlas_pipeline.acquisition import (
    FrozenInput,
    ZipManifestApproval,
    build_zip_candidate_manifest,
    canonical_zip_candidate_manifest_bytes,
)
from atlas_pipeline.bundle import canonical_json_bytes
from atlas_pipeline.errors import IntegrityError
from atlas_pipeline.geoqa import GeometryEngineIdentity, sorted_fips_bytes
from atlas_pipeline.tiger_qa import build_tiger_qa_binding


def dbf_bytes(fips: tuple[str, ...], *, geoid_width: int = 5) -> bytes:
    fields = (("STATEFP", 2), ("COUNTYFP", 3), ("GEOID", geoid_width))
    header_bytes = 32 + 32 * len(fields) + 1
    record_bytes = 1 + sum(width for _name, width in fields)
    header = bytearray(32)
    header[0] = 0x03
    struct.pack_into("<I", header, 4, len(fips))
    struct.pack_into("<H", header, 8, header_bytes)
    struct.pack_into("<H", header, 10, record_bytes)
    descriptors = bytearray()
    for name, width in fields:
        descriptor = bytearray(32)
        descriptor[: len(name)] = name.encode("ascii")
        descriptor[11] = ord("C")
        descriptor[16] = width
        descriptors.extend(descriptor)
    rows = []
    for fips_id in fips:
        rows.append(
            b" "
            + fips_id[:2].encode("ascii")
            + fips_id[2:].encode("ascii")
            + fips_id[:geoid_width].encode("ascii").ljust(geoid_width)
        )
    return bytes(header) + bytes(descriptors) + b"\r" + b"".join(rows) + b"\x1a"


class TigerQaTests(unittest.TestCase):
    def make_archive(
        self,
        root: Path,
        *,
        fips: tuple[str, ...] = ("01001", "01003"),
        geoid_width: int = 5,
        omit_suffix: str | None = None,
    ):
        archive = root / "tiger.zip"
        bodies = {
            ".cpg": b"UTF-8\n",
            ".dbf": dbf_bytes(fips, geoid_width=geoid_width),
            ".prj": b"SYNTHETIC WKT\n",
            ".shp": b"synthetic-shp",
            ".shx": b"synthetic-shx",
        }
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for suffix, body in sorted(bodies.items()):
                if suffix != omit_suffix:
                    output.writestr(f"tl_2025_us_county{suffix}", body)
            output.writestr("tl_2025_us_county.shp.iso.xml", b"<metadata/>\n")
        body = archive.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        spec = FrozenInput(
            "CENSUS-TIGER-COUNTY-2025",
            "synthetic TIGER fixture",
            "https://example.test/tiger.zip",
            "tiger.zip",
            len(body),
            digest,
            ("example.test",),
            "ZIP",
            ("zip-central-directory",),
            ("application/zip",),
        )
        candidate = build_zip_candidate_manifest(archive, spec)
        candidate_hash = hashlib.sha256(
            canonical_zip_candidate_manifest_bytes(candidate)
        ).hexdigest()
        approval = ZipManifestApproval(
            candidate.source_id,
            digest,
            candidate_hash,
            "synthetic-tiger-approval",
            "Independent Synthetic Integrator",
            "2026-07-10T00:00:00Z",
        )
        return archive, candidate, approval, digest

    def evidence(self, candidate, universe, engine, digest):
        selected = (
            "tl_2025_us_county.cpg\n"
            "tl_2025_us_county.dbf\n"
            "tl_2025_us_county.prj\n"
            "tl_2025_us_county.shp\n"
            "tl_2025_us_county.shx\n"
        ).encode("ascii")
        import atlas_pipeline.tiger_qa as tiger_qa

        return {
            "raw_zip_sha256": digest,
            "member_manifest_sha256": hashlib.sha256(
                canonical_zip_candidate_manifest_bytes(candidate)
            ).hexdigest(),
            "selected_member_set_sha256": hashlib.sha256(selected).hexdigest(),
            "county_fips_set_sha256": hashlib.sha256(
                sorted_fips_bytes(universe)
            ).hexdigest(),
            "resolution": "FULL_RESOLUTION",
            "geometry_engine_sha256": hashlib.sha256(
                canonical_json_bytes(asdict(engine))
            ).hexdigest(),
            "loader_sha256": hashlib.sha256(Path(tiger_qa.__file__).read_bytes()).hexdigest(),
            "point_qa_worksheet_sha256": hashlib.sha256(
                b"synthetic frozen point-QA worksheet"
            ).hexdigest(),
        }

    def test_derives_real_fips_from_approved_dbf_and_binds_all_qa_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, candidate, approval, digest = self.make_archive(root)
            universe = {"01001", "01003"}
            engine = GeometryEngineIdentity(
                "synthetic-geometry",
                "1.0.0",
                hashlib.sha256(b"synthetic geometry package").hexdigest(),
                "covers",
            )
            binding = build_tiger_qa_binding(
                archive,
                candidate,
                approval,
                county_universe=universe,
                geometry_engine=engine,
                receipt_evidence=self.evidence(candidate, universe, engine, digest),
                expected_archive_bytes=archive.stat().st_size,
                expected_archive_sha256=digest,
            )
            self.assertEqual(set(binding.polygon_source.county_fips), universe)
            self.assertEqual(binding.polygon_source.scale, "FULL_RESOLUTION")

    def test_rejects_self_asserted_universe_bad_dbf_schema_and_missing_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = GeometryEngineIdentity(
                "synthetic-geometry",
                "1.0.0",
                hashlib.sha256(b"synthetic geometry package").hexdigest(),
                "covers",
            )
            archive, candidate, approval, digest = self.make_archive(root)
            claimed = {"01001", "01005"}
            with self.assertRaisesRegex(IntegrityError, "DBF-derived FIPS"):
                build_tiger_qa_binding(
                    archive,
                    candidate,
                    approval,
                    county_universe=claimed,
                    geometry_engine=engine,
                    receipt_evidence=self.evidence(candidate, claimed, engine, digest),
                    expected_archive_bytes=archive.stat().st_size,
                    expected_archive_sha256=digest,
                )

            bad_schema = root / "bad-schema"
            bad_schema.mkdir()
            archive, candidate, approval, digest = self.make_archive(
                bad_schema, geoid_width=4
            )
            with self.assertRaisesRegex(IntegrityError, "GEOID schema"):
                build_tiger_qa_binding(
                    archive,
                    candidate,
                    approval,
                    county_universe={"01001", "01003"},
                    geometry_engine=engine,
                    receipt_evidence=self.evidence(
                        candidate, {"01001", "01003"}, engine, digest
                    ),
                    expected_archive_bytes=archive.stat().st_size,
                    expected_archive_sha256=digest,
                )

            missing = root / "missing-sidecar"
            missing.mkdir()
            archive, candidate, approval, digest = self.make_archive(
                missing, omit_suffix=".shx"
            )
            with self.assertRaisesRegex(IntegrityError, "required shapefile sidecars"):
                build_tiger_qa_binding(
                    archive,
                    candidate,
                    approval,
                    county_universe={"01001", "01003"},
                    geometry_engine=engine,
                    receipt_evidence=self.evidence(
                        candidate, {"01001", "01003"}, engine, digest
                    ),
                    expected_archive_bytes=archive.stat().st_size,
                    expected_archive_sha256=digest,
                )


if __name__ == "__main__":
    unittest.main()
