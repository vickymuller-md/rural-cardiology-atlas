"""
07_compute_distances.py
=======================

Computes `miles_to_nearest_cardiologist` per county and writes the final
`counties.json` consumed by the Next.js app.

Method:
  - Cardiologist positions: ZCTA centroids of their practice ZIP5s (one point per
    unique NPI×ZIP — a single specialist is counted once per practice location).
  - County reference: TIGER gazetteer county centroid (internal point).
  - Distance: great-circle haversine via scipy k-d tree over 3D unit vectors.

For each county, store the distance to the nearest cardiologist regardless of
whether the cardiologist's ZCTA falls inside that county. This means a county
with 0 cardiologists still reports a distance to the nearest provider in a
neighboring county.

Output:
  data/processed/counties.json
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from _common import INTERIM_DIR, PROCESSED_DIR, log, pad_zip

EARTH_RADIUS_MI = 3958.7613


def to_cartesian(lat: np.ndarray, lng: np.ndarray) -> np.ndarray:
    lat_r = np.radians(lat)
    lng_r = np.radians(lng)
    return np.column_stack(
        (
            np.cos(lat_r) * np.cos(lng_r),
            np.cos(lat_r) * np.sin(lng_r),
            np.sin(lat_r),
        )
    )


def chord_to_arc_miles(chord: np.ndarray) -> np.ndarray:
    # For unit-sphere k-d tree distances (chord length), convert to arc length.
    chord = np.clip(chord, 0, 2)
    return EARTH_RADIUS_MI * 2 * np.arcsin(chord / 2)


def main() -> None:
    counties = pd.read_parquet(INTERIM_DIR / "counties_merged.parquet")
    zcta_pts = pd.read_parquet(INTERIM_DIR / "zcta_centroids.parquet")
    nppes = pd.read_parquet(INTERIM_DIR / "nppes_cardio.parquet")

    # Provider positions: dedupe (npi, zip) then join to ZCTA centroid
    nppes = nppes.assign(practice_zip5=nppes["practice_zip5"].astype(str).map(pad_zip))
    unique_provider_locs = nppes[["npi", "practice_zip5"]].drop_duplicates()
    provider_pts = unique_provider_locs.merge(
        zcta_pts.rename(columns={"zcta": "practice_zip5"}), on="practice_zip5", how="inner"
    ).dropna(subset=["lat", "lng"])

    if provider_pts.empty:
        raise RuntimeError("no provider coordinates resolved — check ZCTA crosswalk")

    log(f"provider locations geocoded: {len(provider_pts):,}")
    provider_xyz = to_cartesian(provider_pts["lat"].values, provider_pts["lng"].values)
    tree = cKDTree(provider_xyz)

    counties = counties.dropna(subset=["county_lat", "county_lng"]).copy()
    county_xyz = to_cartesian(counties["county_lat"].values, counties["county_lng"].values)
    chord, _ = tree.query(county_xyz, k=1)
    counties["miles_to_nearest_cardiologist"] = np.round(chord_to_arc_miles(chord), 1)

    # Drop lat/lng helpers from the public JSON
    cols = [
        "fips",
        "state",
        "county",
        "population",
        "median_hh_income",
        "rucc_2023",
        "rural",
        "n_cardiologists",
        "miles_to_nearest_cardiologist",
        "n_hospitals",
        "n_cah",
        "hpsa_primary_care",
        "heart_disease_mortality_per_100k",
    ]
    out = counties[cols].sort_values("fips").reset_index(drop=True)

    # Serialize to JSON with nullable ints → plain numbers or null
    def clean(v):
        if pd.isna(v):
            return None
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            return float(v)
        if isinstance(v, np.bool_):
            return bool(v)
        return v

    records = [
        {k: clean(row[k]) for k in cols} for row in out.to_dict(orient="records")
    ]
    dest = PROCESSED_DIR / "counties.json"
    dest.write_text(json.dumps(records, separators=(",", ":")))
    log(f"wrote {dest} ({len(records):,} counties, {dest.stat().st_size / 1e6:.2f} MB)")

    # Quick sanity summary
    zero = sum(1 for r in records if r["n_cardiologists"] == 0)
    median_rural = pd.Series(
        [r["miles_to_nearest_cardiologist"] for r in records if r["rural"]]
    ).median()
    log(
        f"  {zero:,} counties with zero cardiologists "
        f"({zero / len(records):.1%}); median distance in rural counties: {median_rural:.1f} mi"
    )


if __name__ == "__main__":
    main()
