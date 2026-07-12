"""Atlas V19 frozen phase orchestrator; acquisition and offline build stay separate."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from atlas_pipeline.build import V19_RELEASE_EXPECTATIONS
from atlas_pipeline.pipeline_cli import main as pipeline_main


def main() -> None:
    arguments = sys.argv[1:]
    if arguments == ["describe"]:
        print(json.dumps({
            "source_tree": "externally approved canonical manifest required for every phase",
            "phase_2b": "known-hash manifest materialization with immutable validators",
            "phase_2c": "single identity HTTP 200 NPPES transport; stop unopened",
            "phase_2d_i": "central manifest, external approval, then streaming CRC",
            "phase_2d_ii": "prepare requests and derive county-only PLACES snapshot",
            "phase_2e": "authorized Census and authenticated HUD acquisition",
            "phase_2f": "network-denied build --offline with typed chain receipts",
            "county_count": V19_RELEASE_EXPECTATIONS.county_count,
        }, indent=2))
        return
    if arguments == ["test"]:
        scripts_dir = Path(__file__).resolve().parent
        suite = unittest.defaultTestLoader.discover(
            str(scripts_dir / "tests"), top_level_dir=str(scripts_dir)
        )
        if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
            raise SystemExit(1)
        return
    pipeline_main(arguments)


if __name__ == "__main__":
    main()
