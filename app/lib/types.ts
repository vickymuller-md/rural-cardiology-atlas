export interface County {
  fips: string;
  state: string;
  county: string;
  population: number;
  median_hh_income: number | null;
  rucc_2023: number;
  rural: boolean;
  n_cardiologists: number;
  miles_to_nearest_cardiologist: number | null;
  n_hospitals: number;
  n_cah: number;
  n_cah_20plus_miles_to_nearest_cardiologist: number;
  hpsa_primary_care: boolean;
  chd_age_adjusted_prevalence_pct: number | null;
}

export type CountyIndex = Record<string, County>;

export type ChoroplethMetric = "density" | "distance";

export interface NationalSummary {
  counties_json_sha256: string;
  provider_locations_sha256: string;
  hospital_locations_sha256: string;
  county_count: number;
  zero_provider_count: number;
  zero_provider_pct: number;
  population_in_zero_provider_counties: number;
  rural_count_with_evaluable_distance: number;
  rural_median_distance_miles: number;
  cah_total_in_scope: number;
  cah_evaluable: number;
  cah_unmatched: number;
  cah_gte_20_miles: number;
  cah_gte_20_pct_evaluable: number;
}

export interface AtlasData {
  list: County[];
  index: CountyIndex;
  summary: NationalSummary;
  countiesJsonSha256: string;
}
