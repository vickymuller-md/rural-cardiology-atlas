import type { County } from "./types";

export interface NationalStats {
  total: number;
  zeroCardioCount: number;
  zeroCardioPct: number;
  medianRuralDistance: number;
  cahFarFromCardiologist: number;
  populationUnderserved: number;
}

export function computeStats(list: County[]): NationalStats {
  const total = list.length;
  const zeroCardioCount = list.filter((c) => c.n_cardiologists === 0).length;
  const ruralDistances = list
    .filter((c) => c.rural && c.miles_to_nearest_cardiologist != null)
    .map((c) => c.miles_to_nearest_cardiologist as number)
    .sort((a, b) => a - b);
  const medianRuralDistance = ruralDistances.length
    ? ruralDistances[Math.floor(ruralDistances.length / 2)]
    : 0;
  const cahFarFromCardiologist = list.filter(
    (c) => c.n_cah > 0 && (c.miles_to_nearest_cardiologist ?? 0) >= 20
  ).length;
  const populationUnderserved = list
    .filter((c) => c.n_cardiologists === 0)
    .reduce((sum, c) => sum + (c.population ?? 0), 0);
  return {
    total,
    zeroCardioCount,
    zeroCardioPct: total ? zeroCardioCount / total : 0,
    medianRuralDistance,
    cahFarFromCardiologist,
    populationUnderserved,
  };
}
