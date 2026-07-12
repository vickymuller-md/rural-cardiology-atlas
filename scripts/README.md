# Rural Cardiology Desert Atlas — controlled pipeline

The 0.2.0 pipeline separates authorized acquisition from a network-denied,
content-addressed analytical build. It does not download mutable “latest”
sources during release verification, and `counties.json` is not produced from a
ZIP=ZCTA shortcut.

All inputs are public administrative or aggregate data. No patient data are
used.

## Frozen toolchain

- Python 3.12.12
- uv 0.10.4
- Node 22.17.0
- npm 10.9.2

Exact versions are recorded in the repository and release receipts. Release
commands fail closed if the approved source tree, bundle, authorization,
receipt chain, or runtime versions differ.

## Sources and roles

| Source | Frozen role in 0.2.0 |
|-|-|
| CMS NPPES | Individual, active records with one of five primary adult-cardiology taxonomies and controlled practice addresses |
| CMS Provider of Services | Hospitals, Hospital Type, Critical Access Hospital status, and facility addresses |
| Census batch geocoder | Preserved direct-match responses for the frozen request set |
| HUD-USPS 2025 Q4 | Authenticated ZIP-to-county ratios for deterministic same-state fallback |
| Census Gazetteer/TIGER 2025 | County universe, internal points, full-resolution QA geography, and derived display topology |
| ACS 2024 five-year | County population and median household income |
| CDC PLACES 2025 release (2023 estimate year) | County coronary-heart-disease prevalence |
| USDA RUCC 2023 | Rural/urban classification |
| HRSA HPSA | Pinned primary-care shortage-area designations |

The exact 54-input release manifest records source IDs, URLs, byte counts,
SHA-256 values, and source snapshot identities.

## Pipeline states

`run_all.py` exposes separate commands for known-hash materialization, NPPES
transport/container inspection, request preparation, PLACES derivation, Census
and HUD acquisition, offline build, validation completion, and clean-room
verification:

```bash
python run_all.py describe
python run_all.py --help
```

Release execution requires explicit authority arguments. A minimal unqualified
`python run_all.py` command is intentionally not a valid release path.

## Main versioned outputs

- canonical provider locations, sorted by `(npi, address_id)`;
- canonical hospital locations, sorted by `(facility_id, address_id)`;
- the exact 3,144-row county analytical/public dataset;
- the authoritative national summary computed from unrounded analytical data;
- Census-derived county and state topology;
- route, waterfall, duplicate, geography, geocoder, sensitivity, validation,
  and build receipts.

The pipeline and app copies of county data, summary data, and topology must be
byte-identical and hash-bound by the release manifest.

## Method boundaries

- Provider supply means qualifying NPPES-listed mapped locations, not verified
  availability, FTEs, appointments, board certification, or telehealth.
- Accepted Census coordinates are used directly. HUD fallback uses a unique
  maximum same-state `BUS_RATIO` candidate and the pinned county internal point.
- Unmatched records never contribute to county counts or distance indexes.
- County nearest-provider distances and CAH facility-to-provider distances use
  the frozen six-decimal canonical coordinates and great-circle distance.
- `chd_prevalence_pct` is CDC PLACES coronary-heart-disease prevalence; it is
  not a mortality rate.

## Tests and determinism

The release gate runs the full Python suite, app tests, typecheck, and production
Next build. Two clean-room builds start from distinct empty output/cache roots,
use isolated package/build caches, deny network access, and must emit identical
complete output manifests. Generated caches such as `node_modules/.vite` are not
approved dependencies.

Manual validation is a later, human-completed gate over the frozen 202-row
stratified sample, including an independently coded 41-row subset. It may
diagnose a failure but cannot edit canonical analytical rows.
