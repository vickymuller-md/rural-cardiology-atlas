import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  parseObservedVersions,
  readTrackedVersionPins,
  validateObservedVersions,
  validateTrackedLockfiles,
  validateToolchainManifest,
  validateTrackedVersionPins,
} from "../verify-runtime.mjs";

const ENVIRONMENT_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const REPOSITORY_ROOT = path.resolve(ENVIRONMENT_DIR, "..");
const MANIFEST = JSON.parse(
  await readFile(path.join(ENVIRONMENT_DIR, "toolchain.json"), "utf8"),
);

test("runtime manifest and tracked version files carry the exact preregistered pins", async () => {
  const expected = validateToolchainManifest(MANIFEST);
  const tracked = await readTrackedVersionPins(MANIFEST, REPOSITORY_ROOT);
  validateTrackedVersionPins(MANIFEST, tracked);

  assert.deepEqual(expected, {
    python: "3.12.12",
    uv: "0.10.4",
    node: "22.17.0",
    npm: "10.9.2",
  });
  assert.deepEqual(tracked, { python: "3.12.12", node: "22.17.0" });
  assert.deepEqual(await validateTrackedLockfiles(MANIFEST, REPOSITORY_ROOT), {
    "scripts/uv.lock": "5f0c837281a80748cc5084bfe70006f4ba7a106cb273fc81f2874255098041f3",
    "app/package-lock.json": "ebd3ae6323c0cc4046fcccdbfd8a98132614809c29e4575cd2975b9cd1c660e0",
    "tooling/topology/package-lock.json":
      "89f33ff6eee602303122263260f8b3bda3222e526dfecc16edd6b9e908278125",
  });
});

test("offline dependency materialization remains explicitly pending Phase 2B", () => {
  assert.match(MANIFEST.offlineDependencyPlan.status, /pending_phase_2b/);
  assert.equal(MANIFEST.phase2aVerification.allowsNetwork, false);
  assert.equal(
    MANIFEST.phase2aVerification.allowsSourceBoundTopologyDerivation,
    false,
  );
  assert.deepEqual(MANIFEST.offlineDependencyPlan.commands, {
    python: "uv sync --frozen --offline",
    app: "npm ci --offline --ignore-scripts",
    topology: "npm ci --offline --ignore-scripts",
  });
  assert.equal(
    new Set(MANIFEST.offlineDependencyPlan.lockfiles.map((entry) => entry.sha256)).size,
    3,
  );
  assert.ok(
    MANIFEST.offlineDependencyPlan.forbiddenDependencySources.includes(
      "git+ssh",
    ),
  );
});

test("observed runtime parsing and exact-version rejection are fail closed", () => {
  const observed = parseObservedVersions({
    python: "Python 3.12.12\n",
    uv: "uv 0.10.4 (synthetic fixture)",
    node: "v22.17.0",
    npm: "10.9.2\n",
  });
  validateObservedVersions(MANIFEST, observed);

  assert.throws(
    () => validateObservedVersions(MANIFEST, { ...observed, node: "22.17.1" }),
    /node version mismatch/,
  );
});
