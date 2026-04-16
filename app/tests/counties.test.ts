import { describe, it, expect } from "vitest";
import { computeStats } from "@/lib/stats";
import type { County } from "@/lib/types";

function c(partial: Partial<County>): County {
  return {
    fips: "00000",
    state: "AL",
    county: "Test",
    population: 1000,
    median_hh_income: null,
    rucc_2023: null,
    rural: false,
    n_cardiologists: 0,
    miles_to_nearest_cardiologist: 0,
    n_hospitals: 0,
    n_cah: 0,
    hpsa_primary_care: false,
    heart_disease_mortality_per_100k: null,
    ...partial,
  };
}

describe("computeStats", () => {
  const list: County[] = [
    c({ n_cardiologists: 0, population: 500, rural: true, miles_to_nearest_cardiologist: 40 }),
    c({ n_cardiologists: 0, population: 800, rural: true, miles_to_nearest_cardiologist: 60, n_cah: 1 }),
    c({ n_cardiologists: 3, population: 5000, rural: false, miles_to_nearest_cardiologist: 2 }),
    c({ n_cardiologists: 12, population: 100000, rural: false, miles_to_nearest_cardiologist: 0 }),
  ];

  it("counts zero-cardio counties and their pct", () => {
    const s = computeStats(list);
    expect(s.zeroCardioCount).toBe(2);
    expect(s.zeroCardioPct).toBeCloseTo(0.5, 5);
  });

  it("computes rural median distance", () => {
    const s = computeStats(list);
    expect(s.medianRuralDistance).toBe(60);
  });

  it("sums underserved population", () => {
    const s = computeStats(list);
    expect(s.populationUnderserved).toBe(1300);
  });

  it("counts CAHs ≥ 20 miles from a cardiologist", () => {
    const s = computeStats(list);
    expect(s.cahFarFromCardiologist).toBe(1);
  });
});
