import { describe, it, expect } from "vitest";
import { applyFilters, DEFAULT_FILTERS } from "@/lib/filters";
import type { County } from "@/lib/types";
import { makeCounty as c } from "./fixtures";

describe("applyFilters", () => {
  const list: County[] = [
    c({ fips: "48301", state: "TX", rucc_2023: 9, rural: true, n_hospitals: 1, n_cah: 1, hpsa_primary_care: true }),
    c({ fips: "48201", state: "TX", rural: false, n_cah: 0, hpsa_primary_care: false }),
    c({ fips: "36061", state: "NY", rural: false, n_cah: 0, hpsa_primary_care: false }),
    c({ fips: "54047", state: "WV", rucc_2023: 8, rural: true, n_cah: 0, hpsa_primary_care: true }),
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
