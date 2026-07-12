import type { NationalSummary } from "@/lib/types";

interface Props {
  summary: NationalSummary;
}

export function StatsBar({ summary }: Props) {
  const tiles: Array<{ label: string; value: string; sub?: string }> = [
    {
      label: "Areas with zero qualifying providers",
      value: summary.zero_provider_count.toLocaleString("en-US"),
      sub: `of ${summary.county_count.toLocaleString("en-US")} (${summary.zero_provider_pct.toFixed(1)}%)`,
    },
    {
      label: "Median distance — rural areas",
      value: `${summary.rural_median_distance_miles.toFixed(1)} mi`,
      sub: `raw-first median across ${summary.rural_count_with_evaluable_distance.toLocaleString("en-US")} evaluable areas`,
    },
    {
      label: "CAHs ≥20 mi from a qualifying provider",
      value: summary.cah_gte_20_miles.toLocaleString("en-US"),
      sub: `${summary.cah_gte_20_pct_evaluable.toFixed(1)}% among ${summary.cah_evaluable.toLocaleString("en-US")} evaluable in-scope CAHs`,
    },
    {
      label: "Population in zero-provider areas",
      value: summary.population_in_zero_provider_counties.toLocaleString("en-US"),
      sub: "residents of areas with zero qualifying providers",
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {tiles.map((t) => (
        <div
          key={t.label}
          className="rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)] p-4"
        >
          <p className="text-xs uppercase tracking-[0.18em] text-[var(--color-stone)]">
            {t.label}
          </p>
          <p className="mt-1 font-[var(--font-display)] text-3xl leading-tight text-[var(--color-cool)]">
            {t.value}
          </p>
          {t.sub && (
            <p className="mt-1 text-xs text-[var(--color-stone)]">{t.sub}</p>
          )}
        </div>
      ))}
    </div>
  );
}
