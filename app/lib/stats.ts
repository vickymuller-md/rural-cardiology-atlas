/** Conventional median for non-authoritative fixtures and UI utilities. */
export function conventionalMedian(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  if (values.some((value) => !Number.isFinite(value))) {
    throw new Error("Median inputs must all be finite numbers");
  }
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[middle];
  return (sorted[middle - 1] + sorted[middle]) / 2;
}
