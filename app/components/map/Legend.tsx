"use client";

import type { ChoroplethMetric } from "@/lib/types";
import { legendFor } from "@/lib/color-scale";

interface Props {
  metric: ChoroplethMetric;
}

export function Legend({ metric }: Props) {
  const entries = legendFor(metric);
  const title =
    metric === "density"
      ? "Cardiologists per county"
      : "Miles to nearest cardiologist";
  return (
    <div className="rounded-md border border-[var(--color-grid)] bg-[var(--color-panel)] p-3 text-xs">
      <p className="mb-2 font-medium uppercase tracking-[0.15em] text-[var(--color-stone)]">
        {title}
      </p>
      <div className="flex flex-wrap gap-3">
        {entries.map(({ color, label }) => (
          <div key={label} className="flex items-center gap-2">
            <span
              className="inline-block h-3.5 w-3.5 rounded-sm border border-[var(--color-grid-hi)]"
              style={{ background: color }}
              aria-hidden
            />
            <span>{label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
