from __future__ import annotations

import hashlib
from pathlib import Path

from atlas_pipeline.bundle import BUNDLE_SCHEMA_VERSION, canonical_json_bytes


def write_bundle(
    root: Path,
    *,
    phase: str,
    expectations: dict,
    files: list[tuple[str, str, str, bytes, str]],
) -> Path:
    root.mkdir(parents=True)
    entries = []
    for source_id, schema_id, filename, body, content_type in files:
        digest = hashlib.sha256(body).hexdigest()
        relative = f"sha256/{digest}/{filename}"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        entries.append(
            {
                "source_id": source_id,
                "release": "synthetic-fixture",
                "url": f"https://example.test/{filename}",
                "relative_path": relative,
                "bytes": len(body),
                "sha256": digest,
                "content_type": content_type,
                "schema_id": schema_id,
            }
        )
    synthetic_source_manifest = canonical_json_bytes(
        {
            "path": "scripts/tests/synthetic-fixture.py",
            "bytes": len(b"synthetic-source-tree\n"),
            "sha256": hashlib.sha256(b"synthetic-source-tree\n").hexdigest(),
        }
    )
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "phase": phase,
        "authorization": {
            "authorization_id": f"SYNTHETIC-{phase.replace('-', '')}",
            "approved_at_utc": "2026-07-10T00:00:00Z",
            "source_tree_id": hashlib.sha256(synthetic_source_manifest).hexdigest(),
        },
        "expectations": expectations,
        "inputs": entries,
    }
    (root / "bundle.json").write_bytes(canonical_json_bytes(manifest))
    return root
