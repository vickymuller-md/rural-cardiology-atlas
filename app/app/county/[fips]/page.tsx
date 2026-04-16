import { notFound } from "next/navigation";
import type { Metadata } from "next";
import { loadCounties } from "@/lib/counties";
import { STATE_ABBR_TO_NAME } from "@/lib/fips";

interface Params {
  fips: string;
}

export async function generateStaticParams() {
  const { list } = await loadCounties();
  return list.map((c) => ({ fips: c.fips }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<Params>;
}): Promise<Metadata> {
  const { fips } = await params;
  const { index } = await loadCounties();
  const c = index[fips];
  if (!c) return { title: "County not found" };
  return {
    title: `${c.county}, ${c.state}`,
    description: `${c.county} County cardiology access summary. ${c.n_cardiologists} cardiologists; ${c.miles_to_nearest_cardiologist ?? "n/a"} miles to nearest.`,
  };
}

export default async function CountyPage({
  params,
}: {
  params: Promise<Params>;
}) {
  const { fips } = await params;
  const { index } = await loadCounties();
  const c = index[fips];
  if (!c) notFound();

  const stateName = STATE_ABBR_TO_NAME[c.state] ?? c.state;

  return (
    <article className="mx-auto mt-10 flex max-w-3xl flex-col gap-6 print:mt-0">
      <header>
        <p className="text-xs uppercase tracking-[0.25em] text-[var(--color-stone)]">
          County report · FIPS {c.fips}
        </p>
        <h1 className="mt-1 font-[var(--font-display)] text-4xl">{c.county}</h1>
        <p className="text-lg text-[var(--color-stone)]">{stateName}</p>
      </header>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Tile label="Cardiologists" value={`${c.n_cardiologists}`} emphasize={c.n_cardiologists === 0} />
        <Tile
          label="Miles to nearest"
          value={
            c.miles_to_nearest_cardiologist != null
              ? `${c.miles_to_nearest_cardiologist.toFixed(1)}`
              : "—"
          }
          emphasize={(c.miles_to_nearest_cardiologist ?? 0) >= 50}
        />
        <Tile
          label="Population"
          value={c.population != null ? c.population.toLocaleString("en-US") : "—"}
        />
        <Tile label="Hospitals" value={`${c.n_hospitals}`} />
        <Tile label="Critical Access Hospitals" value={`${c.n_cah}`} emphasize={c.n_cah > 0} />
        <Tile
          label="HPSA — primary care"
          value={c.hpsa_primary_care ? "Designated" : "—"}
          emphasize={c.hpsa_primary_care}
        />
        <Tile
          label="Heart disease mortality / 100k"
          value={
            c.heart_disease_mortality_per_100k != null
              ? c.heart_disease_mortality_per_100k.toFixed(1)
              : "—"
          }
        />
        <Tile
          label="RUCC 2023"
          value={c.rucc_2023 != null ? `${c.rucc_2023} · ${c.rural ? "Rural" : "Urban"}` : "—"}
        />
        <Tile
          label="Median HH income"
          value={c.median_hh_income != null ? `$${c.median_hh_income.toLocaleString("en-US")}` : "—"}
        />
      </section>

      <section className="rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)] p-5 text-sm leading-relaxed">
        <p className="font-medium text-[var(--color-cool)]">Context</p>
        <p className="mt-2 text-[var(--color-stone)]">
          {c.n_cardiologists === 0 && (c.miles_to_nearest_cardiologist ?? 0) >= 30
            ? `${c.county} County has no cardiologist with a primary practice ZIP in the county. The nearest specialist is approximately ${(c.miles_to_nearest_cardiologist ?? 0).toFixed(0)} miles away.`
            : c.n_cardiologists === 0
              ? `${c.county} County has no cardiologist with a primary practice ZIP in the county.`
              : `${c.county} County has ${c.n_cardiologists} cardiologist${c.n_cardiologists === 1 ? "" : "s"} with primary practice ZIPs inside the county.`}{" "}
          The HEARTLAND Protocol is designed to support primary care–led heart
          failure management in this setting.
        </p>
      </section>

      <footer className="text-xs text-[var(--color-stone)] no-print">
        Data sources and methodology:{" "}
        <a className="underline" href="/about">
          About this atlas
        </a>
        .
      </footer>
    </article>
  );
}

function Tile({
  label,
  value,
  emphasize,
}: {
  label: string;
  value: string;
  emphasize?: boolean;
}) {
  return (
    <div className="rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)] p-3">
      <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--color-stone)]">
        {label}
      </p>
      <p
        className={
          "mt-1 font-[var(--font-display)] text-2xl leading-tight " +
          (emphasize ? "text-[var(--color-alert-deep)]" : "text-[var(--color-cool)]")
        }
      >
        {value}
      </p>
    </div>
  );
}
