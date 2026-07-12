#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ENVIRONMENT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(ENVIRONMENT_DIR, "..");
const MANIFEST_PATH = path.join(ENVIRONMENT_DIR, "toolchain.json");

function invariant(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

export function validateToolchainManifest(manifest) {
  invariant(manifest.schemaVersion === 1, "toolchain schemaVersion must be 1");
  invariant(manifest.phase === "2A", "toolchain phase must be 2A");

  const expected = {
    python: "3.12.12",
    uv: "0.10.4",
    node: "22.17.0",
    npm: "10.9.2",
  };
  for (const [tool, version] of Object.entries(expected)) {
    invariant(
      manifest.runtimePins?.[tool] === version,
      `${tool} must be pinned to ${version}`,
    );
  }

  invariant(
    manifest.offlineDependencyPlan?.networkPolicy ===
      "deny_all_outbound_during_install_and_verification",
    "offline dependency plan must deny outbound networking",
  );
  invariant(
    manifest.offlineDependencyPlan?.status.includes("pending_phase_2b"),
    "environment materialization must remain pending Phase 2B",
  );
  const expectedCommands = {
    python: "uv sync --frozen --offline",
    app: "npm ci --offline --ignore-scripts",
    topology: "npm ci --offline --ignore-scripts",
  };
  invariant(
    JSON.stringify(manifest.offlineDependencyPlan?.commands) ===
      JSON.stringify(expectedCommands),
    "offline install commands do not match the frozen no-scripts policy",
  );
  const lockfiles = manifest.offlineDependencyPlan?.lockfiles;
  invariant(Array.isArray(lockfiles) && lockfiles.length === 3, "three lockfiles are required");
  invariant(
    new Set(lockfiles.map((entry) => entry.path)).size === lockfiles.length &&
      new Set(lockfiles.map((entry) => entry.sha256)).size === lockfiles.length,
    "lockfile paths and SHA-256 identities must be distinct",
  );
  for (const entry of lockfiles) {
    invariant(
      typeof entry.path === "string" &&
        !entry.path.startsWith("/") &&
        !entry.path.includes("..") &&
        /^[0-9a-f]{64}$/.test(entry.sha256),
      "invalid frozen lockfile identity",
    );
  }
  invariant(
    manifest.phase2aVerification?.allowsNetwork === false,
    "Phase 2A must not allow network access",
  );
  invariant(
    manifest.phase2aVerification?.allowsSourceBoundTopologyDerivation === false,
    "Phase 2A must not allow source-bound topology derivation",
  );
  return expected;
}

export async function validateTrackedLockfiles(manifest, repositoryRoot = REPOSITORY_ROOT) {
  const receipts = {};
  for (const entry of manifest.offlineDependencyPlan.lockfiles) {
    const bytes = await readFile(path.join(repositoryRoot, entry.path));
    const digest = createHash("sha256").update(bytes).digest("hex");
    invariant(digest === entry.sha256, `${entry.path} SHA-256 mismatch`);
    receipts[entry.path] = digest;
  }
  return receipts;
}

export async function readTrackedVersionPins(
  manifest,
  repositoryRoot = REPOSITORY_ROOT,
) {
  const result = {};
  for (const [tool, relativePath] of Object.entries(
    manifest.trackedVersionFiles ?? {},
  )) {
    result[tool] = (
      await readFile(path.join(repositoryRoot, relativePath), "utf8")
    ).trim();
  }
  return result;
}

export function validateTrackedVersionPins(manifest, trackedPins) {
  invariant(
    trackedPins.python === manifest.runtimePins.python,
    `.python-version must contain ${manifest.runtimePins.python}`,
  );
  invariant(
    trackedPins.node === manifest.runtimePins.node,
    `.node-version must contain ${manifest.runtimePins.node}`,
  );
}

export function parseObservedVersions(raw) {
  const parsed = {
    python: raw.python.match(/Python\s+(\d+\.\d+\.\d+)/)?.[1],
    uv: raw.uv.match(/uv\s+(\d+\.\d+\.\d+)/)?.[1],
    node: raw.node.replace(/^v/, "").trim(),
    npm: raw.npm.trim(),
  };
  for (const [tool, value] of Object.entries(parsed)) {
    invariant(value, `could not parse observed ${tool} version`);
  }
  return parsed;
}

export function validateObservedVersions(manifest, observed) {
  for (const [tool, required] of Object.entries(manifest.runtimePins)) {
    invariant(
      observed[tool] === required,
      `${tool} version mismatch: required ${required}, observed ${observed[tool]}`,
    );
  }
}

function observeCurrentVersions() {
  const run = (command, args = []) =>
    execFileSync(command, args, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    }).trim();

  return parseObservedVersions({
    python: run("python3", ["--version"]),
    uv: run("uv", ["--version"]),
    node: process.version,
    npm: run("npm", ["--version"]),
  });
}

export async function verifyRuntime({ checkCurrent = false } = {}) {
  const manifest = JSON.parse(await readFile(MANIFEST_PATH, "utf8"));
  validateToolchainManifest(manifest);
  const trackedPins = await readTrackedVersionPins(manifest);
  validateTrackedVersionPins(manifest, trackedPins);
  const lockfiles = await validateTrackedLockfiles(manifest);

  const report = { trackedPins, lockfiles };
  if (checkCurrent) {
    const observed = observeCurrentVersions();
    validateObservedVersions(manifest, observed);
    report.observed = observed;
  }
  return report;
}

async function main() {
  const args = new Set(process.argv.slice(2));
  const allowed = new Set(["--check-current", "--manifest-only"]);
  for (const arg of args) {
    invariant(allowed.has(arg), `unknown argument: ${arg}`);
  }
  invariant(
    !(args.has("--check-current") && args.has("--manifest-only")),
    "choose either --check-current or --manifest-only",
  );

  const report = await verifyRuntime({
    checkCurrent: args.has("--check-current"),
  });
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
