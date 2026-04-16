import { describe, it, expect } from "vitest";
import { applyFilters, DEFAULT_FILTERS } from "@/lib/filters";
import type { County } from "@/lib/types";

function c(partial: Partial<County>): County {
  return {
    fips: "00000",
    state: "AL",
    county: "Test",
    population: 100,
    median_hh_income: null,
    rucc_2023: null,
    rural: false,
    n_cardiologists: 0,
    miles_to_nearest_cardiologist: null,
    n_hospitals: 0,
    n_cah: 0,
    hpsa_primary_care: false,
    heart_disease_mortality_per_100k: null,
    ...partial,
  };
}

describe("applyFilters", () => {
  const list: County[] = [
    c({ fips: "48301", state: "TX", rural: true, n_cah: 1, hpsa_primary_care: true }),
    c({ fips: "48201", state: "TX", rural: false, n_cah: 0, hpsa_primary_care: false }),
    c({ fips: "36061", state: "NY", rural: false, n_cah: 0, hpsa_primary_care: false }),
    c({ fips: "54047", state: "WV", rural: true, n_cah: 0, hpsa_primary_care: true }),
  ];

  it("returns all when defaults", () => {
    expect(applyFilters(list, DEFAULT_FILTERS)).toHaveLength(4);
  });

  it("filters by state", () => {
    expect(applyFilters(list, { ...DEFAULT_FILTERS, state: "TX" })).toHaveLength(2);
  });

  it("filters rural only", () => {
    expect(applyFilters(list, { ...DEFAULT_FILTERS, ruralOnly: true })).toHaveLength(2);
  });

  it("filters CAH only", () => {
    expect(applyFilters(list, { ...DEFAULT_FILTERS, cahOnly: true })).toHaveLength(1);
  });

  it("filters HPSA only", () => {
    expect(applyFilters(list, { ...DEFAULT_FILTERS, hpsaOnly: true })).toHaveLength(2);
  });

  it("stacks filters", () => {
    expect(
      applyFilters(list, {
        ...DEFAULT_FILTERS,
        state: "TX",
        ruralOnly: true,
        hpsaOnly: true,
      })
    ).toHaveLength(1);
  });
});
