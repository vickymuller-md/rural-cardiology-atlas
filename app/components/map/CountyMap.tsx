"use client";

import { useEffect, useMemo, useState } from "react";
import {
  ComposableMap,
  Geographies,
  Geography,
} from "@vnedyalk0v/react19-simple-maps";
import { feature } from "topojson-client";
import type { Feature, FeatureCollection } from "geojson";
import type { Topology, GeometryCollection } from "topojson-specification";
import type { County, CountyIndex, ChoroplethMetric } from "@/lib/types";
import { colorFor } from "@/lib/color-scale";
import { STATE_ABBR_TO_FIPS } from "@/lib/fips";
import { US_COUNTIES_TOPO, US_STATES_TOPO } from "./topology";
import { cn } from "@/lib/cn";

interface Props {
  counties: CountyIndex;
  metric: ChoroplethMetric;
  stateFilter: string | null;
  selectedFips: string | null;
  onSelect: (fips: string) => void;
}

type USTopo = Topology<{
  counties: GeometryCollection;
  states: GeometryCollection;
}>;

export function CountyMap({
  counties,
  metric,
  stateFilter,
  selectedFips,
  onSelect,
}: Props) {
  const [topo, setTopo] = useState<USTopo | null>(null);
  const [statesTopo, setStatesTopo] = useState<USTopo | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetch(US_COUNTIES_TOPO).then((r) => r.json()),
      fetch(US_STATES_TOPO).then((r) => r.json()),
    ])
      .then(([c, s]) => {
        if (!cancelled) {
          setTopo(c);
          setStatesTopo(s);
        }
      })
      .catch((e) => console.error("topology load failed", e));
    return () => {
      cancelled = true;
    };
  }, []);

  const { countyFc, stateFc } = useMemo(() => {
    if (!topo || !statesTopo) return { countyFc: null, stateFc: null };
    return {
      countyFc: feature(topo, topo.objects.counties) as unknown as FeatureCollection,
      stateFc: feature(statesTopo, statesTopo.objects.states) as unknown as FeatureCollection,
    };
  }, [topo, statesTopo]);

  const stateFipsPrefix = stateFilter ? STATE_ABBR_TO_FIPS[stateFilter] : null;

  const projectionConfig = useMemo<Record<string, unknown>>(() => {
    if (!stateFilter) return { scale: 1200 };
    if (stateFilter === "AK") return { scale: 1100, center: [-150, 63] };
    if (stateFilter === "HI") return { scale: 2600, center: [-157, 20.5] };
    return { scale: 3200 };
  }, [stateFilter]);

  return (
    <div
      className={cn(
        "relative aspect-[5/3] w-full overflow-hidden rounded-lg border border-[var(--color-grid)]",
        "bg-[var(--color-panel)]"
      )}
      role="application"
      aria-label="US county cardiologist access map"
    >
      {!topo && (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-[var(--color-stone)]">
          Loading map…
        </div>
      )}
      {countyFc && stateFc && (
        <ComposableMap
          projection="geoAlbersUsa"
          projectionConfig={projectionConfig as never}
          width={1000}
          height={600}
          style={{ width: "100%", height: "100%" }}
        >
          <Geographies geography={countyFc}>
            {({ geographies }) =>
              geographies.map((geo: Feature) => {
                const fips = String(geo.id ?? "").padStart(5, "0");
                if (stateFipsPrefix && !fips.startsWith(stateFipsPrefix)) return null;
                const c: County | undefined = counties[fips];
                const fill = colorFor(c, metric);
                const isSelected = selectedFips === fips;
                return (
                  <Geography
                    key={fips}
                    geography={geo}
                    onClick={() => onSelect(fips)}
                    onKeyDown={(e: React.KeyboardEvent) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onSelect(fips);
                      }
                    }}
                    tabIndex={0}
                    role="button"
                    aria-label={
                      c
                        ? `${c.county}, ${c.state}. ${c.n_cardiologists} cardiologist${c.n_cardiologists === 1 ? "" : "s"}.`
                        : `County ${fips}`
                    }
                    className={cn("atlas-county", isSelected && "is-selected")}
                    style={{
                      default: {
                        fill,
                        stroke: "#ffffff",
                        strokeWidth: 0.25,
                        outline: "none",
                      },
                      hover: {
                        fill,
                        stroke: "var(--color-cool)",
                        strokeWidth: 0.6,
                        outline: "none",
                      },
                      pressed: {
                        fill,
                        stroke: "var(--color-cool)",
                        strokeWidth: 1,
                        outline: "none",
                      },
                    }}
                  />
                );
              })
            }
          </Geographies>
          <Geographies geography={stateFc}>
            {({ geographies }) =>
              geographies.map((geo: Feature, i: number) => (
                <Geography
                  key={`state-${i}`}
                  geography={geo}
                  style={{
                    default: {
                      fill: "transparent",
                      stroke: "var(--color-grid-hi)",
                      strokeWidth: 0.6,
                      outline: "none",
                      pointerEvents: "none",
                    },
                    hover: { fill: "transparent", outline: "none" },
                    pressed: { fill: "transparent", outline: "none" },
                  }}
                />
              ))
            }
          </Geographies>
        </ComposableMap>
      )}
    </div>
  );
}
