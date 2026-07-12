import { execFile } from "node:child_process";
import { lstat, mkdtemp, mkdir, readFile, rm, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { afterEach, describe, expect, it } from "vitest";

import {
  REQUIRED_RELEASE_FILES,
  sha256Hex,
  verifyProductionAssets,
} from "../scripts/verify-production-assets.mjs";

const temporaryDirectories: string[] = [];
const execFileAsync = promisify(execFile);

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) =>
      rm(directory, { recursive: true, force: true })
    )
  );
});

function topologyBytes(objectName: "counties" | "states", ids: string[]): Buffer {
  return Buffer.from(
    `${JSON.stringify({
      type: "Topology",
      arcs: [],
      objects: {
        [objectName]: {
          type: "GeometryCollection",
          geometries: ids.map((id) => ({ type: "Polygon", id, arcs: [] })),
        },
      },
    })}\n`,
    "utf8"
  );
}

async function makeSyntheticRelease(sourceTreeLabel = "default") {
  const directory = await mkdtemp(path.join(tmpdir(), "atlas-assets-test-"));
  temporaryDirectories.push(directory);
  await mkdir(path.join(directory, "topology"));
  const countyBytes = await readFile(
    path.join(process.cwd(), "tests/fixtures/counties.synthetic.json")
  );
  const summaryBytes = await readFile(
    path.join(process.cwd(), "tests/fixtures/summary.synthetic.json")
  );
  const universeBytes = Buffer.from("01991\n01993\n01995\n", "utf8");
  const countyTopologyBytes = topologyBytes("counties", ["01991", "01993", "01995"]);
  const stateTopologyBytes = topologyBytes("states", ["01"]);
  const bytesByPath: Record<string, Buffer> = {
    "counties.json": countyBytes,
    "county-universe.ids.txt": universeBytes,
    "summary.json": summaryBytes,
    "topology/counties-2025-5m.topo.json": countyTopologyBytes,
    "topology/states-2025-5m.topo.json": stateTopologyBytes,
  };
  for (const [relative, bytes] of Object.entries(bytesByPath)) {
    await writeFile(path.join(directory, relative), bytes);
  }
  const sourceTreeId = sha256Hex(
    Buffer.from(`atlas synthetic frozen source-tree manifest v1: ${sourceTreeLabel}\n`, "utf8")
  );
  const manifestBytes = Buffer.from(
    `${JSON.stringify({
      schema_version: 1,
      source_tree_id: sourceTreeId,
      files: REQUIRED_RELEASE_FILES.map((relative) => ({
        path: relative,
        bytes: bytesByPath[relative].length,
        sha256: sha256Hex(bytesByPath[relative]),
      })),
    })}\n`,
    "utf8"
  );
  await writeFile(path.join(directory, "release-manifest.json"), manifestBytes);
  const contract = {
    countyCount: 3,
    chdNonNullCount: 2,
    chdNullCount: 1,
    countyUniverseSha256: sha256Hex(universeBytes),
    countyTopology: {
      path: "topology/counties-2025-5m.topo.json",
      objectName: "counties",
      bytes: countyTopologyBytes.length,
      sha256: sha256Hex(countyTopologyBytes),
      featureCount: 3,
      idPattern: /^[0-9]{5}$/,
      requiredIds: [],
      forbiddenIds: [],
    },
    stateTopology: {
      path: "topology/states-2025-5m.topo.json",
      objectName: "states",
      bytes: stateTopologyBytes.length,
      sha256: sha256Hex(stateTopologyBytes),
      featureCount: 1,
      idPattern: /^[0-9]{2}$/,
      requiredIds: [],
      forbiddenIds: [],
    },
  };
  return {
    dataDirectory: directory,
    expectedReleaseManifestSha256: sha256Hex(manifestBytes),
    contract,
    sourceTreeId,
  };
}

async function refreshSyntheticManifest(
  fixture: Awaited<ReturnType<typeof makeSyntheticRelease>>
) {
  const files = await Promise.all(
    REQUIRED_RELEASE_FILES.map(async (relative) => {
      const bytes = await readFile(path.join(fixture.dataDirectory, relative));
      return { path: relative, bytes: bytes.length, sha256: sha256Hex(bytes) };
    })
  );
  const manifestBytes = Buffer.from(
    `${JSON.stringify({
      schema_version: 1,
      source_tree_id: fixture.sourceTreeId,
      files,
    })}\n`,
    "utf8"
  );
  await writeFile(
    path.join(fixture.dataDirectory, "release-manifest.json"),
    manifestBytes
  );
  fixture.expectedReleaseManifestSha256 = sha256Hex(manifestBytes);
}

describe("production asset prebuild/server verifier", () => {
  it("keeps operational controls and the internal dictionary outside app/public", async () => {
    for (const relative of [
      "public/data/README.md",
      "public/data/data-dictionary.json",
      "public/data/topology/README.md",
    ]) {
      await expect(lstat(path.join(process.cwd(), relative))).rejects.toMatchObject({
        code: "ENOENT",
      });
    }
    for (const relative of [
      "control/public-data-contract.md",
      "control/data-dictionary.json",
      "control/topology-contract.md",
    ]) {
      expect((await lstat(path.join(process.cwd(), relative))).isFile()).toBe(true);
    }
  });

  it("is wired into both the npm build lifecycle and the server loader", async () => {
    const packageJson = JSON.parse(
      await readFile(path.join(process.cwd(), "package.json"), "utf8")
    );
    expect(packageJson.scripts.prebuild).toBe(
      "node scripts/verify-production-assets.mjs"
    );
    const serverLoader = await readFile(
      path.join(process.cwd(), "lib/counties.ts"),
      "utf8"
    );
    expect(serverLoader).toContain("await verifyProductionAssets({");
    expect(serverLoader).toContain(
      "expectedReleaseManifestSha256: manifestSha256"
    );
    expect(serverLoader).not.toContain("public\", \"data");
  });

  it("resolves two absolute clean-room data roots without an explicit function path", async () => {
    const first = await makeSyntheticRelease("clean-room-a");
    const second = await makeSyntheticRelease("clean-room-b");
    const original = process.env.ATLAS_RELEASE_DATA_DIR;
    try {
      for (const fixture of [first, second]) {
        process.env.ATLAS_RELEASE_DATA_DIR = fixture.dataDirectory;
        const result = await verifyProductionAssets({
          expectedReleaseManifestSha256: fixture.expectedReleaseManifestSha256,
          contract: fixture.contract,
        });
        expect(result.sourceTreeId).toBe(fixture.sourceTreeId);
        expect(result.universeIds).toEqual(["01991", "01993", "01995"]);
      }
    } finally {
      if (original === undefined) delete process.env.ATLAS_RELEASE_DATA_DIR;
      else process.env.ATLAS_RELEASE_DATA_DIR = original;
    }
    expect(first.dataDirectory).not.toBe(second.dataDirectory);
    expect(first.sourceTreeId).not.toBe(second.sourceTreeId);
  });

  it("rejects a relative ATLAS_RELEASE_DATA_DIR before reading the default served tree", async () => {
    const fixture = await makeSyntheticRelease("relative-path-rejection");
    await expect(
      execFileAsync(
        process.execPath,
        [path.join(process.cwd(), "scripts", "verify-production-assets.mjs")],
        {
          cwd: process.cwd(),
          env: {
            ...process.env,
            ATLAS_RELEASE_DATA_DIR: "public/data",
            ATLAS_RELEASE_MANIFEST_SHA256: fixture.expectedReleaseManifestSha256,
          },
        }
      )
    ).rejects.toMatchObject({
      stderr: expect.stringContaining(
        "ATLAS_RELEASE_DATA_DIR/dataDirectory must be an absolute path"
      ),
    });
  });

  it("accepts a fully hash-bound synthetic county/summary/universe/topology release", async () => {
    const fixture = await makeSyntheticRelease();
    const result = await verifyProductionAssets(fixture);
    expect(result.sourceTreeId).toBe(fixture.sourceTreeId);
    expect(result.universeIds).toEqual(["01991", "01993", "01995"]);
    expect(result.summary).toHaveProperty("zero_provider_count", 2);
  });

  it("fails closed on missing or altered served assets", async () => {
    const missing = await makeSyntheticRelease();
    await unlink(path.join(missing.dataDirectory, "summary.json"));
    await expect(verifyProductionAssets(missing)).rejects.toThrow(/required release assets/);

    const altered = await makeSyntheticRelease();
    await writeFile(path.join(altered.dataDirectory, "counties.json"), "[]\n");
    await expect(verifyProductionAssets(altered)).rejects.toThrow(/release-manifest/);

  });

  it("requires the manifest to bind the exact recursive served set, excluding only itself", async () => {
    const fixture = await makeSyntheticRelease();
    await mkdir(path.join(fixture.dataDirectory, "unlisted"));
    await writeFile(path.join(fixture.dataDirectory, "unlisted", "extra.json"), "{}\n");
    await expect(verifyProductionAssets(fixture)).rejects.toThrow(
      /served data file set differs from release-manifest/
    );
  });

  it.each([
    ["PENDING", /forbidden PENDING\/PENDING_PHASE text/],
    ["PENDING_PHASE", /forbidden PENDING\/PENDING_PHASE text/],
    ["PLACEHOLDER", /forbidden PLACEHOLDER text/],
    ["Phase 2A", /forbidden PHASE 2A\/2B text/],
    ["PHASE 2B", /forbidden PHASE 2A\/2B text/],
    ["Gate B", /forbidden internal gate text/],
  ])("rejects the internal served-data sentinel %s", async (marker, expected) => {
    const fixture = await makeSyntheticRelease();
    const countyPath = path.join(fixture.dataDirectory, "counties.json");
    const rows = JSON.parse(await readFile(countyPath, "utf8"));
    rows[0].county = marker;
    await writeFile(countyPath, `${JSON.stringify(rows)}\n`);
    await refreshSyntheticManifest(fixture);
    await expect(verifyProductionAssets(fixture)).rejects.toThrow(expected);
  });

  it("requires an externally approved release-manifest hash before reading assets", async () => {
    const fixture = await makeSyntheticRelease();
    await expect(
      verifyProductionAssets({ ...fixture, expectedReleaseManifestSha256: undefined })
    ).rejects.toThrow(/must be set before any production asset is read/);
  });
});
