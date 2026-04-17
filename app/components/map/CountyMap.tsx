"use client";

import { useEffect, useMemo, useState } from "react";
import {
  ComposableMap,
  Geographies,
  Geography,
  ZoomableGroup,
} from "@vnedyalk0v/react19-simple-maps";
import { feature } from "topojson-client";
import { geoBounds } from "d3-geo";
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

// Default CONUS view. geoAlbersUsa canvas is 1000x600 and tuned so that
// zoom=1 at center=[-96.9,38] shows the full US including AK/HI insets.
const DEFAULT_CENTER: [number, number] = [-96.9, 38];
const DEFAULT_ZOOM = 1;

// Special-case viewports for the inset states.
// geoAlbersUsa projects AK/HI to canvas corners, not their real lat/lng —
// so we center on the projected inset position (approximate lon/lat that
// AlbersUsa maps into the correct box).
const STATE_VIEW_OVERRIDES: Record<string, { center: [number, number]; zoom: number }> = {
  // For AK/HI, use the projected inset coordinates via d3-geo fallbacks.
  AK: { center: [-150, 63], zoom: 3.2 },
  HI: { center: [-157.5, 20.7], zoom: 4.5 },
};

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

  // Compute center + zoom for the current state filter.
  const viewport = useMemo<{ center: [number, number]; zoom: number }>(() => {
    if (!stateFilter || !countyFc) {
      return { center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM };
    }
    if (STATE_VIEW_OVERRIDES[stateFilter]) {
      return STATE_VIEW_OVERRIDES[stateFilter];
    }
    const prefix = STATE_ABBR_TO_FIPS[stateFilter];
    const stateFeatures = countyFc.features.filter((f) =>
      String(f.id ?? "").padStart(5, "0").startsWith(prefix)
    );
    if (stateFeatures.length === 0) {
      return { center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM };
    }
    const fc: FeatureCollection = { type: "FeatureCollection", features: stateFeatures };
    const [[minLng, minLat], [maxLng, maxLat]] = geoBounds(fc);
    const center: [number, number] = [(minLng + maxLng) / 2, (minLat + maxLat) / 2];
    // CONUS roughly spans lng -125→-67 (58°) and lat 25→49 (24°). AlbersUsa
    // fits CONUS at zoom=1. Zoom = span ratio, capped so very small states
    // (DC, RI) stay readable and very wide states (TX, MT) still fit.
    const lngSpan = Math.max(0.5, maxLng - minLng);
    const latSpan = Math.max(0.5, maxLat - minLat);
    const rawZoom = Math.min(58 / lngSpan, 24 / latSpan) * 0.9;
    const zoom = Math.max(1.4, Math.min(rawZoom, 14));
    return { center, zoom };
  }, [stateFilter, countyFc]);

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
          projectionConfig={{ scale: 1200 } as never}
          width={1000}
          height={600}
          style={{ width: "100%", height: "100%" }}
        >
          <ZoomableGroup
            center={viewport.center as never}
            zoom={viewport.zoom}
            minZoom={1}
            maxZoom={16}
          >
            <Geographies geography={countyFc}>
              {({ geographies }) =>
                geographies.map((geo: Feature) => {
                  const fips = String(geo.id ?? "").padStart(5, "0");
                  const inState = !stateFipsPrefix || fips.startsWith(stateFipsPrefix);
                  const c: County | undefined = counties[fips];
                  const fill = inState ? colorFor(c, metric) : "var(--color-panel-hi)";
                  const isSelected = selectedFips === fips;
                  const opacity = inState ? 1 : 0.35;
                  return (
                    <Geography
                      key={fips}
                      geography={geo}
                      onClick={inState ? () => onSelect(fips) : undefined}
                      onKeyDown={
                        inState
                          ? (e: React.KeyboardEvent) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                onSelect(fips);
                              }
                            }
                          : undefined
                      }
                      tabIndex={inState ? 0 : -1}
                      role={inState ? "button" : "presentation"}
                      aria-label={
                        c && inState
                          ? `${c.county}, ${c.state}. ${c.n_cardiologists} cardiologist${c.n_cardiologists === 1 ? "" : "s"}.`
                          : undefined
                      }
                      className={cn(
                        inState ? "atlas-county" : "atlas-county-dim",
                        isSelected && "is-selected"
                      )}
                      style={{
                        default: {
                          fill,
                          fillOpacity: opacity,
                          stroke: "#ffffff",
                          strokeWidth: 0.25,
                          outline: "none",
                          pointerEvents: inState ? "auto" : "none",
                        },
                        hover: {
                          fill,
                          fillOpacity: inState ? 1 : opacity,
                          stroke: "var(--color-cool)",
                          strokeWidth: inState ? 0.6 : 0.25,
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
          </ZoomableGroup>
        </ComposableMap>
      )}
    </div>
  );
}
