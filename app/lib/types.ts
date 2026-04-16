export interface County {
  fips: string;
  state: string;
  county: string;
  population: number | null;
  median_hh_income: number | null;
  rucc_2023: number | null;
  rural: boolean;
  n_cardiologists: number;
  miles_to_nearest_cardiologist: number | null;
  n_hospitals: number;
  n_cah: number;
  hpsa_primary_care: boolean;
  heart_disease_mortality_per_100k: number | null;
}

export type CountyIndex = Record<string, County>;

export type ChoroplethMetric = "density" | "distance";
