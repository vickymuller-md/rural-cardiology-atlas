from __future__ import annotations

import hashlib
import re
import time
import unittest
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from unittest.mock import patch

from atlas_pipeline.addresses import normalize_address
from atlas_pipeline.errors import ContractError, IntegrityError
from atlas_pipeline.geocoder import CanonicalGeocode
from atlas_pipeline.hud import parse_candidates
from atlas_pipeline.quality import (
    SensitivityEntity,
    build_sensitivity_report,
    compare_sensitivity,
    evaluate_geography_diagnostics,
    evaluate_geography_gates,
    require_sensitivity_set,
    require_sensitivity_entity_equality,
    route_sensitivities,
    summarize_sensitivity,
)
from atlas_pipeline.validation import (
    PROVIDER_STRATA_TARGETS,
    HospitalQuotas,
    ReviewEvidence,
    SampleCandidate,
    agreement_gate,
    assigned_context,
    double_key,
    evaluate_criterion,
    freeze_validation_artifacts,
    hospital_quotas,
    provider_stratum,
    sample_key,
    select_double_review,
    select_hospital_sample,
    select_provider_sample,
)
from tests.synthetic import make_npi

VALIDATION_SOURCE_SHA256 = hashlib.sha256(
    b"synthetic-manual-validation-source"
).hexdigest()


class ManualValidationTests(unittest.TestCase):
    def candidate(self, kind, stratum, entity, method, context=None, is_cah=False):
        seed = int(hashlib.sha256(entity.encode()).hexdigest()[:8], 16)
        if kind == "P":
            entity = make_npi(f"{seed % 1_000_000_000:09d}")
            method = "DIRECT" if stratum.startswith("P_DIRECT_") else "HUD" if stratum.startswith("P_HUD_") else "UNMATCHED"
        elif not re.fullmatch(r"H-(?:[0-9]{6}|[0-9]{5}F)", entity):
            entity = f"H-{seed % 1_000_000:06d}"
        address = "A-" + hashlib.sha256(entity.encode()).hexdigest()
        return SampleCandidate(
            kind,
            stratum,
            entity,
            address,
            VALIDATION_SOURCE_SHA256,
            method,
            context,
            is_cah,
        )

    def test_exact_sample_and_double_hash_serialization(self):
        expected = hashlib.sha256(
            b"\0".join(
                (
                    b"ATLAS-V19-MANUAL-20260709",
                    b"P",
                    b"P_DIRECT_RURAL_BORDER",
                    b"1234567893",
                    b"A-1",
                    VALIDATION_SOURCE_SHA256.encode("ascii"),
                )
            )
        ).hexdigest()
        actual = sample_key(
            "P",
            "P_DIRECT_RURAL_BORDER",
            "1234567893",
            "A-1",
            VALIDATION_SOURCE_SHA256,
        )
        self.assertEqual(actual, expected)
        self.assertEqual(
            double_key(actual),
            hashlib.sha256(("ATLAS-V19-DOUBLE-20260709\0" + actual).encode()).hexdigest(),
        )

    def test_provider_context_strata_and_structural_shortfalls(self):
        self.assertEqual(assigned_context(4, True), "RURAL_BORDER")
        self.assertEqual(provider_stratum("DIRECT", rucc=2, state_border=False), "P_DIRECT_URBAN_NONBORDER")
        self.assertEqual(provider_stratum("UNMATCHED", positive_candidate_ruccs=(4, 9)), "P_UNMATCHED_RURAL")
        self.assertEqual(provider_stratum("UNMATCHED", positive_candidate_ruccs=(1, 3)), "P_UNMATCHED_URBAN")
        self.assertEqual(provider_stratum("UNMATCHED", positive_candidate_ruccs=(1, 4)), "P_UNMATCHED_COMPLEX")
        candidates = [
            self.candidate("P", stratum, f"P-{index}", "DIRECT")
            for index, stratum in enumerate(PROVIDER_STRATA_TARGETS)
        ]
        sample = select_provider_sample(candidates)
        self.assertEqual(sample.actual, len(PROVIDER_STRATA_TARGETS))
        self.assertTrue(sample.shortfalls)
        self.assertEqual(len(select_double_review(sample.selected, hospital=False)), len(PROVIDER_STRATA_TARGETS))

    def test_hospital_quota_formula_exact_solver_and_cah_target(self):
        self.assertEqual(hospital_quotas(100, 100, 0).__dict__, {"direct": 36, "hud": 24, "unmatched": 0, "target": 60, "structural_shortfall": 0})
        self.assertEqual(hospital_quotas(2, 1, 1).target, 4)
        candidates = (
            self.candidate("H", "H_DIRECT_RURAL_BORDER", "H-1", "DIRECT", "RURAL_BORDER", True),
            self.candidate("H", "H_DIRECT_URBAN_NONBORDER", "H-2", "DIRECT", "URBAN_NONBORDER", False),
            self.candidate("H", "H_HUD_RURAL_NONBORDER", "H-3", "HUD", "RURAL_NONBORDER", True),
            self.candidate("H", "H_UNMATCHED", "H-4", "UNMATCHED", None, False),
        )
        sample = select_hospital_sample(candidates)
        self.assertEqual(len(sample.selected), 4)
        self.assertEqual(sample.cah_target, 2)
        self.assertEqual(sample.maximum_feasible_cah, 2)
        self.assertEqual(len(select_double_review(sample.selected, hospital=True)), 3)
        evidence = {
            item.sample_key: ReviewEvidence(
                ("300 TEST RD", "", "CITY", "AL", "01001", "US"),
                ("300 TEST RD", "", "CITY", "AL", "01001", "US"),
                item.method,
                "AL",
                "01001" if item.method != "UNMATCHED" else None,
                "-86.000000" if item.method != "UNMATCHED" else None,
                "32.000000" if item.method != "UNMATCHED" else None,
                "300 TEST RD" if item.method == "DIRECT" else None,
                "Match" if item.method == "DIRECT" else "No_Match" if item.method == "HUD" else None,
                ({"county_fips": "01001", "bus_ratio": "1", "res_ratio": "1", "tot_ratio": "1", "oth_ratio": "0", "same_state": True},) if item.method == "HUD" else (),
                "CRITICAL ACCESS HOSPITALS" if item.is_cah else "ACUTE CARE HOSPITALS",
                item.is_cah,
            )
            for item in sample.selected
        }
        frozen = freeze_validation_artifacts(candidates, sample.selected, evidence)
        self.assertEqual(frozen.sampling_frame_sha256, hashlib.sha256(frozen.sampling_frame_bytes).hexdigest())
        self.assertNotIn(b"headline", frozen.blank_worksheet_bytes.lower())
        self.assertIn(b'"reviewer_id":""', frozen.blank_worksheet_bytes)
        changed = candidates[0]
        invalid_selection = SampleCandidate(
            changed.entity_kind,
            "H_DIRECT_RURAL_NONBORDER" if changed.stratum_id != "H_DIRECT_RURAL_NONBORDER" else "H_DIRECT_RURAL_BORDER",
            changed.entity_id,
            changed.address_id,
            changed.source_snapshot_id,
            changed.method,
            "RURAL_NONBORDER" if changed.context != "RURAL_NONBORDER" else "RURAL_BORDER",
            changed.is_cah,
        )
        with self.assertRaises(IntegrityError):
            freeze_validation_artifacts(candidates, (invalid_selection,), {})

    def test_hospital_sampler_scales_to_full_frame_analogue(self):
        candidates = []
        for index in range(5_432):
            method = "DIRECT" if index < 5_000 else "HUD" if index < 5_350 else "UNMATCHED"
            context = (
                None
                if method == "UNMATCHED"
                else ("RURAL_NONBORDER", "RURAL_BORDER", "URBAN_NONBORDER", "URBAN_BORDER")[index % 4]
            )
            stratum = "H_UNMATCHED" if method == "UNMATCHED" else f"H_{method}_{context}"
            entity = f"H-{index:06d}"
            address = "A-" + hashlib.sha256(entity.encode()).hexdigest()
            candidates.append(
                SampleCandidate(
                    "H",
                    stratum,
                    entity,
                    address,
                    VALIDATION_SOURCE_SHA256,
                    method,
                    context,
                    index % 7 == 0,
                )
            )
        started = time.perf_counter()
        sample = select_hospital_sample(candidates)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(sample.selected), 60)
        self.assertEqual(sample.cah_target, 20)
        self.assertLess(elapsed, 5.0)

    def test_hospital_sampler_matches_exhaustive_lexicographic_solution(self):
        candidates = (
            self.candidate("H", "H_DIRECT_RURAL_BORDER", "H-11", "DIRECT", "RURAL_BORDER", True),
            self.candidate("H", "H_DIRECT_RURAL_BORDER", "H-12", "DIRECT", "RURAL_BORDER", True),
            self.candidate("H", "H_DIRECT_URBAN_NONBORDER", "H-13", "DIRECT", "URBAN_NONBORDER", True),
            self.candidate("H", "H_DIRECT_URBAN_NONBORDER", "H-14", "DIRECT", "URBAN_NONBORDER", True),
            self.candidate("H", "H_HUD_RURAL_BORDER", "H-15", "HUD", "RURAL_BORDER", False),
            self.candidate("H", "H_HUD_URBAN_NONBORDER", "H-16", "HUD", "URBAN_NONBORDER", False),
            self.candidate("H", "H_UNMATCHED", "H-17", "UNMATCHED", None, True),
            self.candidate("H", "H_UNMATCHED", "H-18", "UNMATCHED", None, False),
        )
        fixed = HospitalQuotas(2, 1, 1, 4, 56)
        with patch("atlas_pipeline.validation.hospital_quotas", return_value=fixed):
            actual = select_hospital_sample(candidates)

        feasible = []
        for subset in combinations(candidates, 4):
            methods = {method: sum(item.method == method for item in subset) for method in ("DIRECT", "HUD", "UNMATCHED")}
            contexts = {item.context for item in subset if item.context is not None}
            if methods != {"DIRECT": 2, "HUD": 1, "UNMATCHED": 1}:
                continue
            if contexts != {"RURAL_BORDER", "URBAN_NONBORDER"}:
                continue
            feasible.append(subset)
        maximum = max(sum(item.is_cah for item in subset) for subset in feasible)
        target = min(maximum, 20)
        expected = min(
            (tuple(sorted(subset, key=lambda item: item.identity)) for subset in feasible if sum(item.is_cah for item in subset) >= target),
            key=lambda subset: tuple(item.identity for item in subset),
        )
        self.assertEqual(actual.maximum_feasible_cah, maximum)
        self.assertEqual(actual.selected, expected)

    def test_review_codes_actual_denominators_and_agreement(self):
        gate = evaluate_criterion("provider_direct_county_state", ["CORRECT"] * 59 + ["INCORRECT"])
        self.assertEqual(gate.status, "PASS")
        self.assertEqual(evaluate_criterion("provider_hud_rule", ["CORRECT", "NA"]).status, "PASS")
        self.assertEqual(evaluate_criterion("provider_hud_rule", ["NA"]).status, "NA")
        self.assertEqual(evaluate_criterion("provider_hud_rule", ["UNVERIFIABLE"]).status, "FAIL")
        self.assertEqual(
            evaluate_criterion("provider_direct_county_state", ["CORRECT"] * 60, source_state_mismatches=1).status,
            "FAIL",
        )
        invariant = agreement_gate(["CORRECT"] * 5, ["CORRECT"] * 5)
        self.assertIsNone(invariant.kappa)
        self.assertEqual(invariant.status, "PASS")
        disagreement = agreement_gate(["CORRECT", "INCORRECT"] * 5, ["INCORRECT", "CORRECT"] * 5)
        self.assertEqual(disagreement.status, "FAIL")
        boundary = agreement_gate(
            ["CORRECT"] * 6 + ["INCORRECT"] * 4,
            ["CORRECT"] * 5 + ["INCORRECT"] * 5,
        )
        self.assertEqual(boundary.kappa, Fraction(4, 5))
        self.assertEqual(boundary.status, "PASS")


class NumericalAndSensitivityTests(unittest.TestCase):
    def test_exact_rate_gates_and_reconciliation(self):
        gates = evaluate_geography_gates(
            provider_direct=850, provider_hud=140, provider_unmatched=10,
            provider_total=1000,
            hospital_direct=950, hospital_hud=45, hospital_unmatched=5, hospital_total=1000,
            cah_unmatched=5, cah_total=1000,
        )
        self.assertEqual([gate.name for gate in gates], [
            "provider_unmatched", "hospital_unmatched", "cah_unmatched",
        ])
        self.assertTrue(all(gate.passed for gate in gates))
        diagnostics = evaluate_geography_diagnostics(
            provider_direct=850, provider_hud=140, provider_unmatched=10,
            provider_total=1000, provider_ties=5, submitted_unique_addresses=1000,
            hospital_direct=950, hospital_hud=45, hospital_unmatched=5,
            hospital_total=1000,
        )
        self.assertEqual(len(diagnostics), 5)
        self.assertFalse(any(hasattr(row, "passed") for row in diagnostics))
        with self.assertRaises(IntegrityError):
            evaluate_geography_gates(
                provider_direct=1, provider_hud=0, provider_unmatched=0,
                provider_total=2,
                hospital_direct=1, hospital_hud=0, hospital_unmatched=1, hospital_total=1,
                cah_unmatched=0, cah_total=1,
            )

    def test_all_five_sensitivity_routes_metrics_and_escalations(self):
        direct_provider = normalize_address("100 TEST ST", "", "CITY", "AL", "01001", "US")
        fallback_provider = normalize_address("PO BOX 1", "", "CITY", "AL", "01002", "US")
        direct_cah = normalize_address("300 DISTANT RD", "", "CITY", "AL", "01003", "US")
        entities = {
            "P-1": direct_provider,
            "P-2": fallback_provider,
            "H-000001": direct_cah,
        }
        geocodes = {
            direct_provider.address_id: CanonicalGeocode(
                direct_provider.address_id, "Match", "Match", "Exact", "match",
                "-86", "32", "01001", 1, VALIDATION_SOURCE_SHA256,
            ),
            direct_cah.address_id: CanonicalGeocode(
                direct_cah.address_id, "Match", "Match", "Exact", "match",
                "-90", "35", "01003", 1, VALIDATION_SOURCE_SHA256,
            ),
        }
        universe = {"01001", "01003", "02001"}
        county_points = {"01001": ("32", "-86"), "01003": ("33", "-87"), "02001": ("60", "-150")}
        hud = tuple(parse_candidates([
            {"ZIP": "01002", "COUNTY": "01003", "BUS_RATIO": "0.6", "RES_RATIO": "0.4", "TOT_RATIO": "0.5", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01002", "COUNTY": "01001", "BUS_RATIO": "0.3", "RES_RATIO": "0.6", "TOT_RATIO": "0.4", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01002", "COUNTY": "02001", "BUS_RATIO": "0.9", "RES_RATIO": "0.1", "TOT_RATIO": "0.7", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
        ], universe))
        routed = route_sensitivities(
            entities, geocodes, hud, county_points,
            hud_source_snapshot_id=VALIDATION_SOURCE_SHA256,
            gazetteer_source_snapshot_id=VALIDATION_SOURCE_SHA256,
        )
        self.assertEqual(set(routed), {"PRIMARY", "DIRECT_ONLY", "HUD_RES_SAME_STATE", "HUD_TOT_SAME_STATE", "HUD_BUS_STATE_UNRESTRICTED"})
        unrestricted = next(row for row in routed["HUD_BUS_STATE_UNRESTRICTED"] if row.entity_id == "P-2")
        self.assertEqual(unrestricted.county_fips, "02001")
        self.assertTrue(unrestricted.cross_state)
        total = next(row for row in routed["HUD_TOT_SAME_STATE"] if row.entity_id == "P-2")
        self.assertEqual(total.county_fips, "01003")
        kinds = {"P-1": ("P", False), "P-2": ("P", False), "H-000001": ("H", True)}
        primary_assignments = {row.entity_id: row.county_fips for row in routed["PRIMARY"]}
        summaries = {}
        for name, routes in routed.items():
            sensitivity_entities = tuple(
                SensitivityEntity(route, kinds[route.entity_id][0], kinds[route.entity_id][1])
                for route in routes
            )
            summaries[name] = summarize_sensitivity(
                name,
                sensitivity_entities,
                county_universe=universe,
                county_points=county_points,
                rucc={"01001": 2, "01003": 4, "02001": 9},
                primary_assignments=primary_assignments,
            )
        require_sensitivity_set(summaries)
        self.assertEqual(summaries["PRIMARY"].cah_evaluable, 1)
        self.assertEqual(summaries["PRIMARY"].cah_gte_20, 1)
        self.assertGreater(summaries["DIRECT_ONLY"].zero_provider_pct, summaries["PRIMARY"].zero_provider_pct)
        comparison = compare_sensitivity(summaries["PRIMARY"], summaries["DIRECT_ONLY"])
        self.assertTrue(comparison.zero_provider_escalation)
        self.assertIsInstance(comparison.rural_median_delta_miles, Decimal)
        report_entities = {
            name: tuple(
                SensitivityEntity(
                    route,
                    kinds[route.entity_id][0],
                    kinds[route.entity_id][1],
                )
                for route in routes
            )
            for name, routes in routed.items()
        }
        report = build_sensitivity_report(
            report_entities,
            county_universe=universe,
            county_points=county_points,
            rucc={"01001": 2, "01003": 4, "02001": 9},
            primary_assignments=primary_assignments,
        )
        self.assertEqual(report.sha256, hashlib.sha256(report.canonical_bytes).hexdigest())
        self.assertEqual(report.summaries["PRIMARY"].provider_total, 2)
        self.assertEqual(report.summaries["PRIMARY"].hospital_total, 1)
        self.assertEqual(report.summaries["PRIMARY"].zero_provider_count, 1)
        drifted = dict(report_entities)
        drifted["DIRECT_ONLY"] = drifted["DIRECT_ONLY"][:-1]
        with self.assertRaises(IntegrityError):
            require_sensitivity_entity_equality(drifted)

    def test_primary_tie_uses_hud_and_invalid_scope_never_enters_routing(self):
        address = normalize_address("100 TEST ST", "", "CITY", "AL", "01002", "US")
        geocode = CanonicalGeocode(
            address.address_id, "Tie", "Tie", "", "", None, None, None, 1,
            VALIDATION_SOURCE_SHA256,
        )
        hud = tuple(parse_candidates([
            {"ZIP": "01002", "COUNTY": "01001", "BUS_RATIO": "0.75", "RES_RATIO": "0.4", "TOT_RATIO": "0.6", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01002", "COUNTY": "01003", "BUS_RATIO": "0.25", "RES_RATIO": "0.6", "TOT_RATIO": "0.4", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
        ], {"01001", "01003"}))
        routes = route_sensitivities(
            {"P-1": address}, {address.address_id: geocode}, hud,
            {"01001": ("32", "-86"), "01003": ("33", "-87")},
            hud_source_snapshot_id=VALIDATION_SOURCE_SHA256,
            gazetteer_source_snapshot_id=VALIDATION_SOURCE_SHA256,
        )
        self.assertEqual(routes["PRIMARY"][0].county_fips, "01001")
        self.assertEqual(
            routes["PRIMARY"][0].assignment_method,
            "HUD_BUS_RATIO_SAME_STATE_COUNTY_INTERNAL_POINT",
        )
        invalid = normalize_address("100 TEST ST", "", "CITY", "PR", "01002", "US")
        with self.assertRaises(IntegrityError):
            route_sensitivities(
                {"P-2": invalid}, {}, hud,
                {"01001": ("32", "-86"), "01003": ("33", "-87")},
                hud_source_snapshot_id=VALIDATION_SOURCE_SHA256,
                gazetteer_source_snapshot_id=VALIDATION_SOURCE_SHA256,
            )

    def test_res_tot_and_unrestricted_ties_retain_complete_provenance(self):
        address = normalize_address("PO BOX 9", "", "CITY", "AL", "01002", "US")
        hud = tuple(parse_candidates([
            {"ZIP": "01002", "COUNTY": "01001", "BUS_RATIO": "0.6", "RES_RATIO": "0.5", "TOT_RATIO": "0.5", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01002", "COUNTY": "01003", "BUS_RATIO": "0.4", "RES_RATIO": "0.5", "TOT_RATIO": "0.5", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01002", "COUNTY": "02001", "BUS_RATIO": "0.6", "RES_RATIO": "0", "TOT_RATIO": "0", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
        ], {"01001", "01003", "02001"}))
        routes = route_sensitivities(
            {"P-1": address}, {}, hud,
            {"01001": ("32", "-86"), "01003": ("33", "-87"), "02001": ("60", "-150")},
            hud_source_snapshot_id=VALIDATION_SOURCE_SHA256,
            gazetteer_source_snapshot_id=VALIDATION_SOURCE_SHA256,
        )
        expected = {
            "HUD_RES_SAME_STATE": ("HUD_RES_TIE", "RES_RATIO"),
            "HUD_TOT_SAME_STATE": ("HUD_TOT_TIE", "TOT_RATIO"),
            "HUD_BUS_STATE_UNRESTRICTED": ("HUD_BUS_TIE", "BUS_RATIO"),
        }
        for name, (outcome, basis) in expected.items():
            route = routes[name][0]
            self.assertEqual((route.hud_outcome, route.hud_ratio_basis), (outcome, basis))
            self.assertEqual(route.hud_selected_ratio, route.hud_second_highest_ratio)
            self.assertEqual(route.hud_margin_to_second, "0")
            self.assertEqual(route.final_disposition, "final_unmatched")
            self.assertEqual(route.hud_source_snapshot_id, VALIDATION_SOURCE_SHA256)
            self.assertIsNone(route.gazetteer_source_snapshot_id)


if __name__ == "__main__":
    unittest.main()
