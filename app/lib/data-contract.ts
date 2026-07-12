import { STATE_FIPS_TO_ABBR } from "./fips";
import type { County, CountyIndex, NationalSummary } from "./types";

export const COUNTY_FIELDS = [
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
] as const;

export const SUMMARY_FIELDS = [
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
] as const;

const SHA256_RE = /^[0-9a-f]{64}$/;
const FIPS_RE = /^[0-9]{5}$/;

export interface CountyDatasetRequirements {
  expectedCountyCount: number;
  expectedChdNonNullCount: number;
  expectedChdNullCount: number;
  expectedFips?: readonly string[];
}

export const PRODUCTION_COUNTY_REQUIREMENTS: CountyDatasetRequirements = {
  expectedCountyCount: 3_144,
  expectedChdNonNullCount: 2_956,
  expectedChdNullCount: 188,
};

function fail(message: string): never {
  throw new Error(`Atlas data contract violation: ${message}`);
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    fail(`${label} must be a JSON object`);
  }
  return value as Record<string, unknown>;
}

function requireExactOrderedKeys(
  record: Record<string, unknown>,
  expected: readonly string[],
  label: string
): void {
  const actual = Object.keys(record);
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    fail(`${label} keys/order must be exactly: ${expected.join(", ")}`);
  }
}

function requireString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) fail(`${label} must be a non-empty string`);
  return value;
}

function requireBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") fail(`${label} must be boolean`);
  return value;
}

function requireInteger(value: unknown, label: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    fail(`${label} must be a safe integer >= ${minimum}`);
  }
  return value as number;
}

function requireFiniteNumber(value: unknown, label: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum) {
    fail(`${label} must be a finite number >= ${minimum}`);
  }
  return value;
}

function requireNullableFiniteNumber(
  value: unknown,
  label: string,
  minimum = 0
): number | null {
  if (value === null) return null;
  return requireFiniteNumber(value, label, minimum);
}

function requireOneDecimalBoundary(value: number, label: string): void {
  if (!Number.isInteger(value * 10)) {
    fail(`${label} must be quantized to one decimal place`);
  }
}

export function parseCounty(value: unknown, rowNumber: number): County {
  const label = `county row ${rowNumber}`;
  const row = asRecord(value, label);
  requireExactOrderedKeys(row, COUNTY_FIELDS, label);

  const fips = requireString(row.fips, `${label}.fips`);
  if (!FIPS_RE.test(fips)) fail(`${label}.fips must be exactly five digits`);
  const expectedState = STATE_FIPS_TO_ABBR[fips.slice(0, 2)];
  if (!expectedState) fail(`${label}.fips is outside the 50-state/DC universe`);

  const state = requireString(row.state, `${label}.state`);
  if (state !== expectedState) fail(`${label}.state does not match its FIPS prefix`);
  const county = requireString(row.county, `${label}.county`);
  const population = requireInteger(row.population, `${label}.population`);
  const medianHhIncome =
    row.median_hh_income === null
      ? null
      : requireInteger(row.median_hh_income, `${label}.median_hh_income`);
  const rucc = requireInteger(row.rucc_2023, `${label}.rucc_2023`, 1);
  if (rucc > 9) fail(`${label}.rucc_2023 must be in 1..9`);
  const rural = requireBoolean(row.rural, `${label}.rural`);
  if (rural !== (rucc >= 4)) fail(`${label}.rural must equal rucc_2023 >= 4`);

  const nCardiologists = requireInteger(row.n_cardiologists, `${label}.n_cardiologists`);
  const distance = requireNullableFiniteNumber(
    row.miles_to_nearest_cardiologist,
    `${label}.miles_to_nearest_cardiologist`
  );
  if (distance !== null) requireOneDecimalBoundary(distance, `${label}.miles_to_nearest_cardiologist`);
  const nHospitals = requireInteger(row.n_hospitals, `${label}.n_hospitals`);
  const nCah = requireInteger(row.n_cah, `${label}.n_cah`);
  const nCahGte20 = requireInteger(
    row.n_cah_20plus_miles_to_nearest_cardiologist,
    `${label}.n_cah_20plus_miles_to_nearest_cardiologist`
  );
  if (nCah > nHospitals) fail(`${label}.n_cah cannot exceed n_hospitals`);
  if (nCahGte20 > nCah) {
    fail(`${label}.n_cah_20plus_miles_to_nearest_cardiologist cannot exceed n_cah`);
  }

  const chd = requireNullableFiniteNumber(
    row.chd_age_adjusted_prevalence_pct,
    `${label}.chd_age_adjusted_prevalence_pct`
  );
  if (chd !== null && chd > 100) {
    fail(`${label}.chd_age_adjusted_prevalence_pct cannot exceed 100`);
  }
  if (chd !== null) requireOneDecimalBoundary(chd, `${label}.chd_age_adjusted_prevalence_pct`);

  return {
    fips,
    state,
    county,
    population,
    median_hh_income: medianHhIncome,
    rucc_2023: rucc,
    rural,
    n_cardiologists: nCardiologists,
    miles_to_nearest_cardiologist: distance,
    n_hospitals: nHospitals,
    n_cah: nCah,
    n_cah_20plus_miles_to_nearest_cardiologist: nCahGte20,
    hpsa_primary_care: requireBoolean(row.hpsa_primary_care, `${label}.hpsa_primary_care`),
    chd_age_adjusted_prevalence_pct: chd,
  };
}

export function validateCountyDataset(
  value: unknown,
  requirements: CountyDatasetRequirements = PRODUCTION_COUNTY_REQUIREMENTS
): { list: County[]; index: CountyIndex } {
  if (!Array.isArray(value)) fail("counties.json root must be an array");
  if (value.length !== requirements.expectedCountyCount) {
    fail(
      `counties.json must contain exactly ${requirements.expectedCountyCount} rows; found ${value.length}`
    );
  }

  const list = value.map((row, index) => parseCounty(row, index + 1));
  const index: CountyIndex = {};
  let previous = "";
  let chdNonNullCount = 0;
  for (const county of list) {
    if (county.fips <= previous) fail("county rows must be uniquely sorted by ascending FIPS");
    previous = county.fips;
    index[county.fips] = county;
    if (county.chd_age_adjusted_prevalence_pct !== null) chdNonNullCount += 1;
  }

  const chdNullCount = list.length - chdNonNullCount;
  if (
    chdNonNullCount !== requirements.expectedChdNonNullCount ||
    chdNullCount !== requirements.expectedChdNullCount
  ) {
    fail(
      `PLACES completeness must be ${requirements.expectedChdNonNullCount} non-null / ` +
        `${requirements.expectedChdNullCount} null; found ${chdNonNullCount} / ${chdNullCount}`
    );
  }

  if (requirements.expectedFips) {
    const expected = [...requirements.expectedFips].sort();
    if (
      expected.length !== list.length ||
      expected.some((fips, position) => fips !== list[position].fips)
    ) {
      fail("county FIPS do not equal the required analytical universe");
    }
  }

  return { list, index };
}

export function validateNationalSummary(value: unknown): NationalSummary {
  const row = asRecord(value, "summary.json");
  requireExactOrderedKeys(row, SUMMARY_FIELDS, "summary.json");

  for (const field of [
    "counties_json_sha256",
    "provider_locations_sha256",
    "hospital_locations_sha256",
  ] as const) {
    if (typeof row[field] !== "string" || !SHA256_RE.test(row[field] as string)) {
      fail(`summary.json.${field} must be a lowercase SHA-256 hex digest`);
    }
  }

  const summary: NationalSummary = {
    counties_json_sha256: row.counties_json_sha256 as string,
    provider_locations_sha256: row.provider_locations_sha256 as string,
    hospital_locations_sha256: row.hospital_locations_sha256 as string,
    county_count: requireInteger(row.county_count, "summary.json.county_count", 1),
    zero_provider_count: requireInteger(row.zero_provider_count, "summary.json.zero_provider_count"),
    zero_provider_pct: requireFiniteNumber(row.zero_provider_pct, "summary.json.zero_provider_pct"),
    population_in_zero_provider_counties: requireInteger(
      row.population_in_zero_provider_counties,
      "summary.json.population_in_zero_provider_counties"
    ),
    rural_count_with_evaluable_distance: requireInteger(
      row.rural_count_with_evaluable_distance,
      "summary.json.rural_count_with_evaluable_distance"
    ),
    rural_median_distance_miles: requireFiniteNumber(
      row.rural_median_distance_miles,
      "summary.json.rural_median_distance_miles"
    ),
    cah_total_in_scope: requireInteger(row.cah_total_in_scope, "summary.json.cah_total_in_scope"),
    cah_evaluable: requireInteger(row.cah_evaluable, "summary.json.cah_evaluable"),
    cah_unmatched: requireInteger(row.cah_unmatched, "summary.json.cah_unmatched"),
    cah_gte_20_miles: requireInteger(row.cah_gte_20_miles, "summary.json.cah_gte_20_miles"),
    cah_gte_20_pct_evaluable: requireFiniteNumber(
      row.cah_gte_20_pct_evaluable,
      "summary.json.cah_gte_20_pct_evaluable"
    ),
  };

  if (summary.zero_provider_pct > 100 || summary.cah_gte_20_pct_evaluable > 100) {
    fail("summary percentages must be percentage points in 0..100");
  }
  requireOneDecimalBoundary(summary.zero_provider_pct, "summary.json.zero_provider_pct");
  requireOneDecimalBoundary(
    summary.rural_median_distance_miles,
    "summary.json.rural_median_distance_miles"
  );
  requireOneDecimalBoundary(
    summary.cah_gte_20_pct_evaluable,
    "summary.json.cah_gte_20_pct_evaluable"
  );
  if (summary.zero_provider_count > summary.county_count) {
    fail("summary zero_provider_count cannot exceed county_count");
  }
  if (summary.cah_evaluable > summary.cah_total_in_scope) {
    fail("summary cah_evaluable cannot exceed cah_total_in_scope");
  }
  if (summary.cah_unmatched !== summary.cah_total_in_scope - summary.cah_evaluable) {
    fail("summary CAH S/E/U accounting does not reconcile");
  }
  if (summary.cah_gte_20_miles > summary.cah_evaluable) {
    fail("summary cah_gte_20_miles cannot exceed cah_evaluable");
  }
  return summary;
}

export function percentagePointsHalfUp(numerator: number, denominator: number): number {
  if (!Number.isSafeInteger(numerator) || !Number.isSafeInteger(denominator) || denominator <= 0) {
    fail("percentage inputs must be safe integers with a positive denominator");
  }
  return Math.floor((numerator * 1_000) / denominator + 0.5) / 10;
}

export function assertSummaryReconciles(
  summary: NationalSummary,
  counties: readonly County[],
  countiesJsonSha256: string
): void {
  if (summary.counties_json_sha256 !== countiesJsonSha256) {
    fail("summary counties_json_sha256 does not bind the exact loaded county bytes");
  }
  if (summary.county_count !== counties.length) fail("summary county_count does not match counties.json");

  const zeroProvider = counties.filter((county) => county.n_cardiologists === 0);
  if (summary.zero_provider_count !== zeroProvider.length) {
    fail("summary zero_provider_count does not reconcile to counties.json");
  }
  const expectedZeroPct = percentagePointsHalfUp(zeroProvider.length, counties.length);
  if (summary.zero_provider_pct !== expectedZeroPct) {
    fail("summary zero_provider_pct does not reconcile with decimal ROUND_HALF_UP");
  }

  const zeroPopulation = zeroProvider.reduce((total, county) => total + county.population, 0);
  if (summary.population_in_zero_provider_counties !== zeroPopulation) {
    fail("summary population_in_zero_provider_counties does not reconcile to counties.json");
  }

  const ruralEvaluable = counties.filter(
    (county) => county.rural && county.miles_to_nearest_cardiologist !== null
  ).length;
  if (summary.rural_count_with_evaluable_distance !== ruralEvaluable) {
    fail("summary rural_count_with_evaluable_distance does not reconcile to counties.json");
  }

  const assignedCah = counties.reduce((total, county) => total + county.n_cah, 0);
  if (summary.cah_evaluable !== assignedCah) {
    fail("summary cah_evaluable does not reconcile to distinct assigned facility-level CAHs");
  }
  const cahGte20 = counties.reduce(
    (total, county) => total + county.n_cah_20plus_miles_to_nearest_cardiologist,
    0
  );
  if (summary.cah_gte_20_miles !== cahGte20) {
    fail("summary cah_gte_20_miles does not reconcile to the county facility-level CAH field");
  }
  if (summary.cah_evaluable === 0) {
    fail("summary cah_evaluable cannot be zero in a release asset");
  }
  const expectedCahPct = percentagePointsHalfUp(summary.cah_gte_20_miles, summary.cah_evaluable);
  if (summary.cah_gte_20_pct_evaluable !== expectedCahPct) {
    fail("summary cah_gte_20_pct_evaluable does not reconcile with decimal ROUND_HALF_UP");
  }

  // The raw-first rural median is intentionally not reconstructed here: counties.json
  // exposes one-decimal display distances, while summary.json binds the authoritative value.
}

// This literal is the sole runtime migration denylist for the retired public field.
export const FORBIDDEN_LEGACY_COUNTY_FIELD = "heart_disease_mortality_per_100k";
