from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atlas_pipeline.acquisition import FrozenInput
from atlas_pipeline.build import OfflineBuildInputs
from atlas_pipeline.bundle import FrozenBundle
from atlas_pipeline.contracts import FIPS_TO_STATE, METHOD_LINEAGE_ID, STATE_FIPS
from atlas_pipeline.errors import IntegrityError
from atlas_pipeline.control_receipts import ReceiptContext, validate_control_receipt
from atlas_pipeline.hud_surface import (
    CURRENT_CT_PLANNING_REGIONS,
    _validate_hud_api_acquisition_receipt,
    build_hud_api_handoff,
    canonicalize_hud_api,
    parse_hud_api_response,
)
from atlas_pipeline.geoqa import sorted_fips_bytes
from atlas_pipeline.hud import parse_candidates
from atlas_pipeline.materialization import (
    MATERIALIZATION_SCHEMA_VERSION,
    STREAM_RECEIPT_SCHEMA_VERSION,
    MaterializationManifest,
)
from atlas_pipeline.phase2f_chain import _verify_hud_api
from atlas_pipeline.source_registry import SOURCE_POLICIES
from atlas_pipeline.strict_json import canonical_json_bytes
from tests.bundle_helpers import write_bundle


def api_body(rows: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "data": {
                "year": "2025",
                "quarter": "4",
                "input": "All",
                "crosswalk_type": "zip-county",
                "results": rows,
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")


def row(
    zip5: str,
    geoid: str,
    state: str,
    *,
    city: str = "CITY",
    bus: object = 1,
    res: object = 1,
    tot: object = 1,
    oth: object = 0,
) -> dict[str, object]:
    return {
        "zip": zip5,
        "geoid": geoid,
        "city": city,
        "state": state,
        "res_ratio": res,
        "bus_ratio": bus,
        "oth_ratio": oth,
        "tot_ratio": tot,
    }


def synthetic_county_universe() -> list[str]:
    result = set(CURRENT_CT_PLANNING_REGIONS)
    for state_fips in sorted(set(STATE_FIPS.values()) - {"09"}):
        for suffix in range(1, 1_000):
            result.add(f"{state_fips}{suffix:03d}")
            if len(result) == 3_144:
                return sorted(result)
    raise AssertionError("synthetic county universe construction failed")


def local_import_receipt(spec: FrozenInput) -> bytes:
    value = {
        "schema_version": STREAM_RECEIPT_SCHEMA_VERSION,
        "receipt_kind": "local_import",
        "source_id": spec.source_id,
        "release": spec.release,
        "request_method": "LOCAL_IMPORT",
        "requested_url": spec.url,
        "request_headers": [],
        "range_request": None,
        "started_at_utc": "2026-07-11T00:00:00Z",
        "ended_at_utc": "2026-07-11T00:00:01Z",
        "redirect_chain": [],
        "final_url": spec.url,
        "http_status": 0,
        "content_type": "application/json",
        "content_encoding": "identity",
        "content_range": None,
        "segment_start": 0,
        "segment_end": spec.bytes - 1,
        "representation_total_bytes": spec.bytes,
        "segment_bytes": spec.bytes,
        "segment_sha256": spec.sha256,
        "response_bytes": spec.bytes,
        "response_sha256": spec.sha256,
        "result_state": "complete",
        "rejection_reason": None,
        "final_bytes": spec.bytes,
        "final_sha256": spec.sha256,
    }
    return canonical_json_bytes(value)


class HudApiSurfaceTests(unittest.TestCase):
    def build_synthetic_handoff(self, root: Path):
        universe = synthetic_county_universe()
        rows = [
            row(
                f"{10_000 + index:05d}",
                county,
                FIPS_TO_STATE[county[:2]],
                city=f"CITY{index}",
            )
            for index, county in enumerate(universe)
        ]
        rows.extend(
            [
                row("80000", "60010", "AS"),
                row("80001", "66010", "GU"),
                row("80002", "69085", "MP"),
                row("80003", "72001", "PR"),
                row("80004", "78010", "VI"),
                row("80005", "60", "AS"),
                row("80006", "64", "FM"),
                row("80007", "68", "MH"),
                row("80008", "70", "PW"),
            ]
        )
        raw_body = api_body(rows)
        raw_path = root / "hud.json"
        raw_path.write_bytes(raw_body)
        raw_sha256 = __import__("hashlib").sha256(raw_body).hexdigest()
        spec = FrozenInput(
            "HUD-ZIP-COUNTY-API-2025Q4", "2025-Q4",
            "https://www.huduser.gov/hudapi/public/usps", "hud.json",
            len(raw_body), raw_sha256, ("www.huduser.gov",), "JSON",
            ("hud-api-2025q4-json",), ("application/json",),
        )
        acquisition = {
            "source": {
                "endpoint": spec.url,
                "request_parameters": {"type": 2, "query": "All", "year": 2025, "quarter": 4},
                "http_status": 200,
                "content_type": "application/json",
            },
            "artifact": {"bytes": spec.bytes, "sha256": spec.sha256},
            "response_identity": {
                "year": 2025, "quarter": 4, "input": "All",
                "crosswalk_type": "zip-county", "rows": len(rows),
            },
        }
        acquisition_body = canonical_json_bytes(acquisition)
        acquisition_path = root / "acquisition.json"
        acquisition_path.write_bytes(acquisition_body)
        import_body = local_import_receipt(spec)
        import_path = root / "import.json"
        import_path.write_bytes(import_body)
        universe_path = root / "universe.json"
        universe_path.write_bytes(canonical_json_bytes(universe))
        source_tree_manifest = canonical_json_bytes(
            {
                "path": "scripts/tests/synthetic-fixture.py",
                "bytes": len(b"synthetic-source-tree\n"),
                "sha256": __import__("hashlib").sha256(
                    b"synthetic-source-tree\n"
                ).hexdigest(),
            }
        )
        source_tree_id = __import__("hashlib").sha256(source_tree_manifest).hexdigest()
        manifest_body = canonical_json_bytes({"synthetic": "R4 Phase-2E"})
        manifest = MaterializationManifest(
            root / "phase2e.json", "2E", source_tree_id,
            {
                "county_universe_sha256": __import__("hashlib").sha256(
                    sorted_fips_bytes(universe)
                ).hexdigest(),
                "county_count": 3_144,
                "acquisition_receipt_sha256": __import__("hashlib").sha256(
                    acquisition_body
                ).hexdigest(),
                "result_row_count": len(rows),
            },
            {spec.source_id: spec}, manifest_body,
            __import__("hashlib").sha256(manifest_body).hexdigest(),
        )
        output = root / "handoff"
        with patch(
            "atlas_pipeline.hud_surface.resolve_release_validators", return_value=()
        ), patch(
            "atlas_pipeline.hud_surface.validate_control_receipt",
            return_value={"synthetic": True},
        ):
            build_hud_api_handoff(
                manifest, raw_path, universe_path, acquisition_path, import_path,
                output, authorization_id="SYNTHETIC-R4-AUTH",
                completed_at_utc="2026-07-11T00:00:02Z",
            )
        return universe, spec, raw_body, acquisition_body, import_body, output

    def test_end_to_end_handoff_ledger_and_phase2f_raw_rederivation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                universe, spec, raw_body, acquisition_body, import_body, handoff_dir
            ) = self.build_synthetic_handoff(root)
            excluded_body = (handoff_dir / "hud_api_excluded_rows.json").read_bytes()
            compatibility_body = (handoff_dir / "hud_api_compatibility.json").read_bytes()
            data_handoff_body = (handoff_dir / "hud_api_handoff.json").read_bytes()
            handoff_receipt_body = (handoff_dir / "receipt.json").read_bytes()
            canonical_csv = (handoff_dir / "hud_zip_county_api_2025q4.csv").read_bytes()
            receipt_value = json.loads(handoff_receipt_body)
            evidence = receipt_value["evidence"]
            self.assertEqual(evidence["excluded_rows_bytes"], len(excluded_body))
            self.assertEqual(
                evidence["excluded_rows_sha256"],
                __import__("hashlib").sha256(excluded_body).hexdigest(),
            )

            files = [
                ("HUD-CANON", "hud_api_zip_county_csv", "hud.csv", canonical_csv, "text/csv"),
                ("HUD-COMPAT", "hud_api_compatibility_json", "compat.json", compatibility_body, "application/json"),
                ("HUD-HANDOFF", "hud_api_data_handoff_json", "handoff.json", data_handoff_body, "application/json"),
                ("HUD-ACQUISITION", "hud_api_acquisition_receipt_json", "acquisition.json", acquisition_body, "application/json"),
                ("HUD-IMPORT", "source_acquisition_receipt_json", "import.json", import_body, "application/json"),
                (spec.source_id, "hud_api_raw_json", spec.filename, raw_body, "application/json"),
                ("HUD-EXCLUDED", "hud_api_excluded_rows_json", "excluded.json", excluded_body, "application/json"),
                ("HUD-HANDOFF-RECEIPT", "hud_api_handoff_receipt_json", "receipt.json", handoff_receipt_body, "application/json"),
            ]
            bundle_root = root / "bundle"
            write_bundle(bundle_root, phase="2F", expectations={}, files=files)
            manifest_path = bundle_root / "bundle.json"
            manifest = json.loads(manifest_path.read_bytes())
            raw_entry = next(
                item for item in manifest["inputs"] if item["source_id"] == spec.source_id
            )
            raw_entry["release"] = spec.release
            raw_entry["url"] = spec.url
            manifest_path.write_bytes(canonical_json_bytes(manifest))
            bundle = FrozenBundle.load(bundle_root, required_phase="2F")
            roles = {
                "hud": "HUD-CANON",
                "hud_api_compatibility": "HUD-COMPAT",
                "hud_api_handoff": "HUD-HANDOFF",
                "hud_api_acquisition_receipt": "HUD-ACQUISITION",
                "hud_api_import_receipt": "HUD-IMPORT",
                "hud_api_raw_json": spec.source_id,
                "hud_api_excluded_rows": "HUD-EXCLUDED",
                "hud_api_handoff_receipt": "HUD-HANDOFF-RECEIPT",
            }
            with (handoff_dir / "hud_zip_county_api_2025q4.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                candidates = tuple(parse_candidates(csv.DictReader(handle), set(universe)))
            inputs = OfflineBuildInputs(
                nppes_rows=(), hospital_rows=(), geocodes=(), hud_candidates=candidates,
                county_points={fips: ("0", "0") for fips in universe},
                county_attributes={}, places_rows=(), source_snapshot_id="1" * 64,
                hud_source_snapshot_id=__import__("hashlib").sha256(canonical_csv).hexdigest(),
                gazetteer_source_snapshot_id="2" * 64,
                method_lineage_id=METHOD_LINEAGE_ID,
            )
            external = {
                "evidence": {
                    **evidence,
                    "hud_api_handoff_receipt_sha256": __import__("hashlib").sha256(
                        handoff_receipt_body
                    ).hexdigest(),
                }
            }
            parsed_receipt = json.loads(handoff_receipt_body)
            with patch(
                "atlas_pipeline.phase2f_chain.validate_control_receipt",
                return_value=parsed_receipt,
            ), patch(
                "atlas_pipeline.phase2f_chain._source_spec", return_value=spec
            ):
                _verify_hud_api(bundle, roles, inputs, external)

            tampered_root = root / "tampered"
            tampered_files = [
                item if item[0] != "HUD-EXCLUDED"
                else (item[0], item[1], item[2], excluded_body + b" ", item[4])
                for item in files
            ]
            write_bundle(tampered_root, phase="2F", expectations={}, files=tampered_files)
            tampered_manifest_path = tampered_root / "bundle.json"
            tampered_manifest = json.loads(tampered_manifest_path.read_bytes())
            raw_entry = next(
                item for item in tampered_manifest["inputs"]
                if item["source_id"] == spec.source_id
            )
            raw_entry["release"] = spec.release
            raw_entry["url"] = spec.url
            tampered_manifest_path.write_bytes(canonical_json_bytes(tampered_manifest))
            with patch(
                "atlas_pipeline.phase2f_chain.validate_control_receipt",
                return_value=parsed_receipt,
            ), patch(
                "atlas_pipeline.phase2f_chain._source_spec", return_value=spec
            ), self.assertRaisesRegex(IntegrityError, "provenance hashes"):
                _verify_hud_api(
                    FrozenBundle.load(tampered_root), roles, inputs, external
                )

    def test_acquisition_receipt_rejects_embedded_token_material(self):
        body = canonical_json_bytes(
            {
                "source": {
                    "endpoint": "https://www.huduser.gov/hudapi/public/usps",
                    "request_parameters": {"type": 2, "query": "All", "year": 2025, "quarter": 4},
                    "http_status": 200,
                    "content_type": "application/json",
                },
                "artifact": {"bytes": 1, "sha256": "a" * 64},
                "response_identity": {
                    "year": 2025, "quarter": 4, "input": "All",
                    "crosswalk_type": "zip-county",
                },
                "leaked": "eyJabc.def.ghi",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            path.write_bytes(body)
            with self.assertRaisesRegex(IntegrityError, "credential material"):
                _validate_hud_api_acquisition_receipt(
                    path,
                    expected_sha256=__import__("hashlib").sha256(body).hexdigest(),
                    raw_bytes=1,
                    raw_sha256="a" * 64,
                )

    def test_api_handoff_receipt_has_separate_exact_accounting_contract(self):
        context = ReceiptContext("a" * 64, "b" * 64, "R4-AUTHORIZATION")
        evidence = {
            "phase2e_bundle_manifest_sha256": "b" * 64,
            "url": "https://www.huduser.gov/hudapi/public/usps",
            "filename": "HUD_USPS_ZIP_COUNTY_2025Q4_API_RAW.json",
            "request_parameters_sha256": "c" * 64,
            "frozen_acquisition_receipt_sha256": (
                "013d18d4019534e28bb2306d0d0fd1afce6b9b911e8a37da7d0489405a2d6782"
            ),
            "import_receipt_sha256": "d" * 64,
            "raw_json_sha256": (
                "72f7c78d1439b6b38c0ebc5a163765c6c0e90caae1bc921e59a0a4a03b99cda4"
            ),
            "canonical_csv_sha256": "e" * 64,
            "county_universe_sha256": "f" * 64,
            "result_row_count": 54_571,
            "in_scope_row_count": 54_500,
            "excluded_row_count": 71,
            "excluded_rows_bytes": 100,
            "excluded_rows_sha256": "1" * 64,
            "release_year": 2025,
            "state_basis": "CT_PLANNING_REGIONS_2022_PLUS",
            "compatibility_receipt_sha256": "2" * 64,
            "hud_api_data_handoff_sha256": "3" * 64,
        }
        value = {
            "schema_version": "atlas-v19-hud-api-handoff-receipt-1",
            "receipt_id": "hud_api_handoff",
            "status": "complete",
            "source_tree_id": context.source_tree_id,
            "bundle_manifest_sha256": context.bundle_manifest_sha256,
            "authorization_id": context.authorization_id,
            "command": "import-hud-api",
            "completed_at_utc": "2026-07-11T18:00:00Z",
            "evidence": evidence,
        }
        body = canonical_json_bytes(value)
        parsed = validate_control_receipt("hud_api_handoff", body, context=context)
        self.assertEqual(parsed["evidence"]["excluded_row_count"], 71)
        evidence["excluded_row_count"] = 70
        with self.assertRaisesRegex(IntegrityError, "accounting"):
            validate_control_receipt(
                "hud_api_handoff", canonical_json_bytes(value), context=context
            )

    def test_r4_source_identity_and_phase2e_manifest_are_separate_from_xlsx(self):
        policy = SOURCE_POLICIES["HUD-ZIP-COUNTY-API-2025Q4"]
        self.assertEqual(policy.artifact_type, "JSON")
        self.assertEqual(policy.bytes, 7_800_489)
        self.assertEqual(
            policy.sha256,
            "72f7c78d1439b6b38c0ebc5a163765c6c0e90caae1bc921e59a0a4a03b99cda4",
        )
        source = {
            "source_id": policy.source_id,
            "release": policy.release,
            "url": policy.url,
            "filename": policy.filename,
            "bytes": policy.bytes,
            "sha256": policy.sha256,
            "allowed_hosts": ["www.huduser.gov"],
            "artifact_type": policy.artifact_type,
            "validator_ids": list(policy.validator_ids),
            "content_types": list(policy.content_types),
            "magic_hex": policy.magic_hex,
        }
        value = {
            "schema_version": MATERIALIZATION_SCHEMA_VERSION,
            "phase": "2E",
            "source_tree_id": "a" * 64,
            "expectations": {
                "county_universe_sha256": "b" * 64,
                "county_count": 3_144,
                "acquisition_receipt_sha256": (
                    "013d18d4019534e28bb2306d0d0fd1afce6b9b911e8a37da7d0489405a2d6782"
                ),
                "result_row_count": 54_571,
            },
            "sources": [source],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "phase2e.json"
            path.write_bytes(canonical_json_bytes(value))
            loaded = MaterializationManifest.load(path, required_phase="2E")
            self.assertEqual(set(loaded.sources), {policy.source_id})
            value["expectations"]["acquisition_receipt_sha256"] = "c" * 64
            path.write_bytes(canonical_json_bytes(value))
            with self.assertRaisesRegex(IntegrityError, "Amendment 03"):
                MaterializationManifest.load(path, required_phase="2E")

    def test_decimal_schema_geography_and_canonical_bytes(self):
        universe = {"01001", *CURRENT_CT_PLANNING_REGIONS}
        rows = [row("01001", "01001", "AL", bus=1.000, res=0, oth=0)]
        rows.extend(
            row(f"06{index:03d}", county, "CT")
            for index, county in enumerate(sorted(CURRENT_CT_PLANNING_REGIONS), start=1)
        )
        rows.extend(
            (
                row("96799", "60010", "AS"),
                row("96910", "66010", "GU"),
                row("96950", "69085", "MP"),
                row("00601", "72001", "PR"),
                row("00801", "78010", "VI"),
                row("96798", "60", "AS"),
                row("96941", "64", "FM"),
                row("96942", "68", "MH"),
                row("96943", "70", "PW"),
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.json"
            second = Path(temporary) / "second.json"
            first.write_bytes(
                api_body(rows).replace(b'"bus_ratio":1.0', b'"bus_ratio":1e0', 1)
            )
            second.write_bytes(api_body(list(reversed(rows))))
            first_csv, first_rows, first_excluded, audit = canonicalize_hud_api(
                first, county_universe=universe
            )
            second_csv, second_rows, second_excluded, second_audit = canonicalize_hud_api(
                second, county_universe=universe
            )
        self.assertEqual(first_csv, second_csv)
        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first_excluded, second_excluded)
        self.assertEqual(audit, second_audit)
        self.assertTrue(
            first_csv.startswith(
                b"ZIP,COUNTY,BUS_RATIO,RES_RATIO,TOT_RATIO,OTH_RATIO,"
                b"USPS_ZIP_PREF_CITY,USPS_ZIP_PREF_STATE\n"
            )
        )
        self.assertIn(b"01001,01001,1,0,1,0,CITY,AL\n", first_csv)
        self.assertEqual(audit["raw_row_count"], 19)
        self.assertEqual(audit["in_scope_row_count"], 10)
        self.assertEqual(audit["excluded_territory_row_count"], 5)
        self.assertEqual(audit["excluded_special_row_count"], 4)
        self.assertEqual(
            set(audit["territory_state_prefix_counts"]),
            {"60:AS", "66:GU", "69:MP", "72:PR", "78:VI"},
        )
        self.assertEqual(
            set(audit["special_pair_counts"]),
            {"60:AS", "64:FM", "68:MH", "70:PW"},
        )
        self.assertRegex(audit["excluded_rows_sha256"], r"^[0-9a-f]{64}$")

    def test_exact_schema_duplicate_keys_and_native_number_types(self):
        valid = api_body([row("01001", "01001", "AL")])
        self.assertEqual(len(parse_hud_api_response(valid)), 1)
        duplicate = valid.replace(
            b'"year":"2025"', b'"year":"2025","year":"2025"'
        )
        with self.assertRaisesRegex(IntegrityError, "duplicate HUD API JSON key"):
            parse_hud_api_response(duplicate)
        duplicate_root = b'{"data":' + valid[len(b'{"data":'):-1] + b',"data":{}}'
        with self.assertRaisesRegex(IntegrityError, "duplicate HUD API JSON key"):
            parse_hud_api_response(duplicate_root)
        duplicate_result = valid.replace(
            b'"zip":"01001"', b'"zip":"01001","zip":"01001"'
        )
        with self.assertRaisesRegex(IntegrityError, "duplicate HUD API JSON key"):
            parse_hud_api_response(duplicate_result)
        wrong_type = valid.replace(b'"bus_ratio":1', b'"bus_ratio":"1"')
        with self.assertRaisesRegex(IntegrityError, "native JSON number"):
            parse_hud_api_response(wrong_type)
        extra = valid.replace(b'"state":"AL"', b'"state":"AL","extra":1')
        with self.assertRaisesRegex(IntegrityError, "result schema mismatch"):
            parse_hud_api_response(extra)

    def test_signed_nonfinite_out_of_range_and_sum_failures(self):
        valid = api_body([row("01001", "01001", "AL")])
        cases = {
            "signed": valid.replace(b'"bus_ratio":1', b'"bus_ratio":-0'),
            "nonfinite": valid.replace(b'"bus_ratio":1', b'"bus_ratio":NaN'),
            "range": valid.replace(b'"bus_ratio":1', b'"bus_ratio":1.01'),
            "sum": valid.replace(b'"tot_ratio":1', b'"tot_ratio":0.9'),
            "boolean": valid.replace(b'"bus_ratio":1', b'"bus_ratio":true'),
        }
        for label, body in cases.items():
            with self.subTest(label=label), self.assertRaises(IntegrityError):
                parse_hud_api_response(body)

    def test_ratio_sum_tolerance_boundary_is_exact_decimal(self):
        rows = [
            row("01001", "01001", "AL", bus=0.5, res=0.5, tot=0.5, oth=0),
            row(
                "01001", "01003", "AL", bus=0.500000000001,
                res=0.500000000001, tot=0.500000000001, oth=0,
            ),
        ]
        self.assertEqual(len(parse_hud_api_response(api_body(rows))), 2)
        rows[1]["bus_ratio"] = 0.500000000002
        with self.assertRaisesRegex(IntegrityError, "bus_ratio sum fails"):
            parse_hud_api_response(api_body(rows))

    def test_metadata_pairs_and_unknown_geography_fail_closed(self):
        metadata_rows = [
            row("01001", "01001", "AL", city="A", bus=0.5, res=0.5, tot=0.5),
            row("01001", "01003", "AL", city="B", bus=0.5, res=0.5, tot=0.5),
        ]
        with self.assertRaisesRegex(IntegrityError, "metadata varies"):
            parse_hud_api_response(api_body(metadata_rows))
        duplicate_rows = [row("01001", "01001", "AL"), row("01001", "01001", "AL")]
        with self.assertRaisesRegex(IntegrityError, "duplicate HUD API ZIP/GEOID"):
            parse_hud_api_response(api_body(duplicate_rows))

        universe = {"01001", *CURRENT_CT_PLANNING_REGIONS}
        rows = [row("01001", "01001", "AL")]
        rows.extend(
            row(f"06{index:03d}", county, "CT")
            for index, county in enumerate(sorted(CURRENT_CT_PLANNING_REGIONS), start=1)
        )
        rows.append(row("99999", "99999", "ZZ"))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unknown.json"
            path.write_bytes(api_body(rows))
            with self.assertRaisesRegex(IntegrityError, "unknown/out-of-universe"):
                canonicalize_hud_api(path, county_universe=universe)
            path.write_bytes(api_body(rows[:-2]))
            with self.assertRaisesRegex(IntegrityError, "omits frozen-universe"):
                canonicalize_hud_api(path, county_universe=universe)

            conflict = [*rows[:-1], row("96910", "66010", "PR")]
            path.write_bytes(api_body(conflict))
            with self.assertRaisesRegex(IntegrityError, "territory GEOID/state conflict"):
                canonicalize_hud_api(path, county_universe=universe)

            domestic = [*rows[:-1], row("99998", "01005", "AL")]
            path.write_bytes(api_body(domestic))
            with self.assertRaisesRegex(IntegrityError, "unknown/out-of-universe"):
                canonicalize_hud_api(path, county_universe=universe)

            obsolete = [*rows[:-1], row("06099", "09001", "CT")]
            path.write_bytes(api_body(obsolete))
            with self.assertRaisesRegex(IntegrityError, "unknown/out-of-universe"):
                canonicalize_hud_api(path, county_universe=universe)

    def test_excluded_hash_is_content_bound(self):
        universe = {"01001", *CURRENT_CT_PLANNING_REGIONS}
        base = [row("01001", "01001", "AL")]
        base.extend(
            row(f"06{index:03d}", county, "CT")
            for index, county in enumerate(sorted(CURRENT_CT_PLANNING_REGIONS), start=1)
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hud.json"
            path.write_bytes(api_body([*base, row("96910", "66010", "GU")]))
            _csv, _rows, _excluded, first = canonicalize_hud_api(path, county_universe=universe)
            path.write_bytes(api_body([*base, row("96911", "66010", "GU")]))
            _csv, _rows, _excluded, second = canonicalize_hud_api(path, county_universe=universe)
        self.assertNotEqual(first["excluded_rows_sha256"], second["excluded_rows_sha256"])


if __name__ == "__main__":
    unittest.main()
