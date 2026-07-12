import "server-only";
import { parseAtlasAssets } from "./atlas-assets";
import { parseCountyUniverse } from "./county-universe";
import { PRODUCTION_COUNTY_REQUIREMENTS } from "./data-contract";
import { verifyProductionAssets } from "../scripts/verify-production-assets.mjs";
import type { AtlasData } from "./types";

export type { NationalSummary } from "./types";

let cache: AtlasData | null = null;

export async function loadAtlasData(): Promise<AtlasData> {
  if (cache) return cache;
  const manifestSha256 = process.env.ATLAS_RELEASE_MANIFEST_SHA256;
  const verified = await verifyProductionAssets({
    expectedReleaseManifestSha256: manifestSha256,
  });
  const [countyBytes, summaryBytes, universeBytes] = [
    verified.countyBytes,
    verified.summaryBytes,
    verified.universeBytes,
  ];
  const expectedFips = parseCountyUniverse(universeBytes);
  cache = parseAtlasAssets(countyBytes, summaryBytes, {
    ...PRODUCTION_COUNTY_REQUIREMENTS,
    expectedFips,
  });
  return cache;
}

export async function loadCounties(): Promise<Pick<AtlasData, "list" | "index">> {
  const { list, index } = await loadAtlasData();
  return { list, index };
}
