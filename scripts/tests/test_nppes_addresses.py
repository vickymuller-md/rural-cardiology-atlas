from __future__ import annotations

import unittest

from atlas_pipeline.addresses import (
    canonical_address_bytes,
    line_kind,
    make_address_id,
    normalize_address,
    normalize_zip5,
)
from atlas_pipeline.contracts import ADULT_CARDIOLOGY_TAXONOMIES, KNOWN_INELIGIBLE_CODES
from atlas_pipeline.errors import IntegrityError
from atlas_pipeline.nppes import (
    GeoDisposition,
    audit_dispositions,
    classify_disposition,
    classify_primary_taxonomy,
    classify_status,
    deduplicate_rows,
    route_cross_tab_row,
    valid_npi,
)
from tests.synthetic import make_npi, nppes_row


class NpiContractTests(unittest.TestCase):
    def test_luhn_and_leading_digits(self):
        valid = make_npi("000000001")
        self.assertEqual(len(valid), 10)
        self.assertTrue(valid_npi(valid))
        self.assertTrue(valid_npi("1234567893"))
        self.assertFalse(valid_npi("1234567894"))
        self.assertFalse(valid_npi("123456789"))
        self.assertFalse(valid_npi("１２３４５６７８９３"))

    def test_each_allowlisted_taxonomy_is_eligible(self):
        for code in ADULT_CARDIOLOGY_TAXONOMIES:
            with self.subTest(code=code):
                result = classify_primary_taxonomy(
                    nppes_row(**{"Healthcare Provider Taxonomy Code_1": f" {code.lower()} "})
                )
                self.assertEqual(result.classification, "eligible")
                self.assertEqual(result.code, code)

    def test_wrong_pediatric_and_arbitrary_codes_are_rejected(self):
        for code in (*sorted(KNOWN_INELIGIBLE_CODES), "999999999X"):
            with self.subTest(code=code):
                result = classify_primary_taxonomy(
                    nppes_row(**{"Healthcare Provider Taxonomy Code_1": code})
                )
                self.assertEqual(result.classification, "primary_taxonomy_not_allowlisted")

    def test_primary_edge_cases(self):
        cases = [
            ({"Healthcare Provider Primary Taxonomy Switch_1": ""}, "primary_flag_malformed"),
            ({"Healthcare Provider Taxonomy Code_1": "", "Healthcare Provider Primary Taxonomy Switch_1": "Y"}, "primary_flag_malformed"),
            ({"Healthcare Provider Primary Taxonomy Switch_1": "M"}, "primary_flag_malformed"),
            ({"Healthcare Provider Primary Taxonomy Switch_1": "N"}, "no_primary_taxonomy"),
            ({"Healthcare Provider Primary Taxonomy Switch_1": "N", "Healthcare Provider Taxonomy Code_2": "207RA0001X", "Healthcare Provider Primary Taxonomy Switch_2": "N"}, "no_primary_taxonomy"),
            ({"Healthcare Provider Taxonomy Code_2": "207RA0001X", "Healthcare Provider Primary Taxonomy Switch_2": "Y"}, "multiple_primary_taxonomies"),
        ]
        for override, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_primary_taxonomy(nppes_row(**override)).classification, expected)

    def test_status_table_and_incoherent_dates(self):
        cases = [
            ({}, ("active_never_deactivated", True)),
            ({"NPI Deactivation Date": "05/01/2026"}, ("inactive_deactivated", False)),
            ({"NPI Deactivation Date": "05/01/2026", "NPI Reactivation Date": "05/02/2026"}, ("active_reactivated", True)),
            ({"NPI Reactivation Date": "05/02/2026"}, ("status_inconsistent", False)),
            ({"NPI Deactivation Date": "06/09/2026"}, ("status_inconsistent", False)),
            ({"NPI Deactivation Date": "05/01/2026", "NPI Reactivation Date": "05/01/2026"}, ("status_inconsistent", False)),
            ({"NPI Deactivation Date": "05/02/2026", "NPI Reactivation Date": "05/01/2026"}, ("status_inconsistent", False)),
            ({"NPI Deactivation Date": "bogus"}, ("status_inconsistent", False)),
            ({"NPI Deactivation Date": "1/02/2026"}, ("status_inconsistent", False)),
            ({"NPI Deactivation Date": " 01/02/2026"}, ("status_inconsistent", False)),
            ({"NPI Deactivation Date": "05/01/2026", "NPI Reactivation Date": "06/09/2026"}, ("status_inconsistent", False)),
        ]
        for override, expected in cases:
            with self.subTest(override=override):
                result = classify_status(nppes_row(**override))
                self.assertEqual((result.classification, result.active), expected)

    def test_identical_duplicate_reconciles_and_conflict_fails(self):
        row = nppes_row()
        unique, audit = deduplicate_rows([row, dict(row)])
        self.assertEqual(len(unique), 1)
        self.assertEqual(audit.raw_rows, audit.retained_unique_npis + audit.identical_duplicate_extra_rows)
        conflict = dict(row)
        conflict["Last Update Date"] = "06/02/2026"
        with self.assertRaises(IntegrityError):
            deduplicate_rows([row, conflict])
        with self.assertRaises(IntegrityError):
            deduplicate_rows([nppes_row(NPI="1234567894")])
        missing = dict(row)
        del missing["NPI Deactivation Reason Code"]
        with self.assertRaises(IntegrityError):
            deduplicate_rows([missing])
        nonstring = dict(row)
        nonstring["NPI Deactivation Reason Code"] = None
        with self.assertRaises(IntegrityError):
            deduplicate_rows([nonstring])

    def test_fixed_waterfall_and_nonexclusive_diagnostics(self):
        rows_and_expected = [
            (nppes_row(**{"Entity Type Code": "2", "Healthcare Provider Primary Taxonomy Switch_1": ""}), "entity_type_not_individual"),
            (nppes_row(**{"Entity Type Code": " 1"}), "entity_type_not_individual"),
            (nppes_row(**{"Healthcare Provider Primary Taxonomy Switch_1": ""}), "primary_flag_malformed"),
            (nppes_row(**{"Healthcare Provider Primary Taxonomy Switch_1": "N"}), "no_primary_taxonomy"),
            (nppes_row(**{"Healthcare Provider Taxonomy Code_2": "207RA0001X", "Healthcare Provider Primary Taxonomy Switch_2": "Y"}), "multiple_primary_taxonomies"),
            (nppes_row(**{"Healthcare Provider Taxonomy Code_1": "207RA0201X"}), "primary_taxonomy_not_allowlisted"),
            (nppes_row(**{"NPI Reactivation Date": "05/01/2026"}), "status_inconsistent"),
            (nppes_row(**{"NPI Deactivation Date": "05/01/2026"}), "inactive_deactivated"),
            (nppes_row(**{"Provider Business Practice Location Address Country Code (If outside U.S.)": "CA"}), "country_out_of_scope"),
            (nppes_row(**{"Provider Business Practice Location Address State Name": "PR"}), "state_out_of_scope_or_invalid"),
        ]
        dispositions = []
        for index, (row, expected) in enumerate(rows_and_expected):
            row["NPI"] = make_npi(f"{100000100 + index:09d}")
            result = classify_disposition(row)
            dispositions.append(result)
            self.assertEqual(result.bucket, expected)
        for offset, final in enumerate(("assigned_direct_census", "assigned_hud_fallback", "final_unmatched")):
            row = nppes_row(make_npi(f"{100000200 + offset:09d}"))
            geo = GeoDisposition(final, "Match" if offset == 0 else "No_Match", "HUD_ASSIGNED" if offset == 1 else None)
            result = classify_disposition(row, geo)
            dispositions.append(result)
            self.assertIn("entity_type_individual", result.diagnostics)
            self.assertIn("primary_taxonomy_allowlisted", result.diagnostics)
            self.assertIn("zip_valid", result.diagnostics)
            self.assertIn("direct_eligible", result.diagnostics)
            tab = route_cross_tab_row(result, geo)
            self.assertEqual(tab.final_disposition, final)
        audit = audit_dispositions(dispositions)
        self.assertEqual(sum(audit.terminal_counts.values()), len(dispositions))
        self.assertEqual(audit.d_geo, 3)
        self.assertEqual(audit.counted_providers, 2)

    def test_diagnostics_remain_nonexclusive_across_waterfall_precedence(self):
        row = nppes_row(
            **{
                "Healthcare Provider Taxonomy Code_2": "207RA0201X",
                "Healthcare Provider Primary Taxonomy Switch_2": "Y",
                "Provider First Line Business Practice Location Address": "PO BOX 7",
                "Provider Second Line Business Practice Location Address": "",
                "Provider Business Practice Location Address City Name": "",
                "Provider Business Practice Location Address State Name": "PR",
                "Provider Business Practice Location Address Postal Code": "bad",
                "Provider Business Practice Location Address Country Code (If outside U.S.)": "CA",
            }
        )
        result = classify_disposition(row)
        self.assertEqual(result.bucket, "multiple_primary_taxonomies")
        for flag in (
            "primary_allowlisted_code_present",
            "primary_ineligible_code_present",
            "foreign_country",
            "invalid_or_out_of_scope_state",
            "missing_city",
            "po_box_only",
            "zip_invalid",
            "direct_ineligible",
            "hud_ineligible",
        ):
            self.assertIn(flag, result.diagnostics)


class AddressContractTests(unittest.TestCase):
    def test_zip_syntax_without_coercion(self):
        accepted = {"01234": "01234", "012345678": "01234", "01234-5678": "01234"}
        for raw, expected in accepted.items():
            self.assertEqual(normalize_zip5(raw), expected)
        for raw in ("00000", "1234", "123456", "12345 6789", "1234A", "12345 extra", 1234, None):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_zip5(raw))

    def test_all_mail_only_variants(self):
        values = (
            "P.O. Box 12", "PO BOX #12-A", "POST OFFICE BOX 12", "BOX 123",
            "RR 2", "RURAL ROUTE 2 BOX 3", "HC 1 BOX 2", "HIGHWAY CONTRACT ROUTE 1 BOX 2",
            "STAR ROUTE 9 BOX 3", "GENERAL DELIVERY", "PMB 9", "PRIVATE MAILBOX A-2",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(line_kind(value), "mail_only")

    def test_all_auxiliary_only_prefixes(self):
        for prefix in ("APT", "APARTMENT", "STE", "SUITE", "UNIT", "RM", "ROOM", "FL", "FLOOR", "DEPT", "DEPARTMENT"):
            self.assertEqual(line_kind(f"{prefix} 2-A"), "auxiliary_only")
        for value in ("# 12", "ATTN TEST TEAM", "C/O TEST TEAM", "CARE OF TEST TEAM"):
            self.assertEqual(line_kind(value), "auxiliary_only")

    def test_anchored_false_positive_regressions_and_mixed_lines(self):
        for value in ("BOX ELDER RD", "123 PO BOX RD"):
            self.assertEqual(line_kind(value), "street_capable")
        for lines in (("PO BOX 1", "100 TEST ST"), ("100 TEST ST", "PO BOX 1")):
            address = normalize_address(*lines, "Town", "AL", "01001", "US")
            self.assertEqual(address.classification, "street_capable")
            self.assertTrue(address.direct_eligible)
            self.assertEqual(address.submitted_street, " ".join(lines).upper())
            self.assertIn("mixed_mail_street", address.diagnostic_flags)

    def test_routing_precedence(self):
        cases = [
            (("100 ST", "", "Town", "AL", "01001", "CA"), ("country_out_of_scope", False, False)),
            (("100 ST", "", "Town", "PR", "01001", "US"), ("state_out_of_scope_or_invalid", False, False)),
            (("", "", "Town", "AL", "01001", "US"), ("missing_street", False, True)),
            (("PO BOX 1", "", "Town", "AL", "01001", "US"), ("nonstreet_only", False, True)),
            (("PO BOX 1", "", "Town", "AL", "bad", "US"), ("nonstreet_only", False, False)),
            (("100 ST", "", "Town", "AL", "bad", "US"), ("street_capable", True, False)),
            (("100 ST", "", "", "AL", "01001", "US"), ("street_capable", True, True)),
            (("100 ST", "", "", "AL", "bad", "US"), ("street_capable", False, False)),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                item = normalize_address(*values)
                self.assertEqual((item.classification, item.direct_eligible, item.hud_eligible), expected)
        self.assertEqual(
            normalize_address("100 ST", "", "", "AL", "", "US").zip_status,
            "missing",
        )
        invalid = normalize_address("100 ST", "", "", "AL", "bad", "US")
        self.assertEqual(invalid.zip_status, "invalid")
        self.assertEqual(
            invalid.direct_reason,
            "DIRECT_INELIGIBLE_STREET_MISSING_CITY_AND_MISSING_OR_INVALID_ZIP",
        )

    def test_address_id_uses_canonical_json_array(self):
        first = ("A|B", 'QUOTE "', "BACK\\SLASH", "AL", "01001", "ÜS")
        second = ("A", "B|QUOTE \"", "BACK\\SLASH", "AL", "01001", "ÜS")
        payload = canonical_address_bytes(first)
        self.assertTrue(payload.startswith(b"["))
        self.assertNotEqual(make_address_id(first), make_address_id(second))
        self.assertEqual(make_address_id(first), make_address_id(tuple(first)))


if __name__ == "__main__":
    unittest.main()
