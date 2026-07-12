"""Approved frozen-bundle manifests, strict source loaders, and receipts.

All production CLIs enter through this module.  Pins that were pending during
Phase 2A are supplied by an approved canonical ``bundle.json``; changing a pin
there changes the bundle hash and invalidates its authorization/receipts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Mapping, TextIO
from urllib.parse import urlparse

from .acquisition import (
    FrozenInput,
    ZipCandidateManifest,
    ZipManifestApproval,
    ZipMemberIdentity,
    _read_zip_central_directory,
    authorize_zip_approval,
    canonical_zip_candidate_manifest_bytes,
    open_verified_zip_member,
    sha256_file,
    verify_zip_after_approval,
    zip_member_central_metadata_sha256,
)
from .contracts import HOSPITAL_FIELDS, NPPES_ANALYTICAL_FIELDS
from .control_receipts import ReceiptContext, validate_control_receipt
from .errors import ContractError, IntegrityError
from .places import PLACES_REQUIRED_FIELDS
from .strict_json import (
    canonical_data_json_bytes as _strict_canonical_data_json_bytes,
    canonical_json_bytes as _strict_canonical_json_bytes,
    reject_control_sentinels,
    require_canonical_json,
    resolve_regular_contained,
    strict_json_loads,
    validate_safe_id,
    validate_safe_relative_posix,
)

BUNDLE_SCHEMA_VERSION = "atlas-v19-frozen-bundle-1"
RECEIPT_SCHEMA_VERSION = "atlas-v19-output-receipt-1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

SCHEMA_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "nppes_csv": tuple(NPPES_ANALYTICAL_FIELDS),
    "cms_hospital_csv": tuple(HOSPITAL_FIELDS),
    "hud_zip_county_csv": (
        "ZIP", "COUNTY", "BUS_RATIO", "RES_RATIO", "TOT_RATIO",
        "USPS_ZIP_PREF_STATE",
    ),
    "hud_api_zip_county_csv": (
        "ZIP", "COUNTY", "BUS_RATIO", "RES_RATIO", "TOT_RATIO",
        "OTH_RATIO", "USPS_ZIP_PREF_CITY", "USPS_ZIP_PREF_STATE",
    ),
    "acs_population_csv": ("GEO_ID", "B01003_E001", "B01003_M001"),
    "acs_income_csv": ("GEO_ID", "B19013_E001", "B19013_M001"),
    "gazetteer_csv": ("USPS", "GEOID", "NAME", "INTPTLAT", "INTPTLONG"),
    "rucc_csv": ("FIPS", "State", "County_Name", "Attribute", "Value"),
    "hrsa_hpsa_csv": ("Common State County FIPS Code", "HPSA Status"),
    "places_raw_csv": tuple(PLACES_REQUIRED_FIELDS),
}

SCHEMA_CSV_DELIMITERS: dict[str, str] = {
    "acs_population_csv": "|",
    "acs_income_csv": "|",
}

SCHEMA_CSV_ENCODINGS: dict[str, str] = {
    "rucc_csv": "cp1252",
}

SCHEMA_EXACT_HEADERS: dict[str, tuple[str, ...]] = {
    "acs_population_csv": SCHEMA_REQUIRED_COLUMNS["acs_population_csv"],
    "acs_income_csv": SCHEMA_REQUIRED_COLUMNS["acs_income_csv"],
    "rucc_csv": SCHEMA_REQUIRED_COLUMNS["rucc_csv"],
    "hud_zip_county_csv": SCHEMA_REQUIRED_COLUMNS["hud_zip_county_csv"],
    "hud_api_zip_county_csv": SCHEMA_REQUIRED_COLUMNS["hud_api_zip_county_csv"],
}

SCHEMA_JSONL_KEYS: dict[str, tuple[str, ...]] = {
    "places_canonical_jsonl": (
        "fips", "chd_age_adjusted_prevalence_pct", "places_release",
        "estimate_year", "measure_id", "data_value_type_id", "unit",
        "low_confidence_limit", "high_confidence_limit", "footnote",
    ),
    "geocoder_canonical_jsonl": (
        "request_id", "outcome", "match_indicator", "match_type",
        "matched_address", "longitude", "latitude", "county_fips",
        "accepted_attempt", "source_response_sha256",
    ),
    "nppes_selected_jsonl": (
        "schema_version", "npi", "source_row", "primary_taxonomy", "active_status",
        "address_id", "address_classification", "direct_eligible", "hud_eligible",
    ),
    "nppes_dispositions_jsonl": (
        "schema_version", "npi", "pre_geography_disposition", "diagnostics",
        "address_id", "direct_eligible", "hud_eligible",
    ),
    "entity_address_mapping_jsonl": (
        "entity_kind", "entity_id", "address_id", "street1", "street2", "city",
        "state", "zip5", "country", "classification", "direct_eligible",
        "hud_eligible",
    ),
    "adjacency_canonical_jsonl": ("primary_fips", "neighbor_fips"),
}

CONTROL_RECEIPT_SCHEMAS = {
    "d2ii_handoff_receipt_json": "d2ii_handoff",
    "census_handoff_receipt_json": "census_handoff",
    "hud_handoff_receipt_json": "hud_handoff",
    "hud_api_handoff_receipt_json": "hud_api_handoff",
    "validation_completion_control_receipt_json": "validation_completion",
    "places_handoff_receipt_json": "places_handoff",
}


def canonical_json_bytes(value: object) -> bytes:
    return _strict_canonical_json_bytes(value)


def canonical_data_json_bytes(value: object) -> bytes:
    """Canonical analytical row bytes without Unicode normalization."""

    return _strict_canonical_data_json_bytes(value)


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    if temporary.exists():
        raise IntegrityError(f"stale atomic-write partial exists: {temporary}")
    with temporary.open("xb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_json_bytes(body: bytes) -> object:
    return strict_json_loads(body, label="bundle artifact")


def _require_exact_keys(value: Mapping[str, object], expected: tuple[str, ...], label: str) -> None:
    if tuple(value) != expected:
        raise IntegrityError(f"{label} keys/order mismatch: {tuple(value)} != {expected}")


def _validate_utc_z(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IntegrityError(f"{label} must be an ISO-8601 UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise IntegrityError(f"{label} is invalid") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise IntegrityError(f"{label} is not UTC")
    return value


@dataclass(frozen=True)
class BundleInput:
    source_id: str
    release: str
    url: str
    relative_path: str
    bytes: int
    sha256: str
    content_type: str
    schema_id: str


@dataclass(frozen=True)
class NppesContainerIdentity:
    archive_sha256: str
    candidate_manifest_sha256: str
    approval_receipt_sha256: str
    approval_trust_root_sha256: str
    selected_member_filename: str
    selected_member_sha256: str


@dataclass(frozen=True)
class FrozenBundle:
    root: Path
    phase: str
    authorization_id: str
    approved_at_utc: str
    source_tree_id: str
    expectations: Mapping[str, object]
    inputs: Mapping[str, BundleInput]
    manifest_bytes: bytes
    manifest_sha256: str

    @classmethod
    def load(cls, root: Path, *, required_phase: str | None = None) -> "FrozenBundle":
        root = root.resolve()
        manifest_path = root / "bundle.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise IntegrityError("bundle.json must be a regular non-symlink file")
        body = manifest_path.read_bytes()
        value = _load_json_bytes(body)
        if not isinstance(value, dict):
            raise IntegrityError("bundle manifest must be a JSON object")
        _require_exact_keys(
            value,
            ("schema_version", "phase", "authorization", "expectations", "inputs"),
            "bundle manifest",
        )
        if value["schema_version"] != BUNDLE_SCHEMA_VERSION:
            raise IntegrityError("unsupported bundle schema version")
        if body != canonical_json_bytes(value):
            raise IntegrityError("bundle.json is not canonical UTF-8/LF JSON")
        phase = value["phase"]
        if phase not in {"2D-II", "2E", "2F"} or (
            required_phase is not None and phase != required_phase
        ):
            raise IntegrityError(f"bundle phase mismatch: {phase!r}")
        authorization = value["authorization"]
        if not isinstance(authorization, dict):
            raise IntegrityError("bundle authorization must be an object")
        _require_exact_keys(
            authorization,
            ("authorization_id", "approved_at_utc", "source_tree_id"),
            "bundle authorization",
        )
        authorization_id = authorization["authorization_id"]
        source_tree_id = authorization["source_tree_id"]
        validate_safe_id(authorization_id, label="bundle authorization ID")
        if not isinstance(source_tree_id, str) or not _SHA256.fullmatch(source_tree_id):
            raise IntegrityError("bundle source_tree_id is invalid")
        approved_at = _validate_utc_z(authorization["approved_at_utc"], "approved_at_utc")
        expectations = value["expectations"]
        if not isinstance(expectations, dict):
            raise IntegrityError("bundle expectations must be an object")
        reject_control_sentinels(expectations, label="bundle expectations")
        entries = value["inputs"]
        if not isinstance(entries, list):
            raise IntegrityError("bundle inputs must be an array")
        parsed_inputs: dict[str, BundleInput] = {}
        for raw in entries:
            if not isinstance(raw, dict):
                raise IntegrityError("bundle input must be an object")
            _require_exact_keys(
                raw,
                (
                    "source_id", "release", "url", "relative_path", "bytes",
                    "sha256", "content_type", "schema_id",
                ),
                "bundle input",
            )
            entry = BundleInput(**raw)
            if (
                not isinstance(entry.source_id, str)
                or _SAFE_ID.fullmatch(entry.source_id) is None
                or entry.source_id in {".", ".."}
                or not entry.release
                or entry.source_id in parsed_inputs
            ):
                raise IntegrityError("bundle source identity is blank, unsafe, or duplicated")
            parsed_url = urlparse(entry.url)
            if parsed_url.scheme != "https" or not parsed_url.hostname:
                raise IntegrityError(f"bundle source URL is not HTTPS: {entry.source_id}")
            relative = PurePosixPath(entry.relative_path)
            if type(entry.bytes) is not int or entry.bytes <= 0 or not _SHA256.fullmatch(entry.sha256):
                raise IntegrityError(f"invalid byte/hash pin: {entry.source_id}")
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or len(relative.parts) != 3
                or relative.parts[0] != "sha256"
                or relative.parts[1] != entry.sha256
                or Path(relative.parts[2]).name != relative.parts[2]
                or not relative.parts[2]
            ):
                raise IntegrityError(
                    f"bundle input is not on its canonical content-addressed path: "
                    f"{entry.relative_path}"
                )
            if not entry.content_type or not entry.schema_id:
                raise IntegrityError(f"blank content/schema pin: {entry.source_id}")
            parsed_inputs[entry.source_id] = entry
        bundle = cls(
            root=root,
            phase=phase,
            authorization_id=authorization_id,
            approved_at_utc=approved_at,
            source_tree_id=source_tree_id,
            expectations=expectations,
            inputs=parsed_inputs,
            manifest_bytes=body,
            manifest_sha256=hashlib.sha256(body).hexdigest(),
        )
        bundle.verify_all_files()
        return bundle

    def path_for(self, source_id: str) -> Path:
        try:
            entry = self.inputs[source_id]
        except KeyError as exc:
            raise IntegrityError(f"bundle missing source: {source_id}") from exc
        path = self.root.joinpath(*PurePosixPath(entry.relative_path).parts)
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise IntegrityError("bundle source path escapes its root") from exc
        if path.is_symlink() or not path.is_file():
            raise IntegrityError(f"bundle source is not a regular file: {source_id}")
        return path

    def verify_file(self, source_id: str) -> Path:
        entry = self.inputs[source_id]
        path = self.path_for(source_id)
        if path.stat().st_size != entry.bytes or sha256_file(path) != entry.sha256:
            raise IntegrityError(f"bundle source bytes/hash mismatch: {source_id}")
        return path

    def verify_all_files(self) -> None:
        for source_id in sorted(self.inputs):
            self.verify_file(source_id)
        declared = {"bundle.json"} | {
            PurePosixPath(entry.relative_path).as_posix() for entry in self.inputs.values()
        }
        observed: set[str] = set()
        for path in self.root.rglob("*"):
            if path.is_symlink():
                raise IntegrityError(f"symlink is forbidden in a frozen bundle: {path}")
            if path.is_file():
                observed.add(path.relative_to(self.root).as_posix())
        if observed != declared:
            raise IntegrityError(
                f"frozen bundle undeclared/missing file set: extra={sorted(observed-declared)}, "
                f"missing={sorted(declared-observed)}"
            )

    def require_sources(self, required: Iterable[str]) -> None:
        missing = sorted(set(required) - set(self.inputs))
        if missing:
            raise IntegrityError(f"bundle missing required sources: {missing}")

    def expectation(self, key: str, expected_type: type) -> object:
        if key not in self.expectations:
            raise IntegrityError(f"bundle missing approved expectation: {key}")
        value = self.expectations[key]
        if type(value) is not expected_type:
            raise IntegrityError(f"bundle expectation {key} has the wrong type")
        if isinstance(value, str) and (not value or value.upper().startswith("PENDING")):
            raise IntegrityError(f"bundle expectation {key} is not frozen")
        return value

    def iter_csv(
        self, source_id: str, required_columns: tuple[str, ...] | None = None
    ) -> Iterator[dict[str, str]]:
        entry = self.inputs[source_id]
        required = required_columns or SCHEMA_REQUIRED_COLUMNS.get(entry.schema_id)
        if required is None:
            raise IntegrityError(f"no CSV schema registered for {entry.schema_id}")
        path = self.verify_file(source_id)
        delimiter = SCHEMA_CSV_DELIMITERS.get(entry.schema_id, ",")
        encoding = SCHEMA_CSV_ENCODINGS.get(entry.schema_id, "utf-8-sig")
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter, strict=True)
            if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise IntegrityError(f"invalid/duplicate CSV header: {source_id}")
            expected_header = SCHEMA_EXACT_HEADERS.get(entry.schema_id)
            if expected_header is not None and tuple(reader.fieldnames) != expected_header:
                raise IntegrityError(
                    f"CSV {source_id} header does not equal its frozen ordered schema"
                )
            missing = [field for field in required if field not in reader.fieldnames]
            if missing:
                raise IntegrityError(f"CSV {source_id} missing columns: {missing}")
            row_count = 0
            try:
                for row in reader:
                    row_count += 1
                    if None in row or any(row[field] is None for field in reader.fieldnames):
                        raise IntegrityError(f"CSV {source_id} has a row-width mismatch")
                    yield {field: row[field] for field in reader.fieldnames}
            except csv.Error as exc:
                raise IntegrityError(f"malformed CSV: {source_id}") from exc
            if row_count == 0:
                raise IntegrityError(f"CSV {source_id} has no data rows")

    def iter_jsonl(
        self, source_id: str, exact_keys: tuple[str, ...] | None = None
    ) -> Iterator[dict[str, object]]:
        entry = self.inputs[source_id]
        expected = exact_keys or SCHEMA_JSONL_KEYS.get(entry.schema_id)
        if expected is None:
            raise IntegrityError(f"no JSONL schema registered for {entry.schema_id}")
        path = self.verify_file(source_id)
        with path.open("rb") as handle:
            row_count = 0
            for line_number, line in enumerate(handle, start=1):
                row_count += 1
                if not line.endswith(b"\n"):
                    raise IntegrityError(f"JSONL final/row LF missing: {source_id}:{line_number}")
                value = _load_json_bytes(line)
                if not isinstance(value, dict):
                    raise IntegrityError(f"JSONL row is not an object: {source_id}:{line_number}")
                serializer = (
                    canonical_data_json_bytes
                    if entry.schema_id == "nppes_selected_jsonl"
                    else canonical_json_bytes
                )
                if line != serializer(value):
                    raise IntegrityError(f"JSONL row is not canonical: {source_id}:{line_number}")
                _require_exact_keys(value, expected, f"JSONL {source_id}:{line_number}")
                yield value
            if row_count == 0:
                raise IntegrityError(f"JSONL {source_id} has no data rows")

    @contextmanager
    def open_nppes_member_with_identity(
        self, source_id: str
    ) -> Iterator[tuple[TextIO, NppesContainerIdentity]]:
        entry = self.inputs[source_id]
        if entry.schema_id != "nppes_zip":
            raise IntegrityError("NPPES source must use nppes_zip schema")
        gate = self.expectation("nppes_container_gate", dict)
        _require_exact_keys(
            gate,
            (
                "candidate_manifest_source",
                "approval_source",
                "approval_trust_root_source",
                "container_receipt_source",
                "selected_member_filename",
            ),
            "NPPES container-gate expectation",
        )
        candidate_source = gate["candidate_manifest_source"]
        approval_source = gate["approval_source"]
        trust_root_source = gate["approval_trust_root_source"]
        container_receipt_source = gate["container_receipt_source"]
        selected_member = gate["selected_member_filename"]
        if not all(isinstance(value, str) and value for value in gate.values()):
            raise IntegrityError("NPPES container-gate values must be nonblank strings")
        self.require_sources(
            (
                candidate_source,
                approval_source,
                trust_root_source,
                container_receipt_source,
            )
        )
        if (
            self.inputs[candidate_source].schema_id != "zip_candidate_manifest_json"
            or self.inputs[approval_source].schema_id != "zip_manifest_approval_json"
            or self.inputs[trust_root_source].schema_id
            != "zip_approval_trust_root_json"
            or self.inputs[container_receipt_source].schema_id
            != "nppes_container_receipt_json"
        ):
            raise IntegrityError("NPPES container-gate sources use the wrong schemas")

        candidate_value = _load_json_bytes(self.verify_file(candidate_source).read_bytes())
        if not isinstance(candidate_value, dict):
            raise IntegrityError("ZIP candidate manifest must be an object")
        _require_exact_keys(
            candidate_value,
            ("source_id", "archive_bytes", "archive_sha256", "members"),
            "ZIP candidate manifest",
        )
        raw_members = candidate_value["members"]
        if not isinstance(raw_members, list):
            raise IntegrityError("ZIP candidate members must be an array")
        members = []
        member_fields = tuple(ZipMemberIdentity.__dataclass_fields__)
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                raise IntegrityError("ZIP candidate member must be an object")
            _require_exact_keys(raw_member, member_fields, "ZIP candidate member")
            members.append(ZipMemberIdentity(**raw_member))
        candidate = ZipCandidateManifest(
            candidate_value["source_id"],
            candidate_value["archive_bytes"],
            candidate_value["archive_sha256"],
            tuple(members),
        )
        approval_body = self.verify_file(approval_source).read_bytes()
        trust_root_body = self.verify_file(trust_root_source).read_bytes()
        container_receipt_body = self.verify_file(container_receipt_source).read_bytes()
        container_value = require_canonical_json(
            container_receipt_body, label="NPPES Phase-2D-I container receipt"
        )
        if not isinstance(container_value, dict):
            raise IntegrityError("NPPES Phase-2D-I container receipt must be an object")
        if container_value.get("source_tree_id") != self.source_tree_id:
            raise IntegrityError("NPPES container receipt uses a different source tree")
        container_receipt = validate_control_receipt(
            "nppes_container",
            container_receipt_body,
            context=ReceiptContext(
                container_value.get("source_tree_id"),
                container_value.get("bundle_manifest_sha256"),
                container_value.get("authorization_id"),
            ),
        )
        container_evidence = container_receipt["evidence"]
        trusted_approval = authorize_zip_approval(
            candidate,
            approval_body,
            trust_root_body,
            independently_approved_trust_root_sha256=container_evidence[
                "approval_trust_root_sha256"
            ],
            source_tree_id=container_receipt["source_tree_id"],
            bundle_manifest_sha256=container_receipt["bundle_manifest_sha256"],
            authorization_id=container_receipt["authorization_id"],
        )
        if selected_member != trusted_approval.approval.selected_member_filename:
            raise IntegrityError(
                "D-II NPPES member differs from the externally trusted approval"
            )
        path = self.verify_file(source_id)
        if candidate.source_id != source_id:
            raise IntegrityError("ZIP candidate source ID does not equal NPPES bundle role")
        verified = verify_zip_after_approval(path, candidate, trusted_approval)
        if selected_member not in {
            member.filename for member in verified.members if not member.is_directory
        }:
            raise IntegrityError("approved NPPES analytical member is absent")
        import io

        member_hashes = dict(verified.member_sha256s)
        selected_identity = next(
            member
            for member in verified.members
            if not member.is_directory and member.filename == selected_member
        )
        selected_metadata_sha256 = zip_member_central_metadata_sha256(
            selected_identity
        )
        if (
            container_evidence["archive_sha256"] != verified.archive_sha256
            or container_evidence["candidate_manifest_sha256"]
            != verified.candidate_manifest_sha256
            or container_evidence["approval_id"] != verified.approval_id
            or container_evidence["approval_receipt_sha256"]
            != hashlib.sha256(approval_body).hexdigest()
            or container_evidence["selected_member_filename"] != selected_member
            or container_evidence["selected_member_central_metadata_sha256"]
            != selected_metadata_sha256
            or container_evidence["selected_member_bytes"]
            != selected_identity.uncompressed_bytes
            or container_evidence["selected_member_sha256"]
            != member_hashes[selected_member]
            or container_evidence["member_count"] != len(verified.members)
        ):
            raise IntegrityError(
                "NPPES Phase-2D-I receipt differs from the reverified D-II container"
            )
        identity = NppesContainerIdentity(
            verified.archive_sha256,
            verified.candidate_manifest_sha256,
            hashlib.sha256(self.verify_file(approval_source).read_bytes()).hexdigest(),
            container_evidence["approval_trust_root_sha256"],
            selected_member,
            member_hashes[selected_member],
        )
        with open_verified_zip_member(verified, selected_member) as binary:
            with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as text:
                yield text, identity

    @contextmanager
    def open_nppes_member(self, source_id: str) -> Iterator[TextIO]:
        with self.open_nppes_member_with_identity(source_id) as opened:
            yield opened[0]

    def validate_registered_schema(self, source_id: str) -> None:
        entry = self.inputs[source_id]
        if entry.schema_id == "nppes_zip":
            with self.open_nppes_member(source_id) as handle:
                reader = csv.reader(handle)
                try:
                    header = next(reader)
                except (StopIteration, csv.Error) as exc:
                    raise IntegrityError("NPPES member has no parseable header") from exc
                missing = [field for field in NPPES_ANALYTICAL_FIELDS if field not in header]
                if missing:
                    raise IntegrityError(f"NPPES member missing columns: {missing}")
            return
        if entry.schema_id in SCHEMA_REQUIRED_COLUMNS:
            for _row in self.iter_csv(source_id):
                pass
            return
        if entry.schema_id in SCHEMA_JSONL_KEYS:
            for row in self.iter_jsonl(source_id):
                if entry.schema_id == "nppes_selected_jsonl":
                    source_row = row["source_row"]
                    if not isinstance(source_row, dict) or tuple(source_row) != tuple(
                        NPPES_ANALYTICAL_FIELDS
                    ):
                        raise IntegrityError("selected NPPES source-row schema/order mismatch")
            return
        if entry.schema_id == "geocoder_request_csv":
            path = self.verify_file(source_id)
            with path.open("r", encoding="utf-8", newline="") as handle:
                try:
                    rows = list(csv.reader(handle))
                except csv.Error as exc:
                    raise IntegrityError("geocoder request CSV is malformed") from exc
            if not rows or any(len(row) != 5 or not row[0] or not row[1] for row in rows):
                raise IntegrityError("geocoder request CSV row contract failed")
            return
        if entry.schema_id == "geocoder_attempts_manifest_json":
            from .geocoder_provenance import ATTEMPT_MANIFEST_KEYS

            body = self.verify_file(source_id).read_bytes()
            value = _load_json_bytes(body)
            if not isinstance(value, list) or not value or body != canonical_json_bytes(value):
                raise IntegrityError("Census attempts manifest is empty or noncanonical")
            previous: tuple[int, int] | None = None
            for row in value:
                if not isinstance(row, dict):
                    raise IntegrityError("Census attempt-manifest row must be an object")
                _require_exact_keys(row, ATTEMPT_MANIFEST_KEYS, "Census attempt-manifest row")
                if (
                    type(row["chunk_index"]) is not int or row["chunk_index"] < 1
                    or type(row["attempt"]) is not int or not 1 <= row["attempt"] <= 3
                    or type(row["accepted"]) is not bool
                    or type(row["metadata_bytes"]) is not int or row["metadata_bytes"] <= 0
                    or type(row["response_bytes"]) is not int or row["response_bytes"] < 0
                    or not isinstance(row["request_csv_sha256"], str)
                    or _SHA256.fullmatch(row["request_csv_sha256"]) is None
                    or not isinstance(row["metadata_sha256"], str)
                    or _SHA256.fullmatch(row["metadata_sha256"]) is None
                ):
                    raise IntegrityError("Census attempt-manifest scalar contract failed")
                validate_safe_relative_posix(
                    row["metadata_filename"], label="Census metadata filename"
                )
                if row["response_filename"] is not None:
                    validate_safe_relative_posix(
                        row["response_filename"], label="Census response filename"
                    )
                response_sha = row["response_sha256"]
                if (row["response_bytes"] == 0) != (row["response_filename"] is None):
                    raise IntegrityError("Census response byte/filename fields are incoherent")
                if row["response_bytes"] == 0:
                    if response_sha not in {
                        None, hashlib.sha256(b"").hexdigest()
                    }:
                        raise IntegrityError("zero-byte Census response hash is incoherent")
                elif not isinstance(response_sha, str) or _SHA256.fullmatch(response_sha) is None:
                    raise IntegrityError("Census response hash is invalid")
                position = (row["chunk_index"], row["attempt"])
                if previous is not None and position <= previous:
                    raise IntegrityError("Census attempts are not globally ordered/unique")
                previous = position
            return
        if entry.schema_id == "geocoder_attempt_json":
            from .geocoder_provenance import ATTEMPT_METADATA_KEYS

            body = self.verify_file(source_id).read_bytes()
            value = _load_json_bytes(body)
            if not isinstance(value, dict) or body != canonical_json_bytes(value):
                raise IntegrityError("Census attempt metadata is not canonical JSON")
            _require_exact_keys(value, ATTEMPT_METADATA_KEYS, "Census attempt metadata")
            if (
                value["request_method"] != "POST"
                or type(value["attempt"]) is not int or not 1 <= value["attempt"] <= 3
                or type(value["prepared_body_bytes"]) is not int
                or value["prepared_body_bytes"] <= 0
                or type(value["response_bytes"]) is not int or value["response_bytes"] < 0
                or not isinstance(value["request_headers"], list)
                or not isinstance(value["redirect_chain"], list)
                or not isinstance(value["response_headers"], dict)
            ):
                raise IntegrityError("Census attempt metadata scalar/collection contract failed")
            for field in ("request_csv_sha256", "prepared_body_sha256"):
                if not isinstance(value[field], str) or _SHA256.fullmatch(value[field]) is None:
                    raise IntegrityError(f"Census attempt metadata {field} is invalid")
            return
        if entry.schema_id == "geocoder_response_bytes":
            if self.verify_file(source_id).stat().st_size <= 0:
                raise IntegrityError("Census response snapshot is empty")
            return
        if entry.schema_id in CONTROL_RECEIPT_SCHEMAS:
            body = self.verify_file(source_id).read_bytes()
            value = _load_json_bytes(body)
            if not isinstance(value, dict):
                raise IntegrityError("control receipt is not an object")
            validate_control_receipt(
                CONTROL_RECEIPT_SCHEMAS[entry.schema_id],
                body,
                context=ReceiptContext(
                    value.get("source_tree_id"),
                    value.get("bundle_manifest_sha256"),
                    value.get("authorization_id"),
                ),
            )
            return
        if entry.schema_id == "source_acquisition_receipt_json":
            body = self.verify_file(source_id).read_bytes()
            value = require_canonical_json(body, label="stream acquisition receipt")
            fields = (
                "schema_version", "receipt_kind", "source_id", "release",
                "request_method", "requested_url", "request_headers",
                "range_request", "started_at_utc", "ended_at_utc",
                "redirect_chain", "final_url", "http_status", "content_type",
                "content_encoding", "content_range", "segment_start",
                "segment_end", "representation_total_bytes", "segment_bytes",
                "segment_sha256", "response_bytes", "response_sha256", "result_state",
                "rejection_reason", "final_bytes", "final_sha256",
            )
            if not isinstance(value, dict):
                raise IntegrityError("stream acquisition receipt is not an object")
            _require_exact_keys(value, fields, "stream acquisition receipt")
            headers = value["request_headers"]
            redirects = value["redirect_chain"]
            if (
                not isinstance(headers, list)
                or any(
                    not isinstance(row, list)
                    or len(row) != 2
                    or not all(isinstance(item, str) for item in row)
                    for row in headers
                )
                or not isinstance(redirects, list)
                or not all(isinstance(item, str) for item in redirects)
                or value["receipt_kind"] not in {
                    "attempt", "local_import", "cache_validation"
                }
                or type(value["response_bytes"]) is not int
                or value["response_bytes"] < 0
                or type(value["segment_bytes"]) is not int
                or value["segment_bytes"] < 0
                or value["segment_bytes"] != value["response_bytes"]
                or value["segment_sha256"] != value["response_sha256"]
            ):
                raise IntegrityError("source acquisition receipt collections are malformed")
            return
        if entry.schema_id == "hud_api_raw_json":
            from .hud_surface import validate_hud_api_envelope

            validate_hud_api_envelope(self.verify_file(source_id))
            return
        if entry.schema_id == "hud_api_acquisition_receipt_json":
            body = self.verify_file(source_id).read_bytes()
            try:
                value = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise IntegrityError("HUD API acquisition receipt is not UTF-8 JSON") from exc
            if not isinstance(value, dict) or not value:
                raise IntegrityError("HUD API acquisition receipt is empty/wrong shape")
            return
        if entry.schema_id == "hud_api_excluded_rows_json":
            body = self.verify_file(source_id).read_bytes()
            value = require_canonical_json(body, label="HUD API excluded-row ledger")
            if not isinstance(value, dict):
                raise IntegrityError("HUD API excluded-row ledger must be an object")
            _require_exact_keys(value, ("rows",), "HUD API excluded-row ledger")
            rows = value["rows"]
            fields = (
                "CLASSIFICATION", "ZIP", "COUNTY", "BUS_RATIO", "RES_RATIO",
                "TOT_RATIO", "OTH_RATIO", "USPS_ZIP_PREF_CITY",
                "USPS_ZIP_PREF_STATE",
            )
            if not isinstance(rows, list) or not rows:
                raise IntegrityError("HUD API excluded-row ledger has no rows")
            for row in rows:
                if not isinstance(row, dict):
                    raise IntegrityError("HUD API excluded ledger row is not an object")
                _require_exact_keys(row, fields, "HUD API excluded ledger row")
                if row["CLASSIFICATION"] not in {
                    "EXCLUDED_TERRITORY_COUNTY", "EXCLUDED_SPECIAL_NONCOUNTY"
                } or any(not isinstance(value, str) for value in row.values()):
                    raise IntegrityError("HUD API excluded ledger row is malformed")
            return
        if entry.schema_id in {
            "tiger_zip", "tiger_raw_zip", "gazetteer_raw_zip", "hud_raw_xlsx"
        }:
            members = _read_zip_central_directory(self.verify_file(source_id))
            if not members:
                raise IntegrityError("approved ZIP/XLSX central directory is empty")
            return
        if entry.schema_id == "zip_candidate_manifest_json":
            body = self.verify_file(source_id).read_bytes()
            value = _load_json_bytes(body)
            if not isinstance(value, dict):
                raise IntegrityError("ZIP candidate manifest is not an object")
            _require_exact_keys(
                value, ("source_id", "archive_bytes", "archive_sha256", "members"),
                "ZIP candidate manifest",
            )
            if not isinstance(value["members"], list):
                raise IntegrityError("ZIP candidate manifest members are not an array")
            members = []
            fields = tuple(ZipMemberIdentity.__dataclass_fields__)
            for row in value["members"]:
                if not isinstance(row, dict):
                    raise IntegrityError("ZIP candidate member is not an object")
                _require_exact_keys(row, fields, "ZIP candidate member")
                members.append(ZipMemberIdentity(**row))
            candidate = ZipCandidateManifest(
                value["source_id"], value["archive_bytes"], value["archive_sha256"],
                tuple(members),
            )
            if body != canonical_zip_candidate_manifest_bytes(candidate):
                raise IntegrityError("ZIP candidate manifest is not canonical")
            return
        if entry.schema_id == "zip_manifest_approval_json":
            body = self.verify_file(source_id).read_bytes()
            value = _load_json_bytes(body)
            fields = tuple(ZipManifestApproval.__dataclass_fields__)
            if not isinstance(value, dict):
                raise IntegrityError("ZIP approval is not an object")
            _require_exact_keys(value, fields, "ZIP manifest approval")
            approval = ZipManifestApproval(**value)
            approval.validate()
            if body != canonical_json_bytes(value):
                raise IntegrityError("ZIP manifest approval is not canonical")
            return
        if entry.schema_id in {
            "nppes_audit_json", "request_manifest_json", "approval_receipt_json",
            "build_receipt_json", "places_derivation_handoff_json",
            "hud_data_handoff_json", "hud_compatibility_json",
            "hud_api_data_handoff_json", "hud_api_compatibility_json",
            "zip_approval_trust_root_json", "nppes_container_receipt_json",
            "gazetteer_extraction_receipt_json",
            "validation_completion_receipt_json",
        }:
            body = self.verify_file(source_id).read_bytes()
            value = _load_json_bytes(body)
            if (
                not isinstance(value, (dict, list))
                or not value
                or body != canonical_json_bytes(value)
            ):
                raise IntegrityError(f"strict JSON source is empty/wrong shape: {source_id}")
            return
        if entry.schema_id == "topology_json":
            value = _load_json_bytes(self.verify_file(source_id).read_bytes())
            if (
                not isinstance(value, dict)
                or value.get("type") != "Topology"
                or not isinstance(value.get("objects"), dict)
                or not value["objects"]
            ):
                raise IntegrityError(f"invalid Topology JSON source: {source_id}")
            return
        raise IntegrityError(f"unknown schema_id: {entry.schema_id}")

    def validate_all_schemas(self) -> None:
        for source_id in sorted(self.inputs):
            self.validate_registered_schema(source_id)

    def frozen_inputs(self) -> tuple[FrozenInput, ...]:
        def artifact_contract(entry: BundleInput) -> tuple[str, tuple[str, ...]]:
            if entry.schema_id == "nppes_zip":
                return "ZIP", ("nppes-approved-zip",)
            if entry.schema_id in {
                "tiger_zip", "tiger_raw_zip", "gazetteer_raw_zip"
            }:
                return "ZIP", (entry.schema_id.replace("_", "-"),)
            if entry.schema_id == "hud_api_raw_json":
                return "JSON", ("hud-api-2025q4-json",)
            if entry.schema_id.endswith("_csv"):
                return "CSV", (entry.schema_id.replace("_", "-"),)
            if entry.schema_id.endswith("_jsonl") or entry.schema_id.endswith("_json"):
                return "JSON", (entry.schema_id.replace("_", "-"),)
            if entry.schema_id == "hud_raw_xlsx":
                return "XLSX", ("hud-2025q4-xlsx",)
            if entry.schema_id.endswith("_xlsx"):
                return "XLSX", (entry.schema_id.replace("_", "-"),)
            return "BINARY", (entry.schema_id.replace("_", "-"),)

        return tuple(
            FrozenInput(
                source_id=entry.source_id,
                release=entry.release,
                url=entry.url,
                filename=Path(entry.relative_path).name,
                bytes=entry.bytes,
                sha256=entry.sha256,
                allowed_hosts=(urlparse(entry.url).hostname or "",),
                artifact_type=artifact_contract(entry)[0],
                validator_ids=artifact_contract(entry)[1],
                content_types=(entry.content_type,),
            )
            for entry in sorted(self.inputs.values(), key=lambda item: item.source_id)
        )


@dataclass(frozen=True)
class OutputArtifact:
    artifact_id: str
    relative_path: str
    bytes: int
    sha256: str


def write_output_receipt(
    output_root: Path,
    *,
    command: str,
    bundle: FrozenBundle,
    artifacts: Mapping[str, Path],
    completed_at_utc: str,
) -> Path:
    _validate_utc_z(completed_at_utc, "completed_at_utc")
    validate_safe_id(command, label="output receipt command")
    lexical_root = output_root.absolute()
    output_root = output_root.resolve()
    rows = []
    for artifact_id, path in sorted(artifacts.items()):
        validate_safe_id(artifact_id, label="output artifact ID")
        lexical = path.absolute()
        try:
            relative_text = lexical.relative_to(lexical_root).as_posix()
        except ValueError as exc:
            try:
                relative_text = lexical.resolve(strict=True).relative_to(output_root).as_posix()
            except ValueError:
                raise IntegrityError("output receipt artifact escapes output root") from exc
        relative = validate_safe_relative_posix(
            relative_text, label="output artifact relative path"
        )
        resolved = resolve_regular_contained(output_root, relative, label="output artifact")
        if resolved != lexical.resolve(strict=True):
            raise IntegrityError("output artifact lexical path uses a symlink")
        rows.append(
            {
                "artifact_id": artifact_id,
                "relative_path": relative.as_posix(),
                "bytes": resolved.stat().st_size,
                "sha256": sha256_file(resolved),
            }
        )
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "command": command,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "authorization_id": bundle.authorization_id,
        "source_tree_id": bundle.source_tree_id,
    }
    method_lineage_id = bundle.expectations.get("method_lineage_id")
    if method_lineage_id is not None:
        if method_lineage_id != "ATLAS-V19-AMENDMENT03-R4":
            raise IntegrityError("output receipt rejects unknown/mixed method lineage")
        receipt["method_lineage_id"] = method_lineage_id
    receipt["completed_at_utc"] = completed_at_utc
    receipt["artifacts"] = rows
    path = output_root / "receipt.json"
    atomic_write(path, canonical_json_bytes(receipt))
    return path


def verify_output_receipt(
    receipt_path: Path,
    *,
    bundle: FrozenBundle,
    artifact_root: Path,
    required_artifacts: Iterable[str],
    expected_command: str,
) -> Mapping[str, object]:
    lexical_root = artifact_root.absolute()
    artifact_root = artifact_root.resolve(strict=True)
    validate_safe_id(expected_command, label="expected output command")
    receipt_lexical = receipt_path.absolute()
    try:
        receipt_relative_text = receipt_lexical.relative_to(lexical_root).as_posix()
    except ValueError as exc:
        try:
            receipt_relative_text = receipt_lexical.resolve(strict=True).relative_to(
                artifact_root
            ).as_posix()
        except ValueError:
            raise IntegrityError("output receipt escapes artifact root") from exc
    receipt_relative = validate_safe_relative_posix(
        receipt_relative_text, label="output receipt path"
    )
    verified_receipt = resolve_regular_contained(
        artifact_root, receipt_relative, label="output receipt"
    )
    if verified_receipt != receipt_lexical.resolve(strict=True):
        raise IntegrityError("output receipt lexical path uses a symlink")
    receipt_body = verified_receipt.read_bytes()
    value = _load_json_bytes(receipt_body)
    if not isinstance(value, dict):
        raise IntegrityError("output receipt must be an object")
    if receipt_body != canonical_json_bytes(value):
        raise IntegrityError("output receipt is not canonical UTF-8/LF JSON")
    expected_keys = (
        "schema_version", "command", "bundle_manifest_sha256",
        "authorization_id", "source_tree_id",
    )
    method_lineage_id = bundle.expectations.get("method_lineage_id")
    if method_lineage_id is not None:
        expected_keys += ("method_lineage_id",)
    expected_keys += ("completed_at_utc", "artifacts")
    _require_exact_keys(value, expected_keys, "output receipt")
    if (
        value["schema_version"] != RECEIPT_SCHEMA_VERSION
        or value["command"] != expected_command
        or value["bundle_manifest_sha256"] != bundle.manifest_sha256
        or value["authorization_id"] != bundle.authorization_id
        or value["source_tree_id"] != bundle.source_tree_id
        or method_lineage_id is not None
        and (
            method_lineage_id != "ATLAS-V19-AMENDMENT03-R4"
            or value["method_lineage_id"] != method_lineage_id
        )
    ):
        raise IntegrityError("output receipt is not bound to the approved bundle")
    _validate_utc_z(value["completed_at_utc"], "receipt completed_at_utc")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list):
        raise IntegrityError("receipt artifacts must be an array")
    required = tuple(required_artifacts)
    if len(required) != len(set(required)):
        raise IntegrityError("required output artifact IDs are duplicated")
    for artifact_id in required:
        validate_safe_id(artifact_id, label="required output artifact ID")
    seen: set[str] = set()
    seen_paths: set[str] = set()
    for row in artifacts:
        if not isinstance(row, dict):
            raise IntegrityError("receipt artifact must be an object")
        _require_exact_keys(
            row, ("artifact_id", "relative_path", "bytes", "sha256"), "receipt artifact"
        )
        artifact_id = row["artifact_id"]
        validate_safe_id(artifact_id, label="receipt artifact ID")
        if artifact_id in seen:
            raise IntegrityError("duplicate receipt artifact ID")
        seen.add(artifact_id)
        relative = validate_safe_relative_posix(
            row["relative_path"], label="receipt artifact path"
        )
        if relative.as_posix() in seen_paths:
            raise IntegrityError("duplicate receipt artifact path")
        seen_paths.add(relative.as_posix())
        if (
            type(row["bytes"]) is not int
            or row["bytes"] <= 0
            or not isinstance(row["sha256"], str)
            or _SHA256.fullmatch(row["sha256"]) is None
        ):
            raise IntegrityError("receipt artifact byte/hash identity is invalid")
        path = resolve_regular_contained(artifact_root, relative, label="receipt artifact")
        if (
            path.stat().st_size != row["bytes"]
            or sha256_file(path) != row["sha256"]
        ):
            raise IntegrityError(f"receipt artifact bytes/hash mismatch: {artifact_id}")
    if seen != set(required):
        raise IntegrityError("receipt artifact set does not equal required outputs")
    return value
