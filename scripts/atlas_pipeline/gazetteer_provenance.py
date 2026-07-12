"""Frozen COUNTY-GAZ-2025 ZIP-to-canonical provenance verification.

The analytical build may not trust a processed Gazetteer merely because it
contains the approved FIPS set.  This module rechecks the frozen ZIP central
directory, streams the one approved pipe-delimited member, deterministically
rederives the canonical CSV, and requires byte-for-byte equality with the
processed bundle role before county coordinates are exposed to the build.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zlib
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .acquisition import (
    ZipCandidateManifest,
    ZipMemberIdentity,
    _read_zip_central_directory,
    canonical_zip_candidate_manifest_bytes,
)
from .bundle import FrozenBundle, _require_exact_keys, canonical_json_bytes
from .contracts import STATE_FIPS
from .errors import IntegrityError
from .predecessor_bridge import require_same_tree_or_predecessor_body
from .source_registry import SOURCE_POLICIES
from .source_tables import parse_gazetteer
from .strict_json import require_canonical_json, validate_safe_id

SOURCE_ID = "COUNTY-GAZ-2025"
SELECTED_MEMBER_FILENAME = "2025_Gaz_counties_national.txt"
EXTRACTION_RECEIPT_SCHEMA = "atlas-v19-gazetteer-extraction-receipt-1"
CANONICAL_TRANSFORM_ID = "atlas-v19-gazetteer-pipe-to-canonical-csv-1"
CANONICAL_COLUMNS = ("USPS", "GEOID", "NAME", "INTPTLAT", "INTPTLONG")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MEMBER_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class GazetteerExtractionReceipt:
    schema_version: str
    status: str
    source_tree_id: str
    authorization_id: str
    source_id: str
    archive_bytes: int
    archive_sha256: str
    candidate_manifest_sha256: str
    approval_id: str
    approved_by: str
    approved_at_utc: str
    selected_member_filename: str
    selected_member_central_metadata_sha256: str
    selected_member_bytes: int
    selected_member_sha256: str
    delimiter: str
    canonical_transform_id: str
    canonical_columns: list[str]
    canonical_bytes: int
    canonical_sha256: str

    def validate(self) -> None:
        if (
            self.schema_version != EXTRACTION_RECEIPT_SCHEMA
            or self.status != "APPROVED_CRC_VERIFIED_CANONICAL"
            or self.source_id != SOURCE_ID
            or self.selected_member_filename != SELECTED_MEMBER_FILENAME
            or self.delimiter != "|"
            or self.canonical_transform_id != CANONICAL_TRANSFORM_ID
            or self.canonical_columns != list(CANONICAL_COLUMNS)
        ):
            raise IntegrityError("Gazetteer extraction approval contract mismatch")
        validate_safe_id(self.authorization_id, label="Gazetteer authorization ID")
        validate_safe_id(self.approval_id, label="Gazetteer extraction approval ID")
        if not self.approved_by.strip() or "\n" in self.approved_by or "\r" in self.approved_by:
            raise IntegrityError("Gazetteer extraction approver is invalid")
        if not self.approved_at_utc.endswith("Z"):
            raise IntegrityError("Gazetteer extraction approval timestamp must be UTC Z")
        try:
            timestamp = datetime.fromisoformat(self.approved_at_utc[:-1] + "+00:00")
        except ValueError as exc:
            raise IntegrityError("Gazetteer extraction approval timestamp is invalid") from exc
        if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
            raise IntegrityError("Gazetteer extraction approval timestamp is not UTC")
        if (
            type(self.archive_bytes) is not int
            or type(self.selected_member_bytes) is not int
            or type(self.canonical_bytes) is not int
            or min(self.archive_bytes, self.selected_member_bytes, self.canonical_bytes) <= 0
        ):
            raise IntegrityError("Gazetteer extraction byte counts must be positive integers")
        for label, value in (
            ("source-tree ID", self.source_tree_id),
            ("archive hash", self.archive_sha256),
            ("candidate-manifest hash", self.candidate_manifest_sha256),
            ("selected-member central metadata hash", self.selected_member_central_metadata_sha256),
            ("selected-member hash", self.selected_member_sha256),
            ("canonical hash", self.canonical_sha256),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise IntegrityError(f"Gazetteer extraction receipt has invalid {label}")


def _candidate_from_body(body: bytes) -> ZipCandidateManifest:
    value = require_canonical_json(body, label="Gazetteer ZIP candidate manifest")
    if not isinstance(value, dict):
        raise IntegrityError("Gazetteer ZIP candidate manifest must be an object")
    _require_exact_keys(
        value,
        ("source_id", "archive_bytes", "archive_sha256", "members"),
        "Gazetteer ZIP candidate manifest",
    )
    raw_members = value["members"]
    if not isinstance(raw_members, list):
        raise IntegrityError("Gazetteer ZIP candidate members must be an array")
    fields = tuple(ZipMemberIdentity.__dataclass_fields__)
    members: list[ZipMemberIdentity] = []
    for row in raw_members:
        if not isinstance(row, dict):
            raise IntegrityError("Gazetteer ZIP candidate member must be an object")
        _require_exact_keys(row, fields, "Gazetteer ZIP candidate member")
        members.append(ZipMemberIdentity(**row))
    candidate = ZipCandidateManifest(
        value["source_id"], value["archive_bytes"], value["archive_sha256"],
        tuple(members),
    )
    candidate.validate()
    if body != canonical_zip_candidate_manifest_bytes(candidate):
        raise IntegrityError("Gazetteer ZIP candidate manifest is not canonical")
    return candidate


def _receipt_from_body(body: bytes) -> GazetteerExtractionReceipt:
    value = require_canonical_json(body, label="Gazetteer extraction approval")
    if not isinstance(value, dict):
        raise IntegrityError("Gazetteer extraction approval must be an object")
    fields = tuple(GazetteerExtractionReceipt.__dataclass_fields__)
    _require_exact_keys(value, fields, "Gazetteer extraction approval")
    receipt = GazetteerExtractionReceipt(**value)
    receipt.validate()
    if body != canonical_json_bytes(asdict(receipt)):
        raise IntegrityError("Gazetteer extraction approval is not canonical")
    return receipt


def _canonical_csv(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=CANONICAL_COLUMNS, lineterminator="\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def derive_canonical_gazetteer(
    member_body: bytes,
    *,
    county_universe: set[str],
    expected_count: int,
) -> bytes:
    """Derive the only accepted analytical CSV from the approved pipe member."""

    if not member_body or len(member_body) > _MAX_MEMBER_BYTES or b"\0" in member_body:
        raise IntegrityError("Gazetteer member is empty, oversized, or contains NUL")
    try:
        text = member_body.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise IntegrityError("Gazetteer member is not strict UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter="|", strict=True)
    if (
        reader.fieldnames is None
        or len(reader.fieldnames) != len(set(reader.fieldnames))
        or any(column not in reader.fieldnames for column in CANONICAL_COLUMNS)
    ):
        raise IntegrityError("Gazetteer pipe header is missing/duplicated")
    source_rows: list[dict[str, str]] = []
    try:
        for row in reader:
            if None in row or any(row[field] is None for field in reader.fieldnames):
                raise IntegrityError("Gazetteer pipe member has a row-width mismatch")
            source_rows.append({column: row[column] for column in CANONICAL_COLUMNS})
    except csv.Error as exc:
        raise IntegrityError("Gazetteer pipe member is malformed") from exc
    if not source_rows:
        raise IntegrityError("Gazetteer pipe member has no data rows")

    points = parse_gazetteer(source_rows, expected_count=expected_count)
    if set(points) != county_universe:
        raise IntegrityError("rederived Gazetteer universe differs from the frozen universe")
    names: dict[str, tuple[str, str]] = {}
    for row in source_rows:
        state = row["USPS"].strip(" \t\r\n\f\v")
        fips = row["GEOID"].strip(" \t\r\n\f\v")
        if fips not in county_universe:
            continue
        name = row["NAME"].strip(" \t\r\n\f\v")
        if not name or state not in STATE_FIPS:
            raise IntegrityError("Gazetteer canonical state/name is blank or invalid")
        names[fips] = (state, name)
    if set(names) != county_universe:
        raise IntegrityError("Gazetteer canonical names differ from the frozen universe")
    canonical_rows = [
        {
            "USPS": names[fips][0],
            "GEOID": fips,
            "NAME": names[fips][1],
            "INTPTLAT": points[fips][0],
            "INTPTLONG": points[fips][1],
        }
        for fips in sorted(county_universe)
    ]
    return _canonical_csv(canonical_rows)


def require_exact_canonical_derivation(
    member_body: bytes,
    canonical_body: bytes,
    *,
    county_universe: set[str],
    expected_count: int,
) -> bytes:
    derived = derive_canonical_gazetteer(
        member_body, county_universe=county_universe, expected_count=expected_count
    )
    if derived != canonical_body:
        raise IntegrityError(
            "processed Gazetteer differs byte-for-byte from the frozen raw ZIP derivation"
        )
    return derived


def _member_body(
    raw_zip: Path,
    candidate: ZipCandidateManifest,
    receipt: GazetteerExtractionReceipt,
) -> bytes:
    actual_members = _read_zip_central_directory(raw_zip)
    if actual_members != candidate.members:
        raise IntegrityError("Gazetteer ZIP central directory differs from approved manifest")
    regular = [member for member in candidate.members if not member.is_directory]
    if len(regular) != 1 or regular[0].filename != SELECTED_MEMBER_FILENAME:
        raise IntegrityError("Gazetteer ZIP must contain exactly the approved root member")
    member = regular[0]
    central_hash = hashlib.sha256(canonical_json_bytes(asdict(member))).hexdigest()
    if (
        central_hash != receipt.selected_member_central_metadata_sha256
        or member.uncompressed_bytes != receipt.selected_member_bytes
        or member.uncompressed_bytes > _MAX_MEMBER_BYTES
    ):
        raise IntegrityError("Gazetteer selected-member central metadata differs from approval")
    try:
        with zipfile.ZipFile(raw_zip) as archive:
            with archive.open(member.filename, "r") as handle:
                body = handle.read(member.uncompressed_bytes + 1)
                if handle.read(1):
                    raise IntegrityError("Gazetteer member exceeds approved byte length")
    except IntegrityError:
        raise
    except (OSError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
        raise IntegrityError("Gazetteer approved member extraction failed") from exc
    if (
        len(body) != member.uncompressed_bytes
        or (zlib.crc32(body) & 0xFFFFFFFF) != member.crc32
        or hashlib.sha256(body).hexdigest() != receipt.selected_member_sha256
    ):
        raise IntegrityError("Gazetteer selected member fails approved bytes/CRC/SHA-256")
    return body


def verify_gazetteer_provenance(
    bundle: FrozenBundle,
    roles: Mapping[str, object],
    *,
    county_universe: set[str],
    expected_count: int,
) -> list[dict[str, str]]:
    """Reverify and rederive Gazetteer before returning any county coordinates."""

    raw_source = roles["gazetteer_raw_zip"]
    candidate_source = roles["gazetteer_candidate_manifest"]
    receipt_source = roles["gazetteer_extraction_receipt"]
    canonical_source = roles["gazetteer"]
    if not all(
        isinstance(value, str)
        for value in (raw_source, candidate_source, receipt_source, canonical_source)
    ):
        raise IntegrityError("Gazetteer provenance roles must be source IDs")
    if raw_source != SOURCE_ID:
        raise IntegrityError("Gazetteer raw ZIP role must be the pinned COUNTY-GAZ-2025 source")
    entries = bundle.inputs
    if (
        entries[raw_source].schema_id != "gazetteer_raw_zip"
        or entries[candidate_source].schema_id != "zip_candidate_manifest_json"
        or entries[receipt_source].schema_id != "gazetteer_extraction_receipt_json"
        or entries[canonical_source].schema_id != "gazetteer_csv"
    ):
        raise IntegrityError("Gazetteer provenance roles use the wrong schemas")
    policy = SOURCE_POLICIES[SOURCE_ID]
    raw_entry = entries[raw_source]
    if (
        raw_entry.release != policy.release
        or raw_entry.url != policy.url
        or Path(raw_entry.relative_path).name != policy.filename
        or raw_entry.bytes != policy.bytes
        or raw_entry.sha256 != policy.sha256
        or raw_entry.content_type not in policy.content_types
    ):
        raise IntegrityError("Gazetteer raw ZIP differs from the immutable COUNTY-GAZ-2025 pin")

    raw_path = bundle.verify_file(raw_source)
    candidate_body = bundle.verify_file(candidate_source).read_bytes()
    candidate = _candidate_from_body(candidate_body)
    if (
        candidate.source_id != SOURCE_ID
        or candidate.archive_bytes != raw_entry.bytes
        or candidate.archive_sha256 != raw_entry.sha256
    ):
        raise IntegrityError("Gazetteer candidate manifest does not bind the pinned raw ZIP")
    receipt_body = bundle.verify_file(receipt_source).read_bytes()
    receipt = _receipt_from_body(receipt_body)
    predecessor_receipt = require_same_tree_or_predecessor_body(
        bundle,
        observed_source_tree_id=receipt.source_tree_id,
        artifact_id=receipt_source,
        body=receipt_body,
    )
    if (
        (not predecessor_receipt and receipt.authorization_id != bundle.authorization_id)
        or receipt.archive_bytes != raw_entry.bytes
        or receipt.archive_sha256 != raw_entry.sha256
        or receipt.candidate_manifest_sha256
        != hashlib.sha256(candidate_body).hexdigest()
    ):
        raise IntegrityError("Gazetteer extraction approval is not bound to this frozen bundle")
    member_body = _member_body(raw_path, candidate, receipt)
    canonical_path = bundle.verify_file(canonical_source)
    canonical_body = canonical_path.read_bytes()
    if (
        receipt.canonical_bytes != len(canonical_body)
        or receipt.canonical_sha256 != hashlib.sha256(canonical_body).hexdigest()
    ):
        raise IntegrityError("Gazetteer canonical role differs from extraction approval")
    require_exact_canonical_derivation(
        member_body,
        canonical_body,
        county_universe=county_universe,
        expected_count=expected_count,
    )
    return list(bundle.iter_csv(canonical_source))
