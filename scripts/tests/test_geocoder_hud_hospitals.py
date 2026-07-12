from __future__ import annotations

import hashlib
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from atlas_pipeline.addresses import normalize_address
from atlas_pipeline.contracts import (
    CENSUS_BENCHMARK_ID,
    CENSUS_BENCHMARK_NAME,
    CENSUS_GEOCODER_ENDPOINT,
    CENSUS_VINTAGE_ID,
    CENSUS_VINTAGE_NAME,
)
from atlas_pipeline.errors import AcquisitionError, ContractError, IntegrityError
from atlas_pipeline.geocoder import (
    AttemptSnapshot,
    CanonicalGeocode,
    GeocoderRequest,
    RawGeocoderRow,
    canonical_snapshot_bytes,
    canonicalize_accepted_attempt,
    canonicalize_response,
    census_response_representation_error,
    chunk_requests,
    first_integrity_valid_attempt,
    parse_response,
    request_csv_bytes,
    request_manifest_bytes,
    response_is_retryable,
    retry_delay_after,
    validate_canonical_geocodes,
)
from atlas_pipeline.hospitals import deduplicate_hospitals, normalize_facility_id, parse_hospital
from atlas_pipeline.hud import assign_hud, parse_candidates
from atlas_pipeline.live_geocoder import prepare_census_request
from tests.synthetic import hospital_row

FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC_RESPONSE_SHA256 = hashlib.sha256(
    b"synthetic-census-geocoder-response"
).hexdigest()


def response_row(request_id: str, indicator: str = "Match", coordinates: str = "-86.5,32.5", state: str = "01", county: str = "001"):
    return RawGeocoderRow(
        request_id, "input", indicator, "Exact", "matched", coordinates,
        "1", "L", state, county, "000100", "1000",
    )


class GeocoderTests(unittest.TestCase):
    def test_rfc4180_request_and_stable_chunks(self):
        requests = [
            GeocoderRequest("A-z", '100 "A", STREET', "", "AL", ""),
            GeocoderRequest("A-a", "200 TEST STREET", "CITY", "AL", "01001"),
        ]
        body = request_csv_bytes(sorted(requests, key=lambda item: item.address_id))
        self.assertIn(b'"100 ""A"", STREET"', body)
        self.assertTrue(body.endswith(b"\n"))
        chunks = chunk_requests(requests)
        self.assertEqual(chunks[0].first_id, "A-a")
        self.assertEqual(chunks[0].sha256, hashlib.sha256(chunks[0].body).hexdigest())
        manifest = request_manifest_bytes(chunks)
        self.assertTrue(manifest.endswith(b"\n"))
        self.assertNotIn(b'"body"', manifest)
        with patch("atlas_pipeline.geocoder.MAX_CHUNK_ROWS", 1):
            self.assertEqual(len(chunk_requests(requests)), 2)
        with self.assertRaises(IntegrityError):
            chunk_requests([requests[0], requests[0]])

    def test_parse_fixture_and_coverage(self):
        body = (FIXTURES / "geocoder_response.csv").read_bytes()
        rows = parse_response(body, {"A-1", "A-2", "A-3"})
        self.assertEqual([row.match_indicator for row in rows], ["Match", "Tie", "No_Match"])
        with self.assertRaises(AcquisitionError):
            parse_response(body, {"A-1", "A-2"})
        with self.assertRaises(AcquisitionError):
            parse_response(body + body.splitlines(keepends=True)[0], {"A-1", "A-2", "A-3"})
        with self.assertRaises(AcquisitionError):
            parse_response(b"A-1,too,few\n", {"A-1"})
        with self.assertRaises(AcquisitionError):
            parse_response(body.replace(b"No_Match", b"Maybe"), {"A-1", "A-2", "A-3"})

    def test_official_short_tie_and_no_match_rows_are_padded_fail_closed(self):
        rows = parse_response(
            b"A-1,input,Tie\nA-2,input,No_Match\n", {"A-1", "A-2"}
        )
        self.assertEqual([row.match_indicator for row in rows], ["Tie", "No_Match"])
        self.assertTrue(all(row.match_type == "" for row in rows))
        with self.assertRaisesRegex(AcquisitionError, "Match row 1 must have 12 fields"):
            parse_response(b"A-1,input,Match\n", {"A-1"})
        with self.assertRaisesRegex(AcquisitionError, "expected 3 or 12"):
            parse_response(b"A-1,input,No_Match,extra\n", {"A-1"})

    def test_census_text_plain_requires_exact_csv_attachment_disposition(self):
        self.assertIsNone(
            census_response_representation_error(
                {
                    "Content-Type": "text/plain",
                    "Content-Disposition": "attachment; filename=GeocodeResults.csv",
                }
            )
        )
        self.assertIsNone(
            census_response_representation_error({"Content-Type": "text/csv"})
        )
        self.assertIn(
            "lacks exact Census CSV attachment disposition",
            census_response_representation_error({"Content-Type": "text/plain"}) or "",
        )
        self.assertEqual(
            census_response_representation_error(
                {
                    "Content-Type": "text/plain",
                    "Content-Disposition": "attachment; filename=other.csv",
                }
            ),
            "HTTP 200 text/plain response lacks exact Census CSV attachment disposition",
        )
        self.assertEqual(
            census_response_representation_error(
                [
                    ("Content-Type", "text/plain"),
                    ("content-type", "text/plain"),
                    ("Content-Disposition", "attachment; filename=GeocodeResults.csv"),
                ]
            ),
            "HTTP 200 response headers are duplicated case-insensitively",
        )
        self.assertEqual(
            census_response_representation_error(
                {
                    "Content-Type": "text/plain; charset=utf-8",
                    "Content-Disposition": "attachment; filename=GeocodeResults.csv",
                }
            ),
            "HTTP 200 response Content-Type is not CSV",
        )
        self.assertEqual(
            census_response_representation_error({"Content-Type": "text/html"}),
            "HTTP 200 response Content-Type is not CSV",
        )

    def test_match_quality_outcomes_and_state_integrity(self):
        rows = [
            response_row("direct"),
            response_row("wrong-state", state="02"),
            response_row("bad-coordinate", coordinates="bad"),
            response_row("latitude-epsilon", coordinates="-86,90.0000000000000001"),
            response_row("longitude-epsilon", coordinates="180.0000000000000001,32"),
            response_row("unknown-county", county="999"),
            response_row("tie", "Tie", "", "", ""),
            response_row("no", "No_Match", "", "", ""),
        ]
        states = {row.request_id: "AL" for row in rows}
        snapshot = canonicalize_response(
            rows, states, {"01001", "02001"}, accepted_attempt=1,
            source_response_sha256=SYNTHETIC_RESPONSE_SHA256,
        )
        self.assertEqual(
            {row.request_id: row.outcome for row in snapshot},
            {
                "direct": "Match",
                "wrong-state": "Invalid_Match_State",
                "bad-coordinate": "Invalid_Match",
                "latitude-epsilon": "Invalid_Match",
                "longitude-epsilon": "Invalid_Match",
                "unknown-county": "Invalid_Match",
                "tie": "Tie",
                "no": "No_Match",
            },
        )
        direct = next(row for row in snapshot if row.request_id == "direct")
        self.assertEqual(direct.county_fips, "01001")
        self.assertTrue(canonical_snapshot_bytes(snapshot).endswith(b"\n"))
        with self.assertRaises(IntegrityError):
            canonicalize_response(
                [rows[0]],
                {},
                {"01001"},
                accepted_attempt=1,
                source_response_sha256=SYNTHETIC_RESPONSE_SHA256,
            )
        with self.assertRaises(IntegrityError):
            canonicalize_response(
                [rows[0]],
                {"direct": "AL"},
                {"01001"},
                accepted_attempt=4,
                source_response_sha256=SYNTHETIC_RESPONSE_SHA256,
            )

    def _attempt(self, number: int, body: bytes, *, status=200, transport=None, integrity=None):
        request_body = b"A-1,STREET,CITY,AL,01001\n"
        prepared = prepare_census_request("geocoder_request_0001.csv", request_body)
        return AttemptSnapshot(
            request_sha256=hashlib.sha256(request_body).hexdigest(),
            request_method=prepared.method,
            requested_url=prepared.url,
            final_url=prepared.url,
            request_headers=prepared.headers,
            prepared_body_bytes=len(prepared.body),
            prepared_body_sha256=prepared.body_sha256,
            redirect_chain=(),
            endpoint=CENSUS_GEOCODER_ENDPOINT,
            benchmark_id=CENSUS_BENCHMARK_ID,
            benchmark_name=CENSUS_BENCHMARK_NAME,
            vintage_id=CENSUS_VINTAGE_ID,
            vintage_name=CENSUS_VINTAGE_NAME,
            attempt=number,
            started_at_utc=f"2026-07-09T00:00:0{number}Z",
            ended_at_utc=f"2026-07-09T00:00:0{number + 1}Z",
            http_status=status,
            response_headers=(("Content-Type", "text/csv"),),
            response_body=body,
            response_sha256=hashlib.sha256(body).hexdigest() if status is not None else None,
            transport_error=transport,
            integrity_error=integrity,
        )

    def test_finite_attempt_controller_accepts_first_valid_even_no_match(self):
        bad = b"truncated\n"
        valid = b"A-1,input,No_Match,,,,,,,,,\n"
        selected, rows = first_integrity_valid_attempt(
            [self._attempt(1, bad, status=503), self._attempt(2, valid)], {"A-1"}
        )
        self.assertEqual(selected.attempt, 2)
        self.assertEqual(rows[0].match_indicator, "No_Match")
        self.assertEqual((retry_delay_after(1), retry_delay_after(2), retry_delay_after(3)), (5, 20, None))
        self.assertTrue(response_is_retryable(http_status=503, integrity_error=False, transport_error=False))
        self.assertFalse(response_is_retryable(http_status=200, integrity_error=False, transport_error=False))
        self.assertFalse(response_is_retryable(http_status=404, integrity_error=True, transport_error=False))
        changed = self._attempt(2, valid)
        changed = AttemptSnapshot(
            **{
                **changed.__dict__,
                "request_sha256": hashlib.sha256(
                    b"drifted-census-request-chunk"
                ).hexdigest(),
            }
        )
        with self.assertRaises(AcquisitionError):
            first_integrity_valid_attempt([self._attempt(1, bad, status=503), changed], {"A-1"})
        with self.assertRaises(AcquisitionError):
            first_integrity_valid_attempt(
                [self._attempt(1, bad, status=404, integrity="bad body"), self._attempt(2, valid)],
                {"A-1"},
            )
        with self.assertRaises(AcquisitionError):
            first_integrity_valid_attempt([self._attempt(1, valid), self._attempt(2, valid)], {"A-1"})
        selected_only, selected_rows = first_integrity_valid_attempt(
            [self._attempt(1, valid, integrity="untrusted label")], {"A-1"}
        )
        self.assertEqual(selected_rows[0].match_indicator, "No_Match")
        canonical = canonicalize_accepted_attempt(
            selected_only,
            expected_ids={"A-1"},
            request_states={"A-1": "AL"},
            county_universe={"01001"},
        )
        self.assertEqual(canonical[0].accepted_attempt, 1)
        self.assertEqual(canonical[0].source_response_sha256, selected_only.response_sha256)

    def test_attempt_snapshots_require_utc_and_coherent_failure_state(self):
        valid = b"A-1,input,No_Match,,,,,,,,,\n"
        offset = self._attempt(1, valid)
        offset = AttemptSnapshot(
            **{
                **offset.__dict__,
                "started_at_utc": "2026-07-09T01:00:01+01:00",
                "ended_at_utc": "2026-07-09T01:00:02+01:00",
            }
        )
        with self.assertRaises(AcquisitionError):
            offset.validate()

        bare_failure = self._attempt(1, b"", status=None)
        with self.assertRaises(AcquisitionError):
            bare_failure.validate()

        transport = self._attempt(1, b"", status=None, transport="timeout")
        selected, rows = first_integrity_valid_attempt(
            [transport, self._attempt(2, valid)], {"A-1"}
        )
        self.assertEqual(selected.attempt, 2)
        self.assertEqual(rows[0].match_indicator, "No_Match")

        incoherent = self._attempt(1, valid, transport="timeout")
        with self.assertRaises(AcquisitionError):
            incoherent.validate()

    def test_offline_canonical_snapshot_revalidates_decimal_coordinate_lexemes(self):
        valid = CanonicalGeocode(
            "A-1", "Match", "Match", "Exact", "MATCHED", "-86", "32",
            "01001", 1, SYNTHETIC_RESPONSE_SHA256,
        )
        self.assertEqual(
            validate_canonical_geocodes(
                (valid,),
                expected_ids={"A-1"},
                request_states={"A-1": "AL"},
                county_universe={"01001"},
            ),
            (valid,),
        )
        for field, value in (
            ("latitude", "90.0000000000000001"),
            ("longitude", "180.0000000000000001"),
            ("latitude", "NaN"),
        ):
            with self.subTest(field=field), self.assertRaises(IntegrityError):
                validate_canonical_geocodes(
                    (CanonicalGeocode(**{**valid.__dict__, field: value}),),
                    expected_ids={"A-1"},
                    request_states={"A-1": "AL"},
                    county_universe={"01001"},
                )


class HudTests(unittest.TestCase):
    def setUp(self):
        self.universe = {"01001", "01003", "02001"}

    def candidates(self, rows):
        return parse_candidates(rows, self.universe)

    def test_same_state_bus_unique_max_and_audit_values(self):
        candidates = self.candidates([
            {"ZIP": "01001", "COUNTY": "01001", "BUS_RATIO": "0.5000", "RES_RATIO": "0.4", "TOT_RATIO": "0.45", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01001", "COUNTY": "01003", "BUS_RATIO": "0.4999", "RES_RATIO": "0.6", "TOT_RATIO": "0.55", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01001", "COUNTY": "02001", "BUS_RATIO": "0.9", "RES_RATIO": "0.9", "TOT_RATIO": "0.9", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
        ])
        result = assign_hud("01001", "AL", candidates)
        self.assertEqual(result.outcome, "HUD_ASSIGNED")
        self.assertEqual(result.county_fips, "01001")
        self.assertEqual(result.margin_to_second, Decimal("0.0001"))
        self.assertEqual(result.same_state_candidate_count, 2)
        res = assign_hud("01001", "AL", candidates, basis="RES_RATIO")
        self.assertEqual(res.county_fips, "01003")
        total = assign_hud("01001", "AL", candidates, basis="TOT_RATIO")
        self.assertEqual(total.county_fips, "01003")
        unrestricted = assign_hud("01001", "AL", candidates, restrict_state=False)
        self.assertEqual(unrestricted.county_fips, "02001")

    def test_tie_zero_missing_and_state_cases(self):
        tie = self.candidates([
            {"ZIP": "01001", "COUNTY": "01001", "BUS_RATIO": "0.5", "RES_RATIO": "0.1", "TOT_RATIO": "0.4", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01001", "COUNTY": "01003", "BUS_RATIO": "0.5", "RES_RATIO": "0.9", "TOT_RATIO": "0.6", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
        ])
        self.assertEqual(assign_hud("01001", "AL", tie).outcome, "HUD_BUS_TIE")
        self.assertEqual(
            assign_hud("01001", "AL", tie, basis="TOT_RATIO").county_fips,
            "01003",
        )
        total_tie = self.candidates([
            {"ZIP": "01001", "COUNTY": "01001", "BUS_RATIO": "0.6", "RES_RATIO": "0.1", "TOT_RATIO": "0.5", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            {"ZIP": "01001", "COUNTY": "01003", "BUS_RATIO": "0.4", "RES_RATIO": "0.9", "TOT_RATIO": "0.5", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
        ])
        self.assertEqual(
            assign_hud("01001", "AL", total_tie, basis="TOT_RATIO").outcome,
            "HUD_TOT_TIE",
        )
        zeros = self.candidates([
            {"ZIP": "01001", "COUNTY": "01001", "BUS_RATIO": "0", "RES_RATIO": "1", "TOT_RATIO": "0.2", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"}
        ])
        self.assertEqual(assign_hud("01001", "AL", zeros).outcome, "HUD_NO_POSITIVE_RATIO")
        self.assertEqual(assign_hud("99999", "AL", zeros).outcome, "HUD_ZIP_ABSENT")
        self.assertEqual(assign_hud("00000", "AL", zeros).outcome, "HUD_INVALID_OR_MISSING_ZIP")
        self.assertEqual(assign_hud("01001", "PR", zeros).outcome, "HUD_INVALID_SOURCE_STATE")
        with self.assertRaises(ContractError):
            self.candidates([{"ZIP": "00000", "COUNTY": "01001", "BUS_RATIO": "1", "RES_RATIO": "1", "TOT_RATIO": "1"}])
        with self.assertRaises(IntegrityError):
            self.candidates([
                {"ZIP": "01001", "COUNTY": "01001", "BUS_RATIO": "1", "RES_RATIO": "1", "TOT_RATIO": "1", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
                {"ZIP": "01001", "COUNTY": "01001", "BUS_RATIO": "1", "RES_RATIO": "1", "TOT_RATIO": "1", "OTH_RATIO": "0", "USPS_ZIP_PREF_CITY": "CITY", "USPS_ZIP_PREF_STATE": "AL"},
            ])
        with self.assertRaises(ContractError):
            self.candidates([{"ZIP": "01001", "COUNTY": "01001", "BUS_RATIO": "1"}])


class HospitalTests(unittest.TestCase):
    def test_facility_id_ccn_federal_and_cah(self):
        self.assertEqual(normalize_facility_id(" 000001\t"), "000001")
        numeric = parse_hospital(hospital_row("000001", **{"Hospital Type": " critical  access hospitals "}))
        self.assertTrue(numeric.is_cah)
        self.assertEqual(numeric.ccn, "000001")
        self.assertEqual(numeric.entity_id, "H-000001")
        federal = parse_hospital(hospital_row("12345f"))
        self.assertTrue(federal.federal_facility)
        self.assertIsNone(federal.ccn)
        with self.assertRaises(ContractError):
            parse_hospital(hospital_row("12345F", **{"Hospital Type": "CRITICAL ACCESS HOSPITALS"}))
        for invalid in ("12345", "1234567", "1234AF", "１２３４５６"):
            with self.assertRaises(ContractError):
                normalize_facility_id(invalid)

    def test_duplicate_contract(self):
        row = hospital_row()
        self.assertEqual(len(deduplicate_hospitals([row, dict(row)])), 1)
        other = dict(row)
        other["Address"] = "Different"
        with self.assertRaises(IntegrityError):
            deduplicate_hospitals([row, other])


if __name__ == "__main__":
    unittest.main()
