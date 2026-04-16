"use client";

import Link from "next/link";
import { X } from "lucide-react";
import type { County } from "@/lib/types";
import { STATE_ABBR_TO_NAME } from "@/lib/fips";

interface Props {
  county: County | null;
  onClose: () => void;
}

export function CountyPanel({ county, onClose }: Props) {
  if (!county) {
    return (
      <aside className="rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)] p-4 text-sm text-[var(--color-stone)]">
        <p className="italic">
          Click any county on the map to open its access-gap summary.
        </p>
      </aside>
    );
  }

  const stateName = STATE_ABBR_TO_NAME[county.state] ?? county.state;
  const rows: Array<{ label: string; value: string; warn?: boolean }> = [
    {
      label: "Population",
      value: county.population != null ? formatInt(county.population) : "—",
    },
    {
      label: "Cardiologists (practice location)",
      value: `${county.n_cardiologists}`,
      warn: county.n_cardiologists === 0,
    },
    {
      label: "Miles to nearest cardiologist",
      value:
        county.miles_to_nearest_cardiologist != null
          ? `${formatNum(county.miles_to_nearest_cardiologist)} mi`
          : "—",
      warn:
        (county.miles_to_nearest_cardiologist ?? 0) >= 50 &&
        county.n_cardiologists === 0,
    },
    { label: "Hospitals", value: `${county.n_hospitals}` },
    {
      label: "Critical Access Hospitals",
      value: `${county.n_cah}`,
      warn: county.n_cah > 0,
    },
    {
      label: "HPSA — primary care",
      value: county.hpsa_primary_care ? "Designated" : "No",
      warn: county.hpsa_primary_care,
    },
    {
      label: "Coronary heart disease (adults, age-adj. %)",
      value:
        county.heart_disease_mortality_per_100k != null
          ? `${formatNum(county.heart_disease_mortality_per_100k)}%`
          : "—",
    },
    {
      label: "Rural-Urban Continuum (RUCC 2023)",
      value:
        county.rucc_2023 != null
          ? `${county.rucc_2023} · ${county.rural ? "Rural" : "Urban"}`
          : "—",
    },
  ];

  return (
    <aside className="flex flex-col gap-4 rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)] p-5">
      <header className="flex items-start justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-[0.22em] text-[var(--color-stone)]">
            FIPS {county.fips}
          </p>
          <h2 className="font-[var(--font-display)] text-2xl leading-tight">
            {county.county}
          </h2>
          <p className="text-sm text-[var(--color-stone)]">{stateName}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close county details"
          className="rounded p-1 text-[var(--color-stone)] transition hover:bg-[var(--color-panel-hi)]"
        >
          <X size={18} />
        </button>
      </header>

      <dl className="grid grid-cols-1 gap-2 text-sm">
        {rows.map(({ label, value, warn }) => (
          <div
            key={label}
            className="flex items-baseline justify-between gap-4 border-b border-dashed border-[var(--color-grid)] pb-1.5"
          >
            <dt className="text-[var(--color-stone)]">{label}</dt>
            <dd
              className={
                warn
                  ? "font-semibold text-[var(--color-alert-deep)]"
                  : "font-medium text-[var(--color-cool)]"
              }
            >
              {value}
            </dd>
          </div>
        ))}
      </dl>

      <div className="flex flex-wrap gap-2 text-sm">
        <Link
          href={`/county/${county.fips}`}
          className="rounded-md bg-[var(--color-signal-deep)] px-3 py-1.5 text-white transition hover:bg-[var(--color-signal)]"
        >
          Open full report
        </Link>
        <a
          href={`https://heartlandprotocol.org`}
          className="rounded-md border border-[var(--color-grid)] px-3 py-1.5 transition hover:bg-[var(--color-panel-hi)]"
          target="_blank"
          rel="noopener noreferrer"
        >
          HEARTLAND Protocol ↗
        </a>
      </div>
    </aside>
  );
}

function formatInt(n: number): string {
  return n.toLocaleString("en-US");
}

function formatNum(n: number): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: 1 });
}
