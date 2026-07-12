import { createHash } from "node:crypto";

export const COUNTY_UNIVERSE_ID_LIST_SHA256 =
  "f2651ec6a9375476e3ff09efb4c2a699cd5690ffd3f0c982aeacad967c21c241";

const FIPS_RE = /^[0-9]{5}$/;

function fail(message: string): never {
  throw new Error(`Atlas county-universe contract violation: ${message}`);
}

export function parseCountyUniverse(
  bytes: Uint8Array,
  options: { expectedCount?: number; expectedSha256?: string } = {}
): readonly string[] {
  const expectedCount = options.expectedCount ?? 3_144;
  const expectedSha256 =
    options.expectedSha256 ?? COUNTY_UNIVERSE_ID_LIST_SHA256;
  const digest = createHash("sha256").update(bytes).digest("hex");
  if (digest !== expectedSha256) fail("canonical sorted-list SHA-256 mismatch");

  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch (error) {
    throw new Error("Atlas county-universe contract violation: list is not UTF-8", {
      cause: error,
    });
  }
  if (!text.endsWith("\n") || text.endsWith("\n\n") || text.includes("\r")) {
    fail("list must use LF with exactly one terminal newline");
  }
  const ids = text.slice(0, -1).split("\n");
  if (ids.length !== expectedCount) {
    fail(`list must contain exactly ${expectedCount} IDs; found ${ids.length}`);
  }
  let previous = "";
  for (const fips of ids) {
    if (!FIPS_RE.test(fips)) fail(`invalid county FIPS: ${fips}`);
    if (fips <= previous) fail("IDs must be unique and ascending ASCII");
    previous = fips;
  }
  return ids;
}
