import type { NationalStats } from "@/lib/stats";

interface Props {
  stats: NationalStats;
}

export function StatsBar({ stats }: Props) {
  const tiles: Array<{ label: string; value: string; sub?: string }> = [
    {
      label: "Counties with zero cardiologists",
      value: `${stats.zeroCardioCount.toLocaleString("en-US")}`,
      sub: `of ${stats.total.toLocaleString("en-US")} (${(stats.zeroCardioPct * 100).toFixed(1)}%)`,
    },
    {
      label: "Median distance — rural counties",
      value: `${stats.medianRuralDistance.toFixed(1)} mi`,
      sub: "to nearest cardiologist",
    },
    {
      label: "CAHs ≥ 20 mi from a cardiologist",
      value: `${stats.cahFarFromCardiologist.toLocaleString("en-US")}`,
      sub: "Critical Access Hospitals",
    },
    {
      label: "Population in zero-cardio counties",
      value: stats.populationUnderserved.toLocaleString("en-US"),
      sub: "residents without a local cardiologist",
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
