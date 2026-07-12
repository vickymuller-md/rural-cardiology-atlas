"""Authenticated HUD 2025-Q4 acquisition/import and canonical handoff."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from xml.etree import ElementTree
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping

from .acquisition import (
    _run_artifact_validators,
    open_verified_zip_member,
    sha256_file,
    verify_zip_after_approval,
)
from .contracts import STATE_FIPS
from .control_receipts import ReceiptContext, validate_control_receipt
from .errors import ContractError, IntegrityError
from .hud import parse_candidates
from .geoqa import sorted_fips_bytes
from .materialization import (
    MaterializationManifest,
    _atomic_write,
    validate_stream_acquisition_receipt,
)
from .source_registry import (
    HUD_API_HEADERS,
    HUD_HEADERS,
    ReleaseValidationContext,
    resolve_release_validators,
)
from .strict_json import canonical_json_bytes, require_canonical_json, validate_safe_id

HUD_HANDOFF_SCHEMA_VERSION = "atlas-v19-hud-handoff-1"
HUD_COMPATIBILITY_SCHEMA_VERSION = "atlas-v19-hud-compatibility-1"
HUD_API_HANDOFF_SCHEMA_VERSION = "atlas-v19-hud-api-handoff-1"
HUD_API_COMPATIBILITY_SCHEMA_VERSION = "atlas-v19-hud-api-compatibility-1"
HUD_API_SOURCE_ID = "HUD-ZIP-COUNTY-API-2025Q4"
HUD_API_RESULT_FIELDS = frozenset(
    {
        "zip", "geoid", "bus_ratio", "res_ratio", "tot_ratio",
        "oth_ratio", "city", "state",
    }
)
HUD_API_REQUEST_PARAMETERS = {
    "type": 2,
    "query": "All",
    "year": 2025,
    "quarter": 4,
}
HUD_API_ACQUISITION_RECEIPT_SHA256 = (
    "013d18d4019534e28bb2306d0d0fd1afce6b9b911e8a37da7d0489405a2d6782"
)
TERRITORY_PREFIX_STATE = {
    "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}
SPECIAL_NONCOUNTY = {
    ("60", "AS"), ("64", "FM"), ("68", "MH"), ("70", "PW"),
}
CURRENT_CT_PLANNING_REGIONS = frozenset(
    {"09110", "09120", "09130", "09140", "09150", "09160", "09170", "09180", "09190"}
)
OBSOLETE_CT_COUNTIES = frozenset(
    {"09001", "09003", "09005", "09007", "09009", "09011", "09013", "09015"}
)


def _strict_decimal_json(body: bytes) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise IntegrityError(f"duplicate HUD API JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise IntegrityError(f"non-finite HUD API JSON number is forbidden: {value}")

    try:
        return json.loads(
            body.decode("utf-8"),
            parse_int=Decimal,
            parse_float=Decimal,
            parse_constant=reject_constant,
            object_pairs_hook=object_pairs,
        )
    except IntegrityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("HUD API input is not strict UTF-8 JSON") from exc


def _canonical_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"-0", ""}:
        raise IntegrityError("HUD API decimal serialization produced signed/empty zero")
    return text


def _api_ratio(value: object, field: str, row_number: int) -> Decimal:
    if type(value) is not Decimal:
        raise IntegrityError(
            f"HUD API {field} must be a native JSON number at result {row_number}"
        )
    if not value.is_finite() or value < 0 or value > 1:
        raise IntegrityError(f"HUD API {field} is outside [0,1] at result {row_number}")
    if value.is_zero() and value.is_signed():
        raise IntegrityError(f"HUD API {field} is signed zero at result {row_number}")
    return value


def parse_hud_api_response(body: bytes) -> tuple[dict[str, object], ...]:
    """Validate the exact API envelope and retain ratios as ``Decimal``."""

    root = _strict_decimal_json(body)
    if not isinstance(root, dict) or set(root) != {"data"}:
        raise IntegrityError("HUD API root key set must be exactly {data}")
    data = root["data"]
    if not isinstance(data, dict) or set(data) != {
        "year", "quarter", "input", "crosswalk_type", "results"
    }:
        raise IntegrityError("HUD API data key set differs from the frozen schema")
    if (
        data["year"] != "2025"
        or data["quarter"] != "4"
        or data["input"] != "All"
        or data["crosswalk_type"] != "zip-county"
        or not isinstance(data["results"], list)
        or not data["results"]
    ):
        raise IntegrityError("HUD API envelope identity differs from 2025-Q4 zip-county/All")

    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    metadata: dict[str, tuple[str, str]] = {}
    sums: dict[str, dict[str, Decimal]] = {}
    for row_number, raw in enumerate(data["results"], start=1):
        if not isinstance(raw, dict) or set(raw) != HUD_API_RESULT_FIELDS:
            raise IntegrityError(f"HUD API result schema mismatch at result {row_number}")
        zip5 = raw["zip"]
        geoid = raw["geoid"]
        city = raw["city"]
        state = raw["state"]
        if (
            not isinstance(zip5, str)
            or re.fullmatch(r"[0-9]{5}", zip5) is None
            or zip5 == "00000"
        ):
            raise IntegrityError(f"HUD API ZIP is invalid at result {row_number}")
        if (
            not isinstance(geoid, str)
            or not geoid
            or not geoid.isascii()
            or not geoid.isdigit()
        ):
            raise IntegrityError(f"HUD API GEOID is invalid at result {row_number}")
        if not isinstance(city, str) or city == "":
            raise IntegrityError(f"HUD API preferred city is empty at result {row_number}")
        if not isinstance(state, str) or re.fullmatch(r"[A-Z]{2}", state) is None:
            raise IntegrityError(f"HUD API preferred state is invalid at result {row_number}")
        key = (zip5, geoid)
        if key in seen:
            raise IntegrityError(f"duplicate HUD API ZIP/GEOID pair: {key}")
        seen.add(key)
        zip_metadata = (city, state)
        if metadata.setdefault(zip5, zip_metadata) != zip_metadata:
            raise IntegrityError(f"HUD API ZIP metadata varies within ZIP {zip5}")
        ratios = {
            field: _api_ratio(raw[field], field, row_number)
            for field in ("bus_ratio", "res_ratio", "tot_ratio", "oth_ratio")
        }
        zip_sums = sums.setdefault(
            zip5,
            {field: Decimal(0) for field in ratios},
        )
        for field, value in ratios.items():
            zip_sums[field] += value
        rows.append(
            {
                "zip": zip5,
                "geoid": geoid,
                "bus_ratio": ratios["bus_ratio"],
                "res_ratio": ratios["res_ratio"],
                "tot_ratio": ratios["tot_ratio"],
                "oth_ratio": ratios["oth_ratio"],
                "city": city,
                "state": state,
            }
        )

    tolerance = Decimal("1e-12")
    for zip5, zip_sums in sums.items():
        for field in ("bus_ratio", "res_ratio", "oth_ratio"):
            value = zip_sums[field]
            if value != 0 and abs(value - 1) > tolerance:
                raise IntegrityError(f"HUD API {field} sum fails for ZIP {zip5}")
        if abs(zip_sums["tot_ratio"] - 1) > tolerance:
            raise IntegrityError(f"HUD API tot_ratio sum fails for ZIP {zip5}")
    return tuple(rows)


def validate_hud_api_envelope(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise IntegrityError("HUD API input must be a regular non-symlink file")
    parse_hud_api_response(path.read_bytes())


def _validate_hud_api_acquisition_receipt(
    path: Path,
    *,
    expected_sha256: str,
    raw_bytes: int,
    raw_sha256: str,
) -> str:
    if not path.is_file() or path.is_symlink():
        raise IntegrityError("HUD API acquisition receipt must be a regular file")
    body = path.read_bytes()
    observed_sha256 = hashlib.sha256(body).hexdigest()
    if observed_sha256 != expected_sha256:
        raise IntegrityError("HUD API acquisition receipt differs from its frozen hash")
    if re.search(
        rb"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", body
    ):
        raise IntegrityError("HUD API acquisition receipt contains credential material")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("HUD API acquisition receipt is not UTF-8 JSON") from exc
    try:
        source = value["source"]
        artifact = value["artifact"]
        identity = value["response_identity"]
    except (TypeError, KeyError) as exc:
        raise IntegrityError("HUD API acquisition receipt lacks frozen sections") from exc
    if (
        source.get("endpoint") != "https://www.huduser.gov/hudapi/public/usps"
        or source.get("request_parameters") != HUD_API_REQUEST_PARAMETERS
        or source.get("http_status") != 200
        or source.get("content_type") != "application/json"
        or artifact.get("bytes") != raw_bytes
        or artifact.get("sha256") != raw_sha256
        or identity.get("year") != 2025
        or identity.get("quarter") != 4
        or identity.get("input") != "All"
        or identity.get("crosswalk_type") != "zip-county"
    ):
        raise IntegrityError("HUD API acquisition receipt semantics differ from Amendment 03")
    return observed_sha256


def canonicalize_hud_api(
    path: Path,
    *,
    county_universe: set[str],
) -> tuple[bytes, tuple[dict[str, str], ...], bytes, dict[str, object]]:
    """Classify every API row and emit the frozen eight-column candidate CSV."""

    rows = parse_hud_api_response(path.read_bytes())
    included: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    territory_counts: dict[str, int] = {}
    special_counts: dict[str, int] = {}
    observed_counties: set[str] = set()
    for raw in rows:
        geoid = str(raw["geoid"])
        state = str(raw["state"])
        if len(geoid) == 5 and geoid in county_universe:
            classification = "IN_SCOPE_COUNTY"
        elif len(geoid) == 5 and geoid[:2] in TERRITORY_PREFIX_STATE:
            expected_state = TERRITORY_PREFIX_STATE[geoid[:2]]
            if state != expected_state:
                raise IntegrityError(
                    f"HUD API territory GEOID/state conflict: {geoid}/{state}"
                )
            classification = "EXCLUDED_TERRITORY_COUNTY"
        elif (geoid, state) in SPECIAL_NONCOUNTY:
            classification = "EXCLUDED_SPECIAL_NONCOUNTY"
        else:
            raise IntegrityError(f"HUD API unknown/out-of-universe geography: {geoid}/{state}")

        canonical = {
            "ZIP": str(raw["zip"]),
            "COUNTY": geoid,
            "BUS_RATIO": _canonical_decimal(raw["bus_ratio"]),
            "RES_RATIO": _canonical_decimal(raw["res_ratio"]),
            "TOT_RATIO": _canonical_decimal(raw["tot_ratio"]),
            "OTH_RATIO": _canonical_decimal(raw["oth_ratio"]),
            "USPS_ZIP_PREF_CITY": str(raw["city"]),
            "USPS_ZIP_PREF_STATE": state,
        }
        if classification == "IN_SCOPE_COUNTY":
            included.append(canonical)
            observed_counties.add(geoid)
        else:
            excluded_row = {"CLASSIFICATION": classification, **canonical}
            excluded.append(excluded_row)
            if classification == "EXCLUDED_TERRITORY_COUNTY":
                key = f"{geoid[:2]}:{state}"
                territory_counts[key] = territory_counts.get(key, 0) + 1
            else:
                key = f"{geoid}:{state}"
                special_counts[key] = special_counts.get(key, 0) + 1

    missing = sorted(county_universe - observed_counties)
    if missing:
        raise IntegrityError(f"HUD API omits frozen-universe counties: {missing[:5]}")
    if OBSOLETE_CT_COUNTIES & observed_counties:
        raise IntegrityError("HUD API includes obsolete Connecticut counties")
    observed_ct = sorted(fips for fips in observed_counties if fips.startswith("09"))
    if set(observed_ct) != CURRENT_CT_PLANNING_REGIONS:
        raise IntegrityError("HUD API Connecticut geography differs from planning regions")

    included.sort(key=lambda row: (row["ZIP"], row["COUNTY"]))
    excluded.sort(key=lambda row: (row["ZIP"], row["COUNTY"], row["CLASSIFICATION"]))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=HUD_API_HEADERS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(included)
    csv_body = output.getvalue().encode("utf-8")
    excluded_body = canonical_json_bytes({"rows": excluded})
    audit = {
        "raw_row_count": len(rows),
        "in_scope_row_count": len(included),
        "in_scope_county_count": len(observed_counties),
        "excluded_territory_row_count": sum(territory_counts.values()),
        "excluded_territory_geoid_count": len(
            {row["COUNTY"] for row in excluded if row["CLASSIFICATION"] == "EXCLUDED_TERRITORY_COUNTY"}
        ),
        "excluded_special_row_count": sum(special_counts.values()),
        "excluded_special_geoid_count": len(
            {row["COUNTY"] for row in excluded if row["CLASSIFICATION"] == "EXCLUDED_SPECIAL_NONCOUNTY"}
        ),
        "excluded_rows_bytes": len(excluded_body),
        "excluded_rows_sha256": hashlib.sha256(excluded_body).hexdigest(),
        "territory_state_prefix_counts": dict(sorted(territory_counts.items())),
        "special_pair_counts": dict(sorted(special_counts.items())),
        "connecticut_fips": observed_ct,
    }
    if len(included) + len(excluded) != len(rows):
        raise IntegrityError("HUD API geographic classification does not reconcile")
    return csv_body, tuple(included), excluded_body, audit


def build_hud_api_handoff(
    manifest: MaterializationManifest,
    raw_json_path: Path,
    county_universe_path: Path,
    frozen_acquisition_receipt_path: Path,
    import_receipt_path: Path,
    output_dir: Path,
    *,
    authorization_id: str,
    completed_at_utc: str | None = None,
) -> Path:
    """Build the R4 API-specific canonical HUD handoff without a live request."""

    if manifest.phase != "2E":
        raise IntegrityError("HUD API handoff requires the Phase-2E manifest")
    validate_safe_id(authorization_id, label="HUD API authorization ID")
    try:
        spec = manifest.sources[HUD_API_SOURCE_ID]
    except KeyError as exc:
        raise IntegrityError("Phase-2E manifest lacks the R4 HUD API source") from exc
    if set(manifest.sources) != {HUD_API_SOURCE_ID}:
        raise IntegrityError("HUD API handoff cannot mix R3 and R4 HUD representations")
    if raw_json_path.is_symlink() or not raw_json_path.is_file():
        raise IntegrityError("HUD API raw input must be a regular file")
    if (
        raw_json_path.stat().st_size != spec.bytes
        or sha256_file(raw_json_path) != spec.sha256
    ):
        raise IntegrityError("HUD API raw input differs from its frozen bytes/hash")
    validators = resolve_release_validators(spec)
    _run_artifact_validators(raw_json_path, spec, validators)
    universe, universe_body = _county_universe(
        county_universe_path,
        manifest.expectations["county_universe_sha256"],
    )
    if manifest.expectations["county_count"] != len(universe):
        raise IntegrityError("HUD API manifest county count differs from the universe")
    acquisition_receipt_sha256 = _validate_hud_api_acquisition_receipt(
        frozen_acquisition_receipt_path,
        expected_sha256=manifest.expectations["acquisition_receipt_sha256"],
        raw_bytes=spec.bytes,
        raw_sha256=spec.sha256,
    )
    if not import_receipt_path.is_file() or import_receipt_path.is_symlink():
        raise IntegrityError("HUD API local-import receipt must be a regular file")
    import_receipt_body = import_receipt_path.read_bytes()
    validate_stream_acquisition_receipt(
        import_receipt_body,
        spec,
        final_path=raw_json_path,
    )
    import_receipt_sha256 = hashlib.sha256(import_receipt_body).hexdigest()
    csv_body, rows, excluded_body, audit = canonicalize_hud_api(
        raw_json_path,
        county_universe=universe,
    )
    if audit["raw_row_count"] != manifest.expectations["result_row_count"]:
        raise IntegrityError("HUD API result count differs from the frozen expectation")
    parse_candidates(rows, universe)

    output_dir.mkdir(parents=True, exist_ok=False)
    csv_path = output_dir / "hud_zip_county_api_2025q4.csv"
    _atomic_write(csv_path, csv_body)
    excluded_path = output_dir / "hud_api_excluded_rows.json"
    _atomic_write(excluded_path, excluded_body)
    compatibility = {
        "schema_version": HUD_API_COMPATIBILITY_SCHEMA_VERSION,
        "release": "2025-Q4",
        "county_universe_sha256": hashlib.sha256(universe_body).hexdigest(),
        "county_universe_count": len(universe),
        "raw_row_count": audit["raw_row_count"],
        "in_scope_row_count": audit["in_scope_row_count"],
        "in_scope_county_count": audit["in_scope_county_count"],
        "excluded_territory_row_count": audit["excluded_territory_row_count"],
        "excluded_territory_geoid_count": audit["excluded_territory_geoid_count"],
        "excluded_special_row_count": audit["excluded_special_row_count"],
        "excluded_special_geoid_count": audit["excluded_special_geoid_count"],
        "excluded_rows_bytes": audit["excluded_rows_bytes"],
        "excluded_rows_sha256": audit["excluded_rows_sha256"],
        "territory_state_prefix_counts": audit["territory_state_prefix_counts"],
        "special_pair_counts": audit["special_pair_counts"],
        "invalid_unknown_geography_count": 0,
        "missing_universe_count": 0,
        "obsolete_connecticut_count": 0,
        "connecticut_fips": audit["connecticut_fips"],
        "state_basis": "CT_PLANNING_REGIONS_2022_PLUS",
        "schema_checks_passed": True,
        "ratio_checks_passed": True,
        "metadata_checks_passed": True,
        "unique_pair_check_passed": True,
        "geography_checks_passed": True,
        "compatible": True,
    }
    compatibility_path = output_dir / "hud_api_compatibility.json"
    _atomic_write(compatibility_path, canonical_json_bytes(compatibility))
    request_parameters_sha256 = hashlib.sha256(
        canonical_json_bytes(HUD_API_REQUEST_PARAMETERS)
    ).hexdigest()
    parser_sha256 = sha256_file(Path(__file__))
    handoff = {
        "schema_version": HUD_API_HANDOFF_SCHEMA_VERSION,
        "source_tree_id": manifest.source_tree_id,
        "materialization_manifest_sha256": manifest.sha256,
        "source_id": spec.source_id,
        "release": spec.release,
        "url": spec.url,
        "filename": spec.filename,
        "request_parameters": HUD_API_REQUEST_PARAMETERS,
        "request_parameters_sha256": request_parameters_sha256,
        "raw_json_bytes": spec.bytes,
        "raw_json_sha256": spec.sha256,
        "frozen_acquisition_receipt_sha256": acquisition_receipt_sha256,
        "import_receipt_sha256": import_receipt_sha256,
        "parser_sha256": parser_sha256,
        "county_universe_sha256": hashlib.sha256(universe_body).hexdigest(),
        "result_row_count": audit["raw_row_count"],
        "canonical_csv_bytes": len(csv_body),
        "canonical_csv_sha256": hashlib.sha256(csv_body).hexdigest(),
        "excluded_rows_bytes": len(excluded_body),
        "excluded_rows_sha256": hashlib.sha256(excluded_body).hexdigest(),
        "compatibility_sha256": hashlib.sha256(
            compatibility_path.read_bytes()
        ).hexdigest(),
        "handoff_state": "APPROVED_FROZEN_HUD_API_INPUT",
    }
    handoff_path = output_dir / "hud_api_handoff.json"
    _atomic_write(handoff_path, canonical_json_bytes(handoff))
    completed = completed_at_utc or _now()
    receipt = {
        "schema_version": "atlas-v19-hud-api-handoff-receipt-1",
        "receipt_id": "hud_api_handoff",
        "status": "complete",
        "source_tree_id": manifest.source_tree_id,
        "bundle_manifest_sha256": manifest.sha256,
        "authorization_id": authorization_id,
        "command": "import-hud-api",
        "completed_at_utc": completed,
        "evidence": {
            "phase2e_bundle_manifest_sha256": manifest.sha256,
            "url": spec.url,
            "filename": spec.filename,
            "request_parameters_sha256": request_parameters_sha256,
            "frozen_acquisition_receipt_sha256": acquisition_receipt_sha256,
            "import_receipt_sha256": import_receipt_sha256,
            "raw_json_sha256": spec.sha256,
            "canonical_csv_sha256": hashlib.sha256(csv_body).hexdigest(),
            "county_universe_sha256": hashlib.sha256(universe_body).hexdigest(),
            "result_row_count": audit["raw_row_count"],
            "in_scope_row_count": audit["in_scope_row_count"],
            "excluded_row_count": (
                audit["excluded_territory_row_count"]
                + audit["excluded_special_row_count"]
            ),
            "excluded_rows_bytes": len(excluded_body),
            "excluded_rows_sha256": audit["excluded_rows_sha256"],
            "release_year": 2025,
            "state_basis": "CT_PLANNING_REGIONS_2022_PLUS",
            "compatibility_receipt_sha256": hashlib.sha256(
                compatibility_path.read_bytes()
            ).hexdigest(),
            "hud_api_data_handoff_sha256": hashlib.sha256(
                handoff_path.read_bytes()
            ).hexdigest(),
        },
    }
    receipt_body = canonical_json_bytes(receipt)
    validate_control_receipt(
        "hud_api_handoff",
        receipt_body,
        context=ReceiptContext(manifest.source_tree_id, manifest.sha256, authorization_id),
    )
    receipt_path = output_dir / "receipt.json"
    _atomic_write(receipt_path, receipt_body)
    return receipt_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _county_universe(path: Path, expected_sha256: str) -> tuple[set[str], bytes]:
    if not path.is_file() or path.is_symlink():
        raise IntegrityError("HUD county universe must be a regular non-symlink JSON file")
    body = path.read_bytes()
    value = require_canonical_json(body, label="HUD county universe")
    if (
        not isinstance(value, list)
        or len(value) != 3_144
        or value != sorted(value)
        or len(value) != len(set(value))
        or any(
            not isinstance(fips, str)
            or len(fips) != 5
            or not fips.isascii()
            or not fips.isdigit()
            or fips[:2] not in set(STATE_FIPS.values())
            for fips in value
        )
        or not CURRENT_CT_PLANNING_REGIONS <= set(value)
        or OBSOLETE_CT_COUNTIES & set(value)
    ):
        raise IntegrityError("HUD county universe is not the frozen 3,144-county/CT set")
    identity = sorted_fips_bytes(value)
    if hashlib.sha256(identity).hexdigest() != expected_sha256:
        raise IntegrityError("HUD county universe differs from the approved sorted-FIPS hash")
    return set(value), identity


def _identifier(value: object, label: str) -> str:
    if isinstance(value, bool):
        raise ContractError(f"HUD {label} cannot be boolean")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not value.is_integer():
            raise ContractError(f"HUD {label} is not an integer")
        text = str(int(value))
    else:
        text = "" if value is None else str(value).strip()
    if not 1 <= len(text) <= 5 or not text.isascii() or not text.isdigit():
        raise ContractError(f"HUD {label} is not a 1..5 digit identifier")
    result = text.zfill(5)
    if label == "ZIP" and result == "00000":
        raise ContractError("HUD ZIP 00000 is invalid")
    return result


def _ratio(value: object, label: str) -> str:
    if value is None or str(value).strip() == "":
        return ""
    text = str(value).strip()
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ContractError(f"HUD {label} is not decimal") from exc
    if not parsed.is_finite() or not Decimal(0) <= parsed <= Decimal(1):
        raise ContractError(f"HUD {label} is outside [0,1]")
    return text


def canonicalize_hud_xlsx(
    path: Path,
    *,
    required_sheet_name: str,
    county_universe: set[str],
    validation_context: ReleaseValidationContext,
) -> tuple[bytes, tuple[dict[str, str], ...]]:
    """Parse a previously approval/CRC/schema-validated HUD workbook."""

    if (
        validation_context.candidate_manifest is None
        or validation_context.manifest_approval is None
    ):
        raise IntegrityError("HUD OOXML parse requires approved candidate/manifest data")
    verified = verify_zip_after_approval(
        path,
        validation_context.candidate_manifest,
        validation_context.manifest_approval,
    )

    def local(element: ElementTree.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    def xml_member(filename: str) -> ElementTree.Element:
        try:
            with open_verified_zip_member(verified, filename) as handle:
                return ElementTree.fromstring(handle.read())
        except ElementTree.ParseError as exc:
            raise IntegrityError(f"HUD OOXML member is unparseable: {filename}") from exc

    workbook = xml_member("xl/workbook.xml")
    relationships = xml_member("xl/_rels/workbook.xml.rels")
    relation_targets = {
        child.attrib.get("Id", ""): child.attrib.get("Target", "")
        for child in relationships
        if local(child) == "Relationship"
    }
    matching_sheets = [
        element
        for element in workbook.iter()
        if local(element) == "sheet" and element.attrib.get("name") == required_sheet_name
    ]
    if len(matching_sheets) != 1:
        raise IntegrityError("HUD required worksheet is absent or duplicated")
    relationship_ids = [
        value
        for key, value in matching_sheets[0].attrib.items()
        if key.rsplit("}", 1)[-1] == "id"
    ]
    if len(relationship_ids) != 1 or relationship_ids[0] not in relation_targets:
        raise IntegrityError("HUD worksheet relationship is missing")
    target = relation_targets[relationship_ids[0]]
    if target.startswith("/"):
        worksheet_member = target[1:]
    else:
        worksheet_member = "xl/" + target
    worksheet = xml_member(worksheet_member)
    names = {member.filename for member in verified.members}
    strings: tuple[str, ...] = ()
    if "xl/sharedStrings.xml" in names:
        shared = xml_member("xl/sharedStrings.xml")
        strings = tuple(
            "".join(node.text or "" for node in item.iter() if local(node) == "t")
            for item in shared
            if local(item) == "si"
        )

    def cell_value(cell: ElementTree.Element) -> str:
        kind = cell.attrib.get("t", "n")
        if kind == "inlineStr":
            return "".join(
                node.text or "" for node in cell.iter() if local(node) == "t"
            )
        values = [child.text or "" for child in cell if local(child) == "v"]
        value = values[0] if values else ""
        if kind == "s":
            if not value.isdigit() or int(value) >= len(strings):
                raise IntegrityError("HUD shared-string index is invalid")
            return strings[int(value)]
        return value

    sheet_data = [child for child in worksheet if local(child) == "sheetData"]
    if len(sheet_data) != 1:
        raise IntegrityError("HUD worksheet lacks exactly one sheetData")
    table: list[tuple[str, ...]] = []
    for row in sheet_data[0]:
        if local(row) != "row":
            continue
        row_number = row.attrib.get("r", "")
        if not row_number.isdigit():
            raise IntegrityError("HUD OOXML row number is invalid")
        cells: dict[int, str] = {}
        for cell in row:
            if local(cell) != "c":
                continue
            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell.attrib.get("r", ""))
            if match is None or match.group(2) != row_number:
                raise IntegrityError("HUD OOXML cell reference is invalid")
            index = 0
            for character in match.group(1):
                index = index * 26 + ord(character) - ord("A") + 1
            index -= 1
            if index >= len(HUD_HEADERS) or index in cells:
                raise IntegrityError("HUD OOXML cell is outside/duplicated in frozen width")
            cells[index] = cell_value(cell)
        table.append(tuple(cells.get(index, "") for index in range(len(HUD_HEADERS))))
    if len(table) < 2 or table[0] != HUD_HEADERS:
        raise IntegrityError("HUD worksheet is header-only or has wrong frozen headers")
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row_number, values in enumerate(table[1:], start=2):
        zip5 = _identifier(values[0], "ZIP")
        county = _identifier(values[1], "COUNTY")
        key = (zip5, county)
        if key in seen:
            raise IntegrityError(f"HUD ZIP/county duplicate at row {row_number}")
        seen.add(key)
        state = values[5].strip().upper()
        if state and state not in STATE_FIPS:
            raise ContractError(f"HUD preferred state is invalid at row {row_number}")
        rows.append(
            {
                "ZIP": zip5,
                "COUNTY": county,
                "BUS_RATIO": _ratio(values[2], "BUS_RATIO"),
                "RES_RATIO": _ratio(values[3], "RES_RATIO"),
                "TOT_RATIO": _ratio(values[4], "TOT_RATIO"),
                "USPS_ZIP_PREF_STATE": state,
            }
        )
    rows.sort(key=lambda row: (row["ZIP"], row["COUNTY"]))
    # R3 retained the six-column XLSX representation. R4 analytical parsing is
    # intentionally limited to the separately identified eight-column API CSV.
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=HUD_HEADERS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8"), tuple(rows)


def build_hud_handoff(
    manifest: MaterializationManifest,
    xlsx_path: Path,
    county_universe_path: Path,
    validation_context: ReleaseValidationContext,
    source_acquisition_receipt_path: Path,
    output_dir: Path,
    *,
    authorization_id: str,
    completed_at_utc: str | None = None,
) -> Path:
    if manifest.phase != "2E":
        raise IntegrityError("HUD handoff requires the Phase-2E materialization manifest")
    validate_safe_id(authorization_id, label="HUD authorization ID")
    spec = manifest.sources["HUD-ZIP-COUNTY-2025Q4"]
    if (
        not source_acquisition_receipt_path.is_file()
        or source_acquisition_receipt_path.is_symlink()
    ):
        raise IntegrityError("HUD source-acquisition receipt is absent/non-regular")
    source_acquisition_receipt_sha256 = hashlib.sha256(
        source_acquisition_receipt_path.read_bytes()
    ).hexdigest()
    if xlsx_path.is_symlink() or not xlsx_path.is_file():
        raise IntegrityError("HUD XLSX handoff input must be a regular file")
    if xlsx_path.stat().st_size != spec.bytes or sha256_file(xlsx_path) != spec.sha256:
        raise IntegrityError("HUD XLSX handoff input differs from its approved bytes/hash")
    validators = resolve_release_validators(spec, context=validation_context)
    _run_artifact_validators(xlsx_path, spec, validators)
    universe, universe_body = _county_universe(
        county_universe_path,
        manifest.expectations["county_universe_sha256"],
    )
    required_sheet_name = manifest.expectations["required_sheet_name"]
    if validation_context.required_sheet_names != (required_sheet_name,):
        raise IntegrityError("HUD validator sheet and approved manifest sheet differ")
    csv_body, rows = canonicalize_hud_xlsx(
        xlsx_path,
        required_sheet_name=required_sheet_name,
        county_universe=universe,
        validation_context=validation_context,
    )
    counties = {row["COUNTY"] for row in rows}
    outside = sorted(counties - universe)
    obsolete = sorted(counties & OBSOLETE_CT_COUNTIES)
    observed_ct = {fips for fips in counties if fips.startswith("09")}
    if outside or obsolete or observed_ct != CURRENT_CT_PLANNING_REGIONS:
        raise ContractError("HUD workbook is incompatible with the 2025 county/CT universe")
    output_dir.mkdir(parents=True, exist_ok=False)
    csv_path = output_dir / "hud_zip_county_2025q4.csv"
    _atomic_write(csv_path, csv_body)
    compatibility = {
        "schema_version": HUD_COMPATIBILITY_SCHEMA_VERSION,
        "release": "2025-Q4",
        "county_universe_sha256": hashlib.sha256(universe_body).hexdigest(),
        "county_universe_count": len(universe),
        "hud_row_count": len(rows),
        "hud_county_count": len(counties),
        "outside_universe_count": 0,
        "obsolete_connecticut_count": 0,
        "connecticut_fips": sorted(observed_ct),
        "state_basis": "CT_PLANNING_REGIONS_2022_PLUS",
        "compatible": True,
    }
    compatibility_path = output_dir / "hud_compatibility.json"
    _atomic_write(compatibility_path, canonical_json_bytes(compatibility))
    handoff = {
        "schema_version": HUD_HANDOFF_SCHEMA_VERSION,
        "source_tree_id": manifest.source_tree_id,
        "materialization_manifest_sha256": manifest.sha256,
        "source_id": spec.source_id,
        "release": spec.release,
        "url": spec.url,
        "filename": spec.filename,
        "raw_xlsx_bytes": spec.bytes,
        "raw_xlsx_sha256": spec.sha256,
        "candidate_manifest_sha256": hashlib.sha256(
            canonical_json_bytes(
                {
                    "source_id": validation_context.candidate_manifest.source_id,
                    "archive_bytes": validation_context.candidate_manifest.archive_bytes,
                    "archive_sha256": validation_context.candidate_manifest.archive_sha256,
                    "members": [
                        vars(member) for member in validation_context.candidate_manifest.members
                    ],
                }
            )
        ).hexdigest(),
        "manifest_approval_id": validation_context.manifest_approval.approval_id,
        "required_sheet_name": required_sheet_name,
        "canonical_csv_bytes": len(csv_body),
        "canonical_csv_sha256": hashlib.sha256(csv_body).hexdigest(),
        "compatibility_sha256": hashlib.sha256(
            compatibility_path.read_bytes()
        ).hexdigest(),
        "handoff_state": "APPROVED_FROZEN_HUD_INPUT",
    }
    handoff_path = output_dir / "hud_handoff.json"
    _atomic_write(handoff_path, canonical_json_bytes(handoff))
    completed = completed_at_utc or _now()
    receipt = {
        "schema_version": "atlas-v19-hud-handoff-receipt-1",
        "receipt_id": "hud_handoff",
        "status": "complete",
        "source_tree_id": manifest.source_tree_id,
        "bundle_manifest_sha256": manifest.sha256,
        "authorization_id": authorization_id,
        "command": "acquire-hud",
        "completed_at_utc": completed,
        "evidence": {
            "phase2e_bundle_manifest_sha256": manifest.sha256,
            "url": spec.url,
            "filename": spec.filename,
            "source_acquisition_receipt_sha256": source_acquisition_receipt_sha256,
            "raw_xlsx_sha256": spec.sha256,
            "canonical_csv_sha256": hashlib.sha256(csv_body).hexdigest(),
            "release_year": 2025,
            "state_basis": "CT_PLANNING_REGIONS_2022_PLUS",
            "compatibility_receipt_sha256": hashlib.sha256(
                compatibility_path.read_bytes()
            ).hexdigest(),
            "hud_data_handoff_sha256": hashlib.sha256(
                handoff_path.read_bytes()
            ).hexdigest(),
        },
    }
    receipt_body = canonical_json_bytes(receipt)
    validate_control_receipt(
        "hud_handoff",
        receipt_body,
        context=ReceiptContext(manifest.source_tree_id, manifest.sha256, authorization_id),
    )
    receipt_path = output_dir / "receipt.json"
    _atomic_write(receipt_path, receipt_body)
    return receipt_path
