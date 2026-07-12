from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from atlas_pipeline.contracts import METHOD_LINEAGE_ID
from atlas_pipeline.bundle import write_output_receipt
from atlas_pipeline.errors import ContractError, IntegrityError
from atlas_pipeline.pipeline_cli import validation_complete_main
from atlas_pipeline.phase2f_runner import VALIDATION_COMPLETION_SCHEMA_VERSION
from atlas_pipeline.strict_json import canonical_json_bytes
from atlas_pipeline.validation import (
    ReviewEvidence,
    SampleCandidate,
    canonical_double_review_bytes,
    canonical_sample_bytes,
    freeze_validation_artifacts,
)
from atlas_pipeline.validation_completion import complete_validation
from tests.synthetic import make_npi

SOURCE = hashlib.sha256(b"synthetic-r4-validation-completion").hexdigest()


def canonical(rows):
    return ("\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n").encode()


class ValidationCompletionTests(unittest.TestCase):
    def setUp(self):
        self.sample = (
            SampleCandidate("P", "P_DIRECT_RURAL_BORDER", make_npi("123456780"), "A-" + "1" * 64, SOURCE, "DIRECT", "RURAL_BORDER", False),
            SampleCandidate("P", "P_HUD_URBAN_NONBORDER", make_npi("123456781"), "A-" + "2" * 64, SOURCE, "HUD", "URBAN_NONBORDER", False),
            SampleCandidate("H", "H_UNMATCHED", "H-000001", "A-" + "3" * 64, SOURCE, "UNMATCHED", None, True),
        )
        evidence = {}
        for item in self.sample:
            assigned = item.method != "UNMATCHED"
            evidence[item.sample_key] = ReviewEvidence(
                ("100 TEST ST", "", "CITY", "AL", "01001", "US"),
                ("100 TEST ST", "", "CITY", "AL", "01001", "US"),
                "CENSUS_MATCH" if item.method == "DIRECT" else "HUD_BUS_RATIO_SAME_STATE_COUNTY_INTERNAL_POINT" if item.method == "HUD" else "HUD_ZIP_ABSENT",
                "AL", "01001" if assigned else None,
                "-86.000000" if assigned else None, "32.000000" if assigned else None,
                "100 TEST ST" if item.method == "DIRECT" else None,
                "Match" if item.method == "DIRECT" else "No_Match" if item.method == "HUD" else None,
                ({"county_fips": "01001", "bus_ratio": "1", "res_ratio": "1", "tot_ratio": "1", "oth_ratio": "0", "same_state": True},) if item.method == "HUD" else (),
                "CRITICAL ACCESS HOSPITALS" if item.entity_kind == "H" else None,
                item.is_cah if item.entity_kind == "H" else None,
            )
        frozen = freeze_validation_artifacts(self.sample, self.sample, evidence)
        self.selected = canonical_sample_bytes(self.sample)
        self.blank = frozen.blank_worksheet_bytes
        self.double = canonical_double_review_bytes(self.sample)

    def completed(self, reviewer, *, incorrect_key=None, mutate_evidence=False):
        rows = [json.loads(line) for line in self.blank.decode().splitlines()]
        for row in rows:
            row["reviewer_id"] = reviewer
            row["identity_normalization_route_code"] = "CORRECT"
            row["county_state_code"] = "CORRECT" if row["method"] in {"DIRECT", "HUD"} else "NA"
            row["coordinate_or_hud_plausibility_code"] = "CORRECT" if row["method"] in {"DIRECT", "HUD"} else "NA"
            row["unmatched_rule_code"] = "CORRECT" if row["method"] == "UNMATCHED" else "NA"
            if row["sample_key"] == incorrect_key:
                row["coordinate_or_hud_plausibility_code"] = "INCORRECT"
        if mutate_evidence:
            rows[0]["source_state"] = "AK"
        return canonical(rows)

    def run_completion(self, primary, secondary, adjudication=b""):
        return complete_validation(
            selected_sample_bytes=self.selected,
            blank_worksheet_bytes=self.blank,
            completed_primary_worksheet_bytes=primary,
            double_review_sample_bytes=self.double,
            completed_secondary_worksheet_bytes=secondary,
            adjudication_bytes=adjudication,
            expected_selected_sample_sha256=hashlib.sha256(self.selected).hexdigest(),
            expected_blank_worksheet_sha256=hashlib.sha256(self.blank).hexdigest(),
            expected_double_review_sample_sha256=hashlib.sha256(self.double).hexdigest(),
            global_assigned_state_mismatches=0,
        )

    def test_pass_receipt_is_canonical_hash_bound_and_uses_actual_denominators(self):
        result = self.run_completion(self.completed("reviewer-a"), self.completed("reviewer-b"))
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.sha256, hashlib.sha256(result.receipt_bytes).hexdigest())
        receipt = json.loads(result.receipt_bytes)
        direct = next(row for row in receipt["criteria"] if row["criterion"] == "provider_direct_county_state")
        self.assertEqual((direct["passes"], direct["denominator"]), (1, 1))
        self.assertEqual(receipt["agreement"]["status"], "PASS")

    def test_frozen_evidence_reviewer_and_hash_contracts_fail_closed(self):
        with self.assertRaises(IntegrityError):
            self.run_completion(self.completed("reviewer-a", mutate_evidence=True), self.completed("reviewer-b"))
        with self.assertRaises(ContractError):
            self.run_completion(self.completed("same"), self.completed("same"))
        with self.assertRaises(IntegrityError):
            complete_validation(
                selected_sample_bytes=self.selected,
                blank_worksheet_bytes=self.blank,
                completed_primary_worksheet_bytes=self.completed("a"),
                double_review_sample_bytes=self.double,
                completed_secondary_worksheet_bytes=self.completed("b"),
                adjudication_bytes=b"",
                expected_selected_sample_sha256="0" * 64,
                expected_blank_worksheet_sha256=hashlib.sha256(self.blank).hexdigest(),
                expected_double_review_sample_sha256=hashlib.sha256(self.double).hexdigest(),
                global_assigned_state_mismatches=0,
            )

    def test_disagreement_requires_exact_independent_adjudication(self):
        key = self.sample[1].sample_key
        primary = self.completed("reviewer-a")
        secondary = self.completed("reviewer-b", incorrect_key=key)
        with self.assertRaises(IntegrityError):
            self.run_completion(primary, secondary)
        adjudication = canonical([{
            "sample_key": key,
            "criterion_field": "coordinate_or_hud_plausibility_code",
            "adjudicated_code": "CORRECT",
            "adjudicator_id": "reviewer-c",
            "notes": "resolved against frozen evidence",
        }])
        result = self.run_completion(primary, secondary, adjudication)
        receipt = json.loads(result.receipt_bytes)
        self.assertEqual(receipt["disagreements"], 1)
        self.assertEqual(receipt["reviewers"]["adjudicator"], "reviewer-c")

        invalid_na = canonical([{
            "sample_key": key,
            "criterion_field": "coordinate_or_hud_plausibility_code",
            "adjudicated_code": "NA",
            "adjudicator_id": "reviewer-c",
            "notes": "must not remove an applicable criterion",
        }])
        with self.assertRaises(ContractError):
            self.run_completion(primary, secondary, invalid_na)

    def test_criterion_or_global_state_failure_returns_fail_receipt(self):
        key = self.sample[0].sample_key
        result = self.run_completion(
            self.completed("reviewer-a", incorrect_key=key),
            self.completed("reviewer-b", incorrect_key=key),
        )
        self.assertEqual(result.status, "FAIL")

    def test_validation_complete_cli_binds_phase2f_and_writes_later_state_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            phase2f = root / "phase2f"
            phase2f.mkdir()
            paths = {
                "validation_selected_sample": phase2f / "selected.jsonl",
                "validation_blank_worksheet": phase2f / "blank.jsonl",
                "validation_double_review_sample": phase2f / "double.jsonl",
                "build_audit": phase2f / "build_audit.json",
                "other_authoritative_artifact": phase2f / "other.json",
            }
            paths["validation_selected_sample"].write_bytes(self.selected)
            paths["validation_blank_worksheet"].write_bytes(self.blank)
            paths["validation_double_review_sample"].write_bytes(self.double)
            paths["other_authoritative_artifact"].write_bytes(b"{}\n")
            paths["build_audit"].write_bytes(canonical_json_bytes({
                "method_lineage_id": METHOD_LINEAGE_ID,
                "manual_review_status": "REQUIRED_BEFORE_GATE_B",
                "validation_completion": {
                    "state": "PENDING_LATER_STATE",
                    "schema_version": VALIDATION_COMPLETION_SCHEMA_VERSION,
                    "method_lineage_id": METHOD_LINEAGE_ID,
                    "selected_sample_sha256": hashlib.sha256(self.selected).hexdigest(),
                    "blank_worksheet_sha256": hashlib.sha256(self.blank).hexdigest(),
                    "double_review_sample_sha256": hashlib.sha256(self.double).hexdigest(),
                    "global_assigned_state_mismatches": 0,
                },
            }))
            phase2f_receipt = phase2f / "receipt.json"
            review_config = root / "review.json"
            review_config.write_bytes(b"{}\n")
            primary = root / "primary.jsonl"
            secondary = root / "secondary.jsonl"
            adjudication = root / "adjudication.jsonl"
            primary.write_bytes(self.completed("reviewer-a"))
            secondary.write_bytes(self.completed("reviewer-b"))
            adjudication.write_bytes(b"")
            output = root / "validation-output"
            bundle = SimpleNamespace(
                expectations={"method_lineage_id": METHOD_LINEAGE_ID},
                source_tree_id="a" * 64,
                manifest_sha256="b" * 64,
                authorization_id="R4-TEST-AUTH",
            )
            write_output_receipt(
                phase2f,
                command="build-offline",
                bundle=bundle,
                artifacts=paths,
                completed_at_utc="2026-07-11T17:30:00Z",
            )
            review = SimpleNamespace(
                method_lineage_id=METHOD_LINEAGE_ID,
                validation_completion_schema_version=VALIDATION_COMPLETION_SCHEMA_VERSION,
            )
            argv = [
                "--bundle", str(root / "bundle"),
                "--review-config", str(review_config),
                "--phase2f-output-root", str(phase2f),
                "--phase2f-output-receipt", str(phase2f_receipt),
                "--completed-primary", str(primary),
                "--completed-secondary", str(secondary),
                "--adjudication", str(adjudication),
                "--output", str(output),
                "--repo-root", str(root),
                "--source-tree-manifest", str(root / "tree.jsonl"),
                "--integrator-control", str(root / "control.json"),
                "--approved-control-sha256", "c" * 64,
                "--authorization-receipt", str(root / "auth.json"),
                "--control-receipts-manifest", str(root / "receipts.json"),
            ]
            with (
                mock.patch(
                    "atlas_pipeline.pipeline_cli._load_bundle_after_authority",
                    return_value=(bundle, object()),
                ),
                mock.patch(
                    "atlas_pipeline.pipeline_cli.ReviewConfig.load",
                    return_value=review,
                ),
                mock.patch.dict(
                    "os.environ",
                    {"ATLAS_VALIDATION_COMPLETED_AT_UTC": "2026-07-11T18:00:00Z"},
                ),
            ):
                validation_complete_main(argv)
            completion = json.loads((output / "validation_completion.json").read_bytes())
            control = json.loads(
                (output / "validation_completion_control_receipt.json").read_bytes()
            )
            output_receipt = json.loads((output / "receipt.json").read_bytes())
            self.assertEqual(completion["status"], "PASS")
            self.assertEqual(control["evidence"]["validation_status"], "PASS")
            self.assertEqual(output_receipt["method_lineage_id"], METHOD_LINEAGE_ID)
            self.assertEqual(output_receipt["command"], "validation-complete")

            paths["other_authoritative_artifact"].write_bytes(b'{"tampered":true}\n')
            tampered_argv = list(argv)
            tampered_argv[tampered_argv.index(str(output))] = str(root / "tampered-output")
            with (
                mock.patch(
                    "atlas_pipeline.pipeline_cli._load_bundle_after_authority",
                    return_value=(bundle, object()),
                ),
                mock.patch(
                    "atlas_pipeline.pipeline_cli.ReviewConfig.load",
                    return_value=review,
                ),
            ):
                with self.assertRaises(IntegrityError):
                    validation_complete_main(tampered_argv)


if __name__ == "__main__":
    unittest.main()
