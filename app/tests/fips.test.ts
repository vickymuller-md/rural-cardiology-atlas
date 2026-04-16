import { describe, it, expect } from "vitest";
import {
  padFips,
  stateOfFips,
  STATE_ABBR_TO_FIPS,
  STATE_FIPS_TO_ABBR,
  ALL_STATE_ABBRS,
} from "@/lib/fips";

describe("padFips", () => {
  it("zero-pads short fips to 5 digits", () => {
    expect(padFips("1001")).toBe("01001");
    expect(padFips(1001)).toBe("01001");
  });

  it("strips trailing .0 artifacts from pandas", () => {
    expect(padFips("1001.0")).toBe("01001");
  });

  it("leaves already-padded values alone", () => {
    expect(padFips("48301")).toBe("48301");
  });
});

describe("stateOfFips", () => {
  it("resolves Texas", () => {
    expect(stateOfFips("48301")).toBe("TX");
  });

  it("resolves DC", () => {
    expect(stateOfFips("11001")).toBe("DC");
  });

  it("returns undefined for an unknown prefix", () => {
    expect(stateOfFips("99999")).toBeUndefined();
  });
});

describe("state lookup tables", () => {
  it("has 51 entries (50 states + DC)", () => {
    expect(Object.keys(STATE_FIPS_TO_ABBR)).toHaveLength(51);
    expect(Object.keys(STATE_ABBR_TO_FIPS)).toHaveLength(51);
    expect(ALL_STATE_ABBRS).toHaveLength(51);
  });

  it("round-trips fips <-> abbr", () => {
    for (const [fips, abbr] of Object.entries(STATE_FIPS_TO_ABBR)) {
      expect(STATE_ABBR_TO_FIPS[abbr]).toBe(fips);
    }
  });
});
