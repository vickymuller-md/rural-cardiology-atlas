import { createHash } from "node:crypto";
import {
  assertSummaryReconciles,
  PRODUCTION_COUNTY_REQUIREMENTS,
  validateCountyDataset,
  validateNationalSummary,
  type CountyDatasetRequirements,
} from "./data-contract";
import type { AtlasData } from "./types";

function parseJson(bytes: Uint8Array, label: string): unknown {
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch (error) {
    throw new Error(`Atlas data contract violation: ${label} is not valid UTF-8 JSON`, {
      cause: error,
    });
  }
}

export function sha256Hex(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

export function parseAtlasAssets(
  countyBytes: Uint8Array,
  summaryBytes: Uint8Array,
  requirements: CountyDatasetRequirements = PRODUCTION_COUNTY_REQUIREMENTS
): AtlasData {
  const countiesJsonSha256 = sha256Hex(countyBytes);
  const { list, index } = validateCountyDataset(
    parseJson(countyBytes, "counties.json"),
    requirements
  );
  const summary = validateNationalSummary(parseJson(summaryBytes, "summary.json"));
  assertSummaryReconciles(summary, list, countiesJsonSha256);
  return { list, index, summary, countiesJsonSha256 };
}
