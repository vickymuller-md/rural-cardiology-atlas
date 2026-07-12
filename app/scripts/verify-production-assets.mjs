#!/usr/bin/env node

import { createHash } from "node:crypto";
import { lstat, readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { feature } from "topojson-client";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(SCRIPT_DIR, "..");

export const COUNTY_FIELDS = Object.freeze([
  "fips",
  "state",
  "county",
  "population",
  "median_hh_income",
  "rucc_2023",
  "rural",
  "n_cardiologists",
  "miles_to_nearest_cardiologist",
  "n_hospitals",
  "n_cah",
  "n_cah_20plus_miles_to_nearest_cardiologist",
  "hpsa_primary_care",
  "chd_age_adjusted_prevalence_pct",
]);

export const SUMMARY_FIELDS = Object.freeze([
  "counties_json_sha256",
  "provider_locations_sha256",
  "hospital_locations_sha256",
  "county_count",
  "zero_provider_count",
  "zero_provider_pct",
  "population_in_zero_provider_counties",
  "rural_count_with_evaluable_distance",
  "rural_median_distance_miles",
  "cah_total_in_scope",
  "cah_evaluable",
  "cah_unmatched",
  "cah_gte_20_miles",
  "cah_gte_20_pct_evaluable",
]);

export const REQUIRED_RELEASE_FILES = Object.freeze([
  "counties.json",
  "county-universe.ids.txt",
  "summary.json",
  "topology/counties-2025-5m.topo.json",
  "topology/states-2025-5m.topo.json",
]);

export const FORBIDDEN_SERVED_TEXT_PATTERNS = Object.freeze([
  Object.freeze({ label: "PENDING/PENDING_PHASE", pattern: /\bPENDING(?:_PHASE)?\b/i }),
  Object.freeze({ label: "PLACEHOLDER", pattern: /\bPLACEHOLDER\b/i }),
  Object.freeze({ label: "PHASE 2A/2B", pattern: /\bPHASE\s+2[AB]\b/i }),
  Object.freeze({ label: "internal gate", pattern: /\b(?:INTERNAL\s+)?GATE[-_ ](?:[A-Z]|[0-9]+)\b/i }),
]);

export const PRODUCTION_ASSET_CONTRACT = Object.freeze({
  countyCount: 3_144,
  chdNonNullCount: 2_956,
  chdNullCount: 188,
  countyUniverseSha256:
    "f2651ec6a9375476e3ff09efb4c2a699cd5690ffd3f0c982aeacad967c21c241",
  countyTopology: Object.freeze({
    path: "topology/counties-2025-5m.topo.json",
    objectName: "counties",
    bytes: 1_349_047,
    sha256: "affdae57c1fe33dd5e64ccf1c735f83db830374d5dcff375231a667aa96aa4d5",
    featureCount: 3_144,
    idPattern: /^[0-9]{5}$/,
    requiredIds: Object.freeze([
      "09110", "09120", "09130", "09140", "09150",
      "09160", "09170", "09180", "09190",
    ]),
    forbiddenIds: Object.freeze([
      "09001", "09003", "09005", "09007",
      "09009", "09011", "09013", "09015",
    ]),
  }),
  stateTopology: Object.freeze({
    path: "topology/states-2025-5m.topo.json",
    objectName: "states",
    bytes: 465_345,
    sha256: "05a7896dc0cd3be17813dbe7b571e74eafd690a6078a99698aa9a52df01b6bbe",
    featureCount: 51,
    idPattern: /^[0-9]{2}$/,
    requiredIds: Object.freeze([]),
    forbiddenIds: Object.freeze([]),
  }),
});

function fail(message) {
  throw new Error(`Atlas production-asset preflight failed: ${message}`);
}

export function sha256Hex(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

export function resolveReleaseDataDirectory(candidate) {
  const selected = candidate ?? process.env.ATLAS_RELEASE_DATA_DIR ?? path.join(APP_ROOT, "public", "data");
  if (typeof selected !== "string" || !path.isAbsolute(selected)) {
    fail("ATLAS_RELEASE_DATA_DIR/dataDirectory must be an absolute path");
  }
  return path.normalize(selected);
}

function exactKeys(value, fields, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    fail(`${label} must be an object`);
  }
  const actual = Object.keys(value);
  if (actual.length !== fields.length || actual.some((key, index) => key !== fields[index])) {
    fail(`${label} keys/order must be exactly ${fields.join(",")}`);
  }
}

function parseJson(bytes, label) {
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch (error) {
    throw new Error(`Atlas production-asset preflight failed: ${label} is invalid UTF-8 JSON`, {
      cause: error,
    });
  }
}

function safeInteger(value, label, minimum = 0) {
  if (!Number.isSafeInteger(value) || value < minimum) fail(`${label} must be an integer >= ${minimum}`);
  return value;
}

function finite(value, label, minimum = 0) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum) {
    fail(`${label} must be a finite number >= ${minimum}`);
  }
  return value;
}

function oneDecimal(value, label) {
  if (!Number.isInteger(value * 10)) fail(`${label} must be quantized to one decimal`);
}

function percentageHalfUp(numerator, denominator) {
  return Math.floor((numerator * 1_000) / denominator + 0.5) / 10;
}

function parseUniverse(bytes, contract) {
  const digest = sha256Hex(bytes);
  if (digest !== contract.countyUniverseSha256) fail("county-universe SHA-256 mismatch");
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  if (!text.endsWith("\n") || text.endsWith("\n\n") || text.includes("\r")) {
    fail("county universe must use LF with exactly one terminal newline");
  }
  const ids = text.slice(0, -1).split("\n");
  if (ids.length !== contract.countyCount) fail("county-universe cardinality mismatch");
  if (ids.some((id) => !/^[0-9]{5}$/.test(id))) fail("county universe contains malformed FIPS");
  if (ids.some((id, index) => index > 0 && ids[index - 1] >= id)) {
    fail("county universe is not unique ascending ASCII");
  }
  return ids;
}

function validateCounties(bytes, universeIds, contract) {
  const rows = parseJson(bytes, "counties.json");
  if (!Array.isArray(rows) || rows.length !== contract.countyCount) {
    fail("counties.json cardinality mismatch");
  }
  let chdNonNull = 0;
  for (const [index, row] of rows.entries()) {
    const label = `counties.json row ${index + 1}`;
    exactKeys(row, COUNTY_FIELDS, label);
    if (row.fips !== universeIds[index]) fail(`${label} does not equal the frozen universe order`);
    if (typeof row.state !== "string" || !/^[A-Z]{2}$/.test(row.state)) fail(`${label}.state is invalid`);
    if (typeof row.county !== "string" || !row.county) fail(`${label}.county is invalid`);
    safeInteger(row.population, `${label}.population`);
    if (row.median_hh_income !== null) safeInteger(row.median_hh_income, `${label}.median_hh_income`);
    safeInteger(row.rucc_2023, `${label}.rucc_2023`, 1);
    if (row.rucc_2023 > 9 || row.rural !== (row.rucc_2023 >= 4)) fail(`${label} RUCC/rural mismatch`);
    for (const field of [
      "n_cardiologists",
      "n_hospitals",
      "n_cah",
      "n_cah_20plus_miles_to_nearest_cardiologist",
    ]) safeInteger(row[field], `${label}.${field}`);
    if (row.n_cah > row.n_hospitals || row.n_cah_20plus_miles_to_nearest_cardiologist > row.n_cah) {
      fail(`${label} hospital/CAH accounting is invalid`);
    }
    if (row.miles_to_nearest_cardiologist !== null) {
      oneDecimal(finite(row.miles_to_nearest_cardiologist, `${label}.distance`), `${label}.distance`);
    }
    if (typeof row.hpsa_primary_care !== "boolean") fail(`${label}.hpsa_primary_care is invalid`);
    if (row.chd_age_adjusted_prevalence_pct !== null) {
      const value = finite(row.chd_age_adjusted_prevalence_pct, `${label}.CHD`);
      if (value > 100) fail(`${label}.CHD exceeds 100 percent`);
      oneDecimal(value, `${label}.CHD`);
      chdNonNull += 1;
    }
  }
  if (
    chdNonNull !== contract.chdNonNullCount ||
    rows.length - chdNonNull !== contract.chdNullCount
  ) fail("PLACES completeness does not match the frozen release contract");
  return rows;
}

function validateSummary(bytes, countyBytes, counties) {
  const summary = parseJson(bytes, "summary.json");
  exactKeys(summary, SUMMARY_FIELDS, "summary.json");
  for (const field of [
    "counties_json_sha256",
    "provider_locations_sha256",
    "hospital_locations_sha256",
  ]) if (typeof summary[field] !== "string" || !/^[0-9a-f]{64}$/.test(summary[field])) {
    fail(`summary.json.${field} is not a lowercase SHA-256`);
  }
  if (summary.counties_json_sha256 !== sha256Hex(countyBytes)) fail("summary does not bind exact county bytes");
  if (safeInteger(summary.county_count, "summary.county_count", 1) !== counties.length) {
    fail("summary county_count mismatch");
  }
  const zeroRows = counties.filter((row) => row.n_cardiologists === 0);
  if (safeInteger(summary.zero_provider_count, "summary.zero_provider_count") !== zeroRows.length) {
    fail("summary zero qualifying-provider count mismatch");
  }
  const zeroPct = finite(summary.zero_provider_pct, "summary.zero_provider_pct");
  oneDecimal(zeroPct, "summary.zero_provider_pct");
  if (zeroPct !== percentageHalfUp(zeroRows.length, counties.length)) fail("summary zero-provider percentage mismatch");
  if (
    safeInteger(summary.population_in_zero_provider_counties, "summary.zero population") !==
    zeroRows.reduce((total, row) => total + row.population, 0)
  ) fail("summary zero-provider population mismatch");
  const ruralEvaluable = counties.filter(
    (row) => row.rural && row.miles_to_nearest_cardiologist !== null,
  ).length;
  if (safeInteger(summary.rural_count_with_evaluable_distance, "summary.rural denominator") !== ruralEvaluable) {
    fail("summary rural evaluable denominator mismatch");
  }
  oneDecimal(finite(summary.rural_median_distance_miles, "summary.rural median"), "summary.rural median");
  const cahEvaluable = counties.reduce((total, row) => total + row.n_cah, 0);
  const cahGte = counties.reduce(
    (total, row) => total + row.n_cah_20plus_miles_to_nearest_cardiologist,
    0,
  );
  if (safeInteger(summary.cah_evaluable, "summary.cah_evaluable") !== cahEvaluable) fail("summary CAH E mismatch");
  const cahTotal = safeInteger(summary.cah_total_in_scope, "summary.cah_total_in_scope");
  const cahUnmatched = safeInteger(summary.cah_unmatched, "summary.cah_unmatched");
  if (cahTotal - cahEvaluable !== cahUnmatched) fail("summary CAH S/E/U mismatch");
  if (safeInteger(summary.cah_gte_20_miles, "summary.cah_gte_20") !== cahGte) fail("summary CAH G20 mismatch");
  if (cahEvaluable <= 0) fail("summary CAH evaluable denominator is zero");
  const cahPct = finite(summary.cah_gte_20_pct_evaluable, "summary.cah percentage");
  oneDecimal(cahPct, "summary.cah percentage");
  if (cahPct !== percentageHalfUp(cahGte, cahEvaluable)) fail("summary CAH percentage mismatch");
  return summary;
}

function validateTopology(bytes, topologyContract, expectedIds) {
  if (bytes.length !== topologyContract.bytes || sha256Hex(bytes) !== topologyContract.sha256) {
    fail(`${topologyContract.objectName} topology byte/hash identity mismatch`);
  }
  const topology = parseJson(bytes, topologyContract.path);
  if (topology?.type !== "Topology" || !topology.objects) fail("invalid Topology root");
  const objectNames = Object.keys(topology.objects);
  if (objectNames.length !== 1 || objectNames[0] !== topologyContract.objectName) {
    fail(`${topologyContract.objectName} topology object-name mismatch`);
  }
  const object = topology.objects[topologyContract.objectName];
  if (object?.type !== "GeometryCollection" || object.geometries.length !== topologyContract.featureCount) {
    fail(`${topologyContract.objectName} topology feature-count mismatch`);
  }
  const ids = object.geometries.map((geometry) => {
    if (
      typeof geometry.id !== "string" ||
      !topologyContract.idPattern.test(geometry.id) ||
      !["Polygon", "MultiPolygon"].includes(geometry.type)
    ) fail(`${topologyContract.objectName} topology contains an invalid geometry/ID`);
    return geometry.id;
  });
  const actualSorted = [...ids].sort();
  const expectedSorted = [...expectedIds].sort();
  if (
    actualSorted.length !== expectedSorted.length ||
    actualSorted.some((id, index) => id !== expectedSorted[index])
  ) fail(`${topologyContract.objectName} topology IDs do not equal the required universe`);
  for (const id of topologyContract.requiredIds) if (!ids.includes(id)) fail(`required topology ID ${id} is absent`);
  for (const id of topologyContract.forbiddenIds) if (ids.includes(id)) fail(`forbidden topology ID ${id} is present`);
  const materialized = feature(topology, object);
  if (
    materialized.features.length !== topologyContract.featureCount ||
    materialized.features.some(
      (item) => item.geometry === null || !["Polygon", "MultiPolygon"].includes(item.geometry.type),
    )
  ) fail(`${topologyContract.objectName} topology does not materialize as polygons`);
}

async function listServedFiles(directory, root = directory) {
  const output = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const full = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) fail(`served data path is a symlink: ${path.relative(root, full)}`);
    if (entry.isDirectory()) output.push(...(await listServedFiles(full, root)));
    else if (entry.isFile()) output.push(path.relative(root, full).split(path.sep).join("/"));
    else fail(`served data path is not a regular file: ${path.relative(root, full)}`);
  }
  return output.sort();
}

async function requireRegularFiles(dataDirectory, relativePaths) {
  const missing = [];
  for (const relative of relativePaths) {
    try {
      const stat = await lstat(path.join(dataDirectory, relative));
      if (!stat.isFile() || stat.isSymbolicLink()) missing.push(relative);
    } catch {
      missing.push(relative);
    }
  }
  if (missing.length) fail(`required release assets are missing/nonregular: ${missing.join(",")}`);
}

function requireExactServedFileSet(actualPaths, manifest) {
  const expected = [
    "release-manifest.json",
    ...manifest.files.map((entry) => entry.path),
  ].sort();
  if (
    actualPaths.length !== expected.length ||
    actualPaths.some((relative, index) => relative !== expected[index])
  ) {
    fail(
      `served data file set differs from release-manifest.json; expected ${expected.join(",")}; found ${actualPaths.join(",")}`,
    );
  }
}

function rejectInternalControlText(bytes, relative) {
  const text = bytes.toString("utf8");
  for (const { label, pattern } of FORBIDDEN_SERVED_TEXT_PATTERNS) {
    if (pattern.test(text)) fail(`served data asset contains forbidden ${label} text: ${relative}`);
  }
}

function validateReleaseManifest(bytes, expectedSha256) {
  if (!/^[0-9a-f]{64}$/.test(expectedSha256 ?? "")) {
    fail("ATLAS_RELEASE_MANIFEST_SHA256 must be an approved lowercase SHA-256");
  }
  if (sha256Hex(bytes) !== expectedSha256) fail("release-manifest.json SHA-256 mismatch");
  const manifest = parseJson(bytes, "release-manifest.json");
  exactKeys(manifest, ["schema_version", "source_tree_id", "files"], "release-manifest.json");
  if (manifest.schema_version !== 1 || !/^[0-9a-f]{64}$/.test(manifest.source_tree_id)) {
    fail("release manifest schema/source_tree_id is invalid");
  }
  if (!Array.isArray(manifest.files) || manifest.files.length !== REQUIRED_RELEASE_FILES.length) {
    fail("release manifest file set is incomplete");
  }
  for (const [index, entry] of manifest.files.entries()) {
    exactKeys(entry, ["path", "bytes", "sha256"], `release manifest file ${index + 1}`);
    if (
      entry.path !== REQUIRED_RELEASE_FILES[index] ||
      !Number.isSafeInteger(entry.bytes) ||
      entry.bytes <= 0 ||
      !/^[0-9a-f]{64}$/.test(entry.sha256)
    ) fail("release manifest file identity/order is invalid");
  }
  return manifest;
}

export async function verifyProductionAssets({
  dataDirectory,
  expectedReleaseManifestSha256 = process.env.ATLAS_RELEASE_MANIFEST_SHA256,
  contract = PRODUCTION_ASSET_CONTRACT,
} = {}) {
  const resolvedDataDirectory = resolveReleaseDataDirectory(dataDirectory);
  if (!/^[0-9a-f]{64}$/.test(expectedReleaseManifestSha256 ?? "")) {
    fail("ATLAS_RELEASE_MANIFEST_SHA256 must be set before any production asset is read");
  }
  await requireRegularFiles(resolvedDataDirectory, ["release-manifest.json", ...REQUIRED_RELEASE_FILES]);
  const manifestBytes = await readFile(path.join(resolvedDataDirectory, "release-manifest.json"));
  const manifest = validateReleaseManifest(manifestBytes, expectedReleaseManifestSha256);
  rejectInternalControlText(manifestBytes, "release-manifest.json");
  const servedFiles = await listServedFiles(resolvedDataDirectory);
  requireExactServedFileSet(servedFiles, manifest);
  const assets = {};
  for (const entry of manifest.files) {
    const bytes = await readFile(path.join(resolvedDataDirectory, entry.path));
    if (bytes.length !== entry.bytes || sha256Hex(bytes) !== entry.sha256) {
      fail(`${entry.path} does not match release-manifest.json`);
    }
    assets[entry.path] = bytes;
    rejectInternalControlText(bytes, entry.path);
  }

  const universeIds = parseUniverse(assets["county-universe.ids.txt"], contract);
  const counties = validateCounties(assets["counties.json"], universeIds, contract);
  const summary = validateSummary(assets["summary.json"], assets["counties.json"], counties);
  validateTopology(assets[contract.countyTopology.path], contract.countyTopology, universeIds);
  const stateIds = [...new Set(universeIds.map((id) => id.slice(0, 2)))].sort();
  validateTopology(assets[contract.stateTopology.path], contract.stateTopology, stateIds);
  return {
    sourceTreeId: manifest.source_tree_id,
    releaseManifestSha256: expectedReleaseManifestSha256,
    countyBytes: assets["counties.json"],
    summaryBytes: assets["summary.json"],
    universeBytes: assets["county-universe.ids.txt"],
    universeIds,
    summary,
  };
}

async function main() {
  const result = await verifyProductionAssets();
  process.stdout.write(`${JSON.stringify({
    source_tree_id: result.sourceTreeId,
    release_manifest_sha256: result.releaseManifestSha256,
    county_count: result.universeIds.length,
  })}\n`);
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
