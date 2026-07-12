import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { FORBIDDEN_LEGACY_COUNTY_FIELD } from "@/lib/data-contract";

const CURRENT_SURFACE_ROOTS = ["app", "components", "lib", "control"];
const CURRENT_SURFACE_FILES = [
  "next.config.ts",
  "package.json",
  "vercel.json",
  "OFFLINE_DEPENDENCIES.md",
];

const NEGATIVE_LIMITATION_ALLOWLIST = [
  "not road distance or travel time",
  "it contains no patient-level data",
  "these area-level measures do not establish service availability, appointment access, or patient-level outcomes",
  "cms registration and hospital type do not establish current cardiology capability, operating status on the review date, or service availability",
] as const;

const AFFIRMATIVE_CLAIM_DENYLIST: ReadonlyArray<readonly [string, RegExp]> = [
  ["practicing cardiologists", /\bpracticing\s+cardiologists?\b/i],
  ["licensed cardiologists", /\blicensed\s+cardiologists?\b/i],
  ["active clinicians", /\bactive\s+clinicians?\b/i],
  ["available cardiologists", /\bavailable\s+cardiologists?\b/i],
  ["full-time cardiologists", /\bfull[- ]time\s+cardiologists?\b/i],
  ["county has no cardiology care", /\bcount(?:y|ies)\s+(?:has|have)\s+no\s+cardiology\s+care\b/i],
  ["residents have no access", /\bresidents?\s+(?:has|have)\s+no\s+access\b/i],
  ["population without a cardiologist", /\bpopulation\s+without\s+(?:a\s+)?cardiologist\b/i],
  ["travel distance", /\btravel\s+distance\b/i],
  ["road distance", /\broad\s+distance\b/i],
  ["driving distance", /\bdriving\s+distance\b/i],
  ["travel time", /\btravel\s+time\b/i],
  ["nearest clinic", /\bnearest\s+clinic\b/i],
  ["heart-disease mortality", /\bheart\s+disease\s+mortality\b/i],
  ["mortality per 100,000", /\bmortality\s+per\s+100,?000\b/i],
  ["automatic update", /\bautomatically\s+updated\b/i],
  ["monthly update", /\bupdated\s+monthly\b/i],
  ["unsupported adoption", /\badopted\s+by\s+(?:researchers|policy\s*makers|hospitals|journalists)\b/i],
  [
    "NPI validation/credential claim",
    /\b(?:npi\s+validation|npi(?:\s+issuance)?\s+(?:validates?|proves?|confirms?|establishes?)\s+(?:licensure|licensing|credentials?|credentialing))\b/i,
  ],
  ["clinical validation", /\bclinical(?:ly)?\s+validat(?:e[ds]?|ion)\b/i],
  ["patient-level data", /\bpatient[- ]level\s+(?:data|information|records?)\b/i],
  ["service availability", /\bservice\s+availability\b/i],
  ["outcomes", /\boutcomes?\b/i],
  ["superseded count", /\b1,?810\b/i],
  ["superseded percentage", /\b57\.6\s*%/i],
  ["remote topology", /cdn\.jsdelivr\.net\/npm\/us-atlas|\bus-atlas@3\b/i],
];

const DENYLIST_POSITIVE_CONTROLS: ReadonlyArray<readonly [string, string]> = [
  ["practicing cardiologists", "practicing cardiologists"],
  ["licensed cardiologists", "licensed cardiologists"],
  ["active clinicians", "active clinicians"],
  ["available cardiologists", "available cardiologists"],
  ["full-time cardiologists", "full-time cardiologists"],
  ["This county has no cardiology care", "county has no cardiology care"],
  ["Residents have no access", "residents have no access"],
  ["population without a cardiologist", "population without a cardiologist"],
  ["travel distance", "travel distance"],
  ["road distance", "road distance"],
  ["driving distance", "driving distance"],
  ["travel time", "travel time"],
  ["nearest clinic", "nearest clinic"],
  ["heart disease mortality", "heart-disease mortality"],
  ["mortality per 100,000", "mortality per 100,000"],
  ["automatically updated monthly", "automatic update"],
  ["updated monthly", "monthly update"],
  ["adopted by researchers", "unsupported adoption"],
  ["NPI issuance validates credentials", "NPI validation/credential claim"],
  ["clinical validation", "clinical validation"],
  ["patient-level data", "patient-level data"],
  ["service availability", "service availability"],
  ["improved outcomes", "outcomes"],
  ["1,810", "superseded count"],
  ["57.6%", "superseded percentage"],
  ["https://cdn.jsdelivr.net/npm/us-atlas@3", "remote topology"],
];

function maskNarrowNegativeLimitations(content: string): string {
  let masked = content.toLocaleLowerCase("en-US").replace(/\s+/g, " ");
  for (const exact of NEGATIVE_LIMITATION_ALLOWLIST) {
    masked = masked.replaceAll(exact, "[allowed-negative-limitation]");
  }
  return masked;
}

function affirmativeClaimHits(content: string): string[] {
  const masked = maskNarrowNegativeLimitations(content);
  return AFFIRMATIVE_CLAIM_DENYLIST
    .filter(([, pattern]) => pattern.test(masked))
    .map(([label]) => label);
}

async function sourceFiles(directory: string, root: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await sourceFiles(fullPath, root)));
    else if (/\.(?:ts|tsx|mjs|md|json|css)$/.test(entry.name)) {
      files.push(path.relative(root, fullPath));
    }
  }
  return files;
}

async function currentSurfaceFiles(root: string): Promise<string[]> {
  const recursive = (
    await Promise.all(
      CURRENT_SURFACE_ROOTS.map((relative) =>
        sourceFiles(path.join(root, relative), root)
      )
    )
  ).flat();
  return [...new Set([...recursive, ...CURRENT_SURFACE_FILES])].sort();
}

describe("current public claim and semantic denylist", () => {
  it.each(DENYLIST_POSITIVE_CONTROLS)(
    "detects the case-insensitive positive control %s",
    (claim, label) => {
      expect(affirmativeClaimHits(claim.toLocaleUpperCase("en-US"))).toContain(label);
    }
  );

  it("is case-insensitive while allowing only the exact negative distance limitation", () => {
    expect(affirmativeClaimHits("NOT ROAD DISTANCE OR TRAVEL TIME.")).toEqual([]);
    expect(
      affirmativeClaimHits("Travel time to the nearest clinic for practicing cardiologists")
    ).toEqual(["practicing cardiologists", "travel time", "nearest clinic"]);
    expect(
      affirmativeClaimHits(
        "Not road distance or travel time; adopted by hospitals."
      )
    ).toEqual(["unsupported adoption"]);
  });

  it("rejects every §16.2 scientific overclaim while preserving only exact limitations", () => {
    expect(
      affirmativeClaimHits(
        "An NPI issuance VALIDATES licensure. The Atlas provides CLINICAL VALIDATION, PATIENT-LEVEL DATA, SERVICE AVAILABILITY, and OUTCOMES."
      )
    ).toEqual([
      "NPI validation/credential claim",
      "clinical validation",
      "patient-level data",
      "service availability",
      "outcomes",
    ]);
    expect(
      affirmativeClaimHits("Road distance, DRIVING DISTANCE, travel distance, and travel time to the nearest clinic.")
    ).toEqual([
      "travel distance",
      "road distance",
      "driving distance",
      "travel time",
      "nearest clinic",
    ]);
    expect(
      affirmativeClaimHits(
        "It contains no patient-level data. These area-level measures do not establish service availability, appointment access, or patient-level outcomes. CMS registration and hospital type do not establish current cardiology capability, operating status on the review date, or service availability."
      )
    ).toEqual([]);
    expect(
      affirmativeClaimHits("The Atlas has clinically validated outcomes and service availability.")
    ).toEqual(["clinical validation", "service availability", "outcomes"]);
  });

  it("contains no affirmative/stale claim on UI, metadata, OG, embed, or current app docs", async () => {
    const root = process.cwd();
    const hits: string[] = [];
    for (const relativePath of await currentSurfaceFiles(root)) {
      const content = await readFile(path.join(root, relativePath), "utf8");
      hits.push(
        ...affirmativeClaimHits(content).map(
          (claim) => `${relativePath}: ${claim}`
        )
      );
    }
    expect(hits).toEqual([]);
  });

  it("contains no retired PLACES field outside the explicit runtime denylist", async () => {
    const root = process.cwd();
    const hits: string[] = [];
    for (const relativePath of await currentSurfaceFiles(root)) {
      if (relativePath === "lib/data-contract.ts") continue;
      const content = await readFile(path.join(root, relativePath), "utf8");
      if (content.includes(FORBIDDEN_LEGACY_COUNTY_FIELD)) hits.push(relativePath);
    }
    expect(hits).toEqual([]);
  });
});
