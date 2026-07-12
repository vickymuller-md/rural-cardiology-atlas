import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

import nextConfig from "../next.config";
import securityHeaders from "../config/security-headers.json";

describe("embed Content-Security-Policy", () => {
  it("has exactly one emitter: the combined authoritative Next header", async () => {
    const nextHeaders = await nextConfig.headers!();
    const nextCsp = nextHeaders
      .filter((entry) => entry.source === "/embed")
      .flatMap((entry) => entry.headers)
      .filter((header) => header.key.toLowerCase() === "content-security-policy");
    expect(nextCsp).toEqual([
      {
        key: "Content-Security-Policy",
        value: securityHeaders.embedContentSecurityPolicy,
      },
    ]);
    expect(
      nextHeaders
        .filter((entry) => entry.source !== "/embed")
        .flatMap((entry) => entry.headers)
        .filter((header) => header.key.toLowerCase() === "content-security-policy")
    ).toEqual([]);

    const vercel = JSON.parse(
      await readFile(path.join(process.cwd(), "vercel.json"), "utf8")
    );
    expect(vercel.headers).toBeUndefined();
    expect(nextCsp).toHaveLength(1);
    expect(securityHeaders.embedContentSecurityPolicy).toBe(
      "connect-src 'self'; frame-ancestors *;"
    );
  });
});
