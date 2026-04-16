# Rural Cardiology Desert Atlas

Interactive US map visualizing the cardiology access gap — cardiologist
density, distance to nearest specialist, Critical Access Hospitals, HRSA
primary-care shortage areas, and heart disease mortality — across every
county in the United States.

Built alongside the [HEARTLAND Protocol](https://heartlandprotocol.org)
to support primary care–led heart failure management in rural and
resource-limited US settings.

Live site: **https://atlas.heartlandprotocol.org**

| | |
|-|-|
| **Author** | Vicky Muller Ferreira, MD ([ORCID 0009-0009-1099-5690](https://orcid.org/0009-0009-1099-5690)) |
| **License** | MIT (code) · public-domain + CC BY 4.0 (upstream data) |
| **Stack** | Next.js 16 · React 19 · TypeScript 5 · Tailwind v4 · react-simple-maps |
| **Pipeline** | Python 3.12 · pandas · scipy |
| **Deployment** | Vercel |
| **DOI** | Minted on first GitHub release via `.zenodo.json` |

## Features

- **Choropleth** by cardiologist density or distance to nearest cardiologist.
- **Clickable counties** with a full detail panel (population, CAHs,
  HPSA status, mortality rate, RUCC code).
- **Filters** — state selector, rural-only, CAH-only, HPSA-only.
- **Per-county deep-link page** at `/county/[fips]` (printable one-pager).
- **Embeddable** `/embed` endpoint with a copy-paste iframe snippet.
- **Dynamic OG image** per county at `/api/og?fips=<FIPS>`.
- **Monthly auto-refresh** via GitHub Actions — opens a PR when upstream
  data changes.

## Data sources

All inputs are public aggregate datasets — no patient information.

| Source | Role |
|-|-|
| CMS NPPES NPI Registry | Cardiology provider locations |
| CMS Hospital General Information | Hospitals + Critical Access Hospitals |
| CDC Atlas of Heart Disease and Stroke | Age-adjusted HD mortality (35+) |
| US Census ACS 5-year | Population, median household income |
| USDA ERS RUCC 2023 | Rural-Urban Continuum Codes |
| HRSA HPSA | Primary-care shortage designations |
| US Census ZCTA↔County (2020) | ZIP-to-county crosswalk |
| US Census 2023 Gazetteer | County and ZCTA centroids |

Full methodology: [atlas.heartlandprotocol.org/about](https://atlas.heartlandprotocol.org/about).

## Repo layout

```
atlas/
├── app/            # Next.js 16 application (deployed to Vercel)
├── scripts/        # Python ETL pipeline → counties.json
├── .github/        # CI + scheduled monthly data refresh
├── .zenodo.json
├── CITATION.cff
├── LICENSE
└── NIW_INTEGRATION.md  # Immigration-petition tracking (internal)
```

## Build locally

### 1. Data pipeline (once, or monthly)

```bash
cd scripts
uv sync
uv run python run_all.py
cp data/processed/counties.json ../app/public/data/counties.json
```

The pipeline downloads NPPES (~7 GB zipped) on first run. Raw files are
cached under `scripts/data/raw/` and are not committed.

### 2. App

```bash
cd app
npm install
npm run dev      # http://localhost:3000
npm run build    # production build
npm test         # vitest
```

## Citation

Muller Ferreira V. _Rural Cardiology Desert Atlas_ [Software &amp; dataset].
2026. `https://atlas.heartlandprotocol.org`. DOI assigned via Zenodo on first
release.

Related publication: Muller Ferreira V. _HEARTLAND Protocol: Heart failure
Evidence-based Access in Rural Treatment, Linking Advanced Network
Delivery._ Cureus, 2026.

## Disclaimers

- This atlas uses publicly available aggregate data. It contains no patient
  health information.
- County provider counts reflect primary practice ZIP codes reported to CMS
  NPPES. They do not reflect part-time, locum tenens, or telehealth
  availability.
- Distance is computed between county centroids and ZIP centroids; values
  inside very large urban ZIP codes are intentionally coarse at the
  county-level resolution of this atlas.
