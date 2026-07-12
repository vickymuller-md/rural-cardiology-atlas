import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

const temporaryDirectories: string[] = [];
const originalDistDir = process.env.ATLAS_NEXT_DIST_DIR;

afterEach(async () => {
  if (originalDistDir === undefined) delete process.env.ATLAS_NEXT_DIST_DIR;
  else process.env.ATLAS_NEXT_DIST_DIR = originalDistDir;
  vi.resetModules();
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) =>
      rm(directory, { recursive: true, force: true })
    )
  );
});

async function emptyBuildRoot(label: string): Promise<string> {
  const root = await mkdtemp(path.join(tmpdir(), `atlas-${label}-`));
  temporaryDirectories.push(root);
  return path.join(root, "next-build");
}

async function importConfigFor(target: string) {
  process.env.ATLAS_NEXT_DIST_DIR = target;
  vi.resetModules();
  return import("../next.config");
}

describe("clean-room Next output isolation", () => {
  it("resolves two absolute controls to distinct exact Next build directories", async () => {
    const first = await emptyBuildRoot("run-a");
    const second = await emptyBuildRoot("run-b");
    const firstModule = await importConfigFor(first);
    const firstResolved = path.resolve(process.cwd(), firstModule.default.distDir!);
    const secondModule = await importConfigFor(second);
    const secondResolved = path.resolve(process.cwd(), secondModule.default.distDir!);

    expect(firstResolved).toBe(first);
    expect(secondResolved).toBe(second);
    expect(firstResolved).not.toBe(secondResolved);
    expect(firstModule.nextDistDirForAbsolute(first)).toBe(
      path.relative(process.cwd(), first)
    );
  });

  it("rejects relative and source-root ATLAS_NEXT_DIST_DIR values", async () => {
    await expect(importConfigFor("relative/.next")).rejects.toThrow(
      /ATLAS_NEXT_DIST_DIR must be an absolute path/
    );
    await expect(importConfigFor(process.cwd())).rejects.toThrow(
      /must be distinct from the app source root/
    );
  });
});
