import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  areaPageMetadata,
  GENERIC_OG_IMAGE_PATH,
  OG_CACHE_CONTROL,
  ogCopyForFips,
  ogImagePathForFips,
  socialImageMetadata,
} from "@/lib/og-copy";
import { metadata as rootMetadata } from "@/app/layout";
import { makeCounty } from "./fixtures";

describe("dynamic OG county copy", () => {
  it("resolves two FIPS independently without appending County to source names", () => {
    const index = {
      "01991": makeCounty({
        fips: "01991",
        state: "AL",
        county: "Fixture County",
        n_cardiologists: 0,
      }),
      "09110": makeCounty({
        fips: "09110",
        state: "CT",
        county: "Capitol Planning Region",
        n_cardiologists: 2,
      }),
    };
    expect(ogCopyForFips("01991", index).title).toBe("Fixture County, Alabama");
    expect(ogCopyForFips("09110", index).title).toBe(
      "Capitol Planning Region, Connecticut"
    );
    expect(ogCopyForFips("01991", index).title).not.toContain("County County");
    expect(ogCopyForFips("09110", index).subtitle).toContain(
      "2 qualifying providers assigned"
    );
    expect(ogCopyForFips("09110", index).tag).toBe("AREA REPORT");
    expect(ogCopyForFips("09110", index).tag).not.toContain("COUNTY");
  });

  it("produces distinct generic and FIPS-specific image URLs for global/county metadata", async () => {
    expect(ogImagePathForFips(null)).toBe(GENERIC_OG_IMAGE_PATH);
    expect(ogImagePathForFips("09110")).toBe("/api/og?fips=09110");
    expect(ogImagePathForFips("01991")).toBe("/api/og?fips=01991");
    expect(ogImagePathForFips("09110")).not.toBe(ogImagePathForFips("01991"));
    expect(() => ogImagePathForFips("9110")).toThrow(/exactly five digits/);
    expect(socialImageMetadata(null, "Atlas")).toEqual({
      openGraph: [
        { url: "/api/og", width: 1200, height: 630, alt: "Atlas" },
      ],
      twitter: ["/api/og"],
    });
    expect(socialImageMetadata("09110", "Capitol Planning Region")).toEqual({
      openGraph: [
        {
          url: "/api/og?fips=09110",
          width: 1200,
          height: 630,
          alt: "Capitol Planning Region",
        },
      ],
      twitter: ["/api/og?fips=09110"],
    });
    expect(() => socialImageMetadata(null, "   ")).toThrow(/alt text/);

    expect(rootMetadata.openGraph?.images).toEqual([
      {
        url: "/api/og",
        width: 1200,
        height: 630,
        alt: "Rural Cardiology Desert Atlas",
      },
    ]);
    expect(rootMetadata.twitter?.images).toEqual(["/api/og"]);
    const ctMetadata = areaPageMetadata(
      makeCounty({
        fips: "09110",
        state: "CT",
        county: "Capitol Planning Region",
      })
    );
    const alMetadata = areaPageMetadata(
      makeCounty({ fips: "01991", state: "AL", county: "Fixture County" })
    );
    expect(ctMetadata.openGraph?.images).toEqual([
      {
        url: "/api/og?fips=09110",
        width: 1200,
        height: 630,
        alt: "Capitol Planning Region, CT",
      },
    ]);
    expect(ctMetadata.twitter?.images).toEqual(["/api/og?fips=09110"]);
    expect(alMetadata.twitter?.images).toEqual(["/api/og?fips=01991"]);
    expect(ctMetadata.openGraph?.images).not.toEqual(alMetadata.openGraph?.images);

    const [layout, areaPage] = await Promise.all([
      readFile(path.join(process.cwd(), "app/layout.tsx"), "utf8"),
      readFile(path.join(process.cwd(), "app/county/[fips]/page.tsx"), "utf8"),
    ]);
    expect(layout).toContain("openGraph:");
    expect(layout).toContain("images: globalSocialImages.openGraph");
    expect(layout).toContain("images: globalSocialImages.twitter");
    expect(areaPage).toContain("return areaPageMetadata(c)");
  });

  it("uses a force-dynamic no-store route for query-dependent images", async () => {
    expect(OG_CACHE_CONTROL).toBe("private, no-store, max-age=0");
    const route = await readFile(
      path.join(process.cwd(), "app/api/og/route.tsx"),
      "utf8"
    );
    expect(route).toContain('export const dynamic = "force-dynamic";');
    expect(route).toContain("export const revalidate = 0;");
    expect(route).toContain('headers: { "Cache-Control": OG_CACHE_CONTROL }');
  });
});
