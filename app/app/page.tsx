import { Suspense } from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { Atlas } from "@/components/Atlas";
import { StatsBar } from "@/components/stats/StatsBar";
import { loadCounties, computeStats } from "@/lib/counties";

export const revalidate = 3600;

export default async function Home() {
  const { list, index } = await loadCounties();
  const stats = computeStats(list);

  return (
    <NuqsAdapter>
      <div className="flex flex-col gap-6 pt-8">
        <section className="flex flex-col gap-3">
          <p className="text-xs uppercase tracking-[0.25em] text-[var(--color-stone)]">
            Public dataset · Updated monthly
          </p>
          <h1 className="font-[var(--font-display)] text-4xl leading-tight md:text-5xl">
            The cardiology access gap, county by county
          </h1>
          <p className="max-w-3xl text-[var(--color-stone)]">
            Interactive map of cardiologist density, distance to nearest specialist,
            Critical Access Hospital presence, and HRSA primary-care shortage
            designations across every US county. Built to support rural heart
            failure care and the HEARTLAND Protocol.
          </p>
        </section>

        <StatsBar stats={stats} />
        <Suspense fallback={<div className="h-[600px] rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)]" />}>
          <Atlas list={list} index={index} />
        </Suspense>

        <section className="rounded-lg border border-[var(--color-grid)] bg-[var(--color-terminal-deep)] p-5 text-sm text-[var(--color-stone)]">
          <p className="mb-2 font-medium text-[var(--color-cool)]">
            How this map is built
          </p>
          <p>
            Cardiologists are identified from the CMS NPPES registry and located
            via their practice ZIP code (crosswalked to county FIPS by the US
            Census ZCTA↔County relationship file). Distance is measured between
            county geographic centroids and ZIP centroids via great-circle
            geometry — appropriate for county-level comparisons and understates
            distance inside very large urban ZIPs. See the{" "}
            <a className="underline" href="/about">
              methodology page
            </a>{" "}
            for a complete description.
          </p>
        </section>
      </div>
    </NuqsAdapter>
  );
}
