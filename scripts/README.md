# Rural Cardiology Atlas — Data Pipeline

Reproducible ETL that produces `scripts/data/processed/counties.json`
(the single artifact consumed by the Next.js app at `../app/public/data/`).

All inputs are **public, aggregate datasets**. No patient data.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

```bash
uv sync              # from this directory
# or: pip install -e .
```

## Sources

| # | Source | URL | License |
|-|-|-|-|
| 1 | NPPES NPI Registry (monthly dump) | https://download.cms.gov/nppes/NPI_Files.html | Public domain |
| 2 | CMS Provider of Services (quarterly) | https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/provider-of-services-file | Public domain |
| 3 | CDC Atlas — Heart Disease Mortality by County | https://nccd.cdc.gov/DHDSPAtlas/ | Public domain |
| 4 | US Census ACS 5-year (counties) | https://api.census.gov/data/2022/acs/acs5 | Public domain |
| 4 | USDA ERS — Rural-Urban Continuum Codes 2023 | https://www.ers.usda.gov/data-products/rural-urban-continuum-codes.aspx | Public domain |
| 5 | HRSA HPSA designations | https://data.hrsa.gov/ | Public domain |
| - | HUD USPS ZIP→County crosswalk | https://www.huduser.gov/portal/datasets/usps_crosswalk.html | Public domain |
| - | SimpleMaps US ZIP centroids (free tier) | https://simplemaps.com/data/us-zips | CC BY 4.0 |
| - | US Census TIGER county centroids | https://www2.census.gov/geo/docs/maps-data/data/gazetteer/ | Public domain |

## Run order

```bash
uv run python 01_download_nppes.py         # ~30-60min, ~7GB zip + ~10GB CSV
uv run python 02_download_cms_pos.py       # ~1min
uv run python 03_download_cdc_mortality.py # ~30s
uv run python 04_download_census.py        # ~2min (API paginated)
uv run python 05_download_hrsa_hpsa.py     # ~1min
uv run python 06_build_county_json.py      # ~30s
uv run python 07_compute_distances.py      # ~1min
```

Or all at once:

```bash
uv run python run_all.py
```

## Outputs

- `data/raw/` — untouched downloads (not committed; reproducible)
- `data/interim/*.parquet` — filtered slices per source (not committed)
- `data/processed/counties.json` — final artifact (~2MB, committed)
- `data/MANIFEST.json` — URLs + SHA256 + fetched-at timestamps for every input

## Fields in counties.json

```json
{
  "fips": "01001",
  "state": "AL",
  "county": "Autauga",
  "population": 59759,
  "rucc_2023": 2,
  "rural": false,
  "n_cardiologists": 0,
  "miles_to_nearest_cardiologist": 28.4,
  "n_hospitals": 2,
  "n_cah": 0,
  "hpsa_primary_care": true,
  "heart_disease_mortality_per_100k": 189.2
}
```

## Methodology notes

- Cardiologists counted per **practice ZIP5 → county FIPS** (HUD residential-ratio crosswalk).
- Distance computed between **county centroid** and **nearest cardiologist ZIP centroid** via k-d tree (great-circle). This deliberately understates variability within large urban ZIPs and is exact enough for the county-level choropleth.
- Counties with 0 cardiologists report the distance to the nearest out-of-county provider.
- RUCC 2023 code ≥ 4 → `rural: true` (per USDA convention).
- All distances in statute miles.
