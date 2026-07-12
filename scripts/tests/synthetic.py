"""Non-identifying fixture builders shared by Phase-2A tests."""

from __future__ import annotations

from atlas_pipeline.nppes import _luhn_check_digit


def make_npi(first_nine: str) -> str:
    assert len(first_nine) == 9 and first_nine.isdigit()
    return first_nine + _luhn_check_digit("80840" + first_nine)


def nppes_row(npi: str | None = None, **overrides):
    row = {
        "NPI": npi or make_npi("100000001"),
        "Entity Type Code": "1",
        "Healthcare Provider Taxonomy Code_1": "207RC0000X",
        "Healthcare Provider Primary Taxonomy Switch_1": "Y",
        "NPI Deactivation Reason Code": "",
        "NPI Deactivation Date": "",
        "NPI Reactivation Date": "",
        "Last Update Date": "06/01/2026",
        "Provider First Line Business Practice Location Address": "100 Test Avenue",
        "Provider Second Line Business Practice Location Address": "Suite 2",
        "Provider Business Practice Location Address City Name": "Example City",
        "Provider Business Practice Location Address State Name": "AL",
        "Provider Business Practice Location Address Postal Code": "01001",
        "Provider Business Practice Location Address Country Code (If outside U.S.)": "US",
    }
    for slot in range(2, 16):
        row[f"Healthcare Provider Taxonomy Code_{slot}"] = ""
        row[f"Healthcare Provider Primary Taxonomy Switch_{slot}"] = ""
    row.update(overrides)
    return row


def hospital_row(facility_id: str = "000001", **overrides):
    row = {
        "Facility ID": facility_id,
        "Facility Name": "Synthetic General",
        "Address": "200 Test Avenue",
        "City/Town": "Example City",
        "State": "AL",
        "ZIP Code": "01001",
        "County/Parish": "Example",
        "Hospital Type": "Acute Care Hospitals",
    }
    row.update(overrides)
    return row
