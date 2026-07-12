"""Frozen V19 constants and source-field contracts.

Nothing in this module is discovered dynamically.  A later source release must
be approved through a new preregistration instead of changing these constants.
"""

from __future__ import annotations

from dataclasses import dataclass

STATUS_AS_OF = "2026-06-08"
METHOD_LINEAGE_ID = "ATLAS-V19-AMENDMENT03-R4"

ADULT_CARDIOLOGY_TAXONOMIES: dict[str, str] = {
    "207RC0000X": "Cardiovascular Disease",
    "207RI0011X": "Interventional Cardiology",
    "207RC0001X": "Clinical Cardiac Electrophysiology",
    "207RA0001X": "Advanced Heart Failure and Transplant Cardiology",
    "207RA0002X": "Adult Congenital Heart Disease",
}

KNOWN_INELIGIBLE_CODES = frozenset(
    {
        "207RA0201X",  # Allergy & Immunology, not advanced heart failure
        "207RR0500X",  # Rheumatology, not adult congenital heart disease
        "2086S0102X",  # Surgical critical care
        "208G00000X",  # Thoracic surgery
        "2080P0202X",  # Pediatric cardiology
    }
)

STATE_FIPS: dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06",
    "CO": "08", "CT": "09", "DE": "10", "DC": "11", "FL": "12",
    "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18",
    "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23",
    "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28",
    "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
    "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
    "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49",
    "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55",
    "WY": "56",
}
FIPS_TO_STATE = {value: key for key, value in STATE_FIPS.items()}

NPPES_ADDRESS_FIELDS = (
    "Provider First Line Business Practice Location Address",
    "Provider Second Line Business Practice Location Address",
    "Provider Business Practice Location Address City Name",
    "Provider Business Practice Location Address State Name",
    "Provider Business Practice Location Address Postal Code",
    "Provider Business Practice Location Address Country Code (If outside U.S.)",
)

NPPES_STATUS_FIELDS = (
    "NPI Deactivation Reason Code",
    "NPI Deactivation Date",
    "NPI Reactivation Date",
    "Last Update Date",
)

NPPES_ANALYTICAL_FIELDS = (
    "NPI",
    "Entity Type Code",
    *tuple(
        field
        for slot in range(1, 16)
        for field in (
            f"Healthcare Provider Taxonomy Code_{slot}",
            f"Healthcare Provider Primary Taxonomy Switch_{slot}",
        )
    ),
    *NPPES_STATUS_FIELDS,
    *NPPES_ADDRESS_FIELDS,
)

HOSPITAL_FIELDS = (
    "Facility ID",
    "Facility Name",
    "Address",
    "City/Town",
    "State",
    "ZIP Code",
    "County/Parish",
    "Hospital Type",
)

COUNTY_PUBLIC_FIELDS = (
    "fips",
    "state",
    "county",
    "population",
    "median_hh_income",
    "rucc_2023",
    "rural",
    "n_cardiologists",
    "miles_to_nearest_cardiologist",
    "n_hospitals",
    "n_cah",
    "n_cah_20plus_miles_to_nearest_cardiologist",
    "hpsa_primary_care",
    "chd_age_adjusted_prevalence_pct",
)

SUMMARY_FIELDS = (
    "counties_json_sha256",
    "provider_locations_sha256",
    "hospital_locations_sha256",
    "county_count",
    "zero_provider_count",
    "zero_provider_pct",
    "population_in_zero_provider_counties",
    "rural_count_with_evaluable_distance",
    "rural_median_distance_miles",
    "cah_total_in_scope",
    "cah_evaluable",
    "cah_unmatched",
    "cah_gte_20_miles",
    "cah_gte_20_pct_evaluable",
)

# The sole Python migration denylist literal.  Current schemas/fixtures may not
# reproduce this retired mortality field.
FORBIDDEN_LEGACY_COUNTY_FIELD = "heart_disease_mortality_per_100k"

WATERFALL_BUCKETS = (
    "entity_type_not_individual",
    "primary_flag_malformed",
    "no_primary_taxonomy",
    "multiple_primary_taxonomies",
    "primary_taxonomy_not_allowlisted",
    "status_inconsistent",
    "inactive_deactivated",
    "country_out_of_scope",
    "state_out_of_scope_or_invalid",
    "assigned_direct_census",
    "assigned_hud_fallback",
    "final_unmatched",
)


@dataclass(frozen=True)
class QualityGates:
    provider_unmatched_max: float = 0.01
    hospital_unmatched_max: float = 0.005
    cah_unmatched_max: float = 0.005


GATES = QualityGates()

NPPES_PINNED_URL = (
    "https://download.cms.gov/nppes/"
    "NPPES_Data_Dissemination_June_2026_V2.zip"
)
CENSUS_GEOCODER_ENDPOINT = (
    "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
)
CENSUS_BENCHMARK_ID = "8"
CENSUS_BENCHMARK_NAME = "Public_AR_ACS2025"
CENSUS_VINTAGE_ID = "825"
CENSUS_VINTAGE_NAME = "ACS2025_ACS2025"
HUD_RELEASE = "2025-Q4"
PLACES_RELEASE = "2025"
PLACES_ESTIMATE_YEAR = "2023"
PLACES_MEASURE_ID = "CHD"
PLACES_DATA_VALUE_TYPE_ID = "AgeAdjPrv"
PLACES_UNIT = "%"
