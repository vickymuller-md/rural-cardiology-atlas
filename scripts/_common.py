"""Shared utilities for the Rural Cardiology Atlas pipeline.

- HTTP download with resume, retry, SHA256 verification.
- FIPS padding / state abbreviation helpers.
- MANIFEST.json append (tracks every fetch: URL, SHA256, fetched_at).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPTS_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
INTERIM_DIR = DATA_DIR / "interim"
PROCESSED_DIR = DATA_DIR / "processed"
MANIFEST_PATH = DATA_DIR / "MANIFEST.json"

for d in (RAW_DIR, INTERIM_DIR, PROCESSED_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP download with retry + resume
# ---------------------------------------------------------------------------
USER_AGENT = (
    "rural-cardiology-atlas/0.1 (Vicky Muller Ferreira, MD; "
    "vickymuller@heartlandprotocol.org)"
)


def download(
    url: str,
    dest: Path,
    *,
    max_retries: int = 5,
    chunk_size: int = 1024 * 256,
    resume: bool = True,
) -> Path:
    """Stream a URL to `dest` with progress bar, retries, and optional resume."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}

    for attempt in range(1, max_retries + 1):
        try:
            mode = "ab" if resume and dest.exists() else "wb"
            existing = dest.stat().st_size if resume and dest.exists() else 0
            if existing:
                headers["Range"] = f"bytes={existing}-"

            with requests.get(url, stream=True, headers=headers, timeout=60) as r:
                if r.status_code == 416:  # range not satisfiable → already complete
                    log(f"already complete: {dest.name}")
                    return dest
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0)) + existing
                with open(dest, mode) as f, tqdm(
                    total=total or None,
                    initial=existing,
                    unit="B",
                    unit_scale=True,
                    desc=dest.name,
                ) as pbar:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        pbar.update(len(chunk))
            return dest
        except (requests.RequestException, OSError) as exc:
            wait = min(30, 2**attempt)
            log(f"attempt {attempt}/{max_retries} failed for {url}: {exc}. Retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"failed to download {url} after {max_retries} attempts")


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------
def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# MANIFEST
# ---------------------------------------------------------------------------
@dataclass
class ManifestEntry:
    script: str
    source: str
    url: str
    local_path: str
    sha256: str
    bytes: int
    fetched_at: str

    def asdict(self) -> dict:
        return self.__dict__


def record_manifest(script: str, source: str, url: str, path: Path) -> None:
    """Append one entry to MANIFEST.json (idempotent by (script, url, sha256))."""
    entry = ManifestEntry(
        script=script,
        source=source,
        url=url,
        local_path=str(path.relative_to(DATA_DIR)),
        sha256=sha256_file(path),
        bytes=path.stat().st_size,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    existing: list[dict] = []
    if MANIFEST_PATH.exists():
        existing = json.loads(MANIFEST_PATH.read_text())
        # dedupe
        existing = [
            e for e in existing
            if not (e["script"] == entry.script and e["url"] == entry.url and e["sha256"] == entry.sha256)
        ]
    existing.append(entry.asdict())
    MANIFEST_PATH.write_text(json.dumps(existing, indent=2))


# ---------------------------------------------------------------------------
# FIPS
# ---------------------------------------------------------------------------
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
    # Territories excluded from the 50-state + DC atlas by default
}

FIPS_TO_STATE = {v: k for k, v in STATE_FIPS.items()}


def pad_fips(value: str | int, width: int = 5) -> str:
    """Zero-pad a county FIPS to 5 digits."""
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(width)


def pad_state(value: str | int) -> str:
    return str(value).strip().zfill(2)


def pad_zip(value: str | int) -> str:
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(5)[:5]


# ---------------------------------------------------------------------------
# Great-circle distance
# ---------------------------------------------------------------------------
def haversine_miles(lat1, lon1, lat2, lon2):
    """Vectorized haversine distance in statute miles. Accepts numpy arrays."""
    import numpy as np

    R = 3958.7613  # Earth radius in miles
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
def require_path(path: Path, hint: str) -> Path:
    if not path.exists():
        log(f"MISSING: {path} — {hint}")
        sys.exit(1)
    return path
