import type { Metadata } from "next";
import { STATE_ABBR_TO_NAME } from "./fips";
import type { County, CountyIndex } from "./types";

export const OG_CACHE_CONTROL = "private, no-store, max-age=0";
export const GENERIC_OG_IMAGE_PATH = "/api/og";

export function ogImagePathForFips(fips: string | null): string {
  if (fips === null) return GENERIC_OG_IMAGE_PATH;
  if (!/^[0-9]{5}$/.test(fips)) throw new Error("OG image FIPS must be exactly five digits");
  return `${GENERIC_OG_IMAGE_PATH}?fips=${encodeURIComponent(fips)}`;
}

export function socialImageMetadata(fips: string | null, alt: string) {
  if (!alt.trim()) throw new Error("OG image alt text must be nonempty");
  const url = ogImagePathForFips(fips);
  return {
    openGraph: [{ url, width: 1200, height: 630, alt }],
    twitter: [url],
  };
}

export function areaPageMetadata(county: County): Metadata {
  const title = `${county.county}, ${county.state}`;
  const description = `${county.county} area summary: ${county.n_cardiologists} qualifying NPPES-listed providers assigned; ${county.miles_to_nearest_cardiologist ?? "n/a"} great-circle miles to the nearest canonical qualifying-provider location.`;
  const socialImages = socialImageMetadata(county.fips, title);
  return {
    title,
    description,
    openGraph: {
      title,
      description,
      images: socialImages.openGraph,
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
      images: socialImages.twitter,
    },
  };
}

export interface OgCopy {
  county: County | null;
  title: string;
  subtitle: string;
  tag: string;
}

export function ogCopyForFips(fips: string | null, index: CountyIndex): OgCopy {
  const county = fips ? index[fips] ?? null : null;
  const title = county
    ? `${county.county}, ${STATE_ABBR_TO_NAME[county.state] ?? county.state}`
    : "Rural Cardiology Desert Atlas";
  const subtitle = county
    ? `${county.n_cardiologists} qualifying provider${county.n_cardiologists === 1 ? "" : "s"} assigned · ${
        county.miles_to_nearest_cardiologist != null
          ? `${county.miles_to_nearest_cardiologist.toFixed(1)} great-circle mi to nearest`
          : "—"
      }`
    : "Cardiology access across US counties and county equivalents";
  const tag = county
    ? county.n_cardiologists === 0
      ? "NO QUALIFYING PROVIDER ASSIGNED"
      : county.hpsa_primary_care
        ? "HPSA · PRIMARY CARE"
        : county.rural
          ? "RURAL AREA"
          : "AREA REPORT"
    : "VERSION-PINNED PUBLIC DATA";
  return { county, title, subtitle, tag };
}
