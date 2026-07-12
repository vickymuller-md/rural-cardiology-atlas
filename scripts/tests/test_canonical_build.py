from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

from atlas_pipeline.addresses import normalize_address
from atlas_pipeline.acquisition import FrozenInput, canonical_manifest_bytes
from atlas_pipeline.build import (
    BuildExpectations,
    OfflineBuildInputs,
    ReleaseQualityGateError,
    build_offline,
    build_release,
)
from atlas_pipeline.canonical import (
    CanonicalNumber,
    HospitalLocation,
    ProviderLocation,
    compute_cah_accounting,
    conventional_median,
    haversine_miles,
    hospital_table_bytes,
    nearest_provider,
    provider_table_bytes,
    public_counties_bytes,
    public_one_decimal,
    quantize_coordinate,
)
from atlas_pipeline.errors import ContractError, IntegrityError
from atlas_pipeline.bundle import FrozenBundle, verify_output_receipt
from atlas_pipeline.contracts import FORBIDDEN_LEGACY_COUNTY_FIELD, METHOD_LINEAGE_ID
from atlas_pipeline.geoqa import (
    GeometryEngineIdentity,
    PolygonSource,
    build_adjacency_receipt,
    build_point_qa_setup,
    sorted_fips_bytes,
)
from atlas_pipeline.geocoder import CanonicalGeocode
from atlas_pipeline.hud import parse_candidates
from atlas_pipeline.places import PlacesCountyRow
from atlas_pipeline.places import canonical_places_snapshot_bytes
from atlas_pipeline.phase2f_review import (
    build_phase2f_review_artifacts,
    parse_adjacency_snapshot,
)
from atlas_pipeline.phase2f_runner import (
    _persist_gate_rejection,
    _write_gate_b_audit_artifacts,
)
from tests.synthetic import hospital_row, make_npi, nppes_row

SNAPSHOT = hashlib.sha256(b"synthetic-canonical-build-snapshot").hexdigest()
GEOCODER_RESPONSE_SHA256 = hashlib.sha256(
    b"synthetic-canonical-build-geocoder-response"
).hexdigest()
ADJACENCY_SOURCE_SHA256 = hashlib.sha256(
    b"synthetic-canonical-build-adjacency"
).hexdigest()
TIGER_SOURCE_SHA256 = hashlib.sha256(
    b"synthetic-canonical-build-tiger"
).hexdigest()
GEOMETRY_ENGINE_SHA256 = hashlib.sha256(
    b"synthetic-canonical-build-geometry-engine"
).hexdigest()
FROZEN_INPUT_SHA256 = hashlib.sha256(
    b"synthetic-canonical-build-frozen-input"
).hexdigest()
HUD_SNAPSHOT_SHA256 = hashlib.sha256(b"synthetic-hud-canonical").hexdigest()
GAZETTEER_SNAPSHOT_SHA256 = hashlib.sha256(b"synthetic-gazetteer-canonical").hexdigest()


def geocode(address_id: str, county: str, longitude: str, latitude: str):
    return CanonicalGeocode(
        address_id, "Match", "Match", "Exact", "SYNTHETIC MATCH",
        longitude, latitude, county, 1, GEOCODER_RESPONSE_SHA256,
    )


def provider(npi: str, latitude: str, longitude: str, county="01001"):
    return ProviderLocation(
        npi=npi,
        address_id="A-" + npi,
        primary_taxonomy="207RC0000X",
        active_status="active_never_deactivated",
        source_state="AL",
        county_fips=county,
        latitude=latitude,
        longitude=longitude,
        assignment_method="CENSUS_MATCH",
        direct_eligible=True,
        hud_eligible=True,
        census_outcome="Match",
        hud_attempted=False,
        hud_outcome="NOT_ATTEMPTED",
        final_disposition="assigned_direct_census",
        geocoder_match_indicator="Match",
        geocoder_match_type="Exact",
        hud_ratio_basis=None,
        hud_selected_ratio=None,
        hud_bus_ratio=None,
        hud_res_ratio=None,
        hud_tot_ratio=None,
        hud_same_state_candidate_count=None,
        hud_second_highest_ratio=None,
        hud_bus_margin_to_second=None,
        quality_flag="DIRECT_MATCH",
        census_source_response_sha256=GEOCODER_RESPONSE_SHA256,
        hud_source_snapshot_id=None,
        gazetteer_source_snapshot_id=None,
        source_snapshot_id=SNAPSHOT,
    )


class CanonicalNumericsTests(unittest.TestCase):
    def test_coordinate_quantization_half_up_negative_zero_and_range(self):
        self.assertEqual(quantize_coordinate("1.2345675", latitude=True), "1.234568")
        self.assertEqual(quantize_coordinate("-1.2345675", latitude=True), "-1.234568")
        self.assertEqual(quantize_coordinate("-0.0000004", latitude=True), "0.000000")
        self.assertEqual(quantize_coordinate("180", latitude=False), "180.000000")
        for raw, latitude in (("NaN", True), ("Infinity", False), ("91", True), ("181", False), ("bad", True)):
            with self.subTest(raw=raw), self.assertRaises(ContractError):
                quantize_coordinate(raw, latitude=latitude)

    def test_reference_haversine_rounding_and_threshold_boundary(self):
        self.assertEqual(haversine_miles(0, 0, 0, 0), 0.0)
        self.assertAlmostEqual(haversine_miles(0, 0, 1, 0), 69.0934, places=3)
        radius = 3958.7613
        for miles, expected in ((19.95, False), (20.0, True), (20.05, True)):
            latitude = math.degrees(miles / radius)
            raw = haversine_miles(0, 0, latitude, 0)
            self.assertEqual(raw >= 20.0, expected)
            if miles == 19.95:
                self.assertEqual(public_one_decimal(raw), "20.0")

    def test_equal_nearest_tie_is_deterministic(self):
        p2 = provider(make_npi("100000002"), "0.000000", "1.000000")
        p1 = provider(make_npi("100000001"), "0.000000", "1.000000")
        result = nearest_provider("0.000000", "0.000000", [p2, p1])
        self.assertEqual(result.npi, min(p1.npi, p2.npi))
        self.assertEqual(conventional_median([3.0, 1.0, 2.0]), 2.0)
        self.assertEqual(conventional_median([4.0, 1.0, 3.0, 2.0]), 2.5)


class OfflineIntegrationTests(unittest.TestCase):
    def make_inputs(self):
        direct = nppes_row(make_npi("100000011"))
        hud_provider = nppes_row(
            make_npi("100000012"),
            **{
                "Provider First Line Business Practice Location Address": "PO BOX 55",
                "Provider Second Line Business Practice Location Address": "",
                "Provider Business Practice Location Address Postal Code": "01002",
            },
        )
        excluded = nppes_row(make_npi("100000013"), **{"Entity Type Code": "2"})
        cah = hospital_row(
            "000001",
            **{
                "Address": "300 DISTANT ROAD",
                "City/Town": "SECOND CITY",
                "State": "AL",
                "ZIP Code": "01003",
                "Hospital Type": "Critical Access Hospitals",
            },
        )
        federal = hospital_row(
            "12345F",
            **{
                "Address": "PO BOX 9",
                "ZIP Code": "01002",
            },
        )
        direct_address = normalize_address(
            direct["Provider First Line Business Practice Location Address"],
            direct["Provider Second Line Business Practice Location Address"],
            direct["Provider Business Practice Location Address City Name"],
            direct["Provider Business Practice Location Address State Name"],
            direct["Provider Business Practice Location Address Postal Code"],
            direct["Provider Business Practice Location Address Country Code (If outside U.S.)"],
        )
        cah_address = normalize_address(cah["Address"], "", cah["City/Town"], cah["State"], cah["ZIP Code"], "US")
        hud = parse_candidates(
            [
                {"ZIP": "01002", "COUNTY": "01003", "BUS_RATIO": "0.8", "RES_RATIO": "0.4", "TOT_RATIO": "0.7", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
                {"ZIP": "01002", "COUNTY": "01001", "BUS_RATIO": "0.2", "RES_RATIO": "0.6", "TOT_RATIO": "0.3", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            ],
            {"01001", "01003"},
        )
        places = (
            PlacesCountyRow("01001", "5.04", "2025", "2023", "CHD", "AgeAdjPrv", "%", "4", "6", None),
            PlacesCountyRow("01003", None, "2025", "2023", "CHD", "AgeAdjPrv", "%", None, None, "Population less than 50"),
        )
        return OfflineBuildInputs(
            nppes_rows=(direct, dict(direct), hud_provider, excluded),
            hospital_rows=(cah, federal),
            geocodes=(
                geocode(direct_address.address_id, "01001", "-86.0000004", "32.0000004"),
                geocode(cah_address.address_id, "01003", "-87.500000", "33.500000"),
            ),
            hud_candidates=tuple(hud),
            county_points={"01001": ("32.00000049", "-86.00000049"), "01003": ("33.00000049", "-87.00000049")},
            county_attributes={
                "01001": {"state": "AL", "county": "Synthetic One", "population": 100, "median_hh_income": 50000, "rucc_2023": 2, "hpsa_primary_care": False},
                "01003": {"state": "AL", "county": "Synthetic Two", "population": 50, "median_hh_income": None, "rucc_2023": 4, "hpsa_primary_care": True},
            },
            places_rows=places,
            source_snapshot_id=SNAPSHOT,
            hud_source_snapshot_id=HUD_SNAPSHOT_SHA256,
            gazetteer_source_snapshot_id=GAZETTEER_SNAPSHOT_SHA256,
            method_lineage_id=METHOD_LINEAGE_ID,
        )

    def make_gate_passing_inputs(self):
        inputs = self.make_inputs()
        nppes_rows = [dict(row) for row in inputs.nppes_rows]
        nppes_rows[2]["Provider First Line Business Practice Location Address"] = (
            "200 SECOND STREET"
        )
        nppes_rows[2]["Provider Business Practice Location Address City Name"] = (
            "SECOND CITY"
        )
        hospital_rows = [dict(row) for row in inputs.hospital_rows]
        hospital_rows[1]["Address"] = "400 FEDERAL STREET"
        hospital_rows[1]["City/Town"] = "SECOND CITY"

        provider_address = normalize_address(
            nppes_rows[2]["Provider First Line Business Practice Location Address"],
            nppes_rows[2]["Provider Second Line Business Practice Location Address"],
            nppes_rows[2]["Provider Business Practice Location Address City Name"],
            nppes_rows[2]["Provider Business Practice Location Address State Name"],
            nppes_rows[2]["Provider Business Practice Location Address Postal Code"],
            nppes_rows[2][
                "Provider Business Practice Location Address Country Code (If outside U.S.)"
            ],
        )
        hospital_address = normalize_address(
            hospital_rows[1]["Address"],
            "",
            hospital_rows[1]["City/Town"],
            hospital_rows[1]["State"],
            hospital_rows[1]["ZIP Code"],
            "US",
        )
        return OfflineBuildInputs(
            **{
                **inputs.__dict__,
                "nppes_rows": tuple(nppes_rows),
                "hospital_rows": tuple(hospital_rows),
                "geocodes": inputs.geocodes
                + (
                    geocode(
                        provider_address.address_id,
                        "01003",
                        "-87.000000",
                        "33.000000",
                    ),
                    geocode(
                        hospital_address.address_id,
                        "01003",
                        "-87.100000",
                        "33.100000",
                    ),
                ),
            }
        )

    def test_miniature_pipeline_is_deterministic_and_hash_bound(self):
        inputs = self.make_inputs()
        first = build_offline(inputs)
        second = build_offline(inputs)
        self.assertEqual(first.provider_locations_bytes, second.provider_locations_bytes)
        self.assertEqual(first.hospital_locations_bytes, second.hospital_locations_bytes)
        self.assertEqual(first.counties_json, second.counties_json)
        self.assertEqual(first.summary_json, second.summary_json)
        self.assertEqual(first.provider_hash_used_for_counts, first.provider_locations_sha256)
        self.assertEqual(first.provider_hash_used_for_distances, first.provider_locations_sha256)
        self.assertEqual(first.hospital_hash_used_for_counts, first.hospital_locations_sha256)
        self.assertEqual(first.hospital_hash_used_for_cah_distances, first.hospital_locations_sha256)
        hospital_qa = json.loads(first.hospital_county_name_qa_bytes)
        self.assertEqual(hospital_qa["summary"]["facility_total"], 2)
        self.assertEqual(
            first.hospital_county_name_qa_sha256,
            hashlib.sha256(first.hospital_county_name_qa_bytes).hexdigest(),
        )
        self.assertTrue(
            all("exact_normalized_mismatch" in row for row in hospital_qa["rows"])
        )
        self.assertEqual(first.duplicate_audit.raw_rows, 4)
        self.assertEqual(first.duplicate_audit.identical_duplicate_extra_rows, 1)
        self.assertEqual(first.waterfall.d_geo, 2)
        self.assertEqual(len(first.route_cross_tab), 3)
        self.assertEqual(first.unique_address_counts["combined_direct_unique"], 2)
        self.assertEqual(first.waterfall.terminal_counts["assigned_direct_census"], 1)
        self.assertEqual(first.waterfall.terminal_counts["assigned_hud_fallback"], 1)
        self.assertEqual(first.cah.total_in_scope, 1)
        self.assertEqual(first.cah.evaluable, 1)
        self.assertEqual(first.cah.gte_20, 1)
        self.assertEqual(sum(row["n_cah_20plus_miles_to_nearest_cardiologist"] for row in first.county_rows), 1)
        self.assertEqual(first.summary["counties_json_sha256"], hashlib.sha256(first.counties_json).hexdigest())
        self.assertEqual(first.summary["cah_gte_20_pct_evaluable"], "100.0")
        self.assertEqual(first.county_rows[0]["chd_age_adjusted_prevalence_pct"], "5.0")
        self.assertNotIn(FORBIDDEN_LEGACY_COUNTY_FIELD.encode(), first.counties_json)
        common_provenance = {
            "direct_eligible", "hud_eligible", "census_outcome", "hud_attempted",
            "hud_outcome", "final_disposition", "assignment_method",
            "geocoder_match_indicator", "geocoder_match_type", "hud_ratio_basis",
            "hud_selected_ratio", "hud_bus_ratio", "hud_res_ratio", "hud_tot_ratio",
            "hud_same_state_candidate_count", "hud_second_highest_ratio",
            "hud_bus_margin_to_second",
            "source_snapshot_id",
            "census_source_response_sha256", "hud_source_snapshot_id",
            "gazetteer_source_snapshot_id",
        }
        self.assertTrue(common_provenance.issubset(first.provider_locations[0].__dict__))
        self.assertTrue(common_provenance.issubset(first.hospital_locations[0].__dict__))
        hud_provider = next(
            row for row in first.provider_locations
            if row.final_disposition == "assigned_hud_fallback"
        )
        hud_hospital = next(
            row for row in first.hospital_locations
            if row.final_disposition == "assigned_hud_fallback"
        )
        for row in (hud_provider, hud_hospital):
            self.assertTrue(row.hud_attempted)
            self.assertEqual(row.hud_outcome, "HUD_ASSIGNED")
            self.assertEqual(row.hud_ratio_basis, "BUS_RATIO")
            self.assertGreater(row.hud_same_state_candidate_count, 0)
            self.assertIsNotNone(row.hud_bus_margin_to_second)
            self.assertEqual(row.hud_source_snapshot_id, HUD_SNAPSHOT_SHA256)
            self.assertEqual(
                row.gazetteer_source_snapshot_id, GAZETTEER_SNAPSHOT_SHA256
            )
        with self.assertRaises(IntegrityError):
            provider_table_bytes(
                [replace(hud_provider, hud_selected_ratio="0.7")],
                set(inputs.county_points),
            )
        with self.assertRaises(IntegrityError):
            hospital_table_bytes(
                [replace(hud_hospital, hud_second_highest_ratio="0.1")],
                set(inputs.county_points),
            )

    def test_primary_hud_tie_preserves_selected_second_margin_when_unmatched(self):
        inputs = self.make_inputs()
        tied = tuple(parse_candidates([
            {"ZIP": "01002", "COUNTY": "01001", "BUS_RATIO": "0.5", "RES_RATIO": "0.4", "TOT_RATIO": "0.5", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01002", "COUNTY": "01003", "BUS_RATIO": "0.5", "RES_RATIO": "0.6", "TOT_RATIO": "0.5", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
        ], set(inputs.county_points)))
        result = build_offline(OfflineBuildInputs(**{
            **inputs.__dict__, "hud_candidates": tied,
        }))
        rows = [
            *[row for row in result.provider_locations if row.hud_outcome == "HUD_BUS_TIE"],
            *[row for row in result.hospital_locations if row.hud_outcome == "HUD_BUS_TIE"],
        ]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertFalse(row.assigned)
            self.assertEqual(row.hud_selected_ratio, "0.5")
            self.assertEqual(row.hud_second_highest_ratio, "0.5")
            self.assertEqual(row.hud_bus_margin_to_second, "0")
            self.assertEqual(row.hud_same_state_candidate_count, 2)

    def test_geocoder_set_drift_and_attribute_coercion_fail(self):
        inputs = self.make_inputs()
        with self.assertRaises(IntegrityError):
            build_offline(OfflineBuildInputs(**{**inputs.__dict__, "geocodes": inputs.geocodes[:-1]}))
        extra = CanonicalGeocode(
            "A-extra",
            "No_Match",
            "No_Match",
            "",
            "",
            None,
            None,
            None,
            1,
            GEOCODER_RESPONSE_SHA256,
        )
        with self.assertRaises(IntegrityError):
            build_offline(OfflineBuildInputs(**{**inputs.__dict__, "geocodes": inputs.geocodes + (extra,)}))
        for field, bad in (("population", 4.5), ("rucc_2023", 4.5), ("median_hh_income", 4.5), ("hpsa_primary_care", 1)):
            attributes = copy.deepcopy(inputs.county_attributes)
            attributes["01001"][field] = bad
            with self.subTest(field=field), self.assertRaises(IntegrityError):
                build_offline(OfflineBuildInputs(**{**inputs.__dict__, "county_attributes": attributes}))

    def test_phase2f_review_outputs_bind_all_sensitivities_samples_and_qa(self):
        inputs = self.make_gate_passing_inputs()
        build = build_offline(inputs)
        universe = set(inputs.county_points)
        adjacency_rows = (
            {"primary_fips": "01001", "neighbor_fips": ["01003"]},
            {"primary_fips": "01003", "neighbor_fips": ["01001"]},
        )
        adjacency = parse_adjacency_snapshot(adjacency_rows, universe)
        empty_border_hash = hashlib.sha256(sorted_fips_bytes(())).hexdigest()
        adjacency_receipt = build_adjacency_receipt(
            adjacency,
            universe,
            source_id="CENSUS-COUNTY-ADJ-2025",
            source_sha256=ADJACENCY_SOURCE_SHA256,
            expected_source_sha256=ADJACENCY_SOURCE_SHA256,
            expected_border_count=0,
            expected_border_sha256=empty_border_hash,
        )
        point_setup = build_point_qa_setup(
            PolygonSource(
                "CENSUS-TIGER-COUNTY-2025",
                "FULL_RESOLUTION",
                frozenset(universe),
                TIGER_SOURCE_SHA256,
            ),
            GeometryEngineIdentity(
                "synthetic-geometry",
                "1.0.0",
                GEOMETRY_ENGINE_SHA256,
                "covers",
            ),
            universe,
        )
        review = build_phase2f_review_artifacts(
            inputs,
            build,
            independent_reviewer="Independent Synthetic Reviewer",
            adjacency_receipt=adjacency_receipt,
            state_borders=frozenset(),
            point_qa_setup=point_setup,
        )
        self.assertEqual(
            set(review.sensitivity.table_bytes),
            {
                "PRIMARY",
                "DIRECT_ONLY",
                "HUD_RES_SAME_STATE",
                "HUD_TOT_SAME_STATE",
                "HUD_BUS_STATE_UNRESTRICTED",
            },
        )
        self.assertEqual(
            review.sensitivity.table_sha256["PRIMARY"],
            hashlib.sha256(review.sensitivity.table_bytes["PRIMARY"]).hexdigest(),
        )
        receipt = json.loads(review.validation_receipt_bytes)
        self.assertEqual(receipt["human_review_status"], "REQUIRED_BEFORE_GATE_B")
        self.assertEqual(receipt["provider_sample"]["actual"], 2)
        self.assertEqual(receipt["hospital_sample"]["actual"], 2)
        self.assertEqual(receipt["double_review_kappa_threshold"], {"numerator": 4, "denominator": 5})
        self.assertGreater(len(review.validation.sampling_frame_bytes), 0)
        self.assertGreater(len(review.validation.blank_worksheet_bytes), 0)
        with self.assertRaises(IntegrityError):
            parse_adjacency_snapshot(adjacency_rows[:1], universe)

    def test_named_migration_denylist_rejects_legacy_key_and_universe_drift(self):
        result = build_offline(self.make_inputs())
        rows = [dict(row) for row in result.county_rows]
        rows[0][FORBIDDEN_LEGACY_COUNTY_FIELD] = 100
        with self.assertRaises(IntegrityError):
            public_counties_bytes(rows, {"01001", "01003"})
        with self.assertRaises(IntegrityError):
            public_counties_bytes(result.county_rows, {"01001"})

    def test_release_entrypoint_requires_frozen_complete_preflight(self):
        inputs = self.make_gate_passing_inputs()
        places_bytes = canonical_places_snapshot_bytes(inputs.places_rows)
        frozen_inputs = (
            FrozenInput(
                "SYNTHETIC-A", "fixture", "https://example.test/a.bin", "a.bin",
                1,
                FROZEN_INPUT_SHA256,
                ("example.test",),
                "BINARY",
                ("synthetic-binary",),
            ),
        )
        frozen_hashes = {"SYNTHETIC-A": FROZEN_INPUT_SHA256}
        manifest_sha = hashlib.sha256(canonical_manifest_bytes(list(frozen_inputs))).hexdigest()
        inputs = OfflineBuildInputs(**{**inputs.__dict__, "source_snapshot_id": manifest_sha})
        expectations = BuildExpectations(
            county_count=2,
            population_total=150,
            places_county_rows=2,
            places_nonnull=1,
            places_public_nulls=1,
            places_snapshot_sha256=hashlib.sha256(places_bytes).hexdigest(),
            input_manifest_sha256=manifest_sha,
            required_input_hashes=frozen_hashes,
            method_lineage_id=METHOD_LINEAGE_ID,
            places_suppressed_fips="01003",
        )
        result = build_release(
            inputs,
            expectations,
            places_snapshot_bytes=places_bytes,
            frozen_inputs=frozen_inputs,
        )
        self.assertEqual(len(result.county_rows), 2)
        self.assertEqual(len(result.quality_gates), 3)
        self.assertEqual(len(result.geography_diagnostics), 5)
        self.assertTrue(all(gate.passed for gate in result.quality_gates))
        self.assertTrue(
            all(type(gate.numerator) is int for gate in result.quality_gates)
        )
        for changed in (
            BuildExpectations(**{**expectations.__dict__, "county_count": 3}),
            BuildExpectations(**{**expectations.__dict__, "places_snapshot_sha256": "PENDING"}),
            BuildExpectations(**{**expectations.__dict__, "population_total": 151}),
        ):
            with self.assertRaises(IntegrityError):
                build_release(
                    inputs,
                    changed,
                    places_snapshot_bytes=places_bytes,
                    frozen_inputs=frozen_inputs,
                )
        drifted = (
            FrozenInput(
                "SYNTHETIC-A", "fixture", "https://example.test/drift.bin", "a.bin",
                1,
                FROZEN_INPUT_SHA256,
                ("example.test",),
                "BINARY",
                ("synthetic-binary",),
            ),
        )
        with self.assertRaises(IntegrityError):
            build_release(
                inputs,
                expectations,
                places_snapshot_bytes=places_bytes,
                frozen_inputs=drifted,
            )

        failing_inputs = OfflineBuildInputs(
            **{
                **self.make_inputs().__dict__,
                "hud_candidates": (),
                "source_snapshot_id": manifest_sha,
            }
        )
        with self.assertRaises(ReleaseQualityGateError) as failure:
            build_release(
                failing_inputs,
                expectations,
                places_snapshot_bytes=places_bytes,
                frozen_inputs=frozen_inputs,
            )
        self.assertEqual(len(failure.exception.gates), 3)
        self.assertTrue(any(not gate.passed for gate in failure.exception.gates))
        self.assertIsNotNone(failure.exception.artifacts)
        self.assertIn("receipts:", str(failure.exception))

    def test_gate_b_audits_and_atomic_rejection_preserve_complete_failure_state(self):
        inputs = self.make_inputs()
        artifacts = build_offline(inputs)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = _write_gate_b_audit_artifacts(root / "audit", inputs, artifacts)
            self.assertEqual(
                set(paths),
                {
                    "waterfall_audit", "duplicate_audit", "route_cross_tab",
                    "provider_route_cross_tab", "hospital_route_cross_tab",
                    "unique_address_counts", "geocoder_report",
                    "geography_diagnostics",
                    "cah_coordinate_method_breakdown",
                },
            )
            waterfall = json.loads(paths["waterfall_audit"].read_bytes())
            self.assertEqual(waterfall["terminal_counts"], artifacts.waterfall.terminal_counts)
            self.assertEqual(waterfall["diagnostic_counts"], artifacts.waterfall.diagnostic_counts)
            diagnostics = json.loads(paths["geography_diagnostics"].read_bytes())
            self.assertEqual(
                {row["name"] for row in diagnostics["diagnostics"]},
                {row.name for row in artifacts.geography_diagnostics},
            )
            self.assertTrue(
                all(
                    not ({"passed", "operator", "threshold_numerator"} & set(row))
                    for row in diagnostics["diagnostics"]
                )
            )
            provider_routes = json.loads(paths["provider_route_cross_tab"].read_bytes())
            hospital_routes = json.loads(paths["hospital_route_cross_tab"].read_bytes())
            self.assertEqual(
                sum(row["count"] for row in provider_routes["cross_tab"]),
                len(artifacts.route_cross_tab),
            )
            self.assertEqual(
                sum(row["count"] for row in hospital_routes["cross_tab"]),
                len(artifacts.hospital_locations),
            )
            self.assertEqual(
                set(provider_routes["cross_tab"][0]),
                {
                    "direct_eligible", "census_outcome", "hud_outcome",
                    "final_disposition", "count",
                },
            )
            cah = json.loads(paths["cah_coordinate_method_breakdown"].read_bytes())
            self.assertEqual(len(cah["facilities"]), artifacts.cah.total_in_scope)
            self.assertEqual(
                {row["facility_id"] for row in cah["facilities"]},
                {
                    row.facility_id
                    for row in artifacts.hospital_locations
                    if row.is_cah
                },
            )

            places_bytes = canonical_places_snapshot_bytes(inputs.places_rows)
            frozen_inputs = (
                FrozenInput(
                    "SYNTHETIC-A", "fixture", "https://example.test/a.bin", "a.bin",
                    1, hashlib.sha256(b"synthetic input").hexdigest(),
                    ("example.test",), "BINARY", ("synthetic-binary",),
                ),
            )
            manifest_sha = hashlib.sha256(
                canonical_manifest_bytes(list(frozen_inputs))
            ).hexdigest()
            failing_inputs = OfflineBuildInputs(
                **{
                    **inputs.__dict__,
                    "hud_candidates": (),
                    "source_snapshot_id": manifest_sha,
                }
            )
            expectations = BuildExpectations(
                county_count=2,
                population_total=150,
                places_county_rows=2,
                places_nonnull=1,
                places_public_nulls=1,
                places_snapshot_sha256=hashlib.sha256(places_bytes).hexdigest(),
                input_manifest_sha256=manifest_sha,
                required_input_hashes={"SYNTHETIC-A": frozen_inputs[0].sha256},
                method_lineage_id=METHOD_LINEAGE_ID,
                places_suppressed_fips="01003",
            )
            with self.assertRaises(ReleaseQualityGateError) as failure:
                build_release(
                    failing_inputs,
                    expectations,
                    places_snapshot_bytes=places_bytes,
                    frozen_inputs=frozen_inputs,
                )
            output = root / "rejected-run"
            output.mkdir()
            source_tree_id = hashlib.sha256(b"synthetic source tree manifest").hexdigest()
            bundle = FrozenBundle(
                root=root,
                phase="2F",
                authorization_id="synthetic-authorization",
                approved_at_utc="2026-07-10T00:00:00Z",
                source_tree_id=source_tree_id,
                expectations={},
                inputs={},
                manifest_bytes=b"{}\n",
                manifest_sha256=hashlib.sha256(b"{}\n").hexdigest(),
            )
            receipt = _persist_gate_rejection(
                output,
                bundle=bundle,
                inputs=failing_inputs,
                failure=failure.exception,
                completed_at_utc="2026-07-10T00:00:00Z",
            )
            rejection = json.loads((output / "rejected/rejection_reason.json").read_bytes())
            self.assertEqual(rejection["status"], "REJECTED")
            self.assertTrue(any(not row["passed"] for row in rejection["quality_gates"]))
            verify_output_receipt(
                receipt,
                bundle=bundle,
                artifact_root=output,
                required_artifacts=(
                    "waterfall_audit", "duplicate_audit", "route_cross_tab",
                    "provider_route_cross_tab", "hospital_route_cross_tab",
                    "unique_address_counts", "geocoder_report", "geography_diagnostics",
                    "cah_coordinate_method_breakdown", "rejection_reason",
                ),
                expected_command="build-offline-rejected",
            )


if __name__ == "__main__":
    unittest.main()
