import "server-only";
import { promises as fs } from "node:fs";
import path from "node:path";
import type { County, CountyIndex } from "./types";

export { computeStats, type NationalStats } from "./stats";

let cache: { list: County[]; index: CountyIndex } | null = null;

export async function loadCounties(): Promise<{ list: County[]; index: CountyIndex }> {
  if (cache) return cache;
  const file = path.join(process.cwd(), "public", "data", "counties.json");
  const raw = await fs.readFile(file, "utf-8");
  const list = JSON.parse(raw) as County[];
  const index: CountyIndex = {};
  for (const c of list) index[c.fips] = c;
  cache = { list, index };
  return cache;
}
