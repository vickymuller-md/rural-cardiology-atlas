"""Immutable release-source identities and validator registry.

Release executors resolve validators here from the frozen ``source_id``.  A
caller can supply immutable data (for example an approved ZIP-member manifest)
but can never supply executable validation callbacks or substitute a no-op.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .acquisition import (
    ArtifactValidator,
    FrozenInput,
    TrustedZipApproval,
    ZipCandidateManifest,
    _read_zip_central_directory,
    csv_artifact_validator,
    json_artifact_validator,
    xlsx_artifact_validator,
)
from .contracts import HOSPITAL_FIELDS
from .errors import IntegrityError
from .places import PLACES_REQUIRED_FIELDS, canonical_places_query_url


@dataclass(frozen=True)
class SourcePolicy:
    source_id: str
    release: str
    url: str
    filename: str
    bytes: int | None
    sha256: str | None
    artifact_type: str
    validator_ids: tuple[str, ...]
    content_types: tuple[str, ...]
    magic_hex: str | None = None


@dataclass(frozen=True)
class ReleaseValidationContext:
    candidate_manifest: ZipCandidateManifest | None = None
    manifest_approval: TrustedZipApproval | None = None
    required_sheet_names: tuple[str, ...] = ()


HUD_HEADERS = (
    "ZIP",
    "COUNTY",
    "BUS_RATIO",
    "RES_RATIO",
    "TOT_RATIO",
    "USPS_ZIP_PREF_STATE",
)

HUD_API_HEADERS = (
    "ZIP",
    "COUNTY",
    "BUS_RATIO",
    "RES_RATIO",
    "TOT_RATIO",
    "OTH_RATIO",
    "USPS_ZIP_PREF_CITY",
    "USPS_ZIP_PREF_STATE",
)


def _policy(
    source_id: str,
    release: str,
    url: str,
    filename: str,
    bytes_: int | None,
    sha256: str | None,
    artifact_type: str,
    validator_ids: tuple[str, ...],
    content_types: tuple[str, ...],
    magic_hex: str | None = None,
) -> SourcePolicy:
    return SourcePolicy(
        source_id, release, url, filename, bytes_, sha256, artifact_type,
        validator_ids, content_types, magic_hex,
    )


_POLICIES = (
    # Test-only immutable policy.  No production phase manifest permits this
    # source ID; it exists solely to exercise release executors without remote
    # or identifying data.
    _policy("SYNTHETIC-IMMUTABLE-CSV", "fixture-v1", "https://example.test/synthetic.csv", "synthetic.csv", None, None, "CSV", ("synthetic-immutable-csv",), ("text/csv",)),
    _policy("TAXONOMY-260", "26.0", "https://www.nucc.org/images/stories/CSV/nucc_taxonomy_260.csv", "nucc_taxonomy_260.csv", 529245, "0615cce1dac2fae75e8a0497657fd3ad81f9505c829fb6129a6551583f4fe0fb", "CSV", ("taxonomy-260-csv",), ("text/csv", "application/octet-stream")),
    _policy("NPPES-INDEX-20260709", "2026-07-09", "https://download.cms.gov/nppes/NPI_Files.html", "NPI_Files.html", 7906, "982d7730ec72703062f95629be7be4016b527a6e7b8d5ba32683e518759361da", "TEXT", ("nppes-index-html",), ("text/html",)),
    _policy("NPPES-202606-V2", "2026-06", "https://download.cms.gov/nppes/NPPES_Data_Dissemination_June_2026_V2.zip", "NPPES_Data_Dissemination_June_2026_V2.zip", 1137408535, None, "ZIP", ("nppes-zip-central",), ("application/zip", "application/octet-stream"), "504b"),
    _policy("NPPES-HEADER", "2026-06", "https://www.cms.gov/regulations-and-guidance/administrative-simplification/nationalprovidentstand/downloads/npidatadisseminationfileheader.zip", "npidatadisseminationfileheader.zip", 1531, "b25255bdd3cefc1146ca7bb116408c305b88b223af96cdfe71ea1d911acea1fc", "ZIP", ("zip-central-directory",), ("application/zip", "application/octet-stream"), "504b"),
    _policy("NPPES-README", "2026-06", "https://www.cms.gov/Regulations-and-Guidance/Administrative-Simplification/NationalProvIdentStand/Downloads/Data_Dissemination_File-Readme.pdf", "Data_Dissemination_File-Readme.pdf", 535198, "d8a3fa658800896405ed4b66a94014f1c32d48f4cd5601110a7c5ce80290ad19", "PDF", ("pdf-complete",), ("application/pdf",), "25504446"),
    _policy("GEO-BENCHMARKS", "2026-07-09", "https://geocoding.geo.census.gov/geocoder/benchmarks", "benchmarks.json", 547, "fc858d2ecef3eab9475b0b6c2a1b2e671f0805e1e62ba7d9d1256e3092114106", "JSON", ("geocoder-benchmarks-json",), ("application/json",)),
    _policy("GEO-VINTAGES-8", "2026-07-09", "https://geocoding.geo.census.gov/geocoder/vintages?benchmark=8", "vintages-8.json", 1936, "e41f0067e993b60ff7e3dde3934636b371fcba0b678475e02c7ba9766309bbf6", "JSON", ("geocoder-vintages-json",), ("application/json",)),
    _policy("CMS-HOSP-META-20260428", "2026-04-28", "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/xubh-q36u", "cms-hospital-metastore.json", 1215, "a421368204acb1b91b4074ef797145aac3a11be132ae285730577b151e370cc4", "JSON", ("cms-metastore-json",), ("application/json",)),
    _policy("CMS-HOSP-20260428", "2026-04-28", "https://data.cms.gov/provider-data/sites/default/files/resources/893c372430d9d71a1c52737d01239d47_1777413958/Hospital_General_Information.csv", "Hospital_General_Information.csv", 1453884, "83c98b2e8687580e0482b13e1e9acd5813534be243e5ccd9f55556a869595d40", "CSV", ("cms-hospital-csv",), ("text/csv", "application/octet-stream")),
    _policy("PLACES-META-2025-20251205-STABLE", "2025-2025-12-05", "https://data.cdc.gov/api/views/metadata/v1/swc5-untb", "swc5-untb.metadata.v1.json", 4027, "afe9380ea6f39a630bd634b252f2f43c042470268729268973f99593f4c331c9", "JSON", ("places-metadata-json", "places-metadata-semantic"), ("application/json",)),
    _policy("PLACES-CHD-RAW-2025", "2025", canonical_places_query_url(), "places-chd-ageadjprv-2025.csv", 479685, "db6bf56dc26841b4da9d32adeae0adb1c0d6a37bfe5398fae017bd6e4f06db9c", "CSV", ("places-raw-csv",), ("text/csv", "application/octet-stream")),
    _policy("ACS-POP-2024-5Y", "2024-5Y", "https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/acsdt5y2024-b01003.dat", "acsdt5y2024-b01003.dat", 18313708, "38d1a992bb058d184009b10b9b34987279aee575e4323165cfb5706c69b6ca90", "CSV", ("acs-population-csv",), ("text/plain", "application/octet-stream")),
    _policy("ACS-INCOME-2024-5Y", "2024-5Y", "https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/acsdt5y2024-b19013.dat", "acsdt5y2024-b19013.dat", 17917916, "b25a176b0e6c339b6f3a2a0d3d8446bf06f5f080b4395993ec9a8313efb1c229", "CSV", ("acs-income-csv",), ("text/plain", "application/octet-stream")),
    _policy("COUNTY-GAZ-2025", "2025", "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_counties_national.zip", "2025_Gaz_counties_national.zip", 138993, "4c90d0f805779923b5958ab13d0c1e9b99fe4932b786bfcf75dd739bb2dcb4ea", "ZIP", ("zip-central-directory",), ("application/zip", "application/octet-stream"), "504b"),
    _policy("RUCC-2023", "2023", "https://ers.usda.gov/sites/default/files/_laserfiche/DataFiles/53251/Ruralurbancontinuumcodes2023.csv", "Ruralurbancontinuumcodes2023.csv", 629322, "ec455ee2a8bc5fc8e070575ea5bee7dce46fc6037f8c3449cbf56e8b45331fa7", "CSV", ("rucc-csv",), ("text/csv", "application/octet-stream")),
    _policy("HRSA-HPSA-WAREHOUSE-20260710", "2026-07-10-daily", "https://data.hrsa.gov/DataDownload/DD_Files/BCD_HPSA_FCT_DET_PC.csv", "BCD_HPSA_FCT_DET_PC.csv", 48274995, "c4faad2a8b0b3a3ce93c58ece2f01192b33f6cfec4964a06e9c87d4ba8205f99", "CSV", ("hrsa-hpsa-csv",), ("text/csv", "application/octet-stream")),
    _policy("CENSUS-CB-COUNTY-2025-5M", "2025", "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_county_5m.zip", "cb_2025_us_county_5m.zip", 2983552, "faec522080681e79be5be435c981009a77891206ff8a7f1d142f3bf5da9ebd74", "ZIP", ("zip-central-directory",), ("application/zip", "application/octet-stream"), "504b"),
    _policy("CENSUS-CB-STATE-2025-5M", "2025", "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_5m.zip", "cb_2025_us_state_5m.zip", 1120533, "8a45692bc532dbd38938a1924f445850cef2682ea67d750d7fd2f19cfe836903", "ZIP", ("zip-central-directory",), ("application/zip", "application/octet-stream"), "504b"),
    _policy("CENSUS-TIGER-COUNTY-2025", "2025", "https://www2.census.gov/geo/tiger/TIGER2025/COUNTY/tl_2025_us_county.zip", "tl_2025_us_county.zip", 83989800, "9c6e9d9076abce2670d1de255de3710c35ecca00a7005d88e012dec52d95f763", "ZIP", ("zip-central-directory",), ("application/zip", "application/octet-stream"), "504b"),
    _policy("CENSUS-COUNTY-ADJ-2025", "2025", "https://www2.census.gov/geo/docs/reference/county_adjacency/county_adjacency2025.txt", "county_adjacency2025.txt", 1062648, "8233d2aed342f62eb02bae9f9002ad1d8fc6bd82d739185eba8bad832bf8fc87", "TEXT", ("county-adjacency-text",), ("text/plain", "application/octet-stream")),
    _policy("MAPSHAPER-0.7.41", "0.7.41", "https://registry.npmjs.org/mapshaper/-/mapshaper-0.7.41.tgz", "mapshaper-0.7.41.tgz", 3786772, "770018b5c6fb29e001c52613251de9df618cd9d7a3bf2e947f962749e77c13d5", "BINARY", ("gzip-tar-container",), ("application/octet-stream", "application/gzip"), "1f8b"),
    _policy("HUD-ZIP-COUNTY-2025Q4", "2025-Q4", "https://www.huduser.gov/portal/datasets/usps/ZIP_COUNTY_122025.xlsx", "ZIP_COUNTY_122025.xlsx", None, None, "XLSX", ("hud-2025q4-xlsx",), ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream"), "504b"),
    _policy("HUD-ZIP-COUNTY-API-2025Q4", "2025-Q4", "https://www.huduser.gov/hudapi/public/usps", "HUD_USPS_ZIP_COUNTY_2025Q4_API_RAW.json", 7800489, "72f7c78d1439b6b38c0ebc5a163765c6c0e90caae1bc921e59a0a4a03b99cda4", "JSON", ("hud-api-2025q4-json",), ("application/json",)),
)

SOURCE_POLICIES: Mapping[str, SourcePolicy] = MappingProxyType(
    {policy.source_id: policy for policy in _POLICIES}
)


def validate_source_identity(spec: FrozenInput) -> SourcePolicy:
    try:
        policy = SOURCE_POLICIES[spec.source_id]
    except KeyError as exc:
        raise IntegrityError(f"source is absent from immutable release registry: {spec.source_id}") from exc
    expected = (
        policy.release,
        policy.url,
        policy.filename,
        policy.artifact_type,
        policy.validator_ids,
        policy.content_types,
        policy.magic_hex,
    )
    observed = (
        spec.release,
        spec.url,
        spec.filename,
        spec.artifact_type,
        spec.validator_ids,
        spec.content_types,
        spec.magic_hex,
    )
    if observed != expected:
        raise IntegrityError(f"source identity/validator policy drift: {spec.source_id}")
    if policy.bytes is not None and spec.bytes != policy.bytes:
        raise IntegrityError(f"source byte pin drift: {spec.source_id}")
    if policy.sha256 is not None and spec.sha256 != policy.sha256:
        raise IntegrityError(f"source SHA-256 pin drift: {spec.source_id}")
    if policy.bytes is None and spec.bytes <= 0:
        raise IntegrityError("conditionally pinned source still requires approved positive bytes")
    if policy.sha256 is None and re.fullmatch(r"[0-9a-f]{64}", spec.sha256) is None:
        raise IntegrityError("conditionally pinned source still requires an approved SHA-256")
    return policy


def _simple_validator(
    validator_id: str, artifact_types: tuple[str, ...], callback
) -> ArtifactValidator:
    return ArtifactValidator(validator_id, artifact_types, callback)


def _text_contains(path: Path, needles: tuple[bytes, ...], label: str) -> None:
    body = path.read_bytes()
    if not body or any(needle not in body for needle in needles):
        raise IntegrityError(f"{label} content contract failed")


def _pdf(path: Path) -> None:
    body = path.read_bytes()
    if not body.startswith(b"%PDF-") or b"%%EOF" not in body[-2048:]:
        raise IntegrityError("PDF signature/trailer contract failed")


def _places_metadata_semantic(path: Path) -> None:
    """Bind the stable CDC metadata representation to the adjudicated release."""

    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("PLACES stable metadata is not parseable JSON") from exc
    expected = {
        "id": "swc5-untb",
        "name": "PLACES: Local Data for Better Health, County Data, 2025 release",
        "attribution": (
            "Centers for Disease Control and Prevention, National Center for "
            "Chronic Disease Prevention and Health Promotion, Division of Population Health"
        ),
        "provenance": "OFFICIAL",
        "license": "Public Domain",
        "domain": "data.cdc.gov",
        "dataUpdatedAt": "2025-12-04T10:35:06+0000",
        "metadataUpdatedAt": "2025-12-05T18:12:42+0000",
    }
    if not isinstance(value, dict) or any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise IntegrityError("PLACES stable metadata semantic identity differs from the frozen release")


def _hrsa_hpsa_csv(path: Path) -> None:
    """Validate HRSA's pinned warehouse CSV, including its blank trailing column."""

    required = {"Common State County FIPS Code", "HPSA Status"}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            header = next(reader)
            if (
                len(header) != 66
                or header[-1] != ""
                or any(not field or "\0" in field for field in header[:-1])
                or len(header[:-1]) != len(set(header[:-1]))
                or not required.issubset(header[:-1])
            ):
                raise IntegrityError("HRSA warehouse header differs from the frozen schema")
            row_count = 0
            for row_number, row in enumerate(reader, start=2):
                row_count += 1
                if len(row) != len(header) - 1 or any("\0" in value for value in row):
                    raise IntegrityError(f"HRSA warehouse row width/content mismatch at row {row_number}")
            if row_count < 1:
                raise IntegrityError("HRSA warehouse snapshot has no data rows")
    except IntegrityError:
        raise
    except (OSError, UnicodeError, csv.Error, StopIteration) as exc:
        raise IntegrityError("HRSA warehouse snapshot is not a complete parseable CSV") from exc


def _gzip(path: Path) -> None:
    try:
        with gzip.open(path, "rb") as handle:
            header = handle.read(512)
    except (OSError, EOFError) as exc:
        raise IntegrityError("gzip stream is not parseable") from exc
    if len(header) < 512 or header[257:262] != b"ustar":
        raise IntegrityError("mapshaper archive is not a gzip-compressed tar stream")


def _rucc_long_form(path: Path) -> None:
    """Validate the pinned USDA 2023 long-form source before promotion."""

    base = csv_artifact_validator(
        "rucc-long-form-structure",
        required_fields=("FIPS", "State", "County_Name", "Attribute", "Value"),
        unique_fields=("FIPS", "Attribute"),
        expected_header=("FIPS", "State", "County_Name", "Attribute", "Value"),
        encoding="cp1252",
    )
    base(path)
    allowed_attributes = {"Population_2020", "RUCC_2023", "Description"}
    identities: dict[str, tuple[str, str]] = {}
    with path.open("r", encoding="cp1252", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        for row_number, row in enumerate(reader, start=2):
            fips = row["FIPS"]
            state = row["State"]
            county = row["County_Name"]
            attribute = row["Attribute"]
            value = row["Value"]
            if re.fullmatch(r"[0-9]{5}", fips) is None:
                raise IntegrityError(f"RUCC long-form FIPS is malformed at row {row_number}")
            if re.fullmatch(r"[A-Z]{2}", state) is None or not county:
                raise IntegrityError(f"RUCC long-form county identity is malformed at row {row_number}")
            identity = (state, county)
            prior = identities.setdefault(fips, identity)
            if prior != identity:
                raise IntegrityError(f"RUCC long-form county identity drifts at row {row_number}")
            if attribute not in allowed_attributes:
                raise IntegrityError(f"RUCC long-form attribute is unsupported at row {row_number}")
            if attribute == "Population_2020" and (
                re.fullmatch(r"[0-9]+", value) is None or int(value) < 0
            ):
                raise IntegrityError(f"RUCC population is malformed at row {row_number}")
            if attribute == "RUCC_2023" and (
                re.fullmatch(r"[0-9]+", value) is None or not 1 <= int(value) <= 9
            ):
                raise IntegrityError(f"RUCC code is malformed at row {row_number}")
            if attribute == "Description" and not value:
                raise IntegrityError(f"RUCC description is blank at row {row_number}")


def _zip_central(path: Path) -> None:
    _read_zip_central_directory(path)


def resolve_release_validators(
    spec: FrozenInput,
    *,
    context: ReleaseValidationContext | None = None,
) -> tuple[ArtifactValidator, ...]:
    """Return the exact built-in validator set for ``spec``.

    ``context`` contains only independently approved data.  It cannot add or
    replace code and is ignored unless the immutable HUD validator requires it.
    """

    policy = validate_source_identity(spec)
    validators: dict[str, ArtifactValidator] = {
        "synthetic-immutable-csv": csv_artifact_validator(
            "synthetic-immutable-csv",
            required_fields=("id", "value"),
            unique_fields=("id",),
            expected_header=("id", "value"),
        ),
        "taxonomy-260-csv": csv_artifact_validator("taxonomy-260-csv", required_fields=("Code",)),
        "nppes-index-html": _simple_validator("nppes-index-html", ("TEXT",), lambda path: _text_contains(path, (b"NPPES_Data_Dissemination_", b"_V2.zip"), "NPPES index")),
        "zip-central-directory": _simple_validator("zip-central-directory", ("ZIP",), _zip_central),
        "nppes-zip-central": _simple_validator("nppes-zip-central", ("ZIP",), _zip_central),
        "pdf-complete": _simple_validator("pdf-complete", ("PDF",), _pdf),
        "geocoder-benchmarks-json": json_artifact_validator("geocoder-benchmarks-json", top_level="object", required_fields=("benchmarks",)),
        "geocoder-vintages-json": json_artifact_validator("geocoder-vintages-json", top_level="object", required_fields=("vintages",)),
        "cms-metastore-json": json_artifact_validator("cms-metastore-json", top_level="object", required_fields=("distribution",)),
        "cms-hospital-csv": csv_artifact_validator("cms-hospital-csv", required_fields=tuple(HOSPITAL_FIELDS), unique_fields=("Facility ID",)),
        "places-metadata-json": json_artifact_validator("places-metadata-json", top_level="object", required_fields=("id", "name", "attribution", "provenance", "license", "domain", "dataUpdatedAt", "metadataUpdatedAt")),
        "places-metadata-semantic": _simple_validator("places-metadata-semantic", ("JSON",), _places_metadata_semantic),
        "places-raw-csv": csv_artifact_validator("places-raw-csv", required_fields=tuple(PLACES_REQUIRED_FIELDS), unique_fields=("locationid",)),
        "acs-population-csv": csv_artifact_validator(
            "acs-population-csv",
            required_fields=("GEO_ID", "B01003_E001", "B01003_M001"),
            unique_fields=("GEO_ID",),
            expected_header=("GEO_ID", "B01003_E001", "B01003_M001"),
            delimiter="|",
        ),
        "acs-income-csv": csv_artifact_validator(
            "acs-income-csv",
            required_fields=("GEO_ID", "B19013_E001", "B19013_M001"),
            unique_fields=("GEO_ID",),
            expected_header=("GEO_ID", "B19013_E001", "B19013_M001"),
            delimiter="|",
        ),
        "rucc-csv": _simple_validator("rucc-csv", ("CSV",), _rucc_long_form),
        "hrsa-hpsa-csv": _simple_validator("hrsa-hpsa-csv", ("CSV",), _hrsa_hpsa_csv),
        "county-adjacency-text": _simple_validator("county-adjacency-text", ("TEXT",), lambda path: _text_contains(path, (b"County Name", b"County GEOID"), "county adjacency")),
        "gzip-tar-container": _simple_validator("gzip-tar-container", ("BINARY",), _gzip),
        "hud-api-2025q4-json": _simple_validator(
            "hud-api-2025q4-json",
            ("JSON",),
            lambda path: __import__(
                "atlas_pipeline.hud_surface", fromlist=["validate_hud_api_envelope"]
            ).validate_hud_api_envelope(path),
        ),
    }
    if policy.validator_ids == ("hud-2025q4-xlsx",):
        if (
            context is None
            or context.candidate_manifest is None
            or context.manifest_approval is None
            or len(context.required_sheet_names) != 1
        ):
            raise IntegrityError("HUD XLSX validation requires one approved member manifest/sheet")
        if (
            context.candidate_manifest.source_id != spec.source_id
            or context.candidate_manifest.archive_bytes != spec.bytes
            or context.candidate_manifest.archive_sha256 != spec.sha256
        ):
            raise IntegrityError("HUD XLSX candidate manifest differs from the frozen source")
        validators["hud-2025q4-xlsx"] = xlsx_artifact_validator(
            "hud-2025q4-xlsx",
            context.candidate_manifest,
            context.manifest_approval,
            required_sheet_names=context.required_sheet_names,
            required_headers=HUD_HEADERS,
            unique_fields=("ZIP", "COUNTY"),
        )
    try:
        resolved = tuple(validators[validator_id] for validator_id in policy.validator_ids)
    except KeyError as exc:
        raise IntegrityError(f"immutable release validator is not implemented: {exc.args[0]}") from exc
    for validator in resolved:
        validator.validate_registration()
        if spec.artifact_type not in validator.artifact_types:
            raise IntegrityError("immutable validator/artifact-type mismatch")
    return resolved
