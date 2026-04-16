"""
04_download_census.py
=====================

Pulls two inputs:
  1. US Census ACS 5-year county estimates (population + median household income).
  2. USDA ERS Rural-Urban Continuum Codes 2023 (RUCC).

Census API is public and does not require a key for light use, but respects one
if provided via CENSUS_API_KEY env var.

Outputs:
  data/interim/census_counties.parquet  (fips, state, county, population, median_hh_income)
  data/interim/rucc_2023.parquet        (fips, rucc_2023, rural)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import requests

from _common import INTERIM_DIR, RAW_DIR, log, pad_fips, record_manifest, download

ACS_YEAR = 2022
ACS_URL = (
    f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"
    "?get=NAME,B01003_001E,B19013_001E"  # total population, median household income
    "&for=county:*"
)

RUCC_URL = "https://ers.usda.gov/sites/default/files/_laserfiche/DataFiles/53251/Ruralurbancontinuumcodes2023.csv"
RUCC_FALLBACK_URL = (
    "https://ers.usda.gov/webdocs/DataFiles/53251/Ruralurbancontinuumcodes2023.csv"
)


def fetch_acs_json(dest: Path) -> list[list[str]]:
    """Fetch the ACS JSON once, with generous timeout and retry."""
    key = os.environ.get("CENSUS_API_KEY")
    url = ACS_URL + (f"&key={key}" if key else "")
    for attempt in range(1, 6):
        try:
            log(f"fetching Census ACS (attempt {attempt}): {ACS_URL}")
            r = requests.get(
                url,
                timeout=(15, 300),  # connect=15s, read=300s — census is slow
                headers={"User-Agent": "rural-cardiology-atlas"},
            )
            r.raise_for_status()
            dest.write_bytes(r.content)
            return r.json()
        except Exception as exc:
            wait = min(30, 2**attempt)
            log(f"  ACS fetch failed: {exc}; retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("Census ACS API unreachable after 5 attempts")


def build_acs_frame(rows: list[list[str]]) -> pd.DataFrame:
    header, *data = rows
    df = pd.DataFrame(data, columns=header)
    df["fips"] = (df["state"].astype(str).str.zfill(2) + df["county"].astype(str).str.zfill(3)).map(pad_fips)
    parts = df["NAME"].str.split(", ", n=1, expand=True)
    df["county_name"] = parts[0].str.replace(r"\s+(County|Parish|Borough|Municipality|Census Area|City and Borough|Municipio)$", "", regex=True)
    df["state_name"] = parts[1]
    df["population"] = pd.to_numeric(df["B01003_001E"], errors="coerce")
    df["median_hh_income"] = pd.to_numeric(df["B19013_001E"], errors="coerce")
    return df[["fips", "state_name", "county_name", "population", "median_hh_income"]]


def fetch_rucc() -> pd.DataFrame:
    dest = RAW_DIR / "rucc_2023.csv"
    try:
        download(RUCC_URL, dest)
        used = RUCC_URL
    except Exception:
        log("primary RUCC URL failed, trying fallback")
        download(RUCC_FALLBACK_URL, dest)
        used = RUCC_FALLBACK_URL
    record_manifest("04_download_census.py", "USDA ERS RUCC 2023", used, dest)
    df = pd.read_csv(dest, dtype=str, encoding="latin-1", on_bad_lines="skip")
    lower = {c.lower(): c for c in df.columns}
    c_fips = lower.get("fips") or lower.get("fips_code") or lower.get("fips_2023") or lower.get("fips code")

    if "Attribute" in df.columns and "Value" in df.columns:
        # Long format: rows with (FIPS, Attribute='RUCC_2023', Value='<code>')
        rucc_rows = df[df["Attribute"] == "RUCC_2023"]
        out = pd.DataFrame(
            {
                "fips": rucc_rows[c_fips].astype(str).map(pad_fips),
                "rucc_2023": pd.to_numeric(rucc_rows["Value"], errors="coerce").astype("Int64"),
            }
        ).dropna()
    else:
        c_rucc = lower.get("rucc_2023") or lower.get("rucc2023") or lower.get("rucc")
        if c_fips is None or c_rucc is None:
            raise RuntimeError(f"RUCC columns not recognized: {list(df.columns)}")
        out = pd.DataFrame(
            {
                "fips": df[c_fips].astype(str).map(pad_fips),
                "rucc_2023": pd.to_numeric(df[c_rucc], errors="coerce").astype("Int64"),
            }
        ).dropna()

    out["rural"] = out["rucc_2023"] >= 4
    return out


def main() -> None:
    # ACS — single fetch, cached to raw/
    raw_path = RAW_DIR / "census_acs_counties.json"
    rows = fetch_acs_json(raw_path)
    record_manifest("04_download_census.py", "US Census ACS 5y", ACS_URL, raw_path)
    acs = build_acs_frame(rows)
    out_acs = INTERIM_DIR / "census_counties.parquet"
    acs.to_parquet(out_acs, index=False, compression="zstd")
    log(f"wrote {out_acs} ({len(acs):,} counties)")

    # RUCC
    rucc = fetch_rucc()
    out_rucc = INTERIM_DIR / "rucc_2023.parquet"
    rucc.to_parquet(out_rucc, index=False, compression="zstd")
    log(f"wrote {out_rucc} ({len(rucc):,} counties, {rucc['rural'].sum():,} rural)")


if __name__ == "__main__":
    main()
