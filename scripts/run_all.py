"""
run_all.py
==========

Runs the full pipeline in order. Use `uv run python run_all.py`.

Each step can be re-run individually if a single stage fails. Raw downloads
are cached under data/raw/; interim parquet files under data/interim/.
"""
from __future__ import annotations

import importlib
import subprocess
import sys

STEPS = [
    "01_download_nppes",
    "02_download_cms_pos",
    "03_download_cdc_mortality",
    "04_download_census",
    "05_download_hrsa_hpsa",
    "06_build_county_json",
    "07_compute_distances",
]


def main() -> None:
    for step in STEPS:
        print(f"\n========== {step} ==========", flush=True)
        # Import as a module so we honor each script's __main__ guard via runpy.
        import runpy

        runpy.run_module(step, run_name="__main__")


if __name__ == "__main__":
    main()
