import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  COUNTY_UNIVERSE_ID_LIST_SHA256,
  parseCountyUniverse,
} from "@/lib/county-universe";

function fixtureBytes(ids: readonly string[]): Buffer {
  return Buffer.from(`${ids.join("\n")}\n`, "utf8");
}

describe("Census 2025 county-universe identity", () => {
  it("pins the preregistered production sorted-list hash", () => {
    expect(COUNTY_UNIVERSE_ID_LIST_SHA256).toBe(
      "f2651ec6a9375476e3ff09efb4c2a699cd5690ffd3f0c982aeacad967c21c241"
    );
  });

  it("accepts only hash-bound, sorted, unique synthetic FIPS", () => {
    const bytes = fixtureBytes(["01001", "09110", "12001"]);
    const expectedSha256 = createHash("sha256").update(bytes).digest("hex");
    expect(
      parseCountyUniverse(bytes, { expectedCount: 3, expectedSha256 })
    ).toEqual(["01001", "09110", "12001"]);

    const reordered = fixtureBytes(["09110", "01001", "12001"]);
    const reorderedHash = createHash("sha256").update(reordered).digest("hex");
    expect(() =>
      parseCountyUniverse(reordered, {
        expectedCount: 3,
        expectedSha256: reorderedHash,
      })
    ).toThrow(/ascending ASCII/);
  });

  it("rejects a substituted list, malformed newline, and wrong cardinality", () => {
    const bytes = fixtureBytes(["01001", "09110", "12001"]);
    expect(() =>
      parseCountyUniverse(bytes, {
        expectedCount: 3,
        expectedSha256: createHash("sha256")
          .update("substituted-county-universe", "utf8")
          .digest("hex"),
      })
    ).toThrow(/SHA-256 mismatch/);

    const noLf = Buffer.from("01001\n09110\n12001", "utf8");
    const noLfHash = createHash("sha256").update(noLf).digest("hex");
    expect(() =>
      parseCountyUniverse(noLf, { expectedCount: 3, expectedSha256: noLfHash })
    ).toThrow(/terminal newline/);

    const expectedSha256 = createHash("sha256").update(bytes).digest("hex");
    expect(() =>
      parseCountyUniverse(bytes, { expectedCount: 4, expectedSha256 })
    ).toThrow(/exactly 4 IDs/);
  });
});
