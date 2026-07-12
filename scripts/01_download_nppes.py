"""Phase-2C NPPES single-200 streaming transport entrypoint."""

from __future__ import annotations

import json
import sys

from atlas_pipeline.contracts import ADULT_CARDIOLOGY_TAXONOMIES, NPPES_PINNED_URL
from atlas_pipeline.pipeline_cli import acquire_nppes_main


def main() -> None:
    if sys.argv[1:] == ["--describe"]:
        print(json.dumps({
            "source": "NPPES-202606-V2",
            "url": NPPES_PINNED_URL,
            "bytes": 1_137_408_535,
            "taxonomies": list(ADULT_CARDIOLOGY_TAXONOMIES),
            "phase": "2C",
            "transport": "single non-resumable identity HTTP 200; stop unopened",
        }, indent=2))
        return
    acquire_nppes_main()


if __name__ == "__main__":
    main()
