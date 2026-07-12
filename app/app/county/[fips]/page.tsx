import { notFound } from "next/navigation";
import type { Metadata } from "next";
import { loadCounties } from "@/lib/counties";
import { STATE_ABBR_TO_NAME } from "@/lib/fips";
import { DISTANCE_DEFINITION, PLACES_LABEL } from "@/lib/methodology";
import { areaPageMetadata } from "@/lib/og-copy";

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
  if (!c) return { title: "Area not found" };
  return areaPageMetadata(c);
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
          Area report · FIPS {c.fips}
        </p>
        <h1 className="mt-1 font-[var(--font-display)] text-4xl">{c.county}</h1>
        <p className="text-lg text-[var(--color-stone)]">{stateName}</p>
      </header>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Tile label="Qualifying providers" value={`${c.n_cardiologists}`} emphasize={c.n_cardiologists === 0} />
        <Tile
          label="Great-circle miles to nearest"
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
          label="CAHs ≥20 mi from qualifying provider"
          value={`${c.n_cah_20plus_miles_to_nearest_cardiologist}`}
          emphasize={c.n_cah_20plus_miles_to_nearest_cardiologist > 0}
        />
        <Tile
          label="HPSA — primary care"
          value={c.hpsa_primary_care ? "Designated" : "—"}
          emphasize={c.hpsa_primary_care}
        />
        <Tile
          label="Adult CHD prevalence (age-adjusted %)"
          value={
            c.chd_age_adjusted_prevalence_pct != null
              ? c.chd_age_adjusted_prevalence_pct.toFixed(1)
              : "—"
          }
        />
        <Tile
          label="RUCC 2023"
          value={`${c.rucc_2023} · ${c.rural ? "Rural" : "Urban"}`}
        />
        <Tile
          label="Median HH income"
          value={c.median_hh_income != null ? `$${c.median_hh_income.toLocaleString("en-US")}` : "—"}
        />
      </section>

      <section className="rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)] p-5 text-sm leading-relaxed">
        <p className="font-medium text-[var(--color-cool)]">Context</p>
        <p className="mt-2 text-[var(--color-stone)]">
          {c.n_cardiologists === 0
            ? `Under the preregistered address-geocoding and fallback rules, no qualifying provider was assigned to ${c.county}.`
            : `Under the preregistered rules, ${c.n_cardiologists} qualifying NPPES-listed provider${c.n_cardiologists === 1 ? " was" : "s were"} assigned to ${c.county}.`}{" "}
          {DISTANCE_DEFINITION} {PLACES_LABEL}. These area-level measures do not
          establish service availability, appointment access, or patient-level outcomes.
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
