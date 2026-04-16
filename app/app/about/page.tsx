import type { Metadata } from "next";
import { EmbedSnippet } from "@/components/export/EmbedSnippet";

export const metadata: Metadata = {
  title: "Methodology",
  description:
    "Data sources, aggregation method, limitations, and citation for the Rural Cardiology Desert Atlas.",
};

export default function AboutPage() {
  return (
    <div className="mx-auto mt-10 flex max-w-3xl flex-col gap-8">
      <header>
        <p className="text-xs uppercase tracking-[0.25em] text-[var(--color-stone)]">
          Methodology · Data sources · Citation
        </p>
        <h1 className="mt-1 font-[var(--font-display)] text-4xl">About this atlas</h1>
      </header>

      <section className="prose prose-slate max-w-none leading-relaxed">
        <h2>Purpose</h2>
        <p>
          The Rural Cardiology Desert Atlas maps the cardiology access gap
          across US counties using exclusively public, aggregate data. It was
          built alongside the{" "}
          <a
            href="https://heartlandprotocol.org"
            target="_blank"
            rel="noopener noreferrer"
          >
            HEARTLAND Protocol
          </a>{" "}
          — an implementation toolkit for primary care–led heart failure
          management in rural and resource-limited settings — and is freely
          available for policy makers, researchers, hospital administrators, and
          journalists.
        </p>

        <h2>Data sources</h2>
        <ul>
          <li>
            <strong>NPPES NPI Registry</strong> (CMS, monthly dump). Every
            licensed individual provider whose primary taxonomy code indicates a
            cardiology subspecialty: Cardiovascular Disease, Interventional
            Cardiology, Clinical Cardiac Electrophysiology, Advanced Heart
            Failure and Transplant Cardiology, Adult Congenital Heart Disease.
          </li>
          <li>
            <strong>CMS Hospital General Information</strong> (Provider Data
            Catalog). Source of hospital counts and Critical Access Hospital
            designation per facility.
          </li>
          <li>
            <strong>CDC Interactive Atlas of Heart Disease and Stroke</strong>.
            Age-adjusted heart disease mortality per 100,000 (adults 35+).
          </li>
          <li>
            <strong>US Census Bureau ACS 5-year estimates</strong> for county
            population and median household income.
          </li>
          <li>
            <strong>USDA Economic Research Service — Rural-Urban Continuum
            Codes 2023 (RUCC)</strong>. A county is flagged as rural when its
            RUCC ≥ 4.
          </li>
          <li>
            <strong>HRSA HPSA designations</strong> — primary care Health
            Professional Shortage Area status per county.
          </li>
          <li>
            <strong>US Census ZCTA↔County relationship file (2020)</strong> —
            maps each ZIP Code Tabulation Area to the county that covers most
            of its residential population.
          </li>
          <li>
            <strong>US Census TIGER gazetteer (2023)</strong> — internal
            latitude/longitude points for counties and ZCTAs.
          </li>
        </ul>

        <h2>Distance calculation</h2>
        <p>
          For each county, we compute the great-circle distance between the
          county centroid and the nearest cardiologist&rsquo;s ZIP centroid.
          This is done with a k-d tree built in 3D unit-vector space over all
          unique provider practice locations nationwide, so a county with zero
          in-county cardiologists still reports the distance to the nearest
          provider in a neighboring county. ZIP centroids understate distance
          variability within very large urban ZIPs; at the county level this
          approximation is appropriate.
        </p>

        <h2>Update cadence</h2>
        <p>
          Underlying datasets refresh at different cadences: NPPES monthly, CMS
          hospitals weekly, Census ACS annually, USDA RUCC every 10 years (most
          recent release 2023), CDC mortality every few years. A GitHub Action
          re-runs the pipeline monthly and opens a pull request when
          <code> counties.json </code> changes.
        </p>

        <h2>Limitations</h2>
        <ul>
          <li>
            Provider counts reflect the number of licensed cardiologists whose
            primary practice ZIP code falls within the county. They do not
            reflect part-time, locum tenens, or telehealth availability.
          </li>
          <li>
            A single cardiologist with multiple practice locations is counted
            at their primary practice ZIP only.
          </li>
          <li>
            Hospital-based practice ZIPs may pull specialists into a different
            county than their patient population.
          </li>
          <li>
            ZCTA↔County crosswalks are an approximation; ~1-2% of ZIPs span
            more than one county, in which case we assign to the county with
            the greatest population overlap.
          </li>
          <li>
            This atlas does not contain patient health information.
          </li>
        </ul>

        <h2>Citation</h2>
        <p>
          Muller Ferreira V. Rural Cardiology Desert Atlas [Software &amp;
          dataset]. 2026. Available from:{" "}
          <a href="https://atlas.heartlandprotocol.org">
            https://atlas.heartlandprotocol.org
          </a>
        </p>
        <p>
          Related publication: Muller Ferreira V. &quot;HEARTLAND Protocol:
          Heart failure Evidence-based Access in Rural Treatment, Linking
          Advanced Network Delivery.&quot; Cureus, 2026.
        </p>

        <h2>License</h2>
        <p>
          Source code and derived data are released under the MIT License.
          Upstream sources retain their own licenses — all public-domain US
          government datasets except the Census ZIP centroid file
          (CC BY 4.0 Census). Please cite this atlas and its source datasets
          when reusing.
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
