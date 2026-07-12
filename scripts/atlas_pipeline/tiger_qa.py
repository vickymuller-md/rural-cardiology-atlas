"""Frozen full-resolution TIGER/Line county source validation for Gate-B QA.

The analytical county universe is never copied from a receipt.  It is derived
again from the approved archive's DBF records and then compared byte-for-byte
with the Gazetteer universe used by the analytical build.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Mapping

from .acquisition import (
    VerifiedZipArchive,
    ZipCandidateManifest,
    ZipMemberIdentity,
    ZipManifestApproval,
    canonical_zip_candidate_manifest_bytes,
    open_verified_zip_member,
    verify_zip_after_approval,
)
from .bundle import (
    FrozenBundle,
    _load_json_bytes,
    _require_exact_keys,
    canonical_json_bytes,
)
from .contracts import FIPS_TO_STATE
from .errors import IntegrityError
from .geoqa import GeometryEngineIdentity, PolygonSource, sorted_fips_bytes

TIGER_SOURCE_ID = "CENSUS-TIGER-COUNTY-2025"
TIGER_ARCHIVE_BYTES = 83_989_800
TIGER_ARCHIVE_SHA256 = "9c6e9d9076abce2670d1de255de3710c35ecca00a7005d88e012dec52d95f763"
TIGER_MEMBER_STEM = "tl_2025_us_county"
TIGER_REQUIRED_SIDECARS = frozenset({".cpg", ".dbf", ".prj", ".shp", ".shx"})


@dataclass(frozen=True)
class TigerQaBinding:
    polygon_source: PolygonSource
    candidate_manifest_sha256: str
    selected_member_set_sha256: str
    county_fips_set_sha256: str
    geometry_engine_sha256: str
    loader_sha256: str
    point_qa_worksheet_sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _selected_members(archive: VerifiedZipArchive) -> dict[str, str]:
    selected: dict[str, str] = {}
    for member in archive.members:
        if member.is_directory:
            continue
        name = Path(member.filename).name
        suffix = Path(name).suffix.lower()
        if name.lower() == f"{TIGER_MEMBER_STEM}{suffix}" and suffix in TIGER_REQUIRED_SIDECARS:
            if suffix in selected:
                raise IntegrityError(f"TIGER archive has duplicate {suffix} sidecars")
            selected[suffix] = member.filename
    if set(selected) != TIGER_REQUIRED_SIDECARS:
        raise IntegrityError("TIGER archive lacks the exact required shapefile sidecars")
    return selected


def _read_exact(handle: BinaryIO, count: int, label: str) -> bytes:
    body = handle.read(count)
    if len(body) != count:
        raise IntegrityError(f"TIGER DBF is truncated while reading {label}")
    return body


def _dbf_county_fips(handle: BinaryIO) -> frozenset[str]:
    fixed = _read_exact(handle, 32, "header")
    if fixed[0] not in {0x03, 0x30, 0x31, 0x32}:
        raise IntegrityError("TIGER DBF uses an unsupported format/version")
    record_count = struct.unpack_from("<I", fixed, 4)[0]
    header_bytes = struct.unpack_from("<H", fixed, 8)[0]
    record_bytes = struct.unpack_from("<H", fixed, 10)[0]
    if record_count <= 0 or header_bytes < 65 or record_bytes <= 1:
        raise IntegrityError("TIGER DBF header counts are invalid")
    descriptor_bytes = header_bytes - 33
    if descriptor_bytes % 32:
        raise IntegrityError("TIGER DBF field descriptor table is malformed")
    descriptors = _read_exact(handle, descriptor_bytes, "field descriptors")
    if _read_exact(handle, 1, "header terminator") != b"\r":
        raise IntegrityError("TIGER DBF lacks its header terminator")

    fields: dict[str, tuple[int, int, str, int]] = {}
    offset = 1
    for index in range(0, len(descriptors), 32):
        descriptor = descriptors[index : index + 32]
        try:
            name = descriptor[:11].split(b"\0", 1)[0].decode("ascii")
            field_type = chr(descriptor[11])
        except (UnicodeDecodeError, ValueError) as exc:
            raise IntegrityError("TIGER DBF field descriptor is not ASCII") from exc
        length = descriptor[16]
        decimals = descriptor[17]
        if not name or name in fields or length <= 0:
            raise IntegrityError("TIGER DBF field names/widths are invalid or duplicated")
        fields[name] = (offset, length, field_type, decimals)
        offset += length
    if offset != record_bytes:
        raise IntegrityError("TIGER DBF declared record width differs from its fields")
    for name, width in (("STATEFP", 2), ("COUNTYFP", 3), ("GEOID", 5)):
        if fields.get(name) != (
            fields.get(name, (None,))[0], width, "C", 0
        ):
            raise IntegrityError(f"TIGER DBF {name} schema is not character width {width}")

    observed: set[str] = set()
    for row_number in range(1, record_count + 1):
        row = _read_exact(handle, record_bytes, f"record {row_number}")
        if row[:1] != b" ":
            raise IntegrityError("TIGER DBF contains a deleted/invalid county record")

        def field(name: str) -> str:
            start, length, _kind, _decimals = fields[name]
            try:
                return row[start : start + length].decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise IntegrityError(f"TIGER DBF {name} is not ASCII") from exc

        state = field("STATEFP")
        county = field("COUNTYFP")
        geoid = field("GEOID")
        if (
            len(state) != 2
            or len(county) != 3
            or len(geoid) != 5
            or not (state + county).isdigit()
            or geoid != state + county
        ):
            raise IntegrityError("TIGER DBF state/county/GEOID identity is malformed")
        if state in FIPS_TO_STATE:
            if geoid in observed:
                raise IntegrityError("TIGER DBF contains a duplicate in-universe GEOID")
            observed.add(geoid)
    trailing = handle.read(2)
    if trailing not in {b"", b"\x1a", b"\x1a\x00"}:
        raise IntegrityError("TIGER DBF has unexpected trailing bytes")
    if not observed:
        raise IntegrityError("TIGER DBF retained no 50-state/DC county records")
    return frozenset(observed)


def build_tiger_qa_binding(
    archive_path: Path,
    candidate: ZipCandidateManifest,
    approval: ZipManifestApproval,
    *,
    county_universe: set[str],
    geometry_engine: GeometryEngineIdentity,
    receipt_evidence: Mapping[str, object],
    expected_archive_bytes: int = TIGER_ARCHIVE_BYTES,
    expected_archive_sha256: str = TIGER_ARCHIVE_SHA256,
) -> TigerQaBinding:
    """Verify the approved raw archive and derive its real county FIPS set."""

    if (
        candidate.source_id != TIGER_SOURCE_ID
        or candidate.archive_bytes != expected_archive_bytes
        or candidate.archive_sha256 != expected_archive_sha256
    ):
        raise IntegrityError("TIGER archive identity differs from the frozen source contract")
    if archive_path.stat().st_size != expected_archive_bytes or _sha256_file(archive_path) != expected_archive_sha256:
        raise IntegrityError("TIGER archive bytes/hash differ from the frozen source contract")
    verified = verify_zip_after_approval(archive_path, candidate, approval)
    selected = _selected_members(verified)
    selected_bytes = ("\n".join(sorted(selected.values())) + "\n").encode("utf-8")
    selected_hash = hashlib.sha256(selected_bytes).hexdigest()
    with open_verified_zip_member(verified, selected[".dbf"]) as dbf:
        derived_fips = _dbf_county_fips(dbf)
    if set(derived_fips) != county_universe:
        raise IntegrityError("TIGER DBF-derived FIPS set differs from Gazetteer universe")

    candidate_hash = hashlib.sha256(
        canonical_zip_candidate_manifest_bytes(candidate)
    ).hexdigest()
    fips_hash = hashlib.sha256(sorted_fips_bytes(derived_fips)).hexdigest()
    engine_hash = hashlib.sha256(canonical_json_bytes(asdict(geometry_engine))).hexdigest()
    loader_hash = _sha256_file(Path(__file__))
    required = {
        "raw_zip_sha256": expected_archive_sha256,
        "member_manifest_sha256": candidate_hash,
        "selected_member_set_sha256": selected_hash,
        "county_fips_set_sha256": fips_hash,
        "resolution": "FULL_RESOLUTION",
        "geometry_engine_sha256": engine_hash,
        "loader_sha256": loader_hash,
    }
    for key, expected in required.items():
        if receipt_evidence.get(key) != expected:
            raise IntegrityError(f"TIGER materialization receipt binding mismatch: {key}")
    worksheet_hash = receipt_evidence.get("point_qa_worksheet_sha256")
    if not isinstance(worksheet_hash, str) or len(worksheet_hash) != 64:
        raise IntegrityError("TIGER materialization receipt lacks the point-QA worksheet hash")
    geometry_engine.validate()
    polygon = PolygonSource(
        TIGER_SOURCE_ID,
        "FULL_RESOLUTION",
        derived_fips,
        expected_archive_sha256,
    )
    polygon.validate_for_point_qa(county_universe)
    return TigerQaBinding(
        polygon,
        candidate_hash,
        selected_hash,
        fips_hash,
        engine_hash,
        loader_hash,
        worksheet_hash,
    )


def load_tiger_qa_binding(
    bundle: FrozenBundle,
    *,
    raw_zip_source: str,
    candidate_manifest_source: str,
    approval_source: str,
    county_universe: set[str],
    geometry_engine: GeometryEngineIdentity,
    receipt_evidence: Mapping[str, object],
) -> TigerQaBinding:
    """Load the three required Phase-2F TIGER roles and validate their chain."""

    expected_schemas = {
        raw_zip_source: "tiger_raw_zip",
        candidate_manifest_source: "zip_candidate_manifest_json",
        approval_source: "zip_manifest_approval_json",
    }
    bundle.require_sources(expected_schemas)
    for source_id, schema_id in expected_schemas.items():
        if bundle.inputs[source_id].schema_id != schema_id:
            raise IntegrityError(f"TIGER role has wrong schema: {source_id}")

    candidate_body = bundle.verify_file(candidate_manifest_source).read_bytes()
    candidate_value = _load_json_bytes(candidate_body)
    if (
        not isinstance(candidate_value, dict)
        or candidate_body != canonical_json_bytes(candidate_value)
    ):
        raise IntegrityError("TIGER candidate manifest is not canonical JSON")
    _require_exact_keys(
        candidate_value,
        ("source_id", "archive_bytes", "archive_sha256", "members"),
        "TIGER candidate manifest",
    )
    if not isinstance(candidate_value["members"], list):
        raise IntegrityError("TIGER candidate manifest members must be an array")
    members = []
    member_keys = tuple(ZipMemberIdentity.__dataclass_fields__)
    for row in candidate_value["members"]:
        if not isinstance(row, dict):
            raise IntegrityError("TIGER candidate member must be an object")
        _require_exact_keys(row, member_keys, "TIGER candidate member")
        members.append(ZipMemberIdentity(**row))
    candidate = ZipCandidateManifest(
        candidate_value["source_id"],
        candidate_value["archive_bytes"],
        candidate_value["archive_sha256"],
        tuple(members),
    )

    approval_body = bundle.verify_file(approval_source).read_bytes()
    approval_value = _load_json_bytes(approval_body)
    if (
        not isinstance(approval_value, dict)
        or approval_body != canonical_json_bytes(approval_value)
    ):
        raise IntegrityError("TIGER member-manifest approval is not canonical JSON")
    approval_keys = tuple(ZipManifestApproval.__dataclass_fields__)
    _require_exact_keys(
        approval_value, approval_keys, "TIGER member-manifest approval"
    )
    approval = ZipManifestApproval(**approval_value)
    raw_entry = bundle.inputs[raw_zip_source]
    if (
        raw_entry.source_id != TIGER_SOURCE_ID
        or raw_entry.bytes != TIGER_ARCHIVE_BYTES
        or raw_entry.sha256 != TIGER_ARCHIVE_SHA256
    ):
        raise IntegrityError("TIGER raw role is not the preregistered source")
    return build_tiger_qa_binding(
        bundle.verify_file(raw_zip_source),
        candidate,
        approval,
        county_universe=county_universe,
        geometry_engine=geometry_engine,
        receipt_evidence=receipt_evidence,
    )
