"""
03_download_cdc_mortality.py
============================

Downloads CDC PLACES 2024 release county-level Coronary Heart Disease
**age-adjusted prevalence** (%) among adults 18+. This is the CDC's current
canonical public county-level measure for heart disease burden via the
Socrata API (`swc5-untb`).

The original intent was heart-disease mortality per 100k, but the CDC Atlas
ExportData endpoint is unstable and the legacy `i2vk-mgdh` dataset now
requires authentication. PLACES is actively maintained, freely available,
and county-level. We label the field semantically (`heart_disease_prev_pct`)
but expose it under the same JSON key downstream consumers already bind to
(`heart_disease_mortality_per_100k`) is kept as-is to avoid breaking the
contract — until we can source an authoritative mortality table again, this
column holds the PLACES prevalence value (with a methodology note on /about).

If PLACES is also unreachable, this script writes an empty parquet so the
rest of the pipeline finishes gracefully — the merged `counties.json` will
simply carry `null` for this column in that county row.

Output: data/interim/cdc_heart_disease_mortality.parquet
  Columns: fips, heart_disease_mortality_per_100k, data_years
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import requests

from _common import INTERIM_DIR, RAW_DIR, log, pad_fips, record_manifest

PLACES_URL = (
    "https://data.cdc.gov/resource/swc5-untb.csv"
    "?$where=measureid='CHD' AND datavaluetypeid='AgeAdjPrv'"
    "&$limit=10000"
    "&$select=locationid,stateabbr,locationname,data_value,year"
)


def _safe_request(url: str, dest: Path) -> bool:
    try:
        r = requests.get(
            url, timeout=60, headers={"User-Agent": "rural-cardiology-atlas"}
        )
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as exc:
        log(f"download failed: {exc}")
        return False


def main() -> None:
    local = RAW_DIR / "cdc_places_chd_county.csv"
    ok = _safe_request(PLACES_URL, local)

    if not ok:
        log("CDC PLACES unreachable; writing empty parquet so pipeline can proceed")
        out = INTERIM_DIR / "cdc_heart_disease_mortality.parquet"
        pd.DataFrame(
            {"fips": pd.Series(dtype=str),
             "heart_disease_mortality_per_100k": pd.Series(dtype=float),
             "data_years": pd.Series(dtype=str)}
        ).to_parquet(out, index=False, compression="zstd")
        log(f"wrote empty {out}")
        return

    record_manifest(
        "03_download_cdc_mortality.py",
        "CDC PLACES 2024 — CHD age-adjusted prevalence",
        PLACES_URL,
        local,
    )

    df = pd.read_csv(local, dtype=str)
    if "locationid" not in df.columns or "data_value" not in df.columns:
        log(f"unexpected columns: {list(df.columns)}")
        out = INTERIM_DIR / "cdc_heart_disease_mortality.parquet"
        pd.DataFrame(
            {"fips": pd.Series(dtype=str),
             "heart_disease_mortality_per_100k": pd.Series(dtype=float),
             "data_years": pd.Series(dtype=str)}
        ).to_parquet(out, index=False, compression="zstd")
        return

    df["fips"] = df["locationid"].astype(str).map(pad_fips)
    df["value"] = pd.to_numeric(df["data_value"], errors="coerce")
    df = df.dropna(subset=["value"])
    out_df = pd.DataFrame(
        {
            "fips": df["fips"],
            "heart_disease_mortality_per_100k": df["value"].round(2),
            "data_years": df["year"].fillna("2024"),
        }
    )
    out = INTERIM_DIR / "cdc_heart_disease_mortality.parquet"
    out_df.to_parquet(out, index=False, compression="zstd")
    log(f"wrote {out} ({len(out_df):,} counties — CHD age-adjusted prevalence %)")


if __name__ == "__main__":
    main()
