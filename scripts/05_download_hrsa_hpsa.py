"""Phase-2B frozen HRSA primary-care HPSA materialization entrypoint."""

from __future__ import annotations

import json
import sys

from atlas_pipeline.pipeline_cli import materialize_main
from atlas_pipeline.source_registry import SOURCE_POLICIES

SOURCE_IDS = ("HRSA-HPSA-WAREHOUSE-20260710",)


def main() -> None:
    if sys.argv[1:] == ["--describe"]:
        print(json.dumps({source_id: SOURCE_POLICIES[source_id].sha256 for source_id in SOURCE_IDS}, indent=2))
        return
    materialize_main(default_source_ids=SOURCE_IDS)


if __name__ == "__main__":
    main()
