"""
01_download_nppes.py
====================

Downloads the latest monthly NPPES Data Dissemination ZIP from CMS and
extracts only the cardiology practitioners we care about.

Output: data/interim/nppes_cardio.parquet
  Columns: npi, last_name, first_name, credential, taxonomy,
           practice_zip5, practice_state, last_updated

The monthly zip (~7GB) is streamed from the archive without full extraction.
We read `npidata_pfile_*.csv` in chunks, keep only rows whose Primary
Healthcare Provider Taxonomy matches one of the cardiology codes below,
and discard the zip afterwards (set KEEP_ZIP=1 to retain).

Run:
    uv run python 01_download_nppes.py
"""
from __future__ import annotations

import os
import re
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from _common import (
    INTERIM_DIR,
    RAW_DIR,
    download,
    log,
    pad_zip,
    record_manifest,
)

# ---------------------------------------------------------------------------
# Cardiology taxonomy codes (NUCC Healthcare Provider Taxonomy, v25.0+)
# ---------------------------------------------------------------------------
CARDIO_TAXONOMIES = {
    "207RC0000X": "Cardiovascular Disease",
    "207RI0011X": "Interventional Cardiology",
    "207RC0001X": "Clinical Cardiac Electrophysiology",
    "207RA0201X": "Advanced Heart Failure and Transplant Cardiology",
    "207RR0500X": "Adult Congenital Heart Disease",
    "2086S0102X": "Cardiothoracic Surgery (Surgery)",
    "208G00000X": "Thoracic Surgery (Cardiothoracic Vascular)",
}

NPI_HTML_INDEX = "https://download.cms.gov/nppes/NPI_Files.html"


def discover_latest_url() -> str:
    """Scrape NPI_Files.html for the latest monthly dump URL.

    CMS URL patterns observed:
      ./NPPES_Data_Dissemination_April_2026_V2.zip   (monthly full dump, ~1GB)
      ./NPPES_Data_Dissemination_040626_041226_Weekly_V2.zip (weekly incremental)

    We want the monthly full dump and ignore weekly files.
    """
    log(f"fetching index page: {NPI_HTML_INDEX}")
    r = requests.get(NPI_HTML_INDEX, timeout=30, headers={"User-Agent": "rural-cardiology-atlas"})
    r.raise_for_status()
    html = r.text

    # Monthly dump: Dissemination_<Month>_<Year>(_V<N>)?.zip — NO "Weekly" or date-range
    pattern = re.compile(
        r'href=[\'"]([^\'"]*NPPES_Data_Dissemination_[A-Za-z]+_\d{4}(?:_V\d+)?\.zip)[\'"]'
    )
    matches = pattern.findall(html)
    matches = [m for m in matches if "Weekly" not in m]
    if not matches:
        raise RuntimeError(
            "no NPPES monthly dump link found in index HTML — set NPPES_URL explicitly"
        )

    import calendar

    month_name_to_num = {name: i for i, name in enumerate(calendar.month_name) if name}

    def sort_key(href: str) -> tuple[int, int, int]:
        m2 = re.search(r"_([A-Za-z]+)_(\d{4})(?:_V(\d+))?\.zip$", href)
        if not m2:
            return (0, 0, 0)
        month_num = month_name_to_num.get(m2.group(1), 0)
        version = int(m2.group(3)) if m2.group(3) else 0
        return (int(m2.group(2)), month_num, version)

    latest = sorted(set(matches), key=sort_key)[-1]
    log(f"latest monthly dump on page: {latest}")

    if latest.startswith("http"):
        return latest
    # Strip leading "./" or "/" for clean join
    return "https://download.cms.gov/nppes/" + latest.lstrip("./").lstrip("/")


def find_main_csv(zf: zipfile.ZipFile) -> zipfile.ZipInfo:
    """NPPES data zip contains several files. We want the biggest
    npidata_pfile_*.csv (the `*_FileHeader.csv` sibling is tiny and must be skipped)."""
    candidates = [
        info for info in zf.infolist()
        if info.filename.lower().endswith(".csv")
        and "npidata_pfile" in info.filename.lower()
        and "fileheader" not in info.filename.lower()
    ]
    if not candidates:
        raise RuntimeError("npidata_pfile_*.csv not found inside NPPES zip")
    return max(candidates, key=lambda i: i.file_size)


# NPPES column names are stable across monthly dumps. Only request what we need.
WANTED_COLUMNS = [
    "NPI",
    "Entity Type Code",
    "Provider Last Name (Legal Name)",
    "Provider First Name",
    "Provider Credential Text",
    "Provider Business Practice Location Address Postal Code",
    "Provider Business Practice Location Address State Name",
    "Last Update Date",
    # Taxonomy columns: 15 pairs of (code, primary-flag)
    *[f"Healthcare Provider Taxonomy Code_{i}" for i in range(1, 16)],
    *[f"Healthcare Provider Primary Taxonomy Switch_{i}" for i in range(1, 16)],
]

OUTPUT_COLUMNS = [
    "npi",
    "last_name",
    "first_name",
    "credential",
    "taxonomy",
    "practice_zip5",
    "practice_state",
    "last_updated",
]


def pick_cardio_taxonomy(row: pd.Series) -> str | None:
    """Return the first cardiology taxonomy on the record (prefer primary)."""
    # Prefer primary-flagged taxonomy if it matches cardio
    for i in range(1, 16):
        code = row.get(f"Healthcare Provider Taxonomy Code_{i}")
        primary = str(row.get(f"Healthcare Provider Primary Taxonomy Switch_{i}", "")).upper()
        if isinstance(code, str) and code in CARDIO_TAXONOMIES and primary == "Y":
            return code
    # Otherwise first cardio match anywhere
    for i in range(1, 16):
        code = row.get(f"Healthcare Provider Taxonomy Code_{i}")
        if isinstance(code, str) and code in CARDIO_TAXONOMIES:
            return code
    return None


def process_csv(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> pd.DataFrame:
    log(f"streaming {info.filename} ({info.file_size / 1e9:.2f} GB) from zip")
    chunks: list[pd.DataFrame] = []
    total_rows = 0
    cardio_rows = 0

    with zf.open(info) as fp:
        reader = pd.read_csv(
            fp,
            usecols=lambda c: c in WANTED_COLUMNS,
            dtype=str,
            chunksize=500_000,
            low_memory=False,
        )
        for chunk in reader:
            total_rows += len(chunk)
            # Entity Type 1 = Individual provider
            chunk = chunk[chunk["Entity Type Code"] == "1"]
            if chunk.empty:
                continue
            # Apply taxonomy filter
            chunk["taxonomy"] = chunk.apply(pick_cardio_taxonomy, axis=1)
            chunk = chunk[chunk["taxonomy"].notna()].copy()
            if chunk.empty:
                continue
            cardio_rows += len(chunk)
            chunk.rename(
                columns={
                    "NPI": "npi",
                    "Provider Last Name (Legal Name)": "last_name",
                    "Provider First Name": "first_name",
                    "Provider Credential Text": "credential",
                    "Provider Business Practice Location Address Postal Code": "practice_zip5",
                    "Provider Business Practice Location Address State Name": "practice_state",
                    "Last Update Date": "last_updated",
                },
                inplace=True,
            )
            chunk["practice_zip5"] = chunk["practice_zip5"].astype(str).map(pad_zip)
            chunks.append(chunk[OUTPUT_COLUMNS])
            log(f"  scanned {total_rows:,} rows, kept {cardio_rows:,} cardiology rows")

    log(f"done scanning {total_rows:,} provider rows → {cardio_rows:,} cardiology rows")
    if not chunks:
        raise RuntimeError("0 cardiology rows matched — taxonomy codes likely out of date")
    return pd.concat(chunks, ignore_index=True)


def main() -> None:
    url = os.environ.get("NPPES_URL") or discover_latest_url()
    zip_path = RAW_DIR / Path(url).name

    if not zip_path.exists():
        log(f"downloading {url}")
        download(url, zip_path)
    else:
        log(f"reusing existing {zip_path.name}")

    record_manifest("01_download_nppes.py", "NPPES", url, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        info = find_main_csv(zf)
        df = process_csv(zf, info)

    out = INTERIM_DIR / "nppes_cardio.parquet"
    df.to_parquet(out, index=False, compression="zstd")
    log(f"wrote {out} ({len(df):,} rows, {out.stat().st_size / 1e6:.1f} MB)")

    if not os.environ.get("KEEP_ZIP"):
        try:
            zip_path.unlink()
            log(f"removed raw zip to save disk ({zip_path.name}). Set KEEP_ZIP=1 to keep.")
        except OSError:
            pass


if __name__ == "__main__":
    main()
