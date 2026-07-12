# Atlas release-data control

The offline analytical pipeline publishes two byte-identical asset pairs:

- `scripts/data/processed/counties.json` and `app/public/data/counties.json`;
- `scripts/data/processed/summary.json` and `app/public/data/summary.json`.

`counties.json` contains exactly 3,144 sorted county/county-equivalent rows for
the 50 states and District of Columbia. `summary.json` is generated from
unrounded analytical values and binds the exact public county bytes through
`counties_json_sha256`. The app validates both schemas, the hash binding,
zero-provider count/population, evaluable rural count, and facility-level CAH
accounting before rendering. It never reconstructs the authoritative rural
median from one-decimal public distances.

Version 0.2.0 replaces the historical public county asset with the exact outputs
of the network-disabled analytical build. The release includes the corrected
county and summary assets at both controlled pipeline and served-app paths. The
loader fails closed if those exact version-bound assets are absent or drift.

The version-bound release also includes
`county-universe.ids.txt`: all 3,144 Census 2025 Gazetteer FIPS in ascending
ASCII order, one per line with one terminal LF. Its exact SHA-256 is pinned to
`f2651ec6a9375476e3ff09efb4c2a699cd5690ffd3f0c982aeacad967c21c241`.
The server validates that identity and exact set equality before rendering.

The release provides `release-manifest.json` with the
exact byte length and SHA-256 of the county, summary, universe, and two topology
assets. The approved manifest SHA-256 is supplied separately as
`ATLAS_RELEASE_MANIFEST_SHA256`, avoiding a self-referential source-tree hash.
Both `npm run build` and the server loader verify the manifest, schemas, exact
IDs, Connecticut planning-region rules, and all file hashes. Missing assets,
hash drift, or internal control markers forbidden by the preflight stop the
build before Next.js executes.

Display topology is separately pinned under [`topology/`](./topology/README.md)
and is never an analytical input.

See [`data-dictionary.json`](./data-dictionary.json) for the internal
machine-readable field contract. No public app data file contains NPI, provider name,
credential, street address, or facility name.
