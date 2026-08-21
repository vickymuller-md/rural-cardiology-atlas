import type { Metadata } from "next";
import { EmbedSnippet } from "@/components/export/EmbedSnippet";
import { loadAtlasData } from "@/lib/counties";
import { datasetStructuredData } from "@/lib/dataset-metadata";
import {
  DISTANCE_DEFINITION,
  HPSA_LIMITATION,
  HPSA_SNAPSHOT_LABEL,
  PLACES_LABEL,
  PROVIDER_DEFINITION,
} from "@/lib/methodology";

export const metadata: Metadata = {
  title: "Methodology",
  description:
    "Preregistered sources, assignment rules, limitations, and citation for the Rural Cardiology Desert Atlas.",
};

export default async function AboutPage() {
  const { summary } = await loadAtlasData();
  const structuredData = JSON.stringify(datasetStructuredData).replace(
    /</g,
    "\\u003c"
  );

  return (
    <div className="mx-auto mt-10 flex max-w-3xl flex-col gap-8 px-6 pb-16">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: structuredData }}
      />
      <header>
        <p className="text-xs uppercase tracking-[0.25em] text-[var(--color-stone)]">
          Methodology · Data sources · Limitations
        </p>
        <h1 className="mt-1 font-[var(--font-display)] text-4xl">About this atlas</h1>
      </header>

      <section className="prose prose-slate max-w-none leading-relaxed">
        <h2>Purpose and current estimate</h2>
        <p>
          The Rural Cardiology Desert Atlas presents area-level public data for
          the 3,144 counties and county equivalents in the 50 states and District
          of Columbia. It is a companion to the{" "}
          <a
            href="https://doi.org/10.5281/zenodo.19101219"
            target="_blank"
            rel="noopener noreferrer"
          >
            HEARTLAND Protocol v3.3
          </a>
          . It contains no patient-level data.
        </p>
        <p>
          Under the preregistered address-geocoding and fallback rules,{" "}
          <strong>
            {summary.zero_provider_count.toLocaleString("en-US")} of{" "}
            {summary.county_count.toLocaleString("en-US")} counties and county
            equivalents ({summary.zero_provider_pct.toFixed(1)}%)
          </strong>{" "}
          had no qualifying provider assigned in the June 8, 2026 NPPES snapshot.
          The Atlas counts {PROVIDER_DEFINITION}.
        </p>

        <h2>Provider selection and location assignment</h2>
        <p>
          Provider selection is restricted to Entity Type 1 records with exactly
          one primary taxonomy flag and one of five adult-cardiology taxonomy
          codes. Deactivation and reactivation dates are evaluated as of June 8,
          2026. The counted unit is a distinct NPI after strict duplicate checks.
          The Atlas uses the main-file Business Practice Location address; it does
          not use the separate Practice Location Reference File.
        </p>
        <p>
          Street-capable addresses are submitted to the Census Batch Geocoder
          using fixed benchmark 8 (<code>Public_AR_ACS2025</code>) and vintage 825
          (<code>ACS2025_ACS2025</code>). Valid matches supply both county and
          coordinates. Direct-ineligible, tied, unmatched, or invalid matches with
          a syntactically valid ZIP receive one HUD-USPS fallback attempt using the
          unique highest same-state <code>BUS_RATIO</code> from the pinned 2025 Q4
          release. HUD is a ZIP-level allocation proxy, not address-level geocoding;
          fallback coordinates are Census county internal points. No method equates
          postal ZIP5 with a Census ZCTA.
        </p>
        <p>
          Provider counts and distances consume the same content-hashed canonical
          provider-location table. {DISTANCE_DEFINITION}
        </p>

        <h2>Hospitals and Critical Access Hospitals</h2>
        <p>
          Hospitals are independently normalized and assigned from the pinned CMS
          Hospital General Information release. <code>Facility ID</code> is treated
          as an opaque identifier: numeric six-digit identifiers are CCNs, while
          five-digit identifiers ending in <code>F</code> identify federal facilities.
          CAH status requires the exact normalized hospital type and a numeric CCN.
          The national ≥20-mile statistic counts distinct evaluable CAH facilities,
          not counties containing a CAH. In this build,{" "}
          <strong>{summary.cah_gte_20_miles.toLocaleString("en-US")}</strong> of{" "}
          <strong>{summary.cah_evaluable.toLocaleString("en-US")}</strong> evaluable
          in-scope CAHs ({summary.cah_gte_20_pct_evaluable.toFixed(1)}%) meet the
          unrounded ≥20.0-mile threshold; {summary.cah_unmatched.toLocaleString("en-US")} of{" "}
          {summary.cah_total_in_scope.toLocaleString("en-US")} in-scope CAHs lack an
          evaluable canonical facility location.
        </p>

        <h2>Contextual data</h2>
        <ul>
          <li>
            <strong>CDC PLACES:</strong> {PLACES_LABEL}. Source-suppressed and
            unavailable area values remain null and are never imputed.
          </li>
          <li>
            <strong>ACS 2024 five-year estimates:</strong> B01003 county population
            and B19013 median household income.
          </li>
          <li>
            <strong>USDA RUCC 2023:</strong> the Atlas explicitly classifies RUCC
            values 4–9 as rural and 1–3 as urban.
          </li>
          <li>
            <strong>HRSA primary-care HPSA:</strong> {HPSA_SNAPSHOT_LABEL}. Included
            normalized statuses are <code>DESIGNATED</code> and{" "}
            <code>PROPOSED FOR WITHDRAWAL</code>. {HPSA_LIMITATION}
          </li>
          <li>
            <strong>Census 2025 geography:</strong> the county Gazetteer defines
            the analytical universe and county internal points. Locally vendored
            1:5,000,000 Census cartographic boundaries render the map only and do
            not assign entities or calculate distance.
          </li>
        </ul>

        <h2>Versioning and reproducibility</h2>
        <p>
          Every analytical input, canonical table, county asset, and national
          summary is version-pinned and hash-validated. The browser loads the
          precomputed raw-first national summary and verifies that its
          <code> counties_json_sha256 </code> value matches the exact county JSON
          bytes. It does not reconstruct the rural median from rounded public
          distances. Automated release checks are versioned; there is no
          unattended “latest” or automatic monthly publication path.
        </p>
        <p>
          The April 2026 v0.1.0 Atlas result is superseded because its taxonomy,
          NPI-status, address-geocoding, ZIP-to-county fallback, and metric-label
          rules differed from the documented v0.2.0 method.
        </p>

        <h2>Limitations</h2>
        <ul>
          <li>
            NPPES is provider-reported administrative data. An active NPI is not
            proof of current practice, employment, availability, licensure,
            credentialing, full-time status, or acceptance of patients.
          </li>
          <li>
            The main-file Business Practice Location address may be administrative,
            stale, or different from service locations. It is not described as a
            verified primary service site. Additional locations, telehealth, locum
            tenens, schedules, and cross-county service patterns are not measured.
          </li>
          <li>
            Census matching can misassign a county. HUD-USPS fallback can misassign
            an individual provider or facility, omits PO-box-only and some other
            ZIPs, and may contain rounded ratios. Direct-only, residential-ratio,
            and state-unrestricted sensitivity analyses accompany the analytical
            review.
          </li>
          <li>
            Fallback coordinates are county internal points and are coarser than
            direct matched addresses. Great-circle county-internal-point distances
            suppress within-county variation and are {"not road distance or travel time"}.
            They do not measure appointment access or establish which facility is
            closest.
          </li>
          <li>
            CMS registration and hospital type do not establish current cardiology
            capability, operating status on the review date, or service availability.
          </li>
          <li>
            PLACES values are model-based adult CHD prevalence percentages, not
            mortality, heart-failure prevalence, incidence, or individual risk.
            ACS estimates have sampling uncertainty and use a 2024 five-year vintage.
          </li>
          <li>
            The 50-state/DC analysis excludes Puerto Rico and all other US territories.
          </li>
        </ul>

        <h2 id="citation-and-license">Citation and license</h2>
        <p>
          Muller Ferreira V. <em>Rural Cardiology Desert Atlas</em> [software and
          dataset]. 2026. Available from{" "}
          <a href="https://atlas.heartlandprotocol.org">
            atlas.heartlandprotocol.org
          </a>
          . Version-specific citation metadata accompanies each published release.
        </p>
        <p>
          Atlas source code is MIT licensed. Upstream data and derived assets retain
          their source-specific terms and attribution requirements.
        </p>

        <h2>Author</h2>
        <p>
          Vicky Muller Ferreira, MD. ORCID{" "}
          <a
            href="https://orcid.org/0009-0009-1099-5690"
            target="_blank"
            rel="noopener noreferrer"
          >
            0009-0009-1099-5690
          </a>
          .
        </p>
      </section>

      <EmbedSnippet />
    </div>
  );
}
