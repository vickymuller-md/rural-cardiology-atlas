import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { parseAtlasAssets } from "@/lib/atlas-assets";
import {
  FORBIDDEN_LEGACY_COUNTY_FIELD,
  validateCountyDataset,
} from "@/lib/data-contract";
import { STATE_FIPS_TO_ABBR } from "@/lib/fips";
import { conventionalMedian } from "@/lib/stats";
import type { County } from "@/lib/types";
import { makeCounty } from "./fixtures";

const FIXTURE_REQUIREMENTS = {
  expectedCountyCount: 3,
  expectedChdNonNullCount: 2,
  expectedChdNullCount: 1,
  expectedFips: ["01991", "01993", "01995"],
};

describe("conventionalMedian fixture utility", () => {
  it("uses the middle value for an odd list", () => {
    expect(conventionalMedian([9, 1, 4])).toBe(4);
  });

  it("uses the arithmetic mean of the two middle values for an even list", () => {
    expect(conventionalMedian([8, 2, 4, 6])).toBe(5);
  });

  it("returns null for an empty list and rejects nonfinite values", () => {
    expect(conventionalMedian([])).toBeNull();
    expect(() => conventionalMedian([1, Number.NaN])).toThrow(/finite/);
  });
});

describe("synthetic county and authoritative-summary assets", () => {
  it("validates exact-byte hash binding and facility-level CAH accounting", async () => {
    const fixtureDirectory = path.join(process.cwd(), "tests", "fixtures");
    const [countyBytes, summaryBytes] = await Promise.all([
      readFile(path.join(fixtureDirectory, "counties.synthetic.json")),
      readFile(path.join(fixtureDirectory, "summary.synthetic.json")),
    ]);
    const atlas = parseAtlasAssets(countyBytes, summaryBytes, FIXTURE_REQUIREMENTS);

    expect(atlas.countiesJsonSha256).toBe(atlas.summary.counties_json_sha256);
    expect(atlas.summary.cah_gte_20_miles).toBe(2);
    expect(
      atlas.list.reduce(
        (sum, county) => sum + county.n_cah_20plus_miles_to_nearest_cardiologist,
        0
      )
    ).toBe(2);
    expect(atlas.summary.cah_evaluable).toBe(3);

    // The raw-first authoritative median intentionally differs from the median
    // reconstructed from one-decimal county display values.
    expect(atlas.summary.rural_median_distance_miles).toBe(19.9);
    expect(
      conventionalMedian(
        atlas.list
          .filter((county) => county.rural)
          .map((county) => county.miles_to_nearest_cardiologist as number)
      )
    ).toBe(20.6);
  });

  it("fails when summary hash binding drifts", async () => {
    const fixtureDirectory = path.join(process.cwd(), "tests", "fixtures");
    const countyBytes = await readFile(path.join(fixtureDirectory, "counties.synthetic.json"));
    const summary = JSON.parse(
      await readFile(path.join(fixtureDirectory, "summary.synthetic.json"), "utf8")
    );
    summary.counties_json_sha256 = createHash("sha256")
      .update("drifted-counties-json", "utf8")
      .digest("hex");
    expect(() =>
      parseAtlasAssets(countyBytes, Buffer.from(JSON.stringify(summary)), FIXTURE_REQUIREMENTS)
    ).toThrow(/does not bind/);
  });

  it("rejects malformed summary fields and percentage fractions", async () => {
    const fixtureDirectory = path.join(process.cwd(), "tests", "fixtures");
    const countyBytes = await readFile(path.join(fixtureDirectory, "counties.synthetic.json"));
    const summary = JSON.parse(
      await readFile(path.join(fixtureDirectory, "summary.synthetic.json"), "utf8")
    );

    const missingField = { ...summary };
    delete missingField.hospital_locations_sha256;
    expect(() =>
      parseAtlasAssets(countyBytes, Buffer.from(JSON.stringify(missingField)), FIXTURE_REQUIREMENTS)
    ).toThrow(/keys\/order/);

    const fractionInsteadOfPercentagePoints = { ...summary, zero_provider_pct: 0.7 };
    expect(() =>
      parseAtlasAssets(
        countyBytes,
        Buffer.from(JSON.stringify(fractionInsteadOfPercentagePoints)),
        FIXTURE_REQUIREMENTS
      )
    ).toThrow(/zero_provider_pct does not reconcile/);

    const wrongCahPercentage = { ...summary, cah_gte_20_pct_evaluable: 0.7 };
    expect(() =>
      parseAtlasAssets(countyBytes, Buffer.from(JSON.stringify(wrongCahPercentage)), FIXTURE_REQUIREMENTS)
    ).toThrow(/cah_gte_20_pct_evaluable does not reconcile/);

    const unquantizedMedian = { ...summary, rural_median_distance_miles: 19.94 };
    expect(() =>
      parseAtlasAssets(countyBytes, Buffer.from(JSON.stringify(unquantizedMedian)), FIXTURE_REQUIREMENTS)
    ).toThrow(/quantized to one decimal place/);
  });

  it("rejects the retired county field and any key-order drift", () => {
    const row = makeCounty({ chd_age_adjusted_prevalence_pct: 5.1 });
    const legacy = { ...row } as Record<string, unknown>;
    delete legacy.chd_age_adjusted_prevalence_pct;
    legacy[FORBIDDEN_LEGACY_COUNTY_FIELD] = 5.1;
    expect(() =>
      validateCountyDataset([legacy], {
        expectedCountyCount: 1,
        expectedChdNonNullCount: 1,
        expectedChdNullCount: 0,
      })
    ).toThrow(/keys\/order/);
  });
});

function generatedProductionAnalogue(): County[] {
  const prefixes = Object.keys(STATE_FIPS_TO_ABBR).sort();
  const rows: County[] = [];
  for (const prefix of prefixes) {
    for (let countyCode = 1; countyCode <= 999 && rows.length < 3_144; countyCode += 1) {
      const index = rows.length;
      rows.push(
        makeCounty({
          fips: `${prefix}${String(countyCode).padStart(3, "0")}`,
          state: STATE_FIPS_TO_ABBR[prefix],
          county: `Synthetic county ${index + 1}`,
          chd_age_adjusted_prevalence_pct: index < 2_956 ? 5.0 : null,
        })
      );
    }
    if (rows.length === 3_144) break;
  }
  return rows;
}

describe("3,144-row release schema gate", () => {
  it("accepts a sorted synthetic schema analogue with the frozen PLACES completeness", () => {
    expect(validateCountyDataset(generatedProductionAnalogue()).list).toHaveLength(3_144);
  });

  it("rejects a malformed row even when the array still contains 3,144 rows", () => {
    const rows = generatedProductionAnalogue() as Array<County & { unexpected?: string }>;
    rows[2_000] = { ...rows[2_000], unexpected: "not allowed" };
    expect(() => validateCountyDataset(rows)).toThrow(/keys\/order/);
  });

  it("rejects missing and duplicate rows", () => {
    const missing = generatedProductionAnalogue().slice(0, -1);
    expect(() => validateCountyDataset(missing)).toThrow(/exactly 3144 rows/);

    const duplicate = generatedProductionAnalogue();
    duplicate[1_001] = { ...duplicate[1_000] };
    expect(() => validateCountyDataset(duplicate)).toThrow(/uniquely sorted/);
  });
});
