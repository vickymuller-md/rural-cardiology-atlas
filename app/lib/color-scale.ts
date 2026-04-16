import { scaleThreshold } from "d3-scale";
import type { County, ChoroplethMetric } from "./types";

export const DENSITY_BUCKETS = [0, 1, 3, 5]; // 0, 1-2, 3-4, 5+
export const DENSITY_LABELS = ["0", "1–2", "3–4", "5+"];
export const DENSITY_COLORS = [
  "var(--color-choro-0)",
  "var(--color-choro-1)",
  "var(--color-choro-2)",
  "var(--color-choro-4)",
];

export const DISTANCE_THRESHOLDS_MI = [10, 25, 50, 100]; // breakpoints
export const DISTANCE_LABELS = ["<10 mi", "10–25", "25–50", "50–100", "100+"];
export const DISTANCE_COLORS = [
  "var(--color-choro-4)",
  "var(--color-choro-3)",
  "var(--color-choro-2)",
  "var(--color-choro-1)",
  "var(--color-choro-0)",
];

export const MISSING_COLOR = "var(--color-grid)";

const densityScale = scaleThreshold<number, string>()
  .domain([1, 3, 5])
  .range(DENSITY_COLORS);

const distanceScale = scaleThreshold<number, string>()
  .domain(DISTANCE_THRESHOLDS_MI)
  .range(DISTANCE_COLORS);

export function colorFor(county: County | undefined, metric: ChoroplethMetric): string {
  if (!county) return MISSING_COLOR;
  if (metric === "density") {
    return densityScale(county.n_cardiologists);
  }
  if (county.miles_to_nearest_cardiologist == null) return MISSING_COLOR;
  return distanceScale(county.miles_to_nearest_cardiologist);
}

export function legendFor(metric: ChoroplethMetric): { color: string; label: string }[] {
  if (metric === "density") {
    return DENSITY_LABELS.map((label, i) => ({ color: DENSITY_COLORS[i], label }));
  }
  return DISTANCE_LABELS.map((label, i) => ({ color: DISTANCE_COLORS[i], label }));
}
