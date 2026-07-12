# Rural Cardiology Desert Atlas

An open-source county-level planning atlas for exploring geographic access to
adult cardiology services in the United States. Version 0.2.0 covers the exact
3,144-county 2025 Census universe and reports qualifying NPPES-listed provider
supply, distance to the nearest mapped qualifying provider location, Critical
Access Hospitals, primary-care HPSA status, RUCC, population, and CDC PLACES
coronary-heart-disease prevalence.

The Atlas is a research and planning resource developed alongside the
[HEARTLAND Protocol](https://heartlandprotocol.org). It is not a provider
directory, referral tool, clinical decision system, or measure of workforce
availability or quality of care.

Live site: **https://atlas.heartlandprotocol.org**

| | |
|-|-|
| **Author** | Vicky Muller Ferreira, MD ([ORCID 0009-0009-1099-5690](https://orcid.org/0009-0009-1099-5690)) |
| **Version** | 0.2.0 |
| **License** | MIT (code); upstream data retain their source terms |
| **Stack** | Next.js 16, React 19, TypeScript 5, Tailwind CSS 4 |
| **Pipeline** | Python 3.12.12, Node 22.17.0, npm 10.9.2, uv 0.10.4 |
| **Archive** | [Zenodo concept DOI 10.5281/zenodo.19634990](https://doi.org/10.5281/zenodo.19634990) |

## Version 0.2.0 results

- 1,869 of 3,144 counties (59.4%) have zero mapped qualifying provider
  locations.
- 34,233,412 people live in those counties, based on ACS 2024 five-year
  population estimates.
- The median nearest-provider distance among 1,958 evaluable rural counties is
  25.2 miles.
- 909 of 1,372 evaluable Critical Access Hospitals (66.3%) are at least 20
  miles from the nearest mapped qualifying provider location.

These results are version-specific. The machine-readable release manifest
binds the displayed dataset to its county, provider-location, hospital-location,
topology, and summary hashes.

## Data and method

The frozen 0.2.0 build uses:

- CMS NPPES for individual, active, primary adult-cardiology taxonomy records;
- CMS Provider of Services for hospitals and Critical Access Hospital status;
- a preserved Census batch-geocoder snapshot for direct address coordinates;
- the authenticated HUD-USPS ZIP-to-county 2025 Q4 crosswalk for deterministic
  same-state fallback when no accepted direct match exists;
- Census 2025 county Gazetteer/internal points and TIGER-derived topology;
- ACS 2024 five-year population and income estimates;
- CDC PLACES 2025 release (2023 estimate year) coronary-heart-disease
  prevalence;
- USDA RUCC 2023 and a pinned HRSA primary-care HPSA snapshot.

Public counts refer to qualifying NPPES-listed records and mapped locations,
not board-certified cardiologists, unique clinicians available for care,
full-time equivalents, appointment capacity, telehealth coverage, or service
quality. NPPES addresses may be stale or administrative. HUD fallback assigns a
county proxy, not a verified street-level practice location. Distances are
straight-line geographic estimates between controlled coordinates and do not
represent travel time or road distance.

Full methodology and limitations are published on the
[About page](https://atlas.heartlandprotocol.org/about).

## Repository layout

```text
atlas/
├── app/          # Next.js application and version-bound public assets
├── scripts/      # controlled acquisition, validation, and offline build code
├── environment/  # frozen environment controls
├── tooling/      # deterministic topology tooling
├── .zenodo.json
└── CITATION.cff
```

## Verification and local app use

The analytical release is produced from a content-addressed 54-input bundle,
not by downloading mutable “latest” sources. Acquisition and offline build are
separate controlled phases. See [scripts/README.md](scripts/README.md) for the
pipeline contract.

To verify the application after the versioned data assets are present:

```bash
cd app
npm ci --offline --ignore-scripts
npm test -- --run
npm run typecheck
npm run build
```

There is no active automatic monthly data refresh. A future data release must
freeze new inputs, produce a separately versioned build, and pass the same
validation and release controls.

## Citation

Muller Ferreira V. *Rural Cardiology Desert Atlas*. Version 0.2.0. 2026.
[https://atlas.heartlandprotocol.org](https://atlas.heartlandprotocol.org).
Use the immutable version DOI shown in the Zenodo record for the release being
cited; the concept DOI above resolves to the latest archived version.

Related protocol: Muller Ferreira V. *HEARTLAND Protocol: A Tiered Clinical
Implementation Toolkit for Primary Care-Led Heart Failure Management in Rural
and Resource-Limited Settings*. *Cureus*. 2026;18(3):e104817.
doi:10.7759/cureus.104817. Scientific archive v3.3:
doi:10.5281/zenodo.19101219.

## Privacy and use limitations

The Atlas uses public administrative and aggregate sources and contains no
patient health information. It should not be used to identify individual
clinicians, determine referral availability, or infer clinical access or
outcomes for a particular person or facility.
