import { createHash } from "node:crypto";
import { lstat, readFile, readdir, realpath } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const TOPOLOGY_DIR = path.dirname(fileURLToPath(import.meta.url));
export const DEFAULT_CONFIG_PATH = path.join(
  TOPOLOGY_DIR,
  "topology-contract.json",
);

const EXPECTED = Object.freeze({
  runtime: { node: "22.17.0", npm: "10.9.2" },
  mapshaper: {
    version: "0.7.41",
    packageBytes: 3_786_772,
    packageSha256:
      "770018b5c6fb29e001c52613251de9df618cd9d7a3bf2e947f962749e77c13d5",
    npmIntegrity:
      "sha512-TC1KL8OtYZPRuq/Wl0AW2LQOic/q/AQbEEiPkCXlN5dGFXPIRj8+Qllc3EWBhN7aViHStI2IIthP2/L7BDzobQ==",
  },
  topojsonClient: {
    version: "3.1.0",
    packageUrl:
      "https://registry.npmjs.org/topojson-client/-/topojson-client-3.1.0.tgz",
    npmIntegrity:
      "sha512-605uxS6bcYxGXw9qi62XyrV6Q3xwbndjachmNxu8HWTtVPxZfEJN9fd/SZS1Q54Sn2y0TMyMxFj/cJINqGHrKw==",
  },
  excludedStateFips: ["60", "66", "69", "72", "78"],
  outputs: {
    counties: {
      bytes: 1_349_047,
      sha256:
        "affdae57c1fe33dd5e64ccf1c735f83db830374d5dcff375231a667aa96aa4d5",
      features: 3_144,
      idListSha256:
        "f2651ec6a9375476e3ff09efb4c2a699cd5690ffd3f0c982aeacad967c21c241",
    },
    states: {
      bytes: 465_345,
      sha256:
        "05a7896dc0cd3be17813dbe7b571e74eafd690a6078a99698aa9a52df01b6bbe",
      features: 51,
      idListSha256:
        "aa0c99338dbb5d715b2990eeeeb2e4a5481f6a3285c05981764bf09862af48a4",
    },
  },
});

function invariant(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function exactArray(actual, expected, label) {
  invariant(Array.isArray(actual), `${label} must be an array`);
  invariant(
    JSON.stringify(actual) === JSON.stringify(expected),
    `${label} does not match the frozen contract`,
  );
}

function exactKeys(actual, expected, label) {
  invariant(actual && typeof actual === "object" && !Array.isArray(actual), `${label} must be an object`);
  exactArray(Object.keys(actual), expected, `${label} keys`);
}

function parseJsonBytes(bytes, label) {
  try {
    return JSON.parse(Buffer.from(bytes).toString("utf8"));
  } catch (error) {
    throw new Error(`${label} is not valid UTF-8 JSON`, { cause: error });
  }
}

function normalizeCanonicalJson(value) {
  if (typeof value === "string") return value.normalize("NFC");
  if (Array.isArray(value)) return value.map(normalizeCanonicalJson);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, child]) => [
        key.normalize("NFC"),
        normalizeCanonicalJson(child),
      ]),
    );
  }
  invariant(typeof value !== "number" || Number.isFinite(value),
    "canonical control JSON forbids non-finite numbers");
  return value;
}

function parseCanonicalJsonBytes(bytes, label) {
  const value = parseJsonBytes(bytes, label);
  const canonical = Buffer.from(`${JSON.stringify(normalizeCanonicalJson(value))}\n`, "utf8");
  invariant(canonical.equals(Buffer.from(bytes)), `${label} is not canonical UTF-8/LF JSON`);
  function rejectSentinels(item) {
    if (typeof item === "string") {
      invariant(!/(?:PENDING|PLACEHOLDER)/i.test(item), `${label} contains an unresolved sentinel`);
    } else if (Array.isArray(item)) {
      item.forEach(rejectSentinels);
    } else if (item && typeof item === "object") {
      for (const [key, child] of Object.entries(item)) {
        invariant(!/(?:PENDING|PLACEHOLDER)/i.test(key), `${label} contains an unresolved sentinel key`);
        rejectSentinels(child);
      }
    }
  }
  rejectSentinels(value);
  return value;
}

export function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

export function canonicalIdListBytes(ids) {
  invariant(Array.isArray(ids), "IDs must be an array");
  invariant(ids.length > 0, "ID list must not be empty");
  for (const id of ids) {
    invariant(typeof id === "string", "every canonical ID must be a string");
  }
  const sorted = [...ids].sort((left, right) =>
    left < right ? -1 : left > right ? 1 : 0,
  );
  invariant(
    new Set(sorted).size === sorted.length,
    "canonical ID list contains duplicates",
  );
  return Buffer.from(`${sorted.join("\n")}\n`, "utf8");
}

export function parseCanonicalIdList(bytes, expectedPattern) {
  const text = Buffer.isBuffer(bytes) ? bytes.toString("utf8") : String(bytes);
  invariant(text.endsWith("\n"), "canonical ID list must end with LF");
  invariant(!text.endsWith("\n\n"), "canonical ID list must have one terminal LF");
  invariant(!text.includes("\r"), "canonical ID list must use LF, not CRLF");
  const ids = text.slice(0, -1).split("\n");
  const pattern = new RegExp(expectedPattern);
  for (const id of ids) {
    invariant(pattern.test(id), `invalid canonical ID: ${id}`);
  }
  invariant(
    canonicalIdListBytes(ids).equals(Buffer.from(text, "utf8")),
    "canonical ID list must be unique and ascending ASCII",
  );
  return ids;
}

export async function loadTopologyConfig(configPath = DEFAULT_CONFIG_PATH) {
  const config = JSON.parse(await readFile(configPath, "utf8"));
  validateTopologyConfig(config);
  return config;
}

export function validateTopologyConfig(config) {
  invariant(config.schemaVersion === 1, "topology schemaVersion must be 1");
  invariant(
    config.phase === "2A",
    "the manifested topology config remains Phase 2A; Phase 2B authority is external evidence",
  );
  invariant(
    config.derivationStatus ===
      "pending_phase_2b_source_materialization_and_integrator_authorization",
    "the source-tree config cannot self-authorize topology derivation",
  );
  invariant(
    config.authorizationReceiptSha256 === null,
    "the manifested config must not embed a circular authorization-receipt hash",
  );
  invariant(
    config.phase2bAuthorization?.model === "external_hash_identified_receipt" &&
      config.phase2bAuthorization?.requiredStatus === "AUTHORIZED_PHASE_2B" &&
      config.phase2bAuthorization?.topologyConfigManifestPath ===
        "tooling/topology/topology-contract.json" &&
      config.phase2bAuthorization?.requiredDependencyStatus === "verified_frozen",
    "Phase 2B external authorization contract is not frozen",
  );
  invariant(
    config.analyticalUse === "forbidden_display_only",
    "display topology must be forbidden for analytical use",
  );
  invariant(
    config.runtime?.node === EXPECTED.runtime.node &&
      config.runtime?.npm === EXPECTED.runtime.npm,
    "Node/npm pins do not match the frozen contract",
  );

  const mapshaper = config.tools?.mapshaper;
  invariant(
    mapshaper?.version === EXPECTED.mapshaper.version &&
      mapshaper?.packageBytes === EXPECTED.mapshaper.packageBytes &&
      mapshaper?.packageSha256 === EXPECTED.mapshaper.packageSha256 &&
      mapshaper?.npmIntegrity === EXPECTED.mapshaper.npmIntegrity,
    "mapshaper identity does not match the frozen contract",
  );
  const topojsonClient = config.tools?.topojsonClient;
  invariant(
    topojsonClient?.version === EXPECTED.topojsonClient.version &&
      topojsonClient?.packageUrl === EXPECTED.topojsonClient.packageUrl &&
      topojsonClient?.npmIntegrity === EXPECTED.topojsonClient.npmIntegrity,
    "topojson-client identity does not match the frozen contract",
  );
  invariant(
    config.dependencyMaterialization?.status.includes("pending_phase_2b") &&
      config.dependencyMaterialization?.networkAllowed === false &&
      config.dependencyMaterialization?.requiredLockfileVersion === 3 &&
      config.dependencyMaterialization?.requiredLockfileSha256 ===
        "89f33ff6eee602303122263260f8b3bda3222e526dfecc16edd6b9e908278125" &&
      config.dependencyMaterialization?.lockOrigin ===
        "generated_from_existing_local_npm_cache_with_offline_mode_in_phase_2a" &&
      config.dependencyMaterialization?.exactPinnedRuntimeInstallVerification ===
        "pending_phase_2b" &&
      config.dependencyMaterialization?.installCommand ===
        "npm ci --offline --ignore-scripts" &&
      config.dependencyMaterialization?.requiredAuthorizedStatus === "verified_frozen",
    "topology dependency installation must remain offline and pending Phase 2B",
  );

  const generation = config.generation;
  exactArray(
    generation?.excludedStateFips,
    EXPECTED.excludedStateFips,
    "excluded state FIPS",
  );
  invariant(generation.encoding === "utf8", "encoding must be utf8");
  invariant(generation.sortField === "GEOID", "sort field must be GEOID");
  exactArray(generation.retainedFields, ["GEOID"], "retained fields");
  invariant(generation.projection === "wgs84", "projection must be wgs84");
  invariant(generation.format === "topojson", "format must be topojson");
  invariant(generation.quantization === 100000, "quantization must be 100000");
  invariant(generation.idField === "GEOID", "ID field must be GEOID");
  invariant(generation.dropTable === true, "drop-table must be enabled");
  invariant(generation.force === true, "force must be enabled");
  invariant(
    generation.additionalSimplification === false,
    "additional simplification is forbidden",
  );
  invariant(
    generation.independentDerivations === 2,
    "exactly two independent derivations are required",
  );

  invariant(
    config.canonicalIdList?.sort === "ascending_ascii" &&
      config.canonicalIdList?.separator === "LF" &&
      config.canonicalIdList?.terminalLf === true,
    "canonical ID list encoding is not frozen",
  );

  const sourcePins = {
    counties: {
      bytes: 2_983_552,
      sha256:
        "faec522080681e79be5be435c981009a77891206ff8a7f1d142f3bf5da9ebd74",
      features: 3_235,
    },
    states: {
      bytes: 1_120_533,
      sha256:
        "8a45692bc532dbd38938a1924f445850cef2682ea67d750d7fd2f19cfe836903",
      features: 56,
    },
  };

  for (const key of ["counties", "states"]) {
    const source = config.sources?.[key];
    const sourceExpected = sourcePins[key];
    invariant(
      source?.archiveBytes === sourceExpected.bytes &&
        source?.archiveSha256 === sourceExpected.sha256 &&
        source?.sourceFeatureCount === sourceExpected.features,
      `${key} source pin does not match the frozen contract`,
    );
    invariant(Array.isArray(source.extractedMembers), `${key} extracted members must be an array`);
    if (
      source.extractedMemberIdentityStatus ===
      "pending_phase_2b_authorized_extraction"
    ) {
      invariant(
        source.extractedMembers.length === 0,
        `${key} pending extracted-member identity must not contain pins`,
      );
    } else {
      invariant(
        source.extractedMemberIdentityStatus === "frozen_phase_2b" &&
          source.extractedMembers.length > 0,
        `${key} extracted-member identity must be pending or externally frozen for Phase 2B`,
      );
      for (const [index, member] of source.extractedMembers.entries()) {
        exactKeys(member, ["path", "bytes", "sha256"], `${key} extracted member ${index + 1}`);
        invariant(
          typeof member.path === "string" &&
            member.path.length > 0 &&
            !path.posix.isAbsolute(member.path) &&
            !member.path.split("/").includes("..") &&
            !member.path.includes("\\") &&
            Number.isInteger(member.bytes) &&
            member.bytes > 0 &&
            /^[0-9a-f]{64}$/.test(member.sha256),
          `${key} extracted member ${index + 1} is invalid`,
        );
      }
      invariant(
        source.extractedMembers.every(
          (member, index) =>
            index === 0 || source.extractedMembers[index - 1].path < member.path,
        ),
        `${key} extracted-member paths must be unique and ascending ASCII`,
      );
      invariant(
        source.extractedMembers.some(
          (member) => member.path === source.inputShapefile,
        ),
        `${key} extracted members do not bind the configured input shapefile`,
      );
    }

    const target = config.targets?.[key];
    const outputExpected = EXPECTED.outputs[key];
    invariant(target?.source === key, `${key} target source mismatch`);
    invariant(target?.objectName === key, `${key} object name mismatch`);
    invariant(
      target.expectedOutputBytes === outputExpected.bytes &&
        target.expectedOutputSha256 === outputExpected.sha256 &&
        target.expectedFeatureCount === outputExpected.features &&
        target.expectedIdListSha256 === outputExpected.idListSha256,
      `${key} output identity does not match the frozen contract`,
    );
  }

  exactArray(
    config.targets.counties.requiredConnecticutIds,
    [
      "09110",
      "09120",
      "09130",
      "09140",
      "09150",
      "09160",
      "09170",
      "09180",
      "09190",
    ],
    "required Connecticut IDs",
  );
  exactArray(
    config.targets.counties.forbiddenConnecticutIds,
    ["09001", "09003", "09005", "09007", "09009", "09011", "09013", "09015"],
    "forbidden Connecticut IDs",
  );

  for (const key of ["counties", "states"]) {
    const args = buildMapshaperArgs(config, key);
    invariant(
      !args.some((argument) => /simplif/i.test(argument)),
      `${key} command must not simplify geometry`,
    );
  }
  return config;
}

export function buildMapshaperArgs(
  config,
  targetKey,
  { inputPath, outputPath } = {},
) {
  const target = config.targets?.[targetKey];
  invariant(target, `unknown topology target: ${targetKey}`);
  const source = config.sources[target.source];
  const generation = config.generation;
  const filter = `![${generation.excludedStateFips
    .map((value) => JSON.stringify(value))
    .join(",")}].includes(STATEFP)`;

  return [
    "-i",
    inputPath ?? source.inputShapefile,
    `encoding=${generation.encoding}`,
    "-filter",
    filter,
    "-sort",
    generation.sortField,
    "-filter-fields",
    generation.retainedFields.join(","),
    "-proj",
    generation.projection,
    "-rename-layers",
    target.objectName,
    "-o",
    outputPath ?? target.outputFile,
    `format=${generation.format}`,
    `quantization=${generation.quantization}`,
    `id-field=${generation.idField}`,
    "drop-table",
    "force",
  ];
}

export function buildPlan(config) {
  return ["counties", "states"].map((targetKey) => {
    const target = config.targets[targetKey];
    const source = config.sources[target.source];
    return {
      target: targetKey,
      executable: config.tools.mapshaper.executable,
      arguments: buildMapshaperArgs(config, targetKey, {
        inputPath: `\${EXTRACTED_SOURCE_DIR}/${source.inputShapefile}`,
        outputPath: `\${STAGING_OUTPUT_DIR}/${target.outputFile}`,
      }),
      expectedOutputBytes: target.expectedOutputBytes,
      expectedOutputSha256: target.expectedOutputSha256,
    };
  });
}

export function validateSourceArchive(bytes, source) {
  invariant(
    bytes.length === source.archiveBytes,
    `${source.pin} archive byte count mismatch`,
  );
  invariant(
    sha256(bytes) === source.archiveSha256,
    `${source.pin} archive SHA-256 mismatch`,
  );
}

export function validateTopologyOutputBytes(bytes, target) {
  invariant(
    bytes.length === target.expectedOutputBytes,
    `${target.objectName} output byte count mismatch`,
  );
  invariant(
    sha256(bytes) === target.expectedOutputSha256,
    `${target.objectName} output SHA-256 mismatch`,
  );
}

export function validateTopologyIdentity(topology, target, universeIds) {
  invariant(topology?.type === "Topology", "output must be Topology JSON");
  invariant(
    topology.objects && typeof topology.objects === "object",
    "topology objects are required",
  );
  exactArray(
    Object.keys(topology.objects),
    [target.objectName],
    `${target.objectName} topology object names`,
  );

  const object = topology.objects[target.objectName];
  invariant(
    object?.type === "GeometryCollection",
    `${target.objectName} must be a GeometryCollection`,
  );
  invariant(
    Array.isArray(object.geometries),
    `${target.objectName} geometries must be an array`,
  );
  invariant(
    object.geometries.length === target.expectedFeatureCount,
    `${target.objectName} feature count mismatch`,
  );

  const pattern = new RegExp(target.idPattern);
  const ids = object.geometries.map((geometry) => {
    invariant(
      geometry?.type === "Polygon" || geometry?.type === "MultiPolygon",
      `${target.objectName} contains a non-polygon topology geometry`,
    );
    invariant(
      typeof geometry.id === "string" && pattern.test(geometry.id),
      `${target.objectName} feature IDs must be matching strings`,
    );
    return geometry.id;
  });
  invariant(new Set(ids).size === ids.length, `${target.objectName} has duplicate IDs`);

  const canonicalIds = canonicalIdListBytes(ids);
  invariant(
    sha256(canonicalIds) === target.expectedIdListSha256,
    `${target.objectName} sorted ID-list SHA-256 mismatch`,
  );
  invariant(
    canonicalIds.equals(canonicalIdListBytes(universeIds)),
    `${target.objectName} IDs do not equal the analytical universe`,
  );

  const excludedPrefixes = new Set(["60", "66", "69", "72", "78"]);
  for (const id of ids) {
    invariant(
      !excludedPrefixes.has(id.slice(0, 2)),
      `${target.objectName} contains excluded territory ID ${id}`,
    );
  }
  for (const id of target.requiredConnecticutIds ?? []) {
    invariant(ids.includes(id), `${target.objectName} is missing required ID ${id}`);
  }
  for (const id of target.forbiddenConnecticutIds ?? []) {
    invariant(!ids.includes(id), `${target.objectName} contains obsolete ID ${id}`);
  }

  return ids;
}

export function validateMaterializedFeatures(featureCollection, expectedCount) {
  invariant(
    featureCollection?.type === "FeatureCollection" &&
      Array.isArray(featureCollection.features),
    "topojson-client must return a FeatureCollection",
  );
  invariant(
    featureCollection.features.length === expectedCount,
    "materialized feature count mismatch",
  );
  for (const feature of featureCollection.features) {
    invariant(feature?.geometry !== null, "materialized geometry must not be null");
    invariant(
      feature?.geometry?.type === "Polygon" ||
        feature?.geometry?.type === "MultiPolygon",
      "materialized geometry must be Polygon or MultiPolygon",
    );
  }
}

export function parseSourceTreeManifest(bytes) {
  const body = Buffer.from(bytes);
  invariant(body.length > 0, "source-tree manifest must not be empty");
  invariant(body.at(-1) === 0x0a, "source-tree manifest must end with LF");
  invariant(!body.includes(0x0d), "source-tree manifest must use LF, not CRLF");
  const rawRows = body.subarray(0, body.length - 1).toString("binary").split("\n");
  invariant(
    rawRows.length > 0 && rawRows.every((row) => row.length > 0),
    "source-tree manifest must contain one non-empty JSON row per line",
  );
  let offset = 0;
  const rows = rawRows.map((rawRow, index) => {
    const rowBytes = body.subarray(offset, offset + rawRow.length + 1);
    offset += rawRow.length + 1;
    const row = parseCanonicalJsonBytes(rowBytes, `source-tree manifest row ${index + 1}`);
    exactKeys(row, ["path", "bytes", "sha256"], `source-tree manifest row ${index + 1}`);
    const pathParts = typeof row.path === "string" ? row.path.split("/") : [];
    invariant(
      typeof row.path === "string" &&
        row.path.length > 0 &&
        !path.posix.isAbsolute(row.path) &&
        path.posix.normalize(row.path) === row.path &&
        pathParts.every((part) => part.length > 0 && part !== "." && part !== "..") &&
        !row.path.includes("\\") &&
        Number.isSafeInteger(row.bytes) &&
        row.bytes >= 0 &&
        /^[0-9a-f]{64}$/.test(row.sha256),
      `source-tree manifest row ${index + 1} is invalid`,
    );
    return row;
  });
  invariant(
    rows.every((row, index) => index === 0 || rows[index - 1].path < row.path),
    "source-tree manifest paths must be unique and ascending ASCII",
  );
  return rows;
}

export function validateFrozenDependencyReceipt(config, receipt) {
  exactKeys(
    receipt,
    [
      "schema_version",
      "status",
      "lockfile_path",
      "lockfile_sha256",
      "cache_or_image_sha256",
      "operating_system",
      "platform",
      "architecture",
      "node_version",
      "npm_version",
      "install_command",
      "install_exit_code",
      "network_denied",
      "network_denied_evidence_sha256",
    ],
    "topology dependency receipt",
  );
  invariant(receipt.schema_version === 1, "dependency receipt schema_version must be 1");
  invariant(
    receipt.status === config.dependencyMaterialization.requiredAuthorizedStatus,
    "topology dependencies are not verified_frozen",
  );
  invariant(
    receipt.lockfile_path === config.dependencyMaterialization.requiredLockfile &&
      receipt.lockfile_sha256 === config.dependencyMaterialization.requiredLockfileSha256,
    "dependency receipt does not bind the topology lockfile",
  );
  for (const field of [
    "cache_or_image_sha256",
    "network_denied_evidence_sha256",
  ]) {
    invariant(/^[0-9a-f]{64}$/.test(receipt[field]), `${field} must be a lowercase SHA-256`);
  }
  for (const field of ["operating_system", "platform", "architecture"]) {
    invariant(typeof receipt[field] === "string" && receipt[field].trim(), `${field} is required`);
  }
  invariant(
    receipt.node_version === config.runtime.node &&
      receipt.npm_version === config.runtime.npm &&
      receipt.install_command === config.dependencyMaterialization.installCommand &&
      receipt.install_exit_code === 0 &&
      receipt.network_denied === true,
    "dependency receipt runtime/install/network evidence does not match the frozen contract",
  );
  return receipt;
}

export function validateExtractedMembersManifest(
  config,
  manifest,
  expectedSourceTreeId,
) {
  exactKeys(
    manifest,
    ["schema_version", "status", "source_tree_id", "sources"],
    "extracted-members manifest",
  );
  invariant(
    manifest.schema_version === 1 && manifest.status === "frozen_phase_2b",
    "extracted-members manifest is not frozen_phase_2b",
  );
  invariant(
    manifest.source_tree_id === expectedSourceTreeId,
    "extracted-members manifest is not bound to source_tree_id",
  );
  exactKeys(manifest.sources, ["counties", "states"], "extracted-members sources");
  for (const sourceKey of ["counties", "states"]) {
    const source = config.sources[sourceKey];
    const frozen = manifest.sources[sourceKey];
    exactKeys(frozen, ["archive_sha256", "members"], `${sourceKey} frozen source`);
    invariant(
      frozen.archive_sha256 === source.archiveSha256,
      `${sourceKey} extracted manifest does not bind the source archive`,
    );
    invariant(
      Array.isArray(frozen.members) && frozen.members.length > 0,
      `${sourceKey} extracted-member pins are empty`,
    );
    for (const [index, member] of frozen.members.entries()) {
      exactKeys(member, ["path", "bytes", "sha256"], `${sourceKey} member ${index + 1}`);
      invariant(
        typeof member.path === "string" &&
          member.path.length > 0 &&
          !path.posix.isAbsolute(member.path) &&
          !member.path.split("/").includes("..") &&
          !member.path.includes("\\") &&
          Number.isInteger(member.bytes) &&
          member.bytes > 0 &&
          /^[0-9a-f]{64}$/.test(member.sha256),
        `${sourceKey} extracted member ${index + 1} is invalid`,
      );
    }
    invariant(
      frozen.members.every(
        (member, index) => index === 0 || frozen.members[index - 1].path < member.path,
      ),
      `${sourceKey} extracted-member paths must be unique ascending ASCII`,
    );
    invariant(
      frozen.members.some((member) => member.path === source.inputShapefile),
      `${sourceKey} extracted members do not bind the input shapefile`,
    );
    const stem = source.inputShapefile.slice(0, -4);
    for (const extension of [".shp", ".shx", ".dbf", ".prj", ".cpg"]) {
      invariant(
        frozen.members.some((member) => member.path === `${stem}${extension}`),
        `${sourceKey} extracted members omit required shapefile sidecar ${extension}`,
      );
    }
  }
  const everyPath = [
    ...manifest.sources.counties.members,
    ...manifest.sources.states.members,
  ].map((member) => member.path);
  invariant(
    new Set(everyPath).size === everyPath.length,
    "extracted-member paths must be globally unique",
  );
  return manifest;
}

export async function verifyExtractedSourceTree(config, manifest, sourcesDir) {
  const rootStat = await lstat(sourcesDir);
  invariant(rootStat.isDirectory() && !rootStat.isSymbolicLink(),
    "extracted source root must be a real directory");
  const rootReal = await realpath(sourcesDir);
  const expected = new Map();
  for (const sourceKey of ["counties", "states"]) {
    for (const member of manifest.sources[sourceKey].members) {
      invariant(!expected.has(member.path), `duplicate extracted member path ${member.path}`);
      expected.set(member.path, member);
    }
  }
  const expectedDirectories = new Set();
  for (const memberPath of expected.keys()) {
    let directory = path.posix.dirname(memberPath);
    while (directory !== ".") {
      expectedDirectories.add(directory);
      directory = path.posix.dirname(directory);
    }
  }

  const observed = new Map();
  async function walk(directory, relativeDirectory = "") {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) => left.name.localeCompare(right.name, "en"));
    for (const entry of entries) {
      invariant(entry.name !== "." && entry.name !== ".." && !entry.name.includes("\\"),
        "unsafe extracted source entry name");
      const absolute = path.join(directory, entry.name);
      const relative = relativeDirectory
        ? path.posix.join(relativeDirectory, entry.name)
        : entry.name;
      const metadata = await lstat(absolute);
      invariant(!metadata.isSymbolicLink(), `extracted source symlink is forbidden: ${relative}`);
      const resolved = await realpath(absolute);
      invariant(
        resolved === rootReal || resolved.startsWith(`${rootReal}${path.sep}`),
        `extracted source path escapes root: ${relative}`,
      );
      if (metadata.isDirectory()) {
        invariant(expectedDirectories.has(relative),
          `undeclared extracted source directory is forbidden: ${relative}`);
        await walk(absolute, relative);
      } else {
        invariant(metadata.isFile(), `extracted source special file is forbidden: ${relative}`);
        observed.set(relative, { absolute, metadata });
      }
    }
  }
  await walk(sourcesDir);
  const expectedPaths = [...expected.keys()].sort();
  const observedPaths = [...observed.keys()].sort();
  invariant(
    JSON.stringify(observedPaths) === JSON.stringify(expectedPaths),
    `extracted source recursive member set mismatch: extra=${JSON.stringify(observedPaths.filter((item) => !expected.has(item)))} missing=${JSON.stringify(expectedPaths.filter((item) => !observed.has(item)))}`,
  );
  for (const memberPath of expectedPaths) {
    const pin = expected.get(memberPath);
    const actual = observed.get(memberPath);
    invariant(actual.metadata.size === pin.bytes, `${memberPath} byte count mismatch`);
    invariant(sha256(await readFile(actual.absolute)) === pin.sha256,
      `${memberPath} SHA-256 mismatch`);
  }
  return observedPaths;
}

export function requirePhase2bAuthorization(config, evidence) {
  invariant(evidence && typeof evidence === "object", "source-bound topology derivation is pending Phase 2B integrator authorization");
  const {
    authorizationReceiptBytes,
    dependencyReceiptBytes,
    extractedMembersManifestBytes,
    sourceTreeManifestBytes,
    topologyConfigBytes,
    topologyConfigManifestPath,
    integratorControlBytes,
    expectedIntegratorControlSha256,
    bundleManifestBytes,
    controlReceiptsManifestBytes,
  } = evidence;
  invariant(
    /^[0-9a-f]{64}$/.test(expectedIntegratorControlSha256) &&
      sha256(integratorControlBytes) === expectedIntegratorControlSha256,
    "integrator trust root differs from the independently approved SHA-256",
  );
  const trust = parseCanonicalJsonBytes(integratorControlBytes, "integrator trust root");
  exactKeys(
    trust,
    [
      "schema_version", "control_id", "phase", "source_tree_manifest_sha256",
      "bundle_manifest_sha256", "authorization_receipt_sha256",
      "review_config_sha256", "control_receipts_manifest_sha256",
      "storage_preflight_receipt_sha256", "approved_by", "approved_at_utc",
    ],
    "integrator trust root",
  );
  invariant(
    trust.schema_version === "atlas-v19-integrator-trust-root-1" &&
      trust.phase === "2B" &&
      /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(trust.control_id) &&
      trust.control_id !== "." && trust.control_id !== ".." &&
      trust.source_tree_manifest_sha256 === sha256(sourceTreeManifestBytes) &&
      trust.bundle_manifest_sha256 === sha256(bundleManifestBytes) &&
      trust.authorization_receipt_sha256 === sha256(authorizationReceiptBytes) &&
      trust.control_receipts_manifest_sha256 === sha256(controlReceiptsManifestBytes) &&
      trust.review_config_sha256 === null &&
      trust.storage_preflight_receipt_sha256 === null &&
      typeof trust.approved_by === "string" && trust.approved_by.trim() &&
      typeof trust.approved_at_utc === "string" && trust.approved_at_utc.endsWith("Z") &&
      !Number.isNaN(Date.parse(trust.approved_at_utc)),
    "integrator trust root does not bind the complete Phase 2B control set",
  );
  parseCanonicalJsonBytes(bundleManifestBytes, "Phase 2B bundle manifest");
  parseCanonicalJsonBytes(controlReceiptsManifestBytes, "control-receipts manifest");
  const dependencyReceipt = validateFrozenDependencyReceipt(
    config,
    parseCanonicalJsonBytes(dependencyReceiptBytes, "topology dependency receipt"),
  );
  const authorization = parseCanonicalJsonBytes(
    authorizationReceiptBytes,
    "topology authorization receipt",
  );
  exactKeys(
    authorization,
    [
      "schema_version",
      "authorization_id",
      "phase",
      "status",
      "source_tree_id",
      "bundle_manifest_sha256",
      "integrator_control_id",
      "topology_config_path",
      "topology_config_sha256",
      "dependency_receipt_sha256",
      "extracted_members_manifest_sha256",
      "approved_by",
      "approved_at_utc",
    ],
    "topology authorization receipt",
  );
  invariant(
    authorization.schema_version === 1 &&
      authorization.phase === "2B" &&
      authorization.status === "AUTHORIZED_PHASE_2B" &&
      /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(authorization.authorization_id) &&
      authorization.authorization_id !== "." && authorization.authorization_id !== "..",
    "topology authorization receipt does not authorize Phase 2B",
  );
  invariant(
    /^[0-9a-f]{64}$/.test(authorization.source_tree_id) &&
      authorization.source_tree_id === sha256(sourceTreeManifestBytes),
    "authorization receipt is not bound to the exact source_tree_id",
  );
  invariant(
    authorization.bundle_manifest_sha256 === sha256(bundleManifestBytes) &&
      authorization.integrator_control_id === trust.control_id,
    "topology authorization is not bound to the external bundle/control",
  );
  invariant(
    authorization.dependency_receipt_sha256 === sha256(dependencyReceiptBytes),
    "authorization receipt does not bind the frozen dependency receipt",
  );
  const expectedConfigPath = config.phase2bAuthorization.topologyConfigManifestPath;
  invariant(
    topologyConfigManifestPath === expectedConfigPath &&
      authorization.topology_config_path === expectedConfigPath &&
      authorization.topology_config_sha256 === sha256(topologyConfigBytes),
    "authorization receipt does not bind the manifested topology config path/hash",
  );
  const manifestRows = parseSourceTreeManifest(sourceTreeManifestBytes);
  const configRow = manifestRows.find((row) => row.path === expectedConfigPath);
  invariant(
    configRow &&
      configRow.bytes === topologyConfigBytes.length &&
      configRow.sha256 === authorization.topology_config_sha256,
    "source-tree manifest does not bind the exact topology config bytes",
  );
  invariant(
    Buffer.isBuffer(extractedMembersManifestBytes) &&
      authorization.extracted_members_manifest_sha256 ===
        sha256(extractedMembersManifestBytes),
    "authorization receipt does not bind the extracted-members manifest",
  );
  const extractedMembers = validateExtractedMembersManifest(
    config,
    parseCanonicalJsonBytes(extractedMembersManifestBytes, "extracted-members manifest"),
    authorization.source_tree_id,
  );
  invariant(
    typeof authorization.approved_by === "string" && authorization.approved_by.trim() &&
      typeof authorization.approved_at_utc === "string" &&
      !Number.isNaN(Date.parse(authorization.approved_at_utc)),
    "authorization receipt approver/timestamp is invalid",
  );
  return {
    authorization,
    dependencyReceipt,
    extractedMembers,
    sourceTreeId: authorization.source_tree_id,
    integratorTrustRootSha256: expectedIntegratorControlSha256,
    bundleManifestSha256: sha256(bundleManifestBytes),
    controlReceiptsManifestSha256: sha256(controlReceiptsManifestBytes),
  };
}
