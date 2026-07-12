"""Phase-2D-II derivation of the frozen county-only PLACES snapshot."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .bundle import FrozenBundle
from .control_receipts import ReceiptContext, validate_control_receipt
from .errors import IntegrityError
from .materialization import _atomic_write
from .geoqa import sorted_fips_bytes
from .places import canonical_places_snapshot_bytes, canonicalize_places, parse_places_csv
from .strict_json import canonical_json_bytes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def derive_places_county(
    bundle: FrozenBundle,
    output_dir: Path,
    *,
    completed_at_utc: str | None = None,
) -> Path:
    if bundle.phase != "2D-II":
        raise IntegrityError("derive-places-county requires a Phase-2D-II bundle")
    source_id = bundle.expectation("places_raw_source", str)
    acquisition_receipt_id = bundle.expectation(
        "places_source_acquisition_receipt", str
    )
    universe_value = bundle.expectation("county_universe", list)
    if (
        not isinstance(source_id, str)
        or source_id not in bundle.inputs
        or not isinstance(acquisition_receipt_id, str)
        or acquisition_receipt_id not in bundle.inputs
        or bundle.inputs[source_id].schema_id != "places_raw_csv"
        or len(universe_value) != 3_144
        or universe_value != sorted(universe_value)
        or len(universe_value) != len(set(universe_value))
        or any(
            not isinstance(fips, str)
            or len(fips) != 5
            or not fips.isascii()
            or not fips.isdigit()
            for fips in universe_value
        )
    ):
        raise IntegrityError("PLACES derivation source/universe is incomplete")
    bundle.validate_registered_schema(source_id)
    bundle.validate_registered_schema(acquisition_receipt_id)
    source_path = bundle.verify_file(source_id)
    raw = source_path.read_bytes()
    parsed = parse_places_csv(raw)
    rows = canonicalize_places(
        raw,
        set(universe_value),
        expected_raw_sha256=bundle.inputs[source_id].sha256,
        expected_county_rows=2_957,
        expected_nonnull=2_956,
        expected_suppressed_fips="48301",
        expected_public_nulls=188,
    )
    county_body = canonical_places_snapshot_bytes(rows)
    if not county_body:
        raise IntegrityError("PLACES county-only snapshot is empty")
    output_dir.mkdir(parents=True, exist_ok=False)
    snapshot_path = output_dir / "places_chd_county_2025.jsonl"
    _atomic_write(snapshot_path, county_body)
    national_count = sum(
        (row.get("locationid") or "").strip() == "59" for row in parsed
    )
    suppressed_count = sum(
        row.chd_age_adjusted_prevalence_pct is None for row in rows
    )
    universe_body = sorted_fips_bytes(universe_value)
    source_acquisition_receipt_sha256 = hashlib.sha256(
        bundle.verify_file(acquisition_receipt_id).read_bytes()
    ).hexdigest()
    derivation = {
        "schema_version": "atlas-v19-places-derivation-handoff-1",
        "source_tree_id": bundle.source_tree_id,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "raw_source_id": source_id,
        "raw_source_bytes": len(raw),
        "raw_source_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_total_rows": len(parsed),
        "national_rows_excluded": national_count,
        "county_rows": len(rows),
        "nonnull_rows": sum(
            row.chd_age_adjusted_prevalence_pct is not None for row in rows
        ),
        "suppressed_rows": suppressed_count,
        "suppressed_fips": "48301",
        "county_universe_sha256": hashlib.sha256(universe_body).hexdigest(),
        "canonical_bytes": len(county_body),
        "canonical_sha256": hashlib.sha256(county_body).hexdigest(),
    }
    handoff_path = output_dir / "places_derivation_handoff.json"
    _atomic_write(handoff_path, canonical_json_bytes(derivation))
    receipt = {
        "schema_version": "atlas-v19-places-handoff-receipt-1",
        "receipt_id": "places_handoff",
        "status": "complete",
        "source_tree_id": bundle.source_tree_id,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "authorization_id": bundle.authorization_id,
        "command": "derive-places-county",
        "completed_at_utc": completed_at_utc or _now(),
        "evidence": {
            "phase2dii_bundle_manifest_sha256": bundle.manifest_sha256,
            "source_acquisition_receipt_sha256": source_acquisition_receipt_sha256,
            "raw_source_sha256": hashlib.sha256(raw).hexdigest(),
            "canonical_table_sha256": hashlib.sha256(county_body).hexdigest(),
            "county_universe_sha256": hashlib.sha256(universe_body).hexdigest(),
            "raw_county_rows": len(rows),
            "retained_rows": sum(
                row.chd_age_adjusted_prevalence_pct is not None for row in rows
            ),
            "suppressed_rows": suppressed_count,
            "national_rows_excluded": national_count,
        },
    }
    receipt_body = canonical_json_bytes(receipt)
    validate_control_receipt(
        "places_handoff",
        receipt_body,
        context=ReceiptContext(
            bundle.source_tree_id, bundle.manifest_sha256, bundle.authorization_id
        ),
    )
    receipt_path = output_dir / "receipt.json"
    _atomic_write(receipt_path, receipt_body)
    return receipt_path
