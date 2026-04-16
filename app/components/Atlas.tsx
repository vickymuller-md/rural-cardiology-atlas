"use client";

import { useMemo } from "react";
import { parseAsBoolean, parseAsString, parseAsStringLiteral, useQueryStates } from "nuqs";
import type { County, CountyIndex } from "@/lib/types";
import type { FilterState } from "@/lib/filters";
import { CountyMap } from "./map/CountyMap";
import { CountyPanel } from "./map/CountyPanel";
import { Legend } from "./map/Legend";
import { FilterBar } from "./filters/FilterBar";

interface Props {
  list: County[];
  index: CountyIndex;
}

const metricParser = parseAsStringLiteral(["density", "distance"] as const).withDefault(
  "density"
);

export function Atlas({ list, index }: Props) {
  const [q, setQ] = useQueryStates(
    {
      state: parseAsString,
      rural: parseAsBoolean.withDefault(false),
      cah: parseAsBoolean.withDefault(false),
      hpsa: parseAsBoolean.withDefault(false),
      metric: metricParser,
      fips: parseAsString,
    },
    { history: "replace", shallow: true }
  );

  const filters: FilterState = {
    state: q.state,
    ruralOnly: q.rural,
    cahOnly: q.cah,
    hpsaOnly: q.hpsa,
    metric: q.metric,
    selected: q.fips,
  };

  const patch = (p: Partial<FilterState>) => {
    setQ({
      state: p.state !== undefined ? p.state : q.state,
      rural: p.ruralOnly ?? q.rural,
      cah: p.cahOnly ?? q.cah,
      hpsa: p.hpsaOnly ?? q.hpsa,
      metric: p.metric ?? q.metric,
      fips: p.selected !== undefined ? p.selected : q.fips,
    });
  };

  const selected = filters.selected ? index[filters.selected] ?? null : null;

  // Mask out filtered-out counties by passing a narrower index to the map.
  const visibleIndex = useMemo(() => {
    if (!filters.ruralOnly && !filters.cahOnly && !filters.hpsaOnly) return index;
    const out: CountyIndex = {};
    for (const c of list) {
      if (filters.ruralOnly && !c.rural) continue;
      if (filters.cahOnly && c.n_cah === 0) continue;
      if (filters.hpsaOnly && !c.hpsa_primary_care) continue;
      out[c.fips] = c;
    }
    return out;
  }, [list, index, filters.ruralOnly, filters.cahOnly, filters.hpsaOnly]);

  return (
    <div className="flex flex-col gap-4">
      <FilterBar value={filters} onChange={patch} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_360px]">
        <div className="flex flex-col gap-3">
          <CountyMap
            counties={visibleIndex}
            metric={filters.metric}
            stateFilter={filters.state}
            selectedFips={filters.selected}
            onSelect={(fips) => patch({ selected: fips })}
          />
          <Legend metric={filters.metric} />
        </div>
        <CountyPanel county={selected} onClose={() => patch({ selected: null })} />
      </div>
    </div>
  );
}
