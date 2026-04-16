import type { County } from "./types";

export interface FilterState {
  state: string | null;       // e.g. "TX"
  ruralOnly: boolean;
  cahOnly: boolean;
  hpsaOnly: boolean;
  metric: "density" | "distance";
  selected: string | null;    // FIPS of the currently open county
}

export const DEFAULT_FILTERS: FilterState = {
  state: null,
  ruralOnly: false,
  cahOnly: false,
  hpsaOnly: false,
  metric: "density",
  selected: null,
};

export function applyFilters(list: County[], f: FilterState): County[] {
  return list.filter((c) => {
    if (f.state && c.state !== f.state) return false;
    if (f.ruralOnly && !c.rural) return false;
    if (f.cahOnly && c.n_cah === 0) return false;
    if (f.hpsaOnly && !c.hpsa_primary_care) return false;
    return true;
  });
}
