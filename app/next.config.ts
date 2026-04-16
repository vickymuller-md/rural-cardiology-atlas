import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static map data lives under /public/data — served as-is.
  typedRoutes: false,
  // atlas.heartlandprotocol.org is the canonical host; allow bare preview URLs too.
  async headers() {
    return [
      {
        source: "/data/:file*",
        headers: [
          { key: "Cache-Control", value: "public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800" },
        ],
      },
      {
        source: "/embed",
        headers: [
          // Embedding is a feature of this page.
          { key: "Content-Security-Policy", value: "frame-ancestors *;" },
        ],
      },
    ];
  },
};

export default nextConfig;
