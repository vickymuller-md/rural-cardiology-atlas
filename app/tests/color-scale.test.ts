import { describe, it, expect } from "vitest";
import { colorFor, legendFor } from "@/lib/color-scale";
import type { County } from "@/lib/types";

function county(partial: Partial<County>): County {
  return {
    fips: "00000",
    state: "XX",
    county: "Test",
    population: 100,
    median_hh_income: 50000,
    rucc_2023: 1,
    rural: false,
    n_cardiologists: 0,
    miles_to_nearest_cardiologist: 0,
    n_hospitals: 0,
    n_cah: 0,
    hpsa_primary_care: false,
    heart_disease_mortality_per_100k: 0,
    ...partial,
  };
}

describe("color-scale — density metric", () => {
  it("returns the critical color for zero cardiologists", () => {
    expect(colorFor(county({ n_cardiologists: 0 }), "density")).toContain("choro-0");
  });

  it("bins 1-2 cardiologists into the second bucket", () => {
    expect(colorFor(county({ n_cardiologists: 1 }), "density")).toContain("choro-1");
    expect(colorFor(county({ n_cardiologists: 2 }), "density")).toContain("choro-1");
  });

  it("bins 3-4 cardiologists into the middle bucket", () => {
    expect(colorFor(county({ n_cardiologists: 3 }), "density")).toContain("choro-2");
    expect(colorFor(county({ n_cardiologists: 4 }), "density")).toContain("choro-2");
  });

  it("bins 5+ into the signal color", () => {
    expect(colorFor(county({ n_cardiologists: 5 }), "density")).toContain("choro-4");
    expect(colorFor(county({ n_cardiologists: 50 }), "density")).toContain("choro-4");
  });
});

describe("color-scale — distance metric", () => {
  it("colors short distances green", () => {
    expect(colorFor(county({ miles_to_nearest_cardiologist: 5 }), "distance")).toContain("choro-4");
  });

  it("colors very long distances critical red", () => {
    expect(colorFor(county({ miles_to_nearest_cardiologist: 150 }), "distance")).toContain("choro-0");
  });

  it("returns the missing color when distance is null", () => {
    expect(
      colorFor(county({ miles_to_nearest_cardiologist: null }), "distance")
    ).toContain("grid");
  });
});

describe("legendFor", () => {
  it("returns 4 buckets for density", () => {
    expect(legendFor("density")).toHaveLength(4);
  });

  it("returns 5 buckets for distance", () => {
    expect(legendFor("distance")).toHaveLength(5);
  });
});
