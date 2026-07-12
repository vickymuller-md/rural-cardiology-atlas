from __future__ import annotations

import hashlib
import unittest

from atlas_pipeline.errors import ContractError, IntegrityError
from atlas_pipeline.geoqa import (
    BoundPointLocator,
    GeometryEngineIdentity,
    PolygonSource,
    build_adjacency_receipt,
    build_point_qa_setup,
    derive_state_border_counties,
    point_county_qa_code,
    sorted_fips_bytes,
)
from atlas_pipeline.source_tables import (
    parse_acs_income,
    parse_acs_population,
    parse_gazetteer,
    parse_hrsa_hpsa,
    parse_rucc,
)


class StaticSourceTableTests(unittest.TestCase):
    def setUp(self):
        self.universe = {"01001", "01003"}

    def test_gazetteer_internal_points_and_scope(self):
        rows = [
            {"USPS": "AL", "GEOID": "01001", "NAME": "One", "INTPTLAT": "32.1", "INTPTLONG": "-86.1"},
            {"USPS": "AL", "GEOID": "01003", "NAME": "Two", "INTPTLAT": "33.2", "INTPTLONG": "-87.2"},
            {"USPS": "PR", "GEOID": "72001", "NAME": "Out", "INTPTLAT": "18", "INTPTLONG": "-66"},
        ]
        parsed = parse_gazetteer(rows, expected_count=2)
        self.assertEqual(parsed["01001"], ("32.100000", "-86.100000"))
        with self.assertRaises(ContractError):
            parse_gazetteer([dict(rows[0], USPS="AL", GEOID="02001")], expected_count=1)
        with self.assertRaises(ContractError):
            parse_gazetteer([*rows, dict(rows[0], USPS="ZZ")], expected_count=2)

    def test_acs_population_income_rucc(self):
        population = [
            {"GEO_ID": "0500000US01001", "B01003_E001": "100"},
            {"GEO_ID": "0500000US01003", "B01003_E001": "50"},
            {"GEO_ID": "0500000US72001", "B01003_E001": "10"},
        ]
        self.assertEqual(sum(parse_acs_population(population, self.universe, expected_total=150).values()), 150)
        income = [
            {"GEO_ID": "0500000US01001", "B19013_E001": "50000"},
            {"GEO_ID": "0500000US01003", "B19013_E001": "-666666666"},
            {"GEO_ID": "0500000US72001", "B19013_E001": "100"},
        ]
        self.assertIsNone(parse_acs_income(income, self.universe)["01003"])
        rucc = [
            {"FIPS": fips, "State": state, "County_Name": name, "Attribute": attribute, "Value": value}
            for fips, state, name, code in (
                ("01001", "AL", "One", "3"),
                ("01003", "AL", "Two", "4"),
                ("72001", "PR", "Out", "5"),
            )
            for attribute, value in (
                ("Population_2020", "100"),
                ("RUCC_2023", code),
                ("Description", "Synthetic"),
            )
        ]
        rucc.extend(
            {
                "FIPS": fips,
                "State": "AS",
                "County_Name": name,
                "Attribute": attribute,
                "Value": value,
            }
            for fips, name in (("60030", "Rose Island"), ("60040", "Swains Island"))
            for attribute, value in (
                ("Population_2020", "0"),
                ("Description", "Out-of-scope territorial row without RUCC code"),
            )
        )
        self.assertEqual(parse_rucc(rucc, self.universe), {"01001": 3, "01003": 4})
        with self.assertRaises(ContractError):
            parse_rucc(
                [
                    dict(row, Value="3.0")
                    if row["FIPS"] == "01001" and row["Attribute"] == "RUCC_2023"
                    else row
                    for row in rucc
                ],
                self.universe,
            )
        with self.assertRaises(IntegrityError):
            parse_rucc(
                [row for row in rucc if not (
                    row["FIPS"] == "01003" and row["Attribute"] == "Description"
                )],
                self.universe,
            )
        with self.assertRaises(IntegrityError):
            parse_rucc([*rucc, dict(rucc[0])], self.universe)
        with self.assertRaises(IntegrityError):
            parse_acs_population(population[:-1], self.universe, expected_total=100)

    def test_hrsa_status_and_fips_audit(self):
        rows = [
            {"Common State County FIPS Code": "01001", "HPSA Status": " Designated "},
            {"Common State County FIPS Code": "01001", "HPSA Status": "PROPOSED   FOR WITHDRAWAL"},
            {"Common State County FIPS Code": "01003", "HPSA Status": "Withdrawn"},
            {"Common State County FIPS Code": "bad", "HPSA Status": "Designated"},
            {"Common State County FIPS Code": "72001", "HPSA Status": "Designated"},
        ]
        flags, audit = parse_hrsa_hpsa(rows, self.universe)
        self.assertEqual(flags, {"01001": True, "01003": False})
        self.assertEqual((audit.malformed_fips, audit.out_of_scope_fips, audit.duplicate_designations), (1, 1, 1))
        self.assertEqual(audit.qualifying_retained_rows, 2)
        with self.assertRaises(ContractError):
            parse_hrsa_hpsa([], self.universe)
        zero_flags, zero_audit = parse_hrsa_hpsa(
            [{"Common State County FIPS Code": "01003", "HPSA Status": "Withdrawn"}],
            self.universe,
        )
        self.assertEqual(zero_flags, {"01001": False, "01003": False})
        self.assertEqual((zero_audit.retained_rows, zero_audit.qualifying_retained_rows), (1, 0))


class GeographicQaTests(unittest.TestCase):
    def test_adjacency_cross_state_includes_any_listed_connection(self):
        universe = {"01001", "01003", "02001"}
        adjacency = {"01001": ["01003", "02001"], "01003": ["01001"], "02001": ["01001"]}
        expected = {"01001", "02001"}
        digest = hashlib.sha256(sorted_fips_bytes(expected)).hexdigest()
        self.assertEqual(derive_state_border_counties(adjacency, universe, expected_count=2, expected_sha256=digest), expected)
        receipt = build_adjacency_receipt(
            adjacency,
            universe,
            source_id="CENSUS-COUNTY-ADJ-2025",
            source_sha256=hashlib.sha256(b"synthetic-adjacency-source").hexdigest(),
            expected_source_sha256=hashlib.sha256(b"synthetic-adjacency-source").hexdigest(),
            expected_border_count=2,
            expected_border_sha256=digest,
        )
        self.assertEqual(receipt.sha256, hashlib.sha256(receipt.canonical_bytes).hexdigest())
        with self.assertRaises(IntegrityError):
            build_adjacency_receipt(
                adjacency,
                universe,
                source_id="CENSUS-COUNTY-ADJ-2025",
                source_sha256=hashlib.sha256(b"drifted-adjacency-source").hexdigest(),
                expected_source_sha256=hashlib.sha256(b"synthetic-adjacency-source").hexdigest(),
                expected_border_count=2,
                expected_border_sha256=digest,
            )
        with self.assertRaises(IntegrityError):
            derive_state_border_counties({"01001": []}, universe, expected_count=0, expected_sha256=hashlib.sha256(b"").hexdigest())

    def test_point_qa_requires_full_resolution_not_display_geometry(self):
        universe = {"01001"}
        source = PolygonSource(
            "CENSUS-TIGER-COUNTY-2025",
            "FULL_RESOLUTION",
            frozenset(universe),
            hashlib.sha256(b"synthetic-full-resolution-polygons").hexdigest(),
        )
        engine = GeometryEngineIdentity(
            "synthetic-geometry-engine",
            "1.0.0",
            hashlib.sha256(b"synthetic-geometry-engine-1.0.0").hexdigest(),
            "inside-boundary-outside",
        )
        setup = build_point_qa_setup(source, engine, universe)
        self.assertEqual(setup.sha256, hashlib.sha256(setup.canonical_bytes).hexdigest())
        for relation, expected in (
            ("inside", "POINT_INSIDE_ASSIGNED_COUNTY"),
            ("boundary", "POINT_ON_ASSIGNED_COUNTY_BOUNDARY"),
            ("outside", "POINT_OUTSIDE_ASSIGNED_COUNTY"),
        ):
            locator = BoundPointLocator(engine, lambda *_args, r=relation: r)
            code = point_county_qa_code(
                -86, 32, "01001", universe, source, locator, engine
            )
            self.assertEqual(code, expected)
        display = PolygonSource(
            "CENSUS-CB-COUNTY-2025-5M",
            "1:5000000",
            frozenset(universe),
            hashlib.sha256(b"synthetic-display-polygons").hexdigest(),
        )
        with self.assertRaises(ContractError):
            point_county_qa_code(
                -86,
                32,
                "01001",
                universe,
                display,
                BoundPointLocator(engine, lambda *_: "inside"),
                engine,
            )
        drifted_engine = GeometryEngineIdentity(
            "synthetic-geometry-engine",
            "1.0.1",
            hashlib.sha256(b"synthetic-geometry-engine-1.0.1").hexdigest(),
            "inside-boundary-outside",
        )
        with self.assertRaises(IntegrityError):
            point_county_qa_code(
                -86,
                32,
                "01001",
                universe,
                source,
                BoundPointLocator(drifted_engine, lambda *_: "inside"),
                engine,
            )


if __name__ == "__main__":
    unittest.main()
