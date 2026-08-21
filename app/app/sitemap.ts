import type { MetadataRoute } from "next";
import { loadCounties } from "@/lib/counties";
import {
  DATASET_LANDING_URL,
  DATASET_RELEASE_DATE,
} from "@/lib/dataset-metadata";

const SITE_URL = "https://atlas.heartlandprotocol.org";

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const { list } = await loadCounties();
  const lastModified = new Date(`${DATASET_RELEASE_DATE}T00:00:00Z`);

  return [
    {
      url: SITE_URL,
      lastModified,
      changeFrequency: "yearly",
      priority: 1,
    },
    {
      url: DATASET_LANDING_URL,
      lastModified,
      changeFrequency: "yearly",
      priority: 0.9,
    },
    ...list.map((county) => ({
      url: `${SITE_URL}/county/${county.fips}`,
      lastModified,
      changeFrequency: "yearly" as const,
      priority: 0.6,
    })),
  ];
}
