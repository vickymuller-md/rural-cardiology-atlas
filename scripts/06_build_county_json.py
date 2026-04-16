"""
06_build_county_json.py
=======================

Merges all interim parquet files into a single per-county record set.

Auxiliary downloads (cached in raw/):
  - Census ZCTA→County crosswalk (2020) — maps NPPES ZIP5 to county FIPS.
  - Census 2023 Gazetteer ZCTA centroids — lat/lng per ZCTA (used by step 07).
  - Census 2023 Gazetteer counties — lat/lng per county (used by step 07).

Output: data/interim/counties_merged.parquet  (distance column still empty)

Step 07 fills `miles_to_nearest_cardiologist` and writes the final JSON.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from _common import (
    INTERIM_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    FIPS_TO_STATE,
    STATE_FIPS,
    download,
    log,
    pad_fips,
    pad_zip,
    record_manifest,
)

ZCTA_TO_COUNTY_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
    "tab20_zcta520_county20_natl.txt"
)
ZCTA_GAZ_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/"
    "2023_Gaz_zcta_national.zip"
)
COUNTY_GAZ_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/"
    "2023_Gaz_counties_national.zip"
)


# ---------------------------------------------------------------------------
# Crosswalk: ZCTA5 → county FIPS (weighted by population)
# ---------------------------------------------------------------------------
def load_zcta_to_county() -> pd.DataFrame:
    local = RAW_DIR / "zcta_to_county.txt"
    if not local.exists():
        download(ZCTA_TO_COUNTY_URL, local)
        record_manifest(
            "06_build_county_json.py", "Census ZCTA→County 2020", ZCTA_TO_COUNTY_URL, local
        )
    # Pipe-delimited
    df = pd.read_csv(local, sep="|", dtype=str, low_memory=False)
    # Standardize column names (Census sometimes ships headers with GEOID_ZCTA5_20 etc.)
    zcol = next((c for c in df.columns if "ZCTA5" in c.upper() and "GEOID" in c.upper()), None)
    ccol = next((c for c in df.columns if "COUNTY" in c.upper() and "GEOID" in c.upper()), None)
    pcol = next((c for c in df.columns if "POPPT" in c.upper() or "POP" in c.upper()), None)
    if not zcol or not ccol:
        raise RuntimeError(f"ZCTA↔county columns not found: {list(df.columns)}")
    out = pd.DataFrame({
        "zcta": df[zcol].astype(str).map(pad_zip),
        "fips": df[ccol].astype(str).map(pad_fips),
        "pop_weight": pd.to_numeric(df[pcol], errors="coerce").fillna(0) if pcol else 1.0,
    })
    # Pick the primary county per ZCTA (max population overlap)
    out = out.sort_values(["zcta", "pop_weight"], ascending=[True, False])
    primary = out.drop_duplicates("zcta", keep="first")[["zcta", "fips"]]
    log(f"ZCTA→county primary crosswalk: {len(primary):,} ZCTAs")
    return primary


# ---------------------------------------------------------------------------
# Gazetteer centroids
# ---------------------------------------------------------------------------
def _gaz_txt_from_zip(zip_path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as zf:
        txt_name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
        with zf.open(txt_name) as fp:
            return pd.read_csv(
                io.TextIOWrapper(fp, encoding="latin-1"),
                sep="\t",
                dtype=str,
            )


def load_zcta_centroids() -> pd.DataFrame:
    local = RAW_DIR / "zcta_gaz_2023.zip"
    if not local.exists():
        download(ZCTA_GAZ_URL, local)
        record_manifest(
            "06_build_county_json.py", "Census ZCTA Gazetteer 2023", ZCTA_GAZ_URL, local
        )
    df = _gaz_txt_from_zip(local)
    df.columns = [c.strip() for c in df.columns]
    zcol = next((c for c in df.columns if c.upper().startswith("GEOID")), None)
    latcol = next((c for c in df.columns if "INTPTLAT" in c.upper()), None)
    loncol = next((c for c in df.columns if "INTPTLONG" in c.upper()), None)
    if not (zcol and latcol and loncol):
        raise RuntimeError(f"gazetteer columns missing: {list(df.columns)}")
    out = pd.DataFrame({
        "zcta": df[zcol].astype(str).map(pad_zip),
        "lat": pd.to_numeric(df[latcol], errors="coerce"),
        "lng": pd.to_numeric(df[loncol], errors="coerce"),
    }).dropna()
    log(f"ZCTA centroids: {len(out):,}")
    return out


def load_county_centroids() -> pd.DataFrame:
    local = RAW_DIR / "county_gaz_2023.zip"
    if not local.exists():
        download(COUNTY_GAZ_URL, local)
        record_manifest(
            "06_build_county_json.py", "Census County Gazetteer 2023", COUNTY_GAZ_URL, local
        )
    df = _gaz_txt_from_zip(local)
    df.columns = [c.strip() for c in df.columns]
    fcol = next((c for c in df.columns if c.upper() == "GEOID"), None)
    latcol = next((c for c in df.columns if "INTPTLAT" in c.upper()), None)
    loncol = next((c for c in df.columns if "INTPTLONG" in c.upper()), None)
    if not (fcol and latcol and loncol):
        raise RuntimeError(f"gazetteer counties columns missing: {list(df.columns)}")
    out = pd.DataFrame({
        "fips": df[fcol].astype(str).map(pad_fips),
        "county_lat": pd.to_numeric(df[latcol], errors="coerce"),
        "county_lng": pd.to_numeric(df[loncol], errors="coerce"),
    }).dropna()
    log(f"county centroids: {len(out):,}")
    return out


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------
def aggregate_cardiologists(nppes: pd.DataFrame, zcta_xw: pd.DataFrame) -> pd.DataFrame:
    df = nppes.merge(zcta_xw, left_on="practice_zip5", right_on="zcta", how="inner")
    per_county = df.groupby("fips")["npi"].nunique().rename("n_cardiologists").reset_index()
    log(f"cardiologists: {per_county['n_cardiologists'].sum():,} unique NPIs across {len(per_county):,} counties")
    return per_county


def aggregate_hospitals(hospitals: pd.DataFrame, zcta_xw: pd.DataFrame) -> pd.DataFrame:
    df = hospitals.assign(zip5=hospitals["zip5"].astype(str).map(pad_zip))
    df = df.merge(zcta_xw, left_on="zip5", right_on="zcta", how="left")
    g = df.groupby("fips").agg(
        n_hospitals=("ccn", "count"),
        n_cah=("is_cah", "sum"),
    ).reset_index()
    g["n_cah"] = g["n_cah"].astype(int)
    return g


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log("loading interim parquet files")
    nppes = pd.read_parquet(INTERIM_DIR / "nppes_cardio.parquet")
    hospitals = pd.read_parquet(INTERIM_DIR / "hospitals.parquet")
    cdc = pd.read_parquet(INTERIM_DIR / "cdc_heart_disease_mortality.parquet")
    census = pd.read_parquet(INTERIM_DIR / "census_counties.parquet")
    rucc = pd.read_parquet(INTERIM_DIR / "rucc_2023.parquet")
    hpsa = pd.read_parquet(INTERIM_DIR / "hrsa_hpsa_primary_care.parquet")

    zcta_xw = load_zcta_to_county()
    zcta_pts = load_zcta_centroids()
    county_pts = load_county_centroids()

    cardio_counts = aggregate_cardiologists(nppes, zcta_xw)
    hosp_counts = aggregate_hospitals(hospitals, zcta_xw)

    # Limit to 50 states + DC
    allowed = set(STATE_FIPS.values())
    base = census[census["fips"].str.startswith(tuple(allowed))].copy()
    # derive state abbrev from FIPS prefix
    base["state"] = base["fips"].str[:2].map(FIPS_TO_STATE)
    base = base.rename(columns={"county_name": "county"})

    merged = (
        base.merge(rucc, on="fips", how="left")
        .merge(cardio_counts, on="fips", how="left")
        .merge(hosp_counts, on="fips", how="left")
        .merge(hpsa, on="fips", how="left")
        .merge(cdc[["fips", "heart_disease_mortality_per_100k"]], on="fips", how="left")
        .merge(county_pts, on="fips", how="left")
    )

    merged["n_cardiologists"] = merged["n_cardiologists"].fillna(0).astype(int)
    merged["n_hospitals"] = merged["n_hospitals"].fillna(0).astype(int)
    merged["n_cah"] = merged["n_cah"].fillna(0).astype(int)
    merged["hpsa_primary_care"] = merged["hpsa_primary_care"].fillna(False).astype(bool)
    merged["rural"] = merged["rural"].fillna(False).astype(bool)
    merged["rucc_2023"] = merged["rucc_2023"].astype("Int64")
    merged["population"] = merged["population"].astype("Int64")
    merged["median_hh_income"] = merged["median_hh_income"].astype("Int64")
    merged["heart_disease_mortality_per_100k"] = merged["heart_disease_mortality_per_100k"].round(1)

    out = INTERIM_DIR / "counties_merged.parquet"
    merged.to_parquet(out, index=False, compression="zstd")
    log(f"wrote {out} ({len(merged):,} counties — awaiting distance fill in step 07)")

    # Also persist a helper lookup for step 07
    zcta_pts.to_parquet(INTERIM_DIR / "zcta_centroids.parquet", index=False, compression="zstd")
    nppes.merge(zcta_xw, left_on="practice_zip5", right_on="zcta", how="inner")[
        ["npi", "fips", "practice_zip5"]
    ].to_parquet(INTERIM_DIR / "cardio_by_fips.parquet", index=False, compression="zstd")


if __name__ == "__main__":
    main()
