"""Phase-2B frozen Census/USDA/geometry/tool materialization entrypoint."""

from __future__ import annotations

import json
import sys

from atlas_pipeline.pipeline_cli import materialize_main
from atlas_pipeline.source_registry import SOURCE_POLICIES

SOURCE_IDS = (
    "TAXONOMY-260",
    "NPPES-INDEX-20260709",
    "NPPES-HEADER",
    "NPPES-README",
    "GEO-BENCHMARKS",
    "GEO-VINTAGES-8",
    "ACS-POP-2024-5Y",
    "ACS-INCOME-2024-5Y",
    "COUNTY-GAZ-2025",
    "RUCC-2023",
    "CENSUS-CB-COUNTY-2025-5M",
    "CENSUS-CB-STATE-2025-5M",
    "CENSUS-TIGER-COUNTY-2025",
    "CENSUS-COUNTY-ADJ-2025",
    "MAPSHAPER-0.7.41",
)


def main() -> None:
    if sys.argv[1:] == ["--describe"]:
        print(json.dumps({source_id: SOURCE_POLICIES[source_id].sha256 for source_id in SOURCE_IDS}, indent=2))
        return
    materialize_main(default_source_ids=SOURCE_IDS)


if __name__ == "__main__":
    main()
