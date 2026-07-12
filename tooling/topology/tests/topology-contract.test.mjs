import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { access, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildMapshaperArgs,
  buildPlan,
  canonicalIdListBytes,
  loadTopologyConfig,
  parseSourceTreeManifest,
  parseCanonicalIdList,
  requirePhase2bAuthorization,
  sha256,
  validateExtractedMembersManifest,
  validateMaterializedFeatures,
  validateSourceArchive,
  validateTopologyConfig,
  validateTopologyIdentity,
  validateTopologyOutputBytes,
  verifyExtractedSourceTree,
} from "../topology-contract.mjs";
import { deriveTopologies, verifyCanonicalSourceTreeWithPython } from "../topology.mjs";

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const TOPOLOGY_DIR = path.resolve(TEST_DIR, "..");
const FIXTURE_DIR = path.join(TEST_DIR, "fixtures");
const CONFIG = await loadTopologyConfig();

test("topology contract freezes source, tool, runtime, output, and display-only identities", () => {
  validateTopologyConfig(CONFIG);

  assert.deepEqual(CONFIG.runtime, { node: "22.17.0", npm: "10.9.2" });
  assert.equal(CONFIG.tools.mapshaper.version, "0.7.41");
  assert.equal(
    CONFIG.tools.mapshaper.packageSha256,
    "770018b5c6fb29e001c52613251de9df618cd9d7a3bf2e947f962749e77c13d5",
  );
  assert.equal(CONFIG.tools.topojsonClient.version, "3.1.0");
  assert.equal(CONFIG.generation.quantization, 100000);
  assert.equal(CONFIG.generation.additionalSimplification, false);
  assert.equal(CONFIG.generation.independentDerivations, 2);
  assert.equal(CONFIG.analyticalUse, "forbidden_display_only");

  assert.deepEqual(CONFIG.targets.counties.requiredConnecticutIds, [
    "09110",
    "09120",
    "09130",
    "09140",
    "09150",
    "09160",
    "09170",
    "09180",
    "09190",
  ]);
  assert.deepEqual(CONFIG.targets.counties.forbiddenConnecticutIds, [
    "09001",
    "09003",
    "09005",
    "09007",
    "09009",
    "09011",
    "09013",
    "09015",
  ]);
});

test("county and state mapshaper arguments exactly match the preregistered commands", () => {
  assert.deepEqual(buildMapshaperArgs(CONFIG, "counties"), [
    "-i",
    "cb_2025_us_county_5m.shp",
    "encoding=utf8",
    "-filter",
    '!["60","66","69","72","78"].includes(STATEFP)',
    "-sort",
    "GEOID",
    "-filter-fields",
    "GEOID",
    "-proj",
    "wgs84",
    "-rename-layers",
    "counties",
    "-o",
    "counties-2025-5m.topo.json",
    "format=topojson",
    "quantization=100000",
    "id-field=GEOID",
    "drop-table",
    "force",
  ]);
  assert.deepEqual(buildMapshaperArgs(CONFIG, "states"), [
    "-i",
    "cb_2025_us_state_5m.shp",
    "encoding=utf8",
    "-filter",
    '!["60","66","69","72","78"].includes(STATEFP)',
    "-sort",
    "GEOID",
    "-filter-fields",
    "GEOID",
    "-proj",
    "wgs84",
    "-rename-layers",
    "states",
    "-o",
    "states-2025-5m.topo.json",
    "format=topojson",
    "quantization=100000",
    "id-field=GEOID",
    "drop-table",
    "force",
  ]);
});

test("plan is deterministic, source-free, and keeps derivation pending Phase 2B", () => {
  const first = buildPlan(CONFIG);
  const second = buildPlan(CONFIG);
  assert.deepEqual(first, second);
  assert.match(CONFIG.derivationStatus, /^pending_phase_2b/);
  assert.equal(CONFIG.sources.counties.extractedMembers.length, 0);
  assert.equal(CONFIG.sources.states.extractedMembers.length, 0);
  assert.equal(first[0].arguments[1], "${EXTRACTED_SOURCE_DIR}/cb_2025_us_county_5m.shp");
  assert.equal(
    first[0].arguments[14],
    "${STAGING_OUTPUT_DIR}/counties-2025-5m.topo.json",
  );
  assert.ok(first.every((entry) => !entry.arguments.some((arg) => /simplif/i.test(arg))));
});

test("the source-bound derive path refuses Phase 2A before touching inputs or outputs", async () => {
  await assert.rejects(
    deriveTopologies(CONFIG, {
      "archives-dir": "/does/not/exist",
      "sources-dir": "/does/not/exist",
      "universe-dir": "/does/not/exist",
      "output-dir": "/does/not/exist",
    }),
    /--authorization-receipt is required/,
  );
  await assert.rejects(
    deriveTopologies(CONFIG, { "authorization-receipt-sha256": sha256(Buffer.from("legacy self pin")) }),
    /unknown derive option/,
  );
});

test("direct topology entrypoint recomputes the canonical source tree in isolated CPython", async () => {
  const repoRoot = path.resolve(TOPOLOGY_DIR, "../..");
  const pythonExecutable = path.join(
    homedir(),
    ".local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12",
  );
  await access(pythonExecutable);
  const temporary = await mkdtemp(path.join(tmpdir(), "atlas-source-freeze-"));
  const manifestPath = path.join(temporary, "source-tree.jsonl");
  try {
    const generator = [
      "import sys",
      "from pathlib import Path",
      "sys.path.insert(0, sys.argv[1])",
      "from atlas_pipeline.source_tree import generate_source_tree_manifest",
      "identity = generate_source_tree_manifest(Path(sys.argv[2]))",
      "Path(sys.argv[3]).write_bytes(identity.manifest_bytes)",
      "print(identity.source_tree_id)",
    ].join("; ");
    const sourceTreeId = execFileSync(
      pythonExecutable,
      ["-I", "-c", generator, path.join(repoRoot, "scripts"), repoRoot, manifestPath],
      { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
    ).trim();
    verifyCanonicalSourceTreeWithPython(
      {
        "repo-root": repoRoot,
        "python-executable": pythonExecutable,
        "source-tree-manifest": manifestPath,
      },
      sourceTreeId,
    );
    const manifestRows = parseSourceTreeManifest(await readFile(manifestPath));
    manifestRows[0].sha256 = sha256(Buffer.from("self-authored drift"));
    await writeFile(
      manifestPath,
      Buffer.from(manifestRows.map((row) => JSON.stringify(row)).join("\n") + "\n", "utf8"),
    );
    assert.throws(
      () => verifyCanonicalSourceTreeWithPython(
        {
          "repo-root": repoRoot,
          "python-executable": pythonExecutable,
          "source-tree-manifest": manifestPath,
        },
        sourceTreeId,
      ),
      /Command failed/,
    );
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("Phase 2B authority binds receipt file, source_tree_id, config path/hash, and frozen dependencies", async () => {
  const canonicalControlBytes = (value) => Buffer.from(`${JSON.stringify(value)}\n`, "utf8");
  const configBytes = Buffer.from(`${JSON.stringify(CONFIG)}\n`, "utf8");
  const configPath = "tooling/topology/topology-contract.json";
  const manifestBytes = canonicalControlBytes({
    path: configPath,
    bytes: configBytes.length,
    sha256: sha256(configBytes),
  });
  const dependencyReceiptBytes = Buffer.from(
    `${JSON.stringify({
      schema_version: 1,
      status: "verified_frozen",
      lockfile_path: "tooling/topology/package-lock.json",
      lockfile_sha256: CONFIG.dependencyMaterialization.requiredLockfileSha256,
      cache_or_image_sha256: sha256(Buffer.from("synthetic-cache-image")),
      operating_system: "synthetic-os",
      platform: "synthetic-platform",
      architecture: "synthetic-arch",
      node_version: "22.17.0",
      npm_version: "10.9.2",
      install_command: "npm ci --offline --ignore-scripts",
      install_exit_code: 0,
      network_denied: true,
      network_denied_evidence_sha256: sha256(Buffer.from("synthetic-network-policy")),
    })}\n`,
    "utf8",
  );
  const sourceTreeId = sha256(manifestBytes);
  const bundleManifestBytes = canonicalControlBytes({
    schema_version: "atlas-v19-phase2b-bundle-1",
    phase: "2B",
    sources: [],
  });
  const controlReceiptsManifestBytes = canonicalControlBytes({
    schema_version: "atlas-v19-control-receipts-manifest-1",
    source_tree_id: sourceTreeId,
    bundle_manifest_sha256: sha256(bundleManifestBytes),
    authorization_id: "SYNTHETIC-TOPOLOGY-AUTHORIZATION",
    receipts: [],
  });
  function sidecarPins(sourceKey) {
    const stem = CONFIG.sources[sourceKey].inputShapefile.slice(0, -4);
    return [".cpg", ".dbf", ".prj", ".shp", ".shx"].map((extension) => {
      const body = Buffer.from(`${sourceKey}${extension}`);
      return { path: `${stem}${extension}`, bytes: body.length, sha256: sha256(body) };
    });
  }
  const extractedMembersManifest = {
    schema_version: 1,
    status: "frozen_phase_2b",
    source_tree_id: sourceTreeId,
    sources: {
      counties: {
        archive_sha256: CONFIG.sources.counties.archiveSha256,
        members: sidecarPins("counties"),
      },
      states: {
        archive_sha256: CONFIG.sources.states.archiveSha256,
        members: sidecarPins("states"),
      },
    },
  };
  validateExtractedMembersManifest(CONFIG, extractedMembersManifest, sourceTreeId);
  const extractedMembersManifestBytes = Buffer.from(
    `${JSON.stringify(extractedMembersManifest)}\n`,
    "utf8",
  );
  const authorizationReceiptBytes = canonicalControlBytes(
    {
      schema_version: 1,
      authorization_id: "SYNTHETIC-TOPOLOGY-AUTHORIZATION",
      phase: "2B",
      status: "AUTHORIZED_PHASE_2B",
      source_tree_id: sourceTreeId,
      bundle_manifest_sha256: sha256(bundleManifestBytes),
      integrator_control_id: "SYNTHETIC-TOPOLOGY-CONTROL",
      topology_config_path: configPath,
      topology_config_sha256: sha256(configBytes),
      dependency_receipt_sha256: sha256(dependencyReceiptBytes),
      extracted_members_manifest_sha256: sha256(extractedMembersManifestBytes),
      approved_by: "synthetic-integrator",
      approved_at_utc: "2026-07-10T00:00:00Z",
    },
  );
  const integratorControlBytes = canonicalControlBytes({
    schema_version: "atlas-v19-integrator-trust-root-1",
    control_id: "SYNTHETIC-TOPOLOGY-CONTROL",
    phase: "2B",
    source_tree_manifest_sha256: sourceTreeId,
    bundle_manifest_sha256: sha256(bundleManifestBytes),
    authorization_receipt_sha256: sha256(authorizationReceiptBytes),
    review_config_sha256: null,
    control_receipts_manifest_sha256: sha256(controlReceiptsManifestBytes),
    storage_preflight_receipt_sha256: null,
    approved_by: "independent-synthetic-integrator",
    approved_at_utc: "2026-07-10T00:00:01Z",
  });
  const evidence = {
    authorizationReceiptBytes,
    dependencyReceiptBytes,
    extractedMembersManifestBytes,
    sourceTreeManifestBytes: manifestBytes,
    topologyConfigBytes: configBytes,
    topologyConfigManifestPath: configPath,
    integratorControlBytes,
    expectedIntegratorControlSha256: sha256(integratorControlBytes),
    bundleManifestBytes,
    controlReceiptsManifestBytes,
  };
  const verified = requirePhase2bAuthorization(CONFIG, evidence);
  assert.equal(verified.sourceTreeId, sha256(manifestBytes));
  assert.equal(verified.dependencyReceipt.status, "verified_frozen");
  assert.equal(
    CONFIG.sources.counties.extractedMemberIdentityStatus,
    "pending_phase_2b_authorized_extraction",
  );
  assert.throws(
    () => requirePhase2bAuthorization(CONFIG, { ...evidence, expectedIntegratorControlSha256: sha256(Buffer.from("unapproved control")) }),
    /independently approved SHA-256/,
  );
  assert.throws(
    () => requirePhase2bAuthorization(CONFIG, { ...evidence, expectedIntegratorControlSha256: undefined }),
    /independently approved SHA-256/,
  );
  assert.throws(
    () => requirePhase2bAuthorization(CONFIG, { ...evidence, topologyConfigBytes: Buffer.concat([configBytes, Buffer.from("\n")]) }),
    /config path\/hash/,
  );
  const unsafeManifest = structuredClone(extractedMembersManifest);
  unsafeManifest.sources.counties.members[0].path = "../outside.shp";
  assert.throws(
    () => validateExtractedMembersManifest(CONFIG, unsafeManifest, sourceTreeId),
    /extracted member 1 is invalid/,
  );
});

test("extracted topology tree requires exact real files, sidecars, and no symlinks/extras", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "atlas-topology-members-"));
  const sourceTreeId = sha256(Buffer.from("synthetic canonical source manifest"));
  const sources = {};
  try {
    for (const sourceKey of ["counties", "states"]) {
      const stem = CONFIG.sources[sourceKey].inputShapefile.slice(0, -4);
      const members = [];
      for (const extension of [".cpg", ".dbf", ".prj", ".shp", ".shx"]) {
        const memberPath = `${stem}${extension}`;
        const body = Buffer.from(`${sourceKey}:${extension}:fixture`);
        await writeFile(path.join(root, memberPath), body, { flag: "wx" });
        members.push({ path: memberPath, bytes: body.length, sha256: sha256(body) });
      }
      sources[sourceKey] = {
        archive_sha256: CONFIG.sources[sourceKey].archiveSha256,
        members,
      };
    }
    const manifest = {
      schema_version: 1,
      status: "frozen_phase_2b",
      source_tree_id: sourceTreeId,
      sources,
    };
    validateExtractedMembersManifest(CONFIG, manifest, sourceTreeId);
    assert.equal((await verifyExtractedSourceTree(CONFIG, manifest, root)).length, 10);

    await writeFile(path.join(root, "undeclared.txt"), "extra");
    await assert.rejects(
      verifyExtractedSourceTree(CONFIG, manifest, root),
      /recursive member set mismatch/,
    );
    await rm(path.join(root, "undeclared.txt"));

    await mkdir(path.join(root, "undeclared-directory"));
    await assert.rejects(
      verifyExtractedSourceTree(CONFIG, manifest, root),
      /undeclared extracted source directory/,
    );
    await rm(path.join(root, "undeclared-directory"), { recursive: true });

    const target = path.join(root, sources.counties.members[0].path);
    await rm(target);
    await symlink(path.join(root, sources.counties.members[1].path), target);
    await assert.rejects(
      verifyExtractedSourceTree(CONFIG, manifest, root),
      /symlink is forbidden/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("canonical ID serialization is ascending UTF-8 with one terminal LF", () => {
  assert.equal(canonicalIdListBytes(["12", "01", "09"]).toString("utf8"), "01\n09\n12\n");
  assert.deepEqual(parseCanonicalIdList("01\n09\n12\n", "^[0-9]{2}$"), [
    "01",
    "09",
    "12",
  ]);
  assert.throws(() => parseCanonicalIdList("01\n09", "^[0-9]{2}$"), /end with LF/);
  assert.throws(
    () => parseCanonicalIdList("09\n01\n", "^[0-9]{2}$"),
    /unique and ascending ASCII/,
  );

  const stateUniverse =
    "01 02 04 05 06 08 09 10 11 12 13 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48 49 50 51 53 54 55 56".split(
      " ",
    );
  assert.equal(
    sha256(canonicalIdListBytes(stateUniverse)),
    "aa0c99338dbb5d715b2990eeeeb2e4a5481f6a3285c05981764bf09862af48a4",
  );
});

async function syntheticCase(key) {
  const bytes = await readFile(path.join(FIXTURE_DIR, `synthetic-${key}.topo.json`));
  const universeBytes = await readFile(
    path.join(FIXTURE_DIR, `synthetic-${key}.ids.txt`),
  );
  const universeIds = parseCanonicalIdList(
    universeBytes,
    key === "counties" ? "^[0-9]{5}$" : "^[0-9]{2}$",
  );
  const target = {
    objectName: key,
    expectedOutputBytes: bytes.length,
    expectedOutputSha256: sha256(bytes),
    expectedFeatureCount: universeIds.length,
    idPattern: key === "counties" ? "^[0-9]{5}$" : "^[0-9]{2}$",
    expectedIdListSha256: sha256(universeBytes),
    requiredConnecticutIds: key === "counties" ? ["09110"] : [],
    forbiddenConnecticutIds: key === "counties" ? ["09001"] : [],
  };
  return { bytes, target, universeIds };
}

test("fabricated topology fixtures satisfy byte, hash, object, ID, and universe checks", async () => {
  for (const key of ["counties", "states"]) {
    const { bytes, target, universeIds } = await syntheticCase(key);
    validateTopologyOutputBytes(bytes, target);
    const ids = validateTopologyIdentity(
      JSON.parse(bytes.toString("utf8")),
      target,
      universeIds,
    );
    assert.deepEqual(ids.sort(), [...universeIds].sort());
  }
});

test("topology verification rejects drift instead of silently repinning", async () => {
  const { bytes, target, universeIds } = await syntheticCase("counties");
  assert.throws(
    () => validateTopologyOutputBytes(bytes, { ...target, expectedOutputBytes: bytes.length + 1 }),
    /byte count mismatch/,
  );
  assert.throws(
    () =>
      validateTopologyOutputBytes(bytes, {
        ...target,
        expectedOutputSha256: sha256(Buffer.from("drifted-topology-output", "utf8")),
      }),
    /SHA-256 mismatch/,
  );

  const numericId = JSON.parse(bytes.toString("utf8"));
  numericId.objects.counties.geometries[0].id = 1001;
  assert.throws(
    () => validateTopologyIdentity(numericId, target, universeIds),
    /feature IDs must be matching strings/,
  );

  const wrongObject = JSON.parse(bytes.toString("utf8"));
  wrongObject.objects.extra = wrongObject.objects.counties;
  assert.throws(
    () => validateTopologyIdentity(wrongObject, target, universeIds),
    /object names does not match/,
  );

  const territory = JSON.parse(bytes.toString("utf8"));
  territory.objects.counties.geometries[0].id = "60010";
  const territoryUniverse = ["60010", "09110", "12001"];
  const territoryTarget = {
    ...target,
    expectedIdListSha256: sha256(canonicalIdListBytes(territoryUniverse)),
  };
  assert.throws(
    () => validateTopologyIdentity(territory, territoryTarget, territoryUniverse),
    /excluded territory ID/,
  );
});

test("synthetic materialization contract rejects null and non-polygon geometry", () => {
  const valid = {
    type: "FeatureCollection",
    features: [
      { type: "Feature", geometry: { type: "Polygon", coordinates: [] } },
      { type: "Feature", geometry: { type: "MultiPolygon", coordinates: [] } },
    ],
  };
  validateMaterializedFeatures(valid, 2);
  assert.throws(
    () => validateMaterializedFeatures({ ...valid, features: [{ geometry: null }] }, 1),
    /must not be null/,
  );
  assert.throws(
    () =>
      validateMaterializedFeatures(
        { ...valid, features: [{ geometry: { type: "Point", coordinates: [] } }] },
        1,
      ),
    /must be Polygon or MultiPolygon/,
  );
});

test("synthetic source and output identity checks fail on stale or altered bytes", () => {
  const bytes = Buffer.from("synthetic archive bytes\n", "utf8");
  const source = {
    pin: "SYNTHETIC",
    archiveBytes: bytes.length,
    archiveSha256: sha256(bytes),
  };
  validateSourceArchive(bytes, source);
  assert.throws(
    () => validateSourceArchive(Buffer.concat([bytes, Buffer.from("drift")]), source),
    /byte count mismatch/,
  );
  assert.throws(
    () => validateSourceArchive(Buffer.from("synthetic archive bytez\n"), source),
    /SHA-256 mismatch/,
  );
});

test("offline package lock pins mapshaper and topojson-client without Git or SSH resolution", async () => {
  const packageJson = JSON.parse(
    await readFile(path.join(TOPOLOGY_DIR, "package.json"), "utf8"),
  );
  const lock = JSON.parse(
    await readFile(path.join(TOPOLOGY_DIR, "package-lock.json"), "utf8"),
  );
  assert.equal(packageJson.packageManager, "npm@10.9.2");
  assert.deepEqual(packageJson.engines, { node: "22.17.0", npm: "10.9.2" });
  assert.equal(lock.lockfileVersion, 3);
  assert.equal(
    sha256(await readFile(path.join(TOPOLOGY_DIR, "package-lock.json"))),
    CONFIG.dependencyMaterialization.requiredLockfileSha256,
  );
  assert.equal(lock.packages["node_modules/mapshaper"].version, "0.7.41");
  assert.equal(
    lock.packages["node_modules/mapshaper"].integrity,
    CONFIG.tools.mapshaper.npmIntegrity,
  );
  assert.equal(lock.packages["node_modules/topojson-client"].version, "3.1.0");
  assert.equal(
    lock.packages["node_modules/topojson-client"].integrity,
    CONFIG.tools.topojsonClient.npmIntegrity,
  );
  for (const entry of Object.values(lock.packages)) {
    assert.doesNotMatch(entry.resolved ?? "", /^(?:git|git\+|ssh)/i);
  }
});

test("the exact cached mapshaper package executes twice on synthetic geometry without network or install", async () => {
  const digestHex = Buffer.from(
    CONFIG.tools.mapshaper.npmIntegrity.split("-", 2)[1],
    "base64",
  ).toString("hex");
  const packageTarball = path.join(
    homedir(),
    ".npm/_cacache/content-v2/sha512",
    digestHex.slice(0, 2),
    digestHex.slice(2, 4),
    digestHex.slice(4),
  );
  const packageBytes = await readFile(packageTarball);
  assert.equal(packageBytes.length, CONFIG.tools.mapshaper.packageBytes);
  assert.equal(sha256(packageBytes), CONFIG.tools.mapshaper.packageSha256);

  const temporary = await mkdtemp(path.join(tmpdir(), "atlas-mapshaper-proof-"));
  try {
    const lock = JSON.parse(
      await readFile(path.join(TOPOLOGY_DIR, "package-lock.json"), "utf8"),
    );
    for (const [packagePath, entry] of Object.entries(lock.packages)) {
      if (!packagePath.startsWith("node_modules/") || !entry.integrity) continue;
      const [algorithm, encoded] = entry.integrity.split("-", 2);
      const digest = Buffer.from(encoded, "base64").toString("hex");
      const cached = path.join(
        homedir(),
        `.npm/_cacache/content-v2/${algorithm}`,
        digest.slice(0, 2),
        digest.slice(2, 4),
        digest.slice(4),
      );
      try {
        await access(cached);
      } catch (error) {
        if (entry.optional) continue;
        throw new Error(`required frozen package is absent from the read-only cache: ${packagePath}`, {
          cause: error,
        });
      }
      const destination = path.join(temporary, packagePath);
      await mkdir(destination, { recursive: true });
      execFileSync("tar", [
        "-xzf", cached, "-C", destination, "--strip-components=1",
      ], { stdio: ["ignore", "pipe", "pipe"] });
    }
    const executable = path.join(temporary, "node_modules/mapshaper/bin/mapshaper");
    const input = path.join(FIXTURE_DIR, "synthetic-mapshaper-source.geo.json");
    const outputs = [path.join(temporary, "a.topo.json"), path.join(temporary, "b.topo.json")];
    for (const output of outputs) {
      execFileSync(process.execPath, [
        executable,
        input,
        "-sort", "GEOID",
        "-filter-fields", "GEOID",
        "-rename-layers", "counties",
        "-o", output,
        "format=topojson",
        "quantization=100000",
        "id-field=GEOID",
        "drop-table",
        "force",
      ], { stdio: ["ignore", "pipe", "pipe"] });
    }
    const [first, second] = await Promise.all(outputs.map((output) => readFile(output)));
    assert.deepEqual(first, second);
    const topology = JSON.parse(first.toString("utf8"));
    assert.deepEqual(
      topology.objects.counties.geometries.map((geometry) => geometry.id),
      ["01991", "01993"],
    );
  } finally {
    execFileSync("chmod", ["-R", "700", temporary], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    await rm(temporary, { recursive: true, force: true });
  }
});
