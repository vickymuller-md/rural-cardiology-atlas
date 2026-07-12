import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const cacheDir = process.env.ATLAS_VITE_CACHE_DIR;

if (!cacheDir || !path.isAbsolute(cacheDir)) {
  throw new Error("clean-room ATLAS_VITE_CACHE_DIR must be an absolute path");
}

export default defineConfig({
  cacheDir,
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
  },
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "."),
    },
  },
});
