"""
02_download_cms_pos.py
======================

Downloads CMS "Hospital General Information" (Provider Data Catalog dataset
`xubh-q36u`) — the authoritative per-hospital list with Hospital Type including
"Critical Access Hospitals".

Output: data/interim/hospitals.parquet
  Columns: ccn, name, address, city, state, zip5, county_name,
           hospital_type, is_cah, ownership, emergency_services
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import requests

from _common import INTERIM_DIR, RAW_DIR, log, pad_zip, record_manifest, download

DATASET_ID = "xubh-q36u"
METASTORE_URL = (
    f"https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/{DATASET_ID}"
)


def find_csv_url() -> str:
    """Resolve the current distribution CSV URL for Hospital General Information."""
    log(f"fetching metastore record: {METASTORE_URL}")
    r = requests.get(METASTORE_URL, timeout=30, headers={"User-Agent": "rural-cardiology-atlas"})
    r.raise_for_status()
    j = r.json()
    # distribution is an array; we want the CSV (identifier field has a URL)
    for dist in j.get("distribution", []):
        url = dist.get("downloadURL") or dist.get("data", {}).get("downloadURL")
        media = (dist.get("mediaType") or dist.get("format") or "").lower()
        if url and ("csv" in media or url.endswith(".csv")):
            return url
    raise RuntimeError("no CSV distribution found in dataset metadata")


def main() -> None:
    url = find_csv_url()
    local = RAW_DIR / "hospital_general_information.csv"
    download(url, local)
    record_manifest("02_download_cms_pos.py", "CMS Hospital General Info", url, local)

    df = pd.read_csv(local, dtype=str)
    log(f"loaded {len(df):,} rows, columns: {list(df.columns)[:8]} ...")

    # Column names in this dataset occasionally drift between releases; be tolerant.
    def col(frame: pd.DataFrame, *candidates: str) -> str | None:
        lower = {c.lower(): c for c in frame.columns}
        for c in candidates:
            if c.lower() in lower:
                return lower[c.lower()]
        return None

    rename_map = {
        col(df, "Facility ID", "CMS Certification Number (CCN)"): "ccn",
        col(df, "Facility Name"): "name",
        col(df, "Address"): "address",
        col(df, "City/Town", "City"): "city",
        col(df, "State"): "state",
        col(df, "ZIP Code", "Zip Code"): "zip5",
        col(df, "County/Parish", "County Name"): "county_name",
        col(df, "Hospital Type"): "hospital_type",
        col(df, "Hospital Ownership"): "ownership",
        col(df, "Emergency Services"): "emergency_services",
    }
    rename_map = {k: v for k, v in rename_map.items() if k is not None}
    df = df.rename(columns=rename_map)[list(rename_map.values())]

    df["zip5"] = df["zip5"].astype(str).map(pad_zip)
    df["is_cah"] = df["hospital_type"].str.contains("Critical Access", case=False, na=False)

    out = INTERIM_DIR / "hospitals.parquet"
    df.to_parquet(out, index=False, compression="zstd")
    log(f"wrote {out} ({len(df):,} hospitals, {df['is_cah'].sum():,} CAHs)")


if __name__ == "__main__":
    main()
