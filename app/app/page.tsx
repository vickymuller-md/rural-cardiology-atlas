import { Suspense } from "react";
import Link from "next/link";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { Atlas } from "@/components/Atlas";
import { StatsBar } from "@/components/stats/StatsBar";
import { AtlasHeroMark } from "@/components/landing/Mark";
import { loadCounties, computeStats } from "@/lib/counties";

export const revalidate = 3600;

export default async function Home() {
  const { list, index } = await loadCounties();
  const stats = computeStats(list);

  return (
    <NuqsAdapter>
      <Hero totalCounties={stats.total} zeroPct={stats.zeroCardioPct} />

      <section
        id="atlas"
        className="mx-auto flex max-w-[1400px] flex-col gap-6 px-6 pb-16"
      >
        <StatsBar stats={stats} />
        <Suspense
          fallback={
            <div className="h-[600px] rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)]" />
          }
        >
          <Atlas list={list} index={index} />
        </Suspense>

        <div className="rounded-lg border border-[var(--color-grid)] bg-[var(--color-terminal-deep)] p-5 text-sm leading-relaxed text-[var(--color-stone)]">
          <p className="mb-1 font-medium text-[var(--color-cool)]">
            How this map is built
          </p>
          <p>
            Cardiologists are identified from the CMS NPPES registry and located
            via their practice ZIP (crosswalked to county FIPS by the US Census
            ZCTA↔County relationship file). Distance is measured between county
            centroids and ZIP centroids via great-circle geometry — suitable for
            county-level comparisons and intentionally coarse inside very large
            urban ZIPs. Full{" "}
            <Link
              href="/about"
              className="underline decoration-[var(--color-grid-hi)] hover:text-[var(--color-alert)]"
            >
              methodology
            </Link>
            .
          </p>
        </div>
      </section>
    </NuqsAdapter>
  );
}

function Hero({
  totalCounties,
  zeroPct,
}: {
  totalCounties: number;
  zeroPct: number;
}) {
  return (
    <section className="relative overflow-hidden">
      <div className="mx-auto max-w-[1200px] px-6 pb-20 pt-14 md:pb-28 md:pt-20">
        <div className="grid grid-cols-1 items-center gap-14 md:grid-cols-12 md:gap-12">
          <div className="md:col-span-7">
            <p className="inline-flex items-center gap-2 rounded-full border border-[var(--color-grid)] bg-[var(--color-panel)] px-3.5 py-1.5 font-[var(--font-editorial)] text-[12px] tracking-tight text-[var(--color-cool)]/80">
              <span
                className="h-1.5 w-1.5 rounded-full bg-[var(--color-signal)]"
                aria-hidden
              />
              Public dataset · Updated monthly · {totalCounties.toLocaleString("en-US")} counties
            </p>

            <h1 className="mt-7 text-[clamp(2.4rem,6vw,4.75rem)] font-[var(--font-editorial)] font-semibold leading-[1.04] tracking-[-0.025em] text-[var(--color-cool)]">
              Heart failure care{" "}
              <span className="font-[var(--font-display)] italic font-normal text-[var(--color-alert)]">
                where there&rsquo;s
              </span>{" "}
              no cardiologist.
            </h1>

            <p className="mt-7 max-w-xl font-[var(--font-editorial)] text-[17px] leading-[1.65] text-[var(--color-cool)]/75 md:text-[18px]">
              An interactive county-level atlas of the US cardiology access gap
              — provider density, distance to the nearest specialist, Critical
              Access Hospitals, and HRSA shortage designations. Companion to the
              peer-reviewed{" "}
              <a
                className="underline decoration-[var(--color-grid-hi)] hover:text-[var(--color-alert)]"
                href="https://heartlandprotocol.org"
                target="_blank"
                rel="noopener noreferrer"
              >
                HEARTLAND Protocol
              </a>
              .
            </p>

            <div className="mt-10 flex flex-col items-start gap-4 sm:flex-row sm:items-center">
              <Link
                href="#atlas"
                className="group inline-flex items-center gap-3 rounded-full bg-[var(--color-cool)] px-7 py-4 font-[var(--font-editorial)] text-[15px] font-medium text-[var(--color-terminal)] transition-colors hover:bg-[var(--color-alert)] hover:text-[var(--color-cool)]"
              >
                Explore the map
                <span className="transition-transform group-hover:translate-x-1">→</span>
              </Link>
              <Link
                href="/about"
                className="group inline-flex items-center gap-2 font-[var(--font-editorial)] text-[15px] font-medium text-[var(--color-cool)]/85 transition-colors hover:text-[var(--color-alert)]"
              >
                Read methodology
                <span className="transition-transform group-hover:translate-x-1">→</span>
              </Link>
            </div>

            <p className="mt-12 max-w-md font-[var(--font-editorial)] text-[12.5px] leading-relaxed text-[var(--color-stone)]">
              {(zeroPct * 100).toFixed(1)}% of US counties have zero cardiologists
              at a primary practice ZIP. Aggregate public data only; no patient
              health information.
            </p>
          </div>

          <div className="md:col-span-5">
            <AtlasHeroMark className="mx-auto h-auto w-full max-w-[460px]" />
          </div>
        </div>
      </div>
    </section>
  );
}
