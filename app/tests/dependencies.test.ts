import { createHash } from "node:crypto";
import { lstat, readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

interface TreeEntry {
  path: string;
  bytes: number;
  sha256: string;
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

async function filesBelow(directory: string, root = directory): Promise<string[]> {
  const output: string[] = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) output.push(...(await filesBelow(fullPath, root)));
    else output.push(path.relative(root, fullPath));
  }
  return output.sort();
}

describe("offline @heartland/ui dependency", () => {
  it("uses a local file dependency with no Git, GitHub shortcut, or SSH resolution", async () => {
    const [packageJson, packageLock] = await Promise.all([
      readFile(path.join(process.cwd(), "package.json"), "utf8"),
      readFile(path.join(process.cwd(), "package-lock.json"), "utf8"),
    ]);
    const pkg = JSON.parse(packageJson);
    const lock = JSON.parse(packageLock);

    expect(pkg.dependencies["@heartland/ui"]).toBe("file:vendor/heartland-ui");
    expect(lock.packages[""].dependencies["@heartland/ui"]).toBe(
      "file:vendor/heartland-ui"
    );
    expect(lock.packages["node_modules/@heartland/ui"]).toEqual({
      resolved: "vendor/heartland-ui",
      link: true,
    });
    expect(packageJson).not.toMatch(/(?:git\+ssh|github:|git@github\.com)/i);
    expect(packageLock).not.toMatch(/(?:git\+ssh|github:|git@github\.com)/i);
    expect(pkg.engines).toEqual({ node: "22.17.0", npm: "10.9.2" });
    expect(pkg.packageManager).toBe("npm@10.9.2");
    expect(pkg.dependencies["topojson-client"]).toBe("3.1.0");
    expect(lock.packages["node_modules/topojson-client"].version).toBe("3.1.0");
  });

  it("separates the lock-bound Vercel install from the offline clean-room install", async () => {
    const [vercelJson, offlinePlan] = await Promise.all([
      readFile(path.join(process.cwd(), "vercel.json"), "utf8"),
      readFile(path.join(process.cwd(), "OFFLINE_DEPENDENCIES.md"), "utf8"),
    ]);
    const vercel = JSON.parse(vercelJson);
    expect(vercel.installCommand).toBe("npm ci --ignore-scripts");
    expect(vercel.installCommand).not.toContain("--offline");
    expect(offlinePlan).toContain("npm ci --offline --ignore-scripts");
    expect(offlinePlan).toContain("npm ci --ignore-scripts");
  });

  it("binds every actual vendored file to the deterministic tree manifest", async () => {
    const vendorRoot = path.join(process.cwd(), "vendor", "heartland-ui");
    const manifestPath = path.join(process.cwd(), "vendor", "heartland-ui.tree.jsonl");
    const provenancePath = path.join(
      process.cwd(),
      "vendor",
      "heartland-ui.provenance.json"
    );
    const [manifestBytes, provenanceBytes] = await Promise.all([
      readFile(manifestPath),
      readFile(provenancePath),
    ]);
    const entries = manifestBytes
      .toString("utf8")
      .trimEnd()
      .split("\n")
      .map((line) => JSON.parse(line) as TreeEntry);
    const provenance = JSON.parse(provenanceBytes.toString("utf8"));

    expect(entries.map((entry) => entry.path)).toEqual(
      [...entries.map((entry) => entry.path)].sort()
    );
    expect(await filesBelow(vendorRoot)).toEqual(entries.map((entry) => entry.path));
    for (const entry of entries) {
      const fullPath = path.join(vendorRoot, entry.path);
      expect((await lstat(fullPath)).isSymbolicLink()).toBe(false);
      const bytes = await readFile(fullPath);
      expect(bytes.byteLength, entry.path).toBe(entry.bytes);
      expect(sha256(bytes), entry.path).toBe(entry.sha256);
    }

    expect(sha256(manifestBytes)).toBe(provenance.vendored_directory.tree_manifest_sha256);
    expect(manifestBytes.byteLength).toBe(
      provenance.vendored_directory.tree_manifest_bytes
    );
    expect(provenance.source_commit).toBe(
      "7f80b6d262ad3910fee4d170c7c7477eec47b307"
    );
    expect(provenance.offline_reproduction.network_used).toBe(false);
    expect(provenance.offline_reproduction.pack_sha256).toBe(
      "b833f61bc99b30eca7dfbe556bcd1259bbda02ae5e2504ad1c1a7d2a69c8a198"
    );
    expect(provenance.offline_reproduction.pack_integrity).toBe(
      "sha512-So77rwtH6qi9efWeieWEkbvZlCWvieIGoOPyWYLfqgOxK6vhaQ22LCizEUnvjwy+piiECvRlIDi+vODANm8c8g=="
    );
    expect(provenance.license.license_file_present_at_source_commit).toBe(false);
  });
});
