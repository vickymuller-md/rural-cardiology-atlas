export const DATASET_LANDING_URL = "https://atlas.heartlandprotocol.org/about";
export const DATASET_RELEASE_DATE = "2026-07-12";

export const datasetStructuredData = {
  "@context": "https://schema.org",
  "@type": "Dataset",
  "@id": `${DATASET_LANDING_URL}#dataset`,
  name: "Rural Cardiology Desert Atlas",
  alternateName: "Rural Cardiology Desert Atlas v0.2.0",
  description:
    "Versioned open research and planning dataset covering the exact 3,144-county and county-equivalent 2025 Census universe in the 50 United States and District of Columbia. It combines qualifying CMS NPPES adult-cardiology provider records, hospitals and Critical Access Hospitals, controlled geographic assignments, ACS demographics, CDC PLACES coronary-heart-disease prevalence, USDA rurality, and HRSA primary-care HPSA status. It contains no patient-level data and does not measure verified clinician availability, appointment capacity, transportation-network metrics, care quality, or individual clinical effects.",
  url: DATASET_LANDING_URL,
  mainEntityOfPage: DATASET_LANDING_URL,
  version: "0.2.0",
  datePublished: DATASET_RELEASE_DATE,
  dateModified: DATASET_RELEASE_DATE,
  inLanguage: "en",
  isAccessibleForFree: true,
  conditionsOfAccess:
    "Open access. Atlas source code is MIT licensed; upstream data and derived assets retain their source-specific terms and attribution requirements.",
  license: `${DATASET_LANDING_URL}#citation-and-license`,
  keywords: [
    "rural health",
    "cardiology",
    "access to care",
    "Critical Access Hospitals",
    "health professional shortage areas",
    "health geography",
    "open data",
    "United States counties",
  ],
  identifier: [
    "https://doi.org/10.5281/zenodo.21323595",
    "RRID:SCR_028847",
  ],
  sameAs: [
    "https://doi.org/10.5281/zenodo.21323595",
    "https://doi.org/10.5281/zenodo.19634990",
    "https://scicrunch.org/resolver/RRID:SCR_028847",
    "https://www.ruralhealthinfo.org/resources/28561",
    "https://github.com/vickymuller-md/rural-cardiology-atlas/tree/v0.2.0",
  ],
  creator: {
    "@type": "Person",
    name: "Vicky Muller Ferreira, MD",
    givenName: "Vicky",
    familyName: "Muller Ferreira",
    sameAs: "https://orcid.org/0009-0009-1099-5690",
  },
  publisher: {
    "@type": "Organization",
    name: "HEARTLAND Protocol",
    url: "https://heartlandprotocol.org",
  },
  spatialCoverage: {
    "@type": "Place",
    name: "50 United States and District of Columbia",
  },
  temporalCoverage: "2023/2026-07-10",
  measurementTechnique: [
    "Preregistered selection of active individual NPPES records with a primary adult-cardiology taxonomy",
    "Controlled Census batch geocoding with deterministic same-state HUD-USPS fallback",
    "Great-circle distance from Census county internal points to canonical qualifying-provider locations",
  ],
  variableMeasured: [
    "Qualifying adult-cardiology provider count",
    "Great-circle distance to nearest qualifying-provider location",
    "Hospital and Critical Access Hospital counts",
    "Primary-care HPSA designation",
    "Rural-Urban Continuum Code",
    "Population and median household income",
    "Age-adjusted adult coronary-heart-disease prevalence",
  ],
  isBasedOn: [
    "https://download.cms.gov/nppes/NPI_Files.html",
    "https://data.cms.gov/provider-data/dataset/xubh-q36u",
    "https://www.huduser.gov/portal/datasets/usps_crosswalk.html",
    "https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.2025.html",
    "https://www.census.gov/data/developers/data-sets/acs-5year.2024.html",
    "https://chronicdata.cdc.gov/500-Cities-Places/PLACES-Local-Data-for-Better-Health-County-Data-2/swc5-untb/about_data",
    "https://www.ers.usda.gov/data-products/rural-urban-continuum-codes",
    "https://data.hrsa.gov/data/download",
  ],
  citation: [
    "https://doi.org/10.7759/cureus.104817",
    "https://doi.org/10.5281/zenodo.19101219",
  ],
  distribution: [
    {
      "@type": "DataDownload",
      name: "Version 0.2.0 county analytical dataset",
      contentUrl: "https://atlas.heartlandprotocol.org/data/counties.json",
      encodingFormat: "application/json",
      contentSize: "1010809 bytes",
    },
    {
      "@type": "DataDownload",
      name: "Version 0.2.0 national summary",
      contentUrl: "https://atlas.heartlandprotocol.org/data/summary.json",
      encodingFormat: "application/json",
      contentSize: "600 bytes",
    },
    {
      "@type": "DataDownload",
      name: "Version 0.2.0 release manifest",
      contentUrl:
        "https://atlas.heartlandprotocol.org/data/release-manifest.json",
      encodingFormat: "application/json",
      contentSize: "744 bytes",
    },
  ],
} as const;
