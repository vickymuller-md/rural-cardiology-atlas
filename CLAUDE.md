# Rural Cardiology Desert Atlas

## Parent Project

Subproject of `~/NIW-project/`. Part of the HEARTLAND Protocol ecosystem for the EB-2 NIW petition of Vicky Muller Ferreira, MD.

## Purpose

Interactive US map visualizing the cardiology access gap in rural America. Shows counties without cardiologists, distance-to-nearest-specialist overlays, HF hospitalization rates, and population demographics. The definitive public resource proving the problem the HEARTLAND Protocol solves.

## NIW Role

- **Prong 2**: demonstrates Vicky has moved beyond publication into public data infrastructure
- **Prong 3**: freely available tool for policy makers, researchers, hospital administrators, and journalists investigating rural health disparities
- **Evidence**: unique visitors, media citations, embeds by other organizations

## Stack

- **Framework**: Next.js (App Router) or Astro (static-first, better for pure visualization)
- **Mapping**: MapLibre GL JS or D3.js with TopoJSON (US counties)
- **Data processing**: Python scripts to preprocess public datasets into static JSON/CSV
- **Deployment**: Vercel → `atlas.heartlandprotocol.org`
- **Language**: English

## Data Sources (all public, no PHI)

1. **NPPES NPI Registry** (CMS) — every licensed provider with specialty code, practice address, and state. Filter for cardiology specialties (Internal Medicine - Cardiovascular Disease, etc.). Source: https://download.cms.gov/nppes/NPI_Files.html
2. **CMS Provider of Services** — facility-level data for hospitals including Critical Access Hospital designation. Source: https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/provider-of-services-file
3. **CDC WONDER** — heart disease mortality rates by county. Source: https://wonder.cdc.gov/
4. **Census Bureau** — county population, rural/urban classification (RUCC codes), demographics. Source: https://www.census.gov/data/datasets/time-series/demo/popest/2020s-counties-total.html
5. **HRSA Area Health Resources Files** — health professional shortage areas (HPSAs). Source: https://data.hrsa.gov/

## Features (MVP)

1. **Choropleth map**: counties colored by cardiologist density (0, 1-2, 3+, 5+)
2. **Distance overlay**: isochrone or nearest-provider distance heat layer
3. **County detail panel**: click county → show population, # cardiologists, nearest specialist distance, HF death rate, CAH count, HPSA status
4. **Filters**: state selector, rural-only toggle, CAH-only toggle, HPSA overlay
5. **Statistics bar**: national summary (X% counties with 0 cardiologists, median distance, etc.)
6. **Export**: screenshot/PNG of current view for presentations, PDF county report
7. **Embed**: `<iframe>` snippet for other websites to embed the map
8. **About**: methodology, data sources, update frequency, link to HEARTLAND Protocol

## Design

- Light theme, clinical/cartographic aesthetic
- Warm cream background matching heartlandprotocol.org palette
- Red/amber/green diverging scale for risk visualization
- Mobile-responsive (map with bottom sheet on mobile)
- Print-friendly single-page county summary

## Domain

`atlas.heartlandprotocol.org`

## DOI Strategy

- Zenodo deposit as "dataset + visualization tool"
- `.zenodo.json` at repo root, creator = Vicky Muller Ferreira
- GitHub repo under `vickymuller-md/rural-cardiology-atlas` (public)

## Disclaimers

- "This visualization uses publicly available aggregate data. It does not contain patient health information."
- "County-level provider counts may not reflect part-time, locum, or telehealth availability."
- Link back to HEARTLAND Protocol and Cureus publication

## Data Pipeline

```
scripts/
  01_download_nppes.py      # Download + filter NPPES for cardiology NPIs
  02_download_cms_pos.py    # Download Provider of Services (CAH identification)
  03_download_cdc_wonder.py # Heart disease mortality by county
  04_download_census.py     # Population + RUCC rural/urban codes
  05_build_county_json.py   # Merge all sources → counties.json (static)
  06_compute_distances.py   # Nearest-cardiologist distance per county centroid
```

Output: `public/data/counties.json` (~3,200 entries, ~2MB)

## Build & Test

```bash
# Data pipeline (run once, or on schedule)
cd scripts && python 01_download_nppes.py && ... && python 06_compute_distances.py

# App
npm install
npm run dev     # localhost:3000
npm run build   # production build
npm test        # vitest
```

## Rules

- English only
- All data must be from public, cited sources — no scraped or proprietary data
- No patient-level data, ever
- County FIPS codes as primary key throughout
- Credit data sources in the About page and README
- Methodology must be reproducible (scripts + raw source URLs documented)
- Author: Vicky Muller Ferreira, MD — consistent with all other subprojects

## Execution Plan

### Session 1: Data pipeline
- Write Python preprocessing scripts (01–06)
- Download and process all public datasets
- Output `counties.json` with merged fields
- Validate: spot-check 10 counties against known data

### Session 2: Map application
- Init Next.js/Astro project
- Implement choropleth with MapLibre or D3
- County click → detail panel
- Filters (state, rural, CAH, HPSA)
- Statistics summary bar
- Responsive layout

### Session 3: Polish + deploy
- Export (PNG, PDF)
- Embed snippet
- About/methodology page
- README + LICENSE (MIT) + .zenodo.json
- Deploy to Vercel → atlas.heartlandprotocol.org
- Push to vickymuller-md/rural-cardiology-atlas
- Zenodo DOI

## Timeline

3 sessions estimated.
