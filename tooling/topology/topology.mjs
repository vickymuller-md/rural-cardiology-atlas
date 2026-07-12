#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import {
  access,
  copyFile,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  DEFAULT_CONFIG_PATH,
  TOPOLOGY_DIR,
  buildMapshaperArgs,
  buildPlan,
  canonicalIdListBytes,
  loadTopologyConfig,
  parseCanonicalIdList,
  requirePhase2bAuthorization,
  sha256,
  validateMaterializedFeatures,
  validateSourceArchive,
  validateTopologyConfig,
  validateTopologyIdentity,
  validateTopologyOutputBytes,
  verifyExtractedSourceTree,
} from "./topology-contract.mjs";

function invariant(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function canonicalize(value) {
  if (Array.isArray(value)) {
    return value.map(canonicalize);
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalize(value[key])]),
    );
  }
  return value;
}

function canonicalJsonBytes(value) {
  return Buffer.from(`${JSON.stringify(canonicalize(value), null, 2)}\n`, "utf8");
}

function parseNamedOptions(args) {
  const options = {};
  for (let index = 0; index < args.length; index += 2) {
    const name = args[index];
    const value = args[index + 1];
    invariant(name?.startsWith("--"), `expected named option, received ${name}`);
    invariant(value && !value.startsWith("--"), `missing value for ${name}`);
    invariant(!(name.slice(2) in options), `duplicate option ${name}`);
    options[name.slice(2)] = value;
  }
  return options;
}

function runVersion(executable, args) {
  return execFileSync(executable, args, {
    cwd: TOPOLOGY_DIR,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

async function pathExists(candidate) {
  try {
    await access(candidate);
    return true;
  } catch {
    return false;
  }
}

async function readRegularFile(candidate, label) {
  const metadata = await lstat(candidate);
  invariant(metadata.isFile() && !metadata.isSymbolicLink(), `${label} must be a regular non-symlink file`);
  return readFile(candidate);
}

export function verifyCanonicalSourceTreeWithPython(options, expectedSourceTreeId) {
  const repoRoot = path.resolve(options["repo-root"]);
  const pythonExecutable = path.resolve(options["python-executable"]);
  const scriptsRoot = path.join(repoRoot, "scripts");
  const sourceManifest = path.resolve(options["source-tree-manifest"]);
  const code = [
    "import sys",
    "from pathlib import Path",
    "assert sys.version_info[:3] == (3, 12, 12), 'CPython 3.12.12 is required'",
    "sys.path.insert(0, sys.argv[1])",
    "from atlas_pipeline.source_tree import verify_imported_module_containment, verify_source_tree_manifest",
    "identity = verify_source_tree_manifest(Path(sys.argv[2]), Path(sys.argv[3]).read_bytes())",
    "verify_imported_module_containment(Path(sys.argv[2]))",
    "print(identity.source_tree_id)",
  ].join("; ");
  const verifiedId = execFileSync(
    pythonExecutable,
    ["-I", "-c", code, scriptsRoot, repoRoot, sourceManifest],
    {
      cwd: repoRoot,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", PYTHONNOUSERSITE: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    },
  ).trim();
  invariant(verifiedId === expectedSourceTreeId,
    "Python source-tree recomputation differs from authorization");
}

async function verifyPhase2bControlEvidence(config, options) {
  const required = [
    "authorization-receipt",
    "dependency-receipt",
    "extracted-members-manifest",
    "source-tree-manifest",
    "integrator-control",
    "approved-control-sha256",
    "bundle-manifest",
    "control-receipts-manifest",
    "repo-root",
    "python-executable",
  ];
  for (const name of required) {
    invariant(options[name], `--${name} is required before Phase 2B topology derivation`);
  }
  const configPath = path.resolve(options.config ?? DEFAULT_CONFIG_PATH);
  const [
    authorizationReceiptBytes,
    dependencyReceiptBytes,
    extractedMembersManifestBytes,
    sourceTreeManifestBytes,
    topologyConfigBytes,
    integratorControlBytes,
    bundleManifestBytes,
    controlReceiptsManifestBytes,
  ] = await Promise.all([
    readRegularFile(path.resolve(options["authorization-receipt"]), "authorization receipt"),
    readRegularFile(path.resolve(options["dependency-receipt"]), "dependency receipt"),
    readRegularFile(path.resolve(options["extracted-members-manifest"]), "extracted-members manifest"),
    readRegularFile(path.resolve(options["source-tree-manifest"]), "source-tree manifest"),
    readRegularFile(configPath, "topology config"),
    readRegularFile(path.resolve(options["integrator-control"]), "integrator control"),
    readRegularFile(path.resolve(options["bundle-manifest"]), "Phase 2B bundle manifest"),
    readRegularFile(path.resolve(options["control-receipts-manifest"]), "control-receipts manifest"),
  ]);
  const authorization = requirePhase2bAuthorization(config, {
    authorizationReceiptBytes,
    dependencyReceiptBytes,
    extractedMembersManifestBytes,
    sourceTreeManifestBytes,
    topologyConfigBytes,
    topologyConfigManifestPath:
      config.phase2bAuthorization.topologyConfigManifestPath,
    integratorControlBytes,
    expectedIntegratorControlSha256: options["approved-control-sha256"],
    bundleManifestBytes,
    controlReceiptsManifestBytes,
  });
  verifyCanonicalSourceTreeWithPython(options, authorization.sourceTreeId);
  return authorization;
}

async function validateDependencyLock(config) {
  const lockPath = path.join(TOPOLOGY_DIR, "package-lock.json");
  const lock = JSON.parse(await readFile(lockPath, "utf8"));
  invariant(lock.lockfileVersion === 3, "topology package-lock must be v3");

  const mapshaper = lock.packages?.["node_modules/mapshaper"];
  invariant(
    mapshaper?.version === config.tools.mapshaper.version &&
      mapshaper?.integrity === config.tools.mapshaper.npmIntegrity,
    "locked mapshaper identity mismatch",
  );
  const topojsonClient = lock.packages?.["node_modules/topojson-client"];
  invariant(
    topojsonClient?.version === config.tools.topojsonClient.version &&
      topojsonClient?.integrity === config.tools.topojsonClient.npmIntegrity,
    "locked topojson-client identity mismatch",
  );

  for (const [packagePath, entry] of Object.entries(lock.packages ?? {})) {
    const resolved = entry.resolved;
    invariant(
      !resolved || /^https:\/\/registry\.npmjs\.org\//.test(resolved),
      `${packagePath || "root"} uses a non-registry dependency source`,
    );
  }
  const lockSha256 = sha256(await readFile(lockPath));
  invariant(
    lockSha256 === config.dependencyMaterialization.requiredLockfileSha256,
    "topology package-lock SHA-256 mismatch",
  );
  return lockSha256;
}

async function verifyOneOutput({
  bytes,
  config,
  targetKey,
  universeIds,
  topojsonClient,
}) {
  const target = config.targets[targetKey];
  validateTopologyOutputBytes(bytes, target);
  const topology = JSON.parse(bytes.toString("utf8"));
  validateTopologyIdentity(topology, target, universeIds);
  const materialized = topojsonClient.feature(
    topology,
    topology.objects[target.objectName],
  );
  validateMaterializedFeatures(materialized, target.expectedFeatureCount);
}

export async function deriveTopologies(config, options) {
  // This guard deliberately occurs before any source, dependency, or output access.
  validateTopologyConfig(config);
  const allowedOptions = new Set([
    "config", "authorization-receipt", "dependency-receipt",
    "extracted-members-manifest", "source-tree-manifest", "integrator-control",
    "approved-control-sha256", "bundle-manifest", "control-receipts-manifest",
    "repo-root", "python-executable", "archives-dir", "sources-dir",
    "universe-dir", "output-dir",
  ]);
  const unknown = Object.keys(options).filter((name) => !allowedOptions.has(name));
  invariant(unknown.length === 0, `unknown derive option: --${unknown[0]}`);
  const authorization = await verifyPhase2bControlEvidence(config, options);

  for (const required of ["archives-dir", "sources-dir", "universe-dir", "output-dir"]) {
    invariant(options[required], `--${required} is required`);
  }
  invariant(
    process.versions.node === config.runtime.node,
    `Node ${config.runtime.node} is required`,
  );
  invariant(
    runVersion("npm", ["--version"]) === config.runtime.npm,
    `npm ${config.runtime.npm} is required`,
  );

  const packageLockSha256 = await validateDependencyLock(config);
  const mapshaperExecutable = path.resolve(
    TOPOLOGY_DIR,
    config.tools.mapshaper.executable,
  );
  const mapshaperVersion = runVersion(mapshaperExecutable, ["--version"]);
  invariant(
    new RegExp(`(^|\\s)${config.tools.mapshaper.version.replaceAll(".", "\\.")}($|\\s)`).test(
      mapshaperVersion,
    ),
    `mapshaper ${config.tools.mapshaper.version} is required`,
  );

  const topojsonPackage = JSON.parse(
    await readFile(
      path.join(TOPOLOGY_DIR, "node_modules", "topojson-client", "package.json"),
      "utf8",
    ),
  );
  invariant(
    topojsonPackage.version === config.tools.topojsonClient.version,
    `topojson-client ${config.tools.topojsonClient.version} is required`,
  );
  const topojsonClient = await import("topojson-client");

  const archivesDir = path.resolve(options["archives-dir"]);
  const sourcesDir = path.resolve(options["sources-dir"]);
  const universeDir = path.resolve(options["universe-dir"]);
  const outputDir = path.resolve(options["output-dir"]);
  invariant(!(await pathExists(outputDir)), "output directory already exists");
  await verifyExtractedSourceTree(
    config,
    authorization.extractedMembers,
    sourcesDir,
  );

  const universeByTarget = {};
  const sourceReceipts = {};
  for (const targetKey of ["counties", "states"]) {
    const target = config.targets[targetKey];
    const source = config.sources[target.source];
    const archiveBytes = await readFile(path.join(archivesDir, source.archiveFile));
    validateSourceArchive(archiveBytes, source);
    const universeBytes = await readFile(path.join(universeDir, target.universeFile));
    invariant(
      sha256(universeBytes) === target.expectedIdListSha256,
      `${targetKey} universe SHA-256 mismatch`,
    );
    const universeIds = parseCanonicalIdList(universeBytes, target.idPattern);
    invariant(
      universeIds.length === target.expectedFeatureCount,
      `${targetKey} universe count mismatch`,
    );
    universeByTarget[targetKey] = universeIds;
    sourceReceipts[targetKey] = {
      archiveFile: source.archiveFile,
      archiveBytes: archiveBytes.length,
      archiveSha256: sha256(archiveBytes),
      universeFile: target.universeFile,
      universeSha256: sha256(canonicalIdListBytes(universeIds)),
    };
  }

  const outputParent = path.dirname(outputDir);
  invariant(await pathExists(outputParent), "output parent directory does not exist");
  const stagingRoot = await mkdtemp(path.join(outputParent, ".atlas-topology-"));
  const runDirectories = [path.join(stagingRoot, "run-a"), path.join(stagingRoot, "run-b")];
  const publishDirectory = path.join(stagingRoot, "publish");

  try {
    await Promise.all([
      ...runDirectories.map((directory) => mkdir(directory)),
      mkdir(publishDirectory),
    ]);

    const outputReceipts = {};
    for (const targetKey of ["counties", "states"]) {
      const target = config.targets[targetKey];
      const source = config.sources[target.source];
      const generatedBytes = [];

      for (const runDirectory of runDirectories) {
        const outputPath = path.join(runDirectory, target.outputFile);
        const args = buildMapshaperArgs(config, targetKey, {
          inputPath: path.join(sourcesDir, source.inputShapefile),
          outputPath,
        });
        execFileSync(mapshaperExecutable, args, {
          cwd: TOPOLOGY_DIR,
          stdio: ["ignore", "pipe", "pipe"],
        });
        const bytes = await readFile(outputPath);
        await verifyOneOutput({
          bytes,
          config,
          targetKey,
          universeIds: universeByTarget[targetKey],
          topojsonClient,
        });
        generatedBytes.push(bytes);
      }

      invariant(
        generatedBytes[0].equals(generatedBytes[1]),
        `${targetKey} independent derivations are not byte-identical`,
      );
      await copyFile(
        path.join(runDirectories[0], target.outputFile),
        path.join(publishDirectory, target.outputFile),
      );
      outputReceipts[targetKey] = {
        file: target.outputFile,
        bytes: generatedBytes[0].length,
        sha256: sha256(generatedBytes[0]),
        independentDerivations: 2,
      };
    }

    const receipt = {
      schemaVersion: 1,
      analyticalUse: config.analyticalUse,
      sourceTreeId: authorization.sourceTreeId,
      authorizationReceiptSha256: sha256(
        await readFile(path.resolve(options["authorization-receipt"])),
      ),
      integratorTrustRootSha256: authorization.integratorTrustRootSha256,
      bundleManifestSha256: authorization.bundleManifestSha256,
      controlReceiptsManifestSha256: authorization.controlReceiptsManifestSha256,
      dependencyReceiptSha256: sha256(
        await readFile(path.resolve(options["dependency-receipt"])),
      ),
      extractedMembersManifestSha256: sha256(
        await readFile(path.resolve(options["extracted-members-manifest"])),
      ),
      dependencies: {
        mapshaper: config.tools.mapshaper.version,
        npm: config.runtime.npm,
        node: config.runtime.node,
        packageLockSha256,
        topojsonClient: config.tools.topojsonClient.version,
      },
      outputs: outputReceipts,
      sources: sourceReceipts,
    };
    await writeFile(
      path.join(publishDirectory, "topology-verification.json"),
      canonicalJsonBytes(receipt),
      { flag: "wx" },
    );
    await rename(publishDirectory, outputDir);
  } finally {
    await rm(stagingRoot, { force: true, recursive: true });
  }
}

async function main() {
  const [command = "plan", ...rawOptions] = process.argv.slice(2);
  const options = parseNamedOptions(rawOptions);
  const config = await loadTopologyConfig(options.config);

  if (command === "plan") {
    const unknown = Object.keys(options).filter((key) => key !== "config");
    invariant(unknown.length === 0, `unknown plan option: --${unknown[0]}`);
    process.stdout.write(`${JSON.stringify(buildPlan(config), null, 2)}\n`);
    return;
  }
  if (command === "derive") {
    await deriveTopologies(config, options);
    return;
  }
  throw new Error(`unknown command: ${command}`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
