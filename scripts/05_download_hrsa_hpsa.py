"""
05_download_hrsa_hpsa.py
========================

Downloads HRSA HPSA (Health Professional Shortage Area) designations.
We want two views:

  1. Geographic / single-county primary-care HPSAs by county FIPS.
  2. (Future) Cardiology-specific HPSAs if/when HRSA adds that discipline.

The HPSA Data Warehouse exposes bulk CSV downloads. The "HPSA Find" export
`BCD_HPSA_FCT_DET_PC.csv` lists all primary-care HPSA designations.

Output: data/interim/hrsa_hpsa_primary_care.parquet
  Columns: fips, hpsa_primary_care
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from _common import INTERIM_DIR, RAW_DIR, log, pad_fips, record_manifest, download

# HRSA HPSA "Facility Detail — Primary Care" bulk download (zip of CSVs).
# HRSA rotates filenames per month; use the stable "latest" alias.
HPSA_URL = (
    "https://data.hrsa.gov/DataDownload/DD_Files/BCD_HPSA_FCT_DET_PC.csv"
)


def main() -> None:
    dest = RAW_DIR / "hpsa_primary_care.csv"
    download(HPSA_URL, dest)
    record_manifest("05_download_hrsa_hpsa.py", "HRSA HPSA Primary Care", HPSA_URL, dest)

    df = pd.read_csv(dest, dtype=str, low_memory=False, on_bad_lines="skip")

    lower = {c.lower(): c for c in df.columns}
    c_fips = (
        lower.get("common state county fips code")
        or lower.get("county or county equivalent federal information processing standard code")
        or lower.get("common county fips code")
        or lower.get("common_county_fips_code")
        or lower.get("county fips code")
        or lower.get("fips county code")
    )
    c_status = lower.get("hpsa status") or lower.get("hpsa_status")

    if c_fips is None:
        raise RuntimeError(
            f"county FIPS column not recognized in HRSA CSV; saw {list(df.columns)[:15]}"
        )

    if c_status is not None:
        df = df[df[c_status].str.strip().str.lower().isin(["designated", "proposed for withdrawal"])]

    fips_set = set(df[c_fips].astype(str).map(pad_fips).dropna())
    log(f"distinct primary-care HPSA counties: {len(fips_set):,}")

    out_df = pd.DataFrame({"fips": sorted(fips_set), "hpsa_primary_care": True})
    out = INTERIM_DIR / "hrsa_hpsa_primary_care.parquet"
    out_df.to_parquet(out, index=False, compression="zstd")
    log(f"wrote {out}")


if __name__ == "__main__":
    main()
