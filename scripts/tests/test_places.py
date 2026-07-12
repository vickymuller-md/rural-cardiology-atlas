from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from atlas_pipeline.errors import ContractError, IntegrityError
from atlas_pipeline.places import (
    PLACES_ENDPOINT,
    canonical_places_query_url,
    canonical_places_snapshot_bytes,
    canonicalize_places,
)

FIXTURE = Path(__file__).parent / "fixtures" / "places_small.csv"


class PlacesTests(unittest.TestCase):
    def setUp(self):
        self.raw = FIXTURE.read_bytes()
        self.universe = {"01001", "01003", "01005"}

    def canonicalize(self, raw=None, **overrides):
        options = {
            "expected_raw_sha256": hashlib.sha256(self.raw if raw is None else raw).hexdigest(),
            "expected_county_rows": 2,
            "expected_nonnull": 1,
            "expected_suppressed_fips": "01003",
            "expected_public_nulls": 2,
        }
        options.update(overrides)
        return canonicalize_places(raw if raw is not None else self.raw, self.universe, **options)

    def test_semantic_contract_national_exclusion_and_suppression(self):
        rows = self.canonicalize()
        self.assertEqual([row.fips for row in rows], ["01001", "01003"])
        self.assertEqual(rows[0].chd_age_adjusted_prevalence_pct, "5.0")
        self.assertIsNone(rows[1].chd_age_adjusted_prevalence_pct)
        snapshot = canonical_places_snapshot_bytes(rows)
        self.assertTrue(snapshot.endswith(b"\n"))
        self.assertNotIn(b'"fips":"00059"', snapshot)
        query_url = canonical_places_query_url()
        self.assertTrue(query_url.startswith(PLACES_ENDPOINT + "?%24select="))
        self.assertIn("measureid%3D%22CHD%22%20AND%20datavaluetypeid%3D%22AgeAdjPrv%22", query_url)
        self.assertTrue(query_url.endswith("%24limit=5000"))

    def test_hash_empty_schema_and_semantic_drift_fail_closed(self):
        with self.assertRaises(IntegrityError):
            canonicalize_places(
                self.raw,
                self.universe,
                expected_raw_sha256=hashlib.sha256(b"unexpected-places-source").hexdigest(),
                expected_county_rows=2,
                expected_nonnull=1,
                expected_suppressed_fips="01003",
                expected_public_nulls=2,
            )
        with self.assertRaises(ContractError):
            self.canonicalize(b"")
        for old, new in (
            (b"AgeAdjPrv", b"CrudePrv"),
            (b",%,Age-adjusted", b",rate,Age-adjusted"),
            (b"Coronary heart disease among adults", b"Heart disease mortality"),
            (b",2023,BRFSS", b",2022,BRFSS"),
            (b",2023,BRFSS", b",2023,PLACES"),
        ):
            changed = self.raw.replace(old, new)
            with self.subTest(change=(old, new)), self.assertRaises(ContractError):
                self.canonicalize(changed)

    def test_duplicate_outside_universe_wrong_count_and_missing_national_fail(self):
        header, national, county1, county2 = self.raw.splitlines(keepends=True)
        with self.assertRaises(IntegrityError):
            self.canonicalize(header + national + county1 + county1 + county2, expected_county_rows=3, expected_nonnull=2)
        outside = county1.replace(b"01001", b"72001", 1)
        with self.assertRaises(ContractError):
            self.canonicalize(header + national + outside + county2)
        with self.assertRaises(ContractError):
            self.canonicalize(header + county1 + county2)
        with self.assertRaises(ContractError):
            self.canonicalize(self.raw, expected_county_rows=3)

    def test_percentage_range_and_expected_footnote(self):
        changed = self.raw.replace(b",5.0,4.0,6.0", b",101.0,4.0,6.0")
        with self.assertRaises(ContractError):
            self.canonicalize(changed)
        changed = self.raw.replace(b"Population less than 50", b"Other reason")
        with self.assertRaises(ContractError):
            self.canonicalize(changed)


if __name__ == "__main__":
    unittest.main()
