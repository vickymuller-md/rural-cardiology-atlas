import type { NextConfig } from "next";
import path from "node:path";
import { fileURLToPath } from "node:url";
import securityHeaders from "./config/security-headers.json";

const APP_ROOT = path.dirname(fileURLToPath(import.meta.url));

export function nextDistDirForAbsolute(candidate: string): string {
  if (!path.isAbsolute(candidate)) {
    throw new Error("ATLAS_NEXT_DIST_DIR must be an absolute path");
  }
  const normalized = path.normalize(candidate);
  if (normalized === APP_ROOT) {
    throw new Error("ATLAS_NEXT_DIST_DIR must be distinct from the app source root");
  }
  return path.relative(APP_ROOT, normalized);
}

const cleanRoomDistDir = process.env.ATLAS_NEXT_DIST_DIR;

const nextConfig: NextConfig = {
  distDir: cleanRoomDistDir ? nextDistDirForAbsolute(cleanRoomDistDir) : ".next",
  // Static map data lives under /public/data — served as-is.
  typedRoutes: false,
  // atlas.heartlandprotocol.org is the canonical host; allow bare preview URLs too.
  async headers() {
    return [
      {
        source: "/data/:file*",
        headers: [
          { key: "Cache-Control", value: "public, max-age=0, must-revalidate" },
        ],
      },
      {
        source: "/embed",
        headers: [
          {
            key: "Content-Security-Policy",
            value: securityHeaders.embedContentSecurityPolicy,
          },
        ],
      },
    ];
  },
};

export default nextConfig;
