"""Independent reconstruction of frozen Census requests from entity mappings."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from .addresses import EntityAddressMapping, make_address_id, normalize_address
from .bundle import FrozenBundle, _load_json_bytes, _require_exact_keys, canonical_json_bytes
from .errors import IntegrityError
from .geocoder import GeocoderRequest, RequestChunk, chunk_requests, request_manifest_bytes
from .hospitals import HospitalRecord, normalize_facility_id
from .nppes import classify_disposition, valid_npi
from .request_prep import MAPPING_KEYS


@dataclass(frozen=True)
class VerifiedRequestChain:
    requests: tuple[GeocoderRequest, ...]
    chunks: tuple[RequestChunk, ...]
    request_states: dict[str, str]
    manifest_sha256: str


def _mapping_bytes(rows: Iterable[EntityAddressMapping]) -> bytes:
    ordered = sorted(rows, key=lambda row: (row.entity_id, row.address_id))
    if not ordered:
        raise IntegrityError("rederived entity-address mapping is empty")
    return b"".join(canonical_json_bytes(asdict(row)) for row in ordered)


def verify_frozen_mappings_from_sources(
    bundle: FrozenBundle,
    *,
    nppes_rows: Iterable[Mapping[str, str]],
    hospitals: Iterable[HospitalRecord],
    provider_mapping_source: str,
    hospital_mapping_source: str,
) -> None:
    """Demand exact D-II mapping bytes independently rederived from sources."""

    providers: list[EntityAddressMapping] = []
    for row in nppes_rows:
        disposition = classify_disposition(row)
        if disposition.bucket != "final_unmatched":
            raise IntegrityError("selected NPPES row is no longer pre-geography eligible")
        address = disposition.address
        providers.append(
            EntityAddressMapping(
                "P", disposition.npi, address.address_id, address.street1,
                address.street2, address.city, address.state, address.zip5,
                address.country, address.classification, address.direct_eligible,
                address.hud_eligible,
            )
        )
    hospital_rows = []
    for hospital in hospitals:
        address = hospital.address
        hospital_rows.append(
            EntityAddressMapping(
                "H", hospital.entity_id, address.address_id, address.street1,
                address.street2, address.city, address.state, address.zip5,
                address.country, address.classification, address.direct_eligible,
                address.hud_eligible,
            )
        )
    if bundle.verify_file(provider_mapping_source).read_bytes() != _mapping_bytes(providers):
        raise IntegrityError("provider mapping differs from selected NPPES rederivation")
    if bundle.verify_file(hospital_mapping_source).read_bytes() != _mapping_bytes(hospital_rows):
        raise IntegrityError("hospital mapping differs from CMS rederivation")


def _mapping_rows(bundle: FrozenBundle, source_ids: Iterable[str]) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for source_id in source_ids:
        path = bundle.verify_file(source_id)
        with path.open("rb") as handle:
            row_count = 0
            for line_number, line in enumerate(handle, start=1):
                row_count += 1
                if not line.endswith(b"\n"):
                    raise IntegrityError(f"mapping row lacks LF: {source_id}:{line_number}")
                value = _load_json_bytes(line)
                if not isinstance(value, dict) or line != canonical_json_bytes(value):
                    raise IntegrityError(f"mapping row is not canonical JSON: {source_id}:{line_number}")
                _require_exact_keys(value, MAPPING_KEYS, "entity mapping row")
                rows.append(value)
            if row_count == 0:
                raise IntegrityError(f"entity mapping is empty: {source_id}")
    return tuple(rows)


def reconstruct_requests_from_mappings(
    bundle: FrozenBundle,
    mapping_source_ids: tuple[str, str],
) -> tuple[tuple[GeocoderRequest, ...], dict[str, str]]:
    """Recompute normalized identities and the global unique request ordering."""

    entity_keys: set[tuple[str, str]] = set()
    address_fields: dict[str, tuple[str, str, str, str, str, str]] = {}
    requests: dict[str, GeocoderRequest] = {}
    states: dict[str, str] = {}
    for row in _mapping_rows(bundle, mapping_source_ids):
        string_fields = (
            "entity_kind", "entity_id", "address_id", "street1", "street2",
            "city", "state", "zip5", "country", "classification",
        )
        if any(not isinstance(row[field], str) for field in string_fields) or any(
            type(row[field]) is not bool for field in ("direct_eligible", "hud_eligible")
        ):
            raise IntegrityError("mapping field types are invalid")
        kind = row["entity_kind"]
        entity_id = row["entity_id"]
        if kind == "P":
            if not valid_npi(entity_id):
                raise IntegrityError("provider mapping contains an invalid NPI")
        elif kind == "H":
            if not re.fullmatch(r"H-(?:[0-9]{6}|[0-9]{5}F)", entity_id):
                raise IntegrityError("hospital mapping contains an invalid entity ID")
            normalize_facility_id(entity_id[2:])
        else:
            raise IntegrityError("mapping entity_kind must be P or H")
        entity_key = (kind, entity_id)
        if entity_key in entity_keys:
            raise IntegrityError("mapping contains a duplicate entity")
        entity_keys.add(entity_key)

        fields = (
            row["street1"], row["street2"], row["city"], row["state"],
            row["zip5"], row["country"],
        )
        expected_id = make_address_id(fields)
        if row["address_id"] != expected_id:
            raise IntegrityError("mapping address_id does not equal the exact normalized tuple")
        prior_fields = address_fields.setdefault(expected_id, fields)
        if prior_fields != fields:
            raise IntegrityError("one mapping address_id refers to multiple normalized tuples")
        recomputed = normalize_address(*fields)
        if (
            recomputed.address_id != expected_id
            or recomputed.classification != row["classification"]
            or recomputed.direct_eligible is not row["direct_eligible"]
            or recomputed.hud_eligible is not row["hud_eligible"]
            or (
                recomputed.street1, recomputed.street2, recomputed.city,
                recomputed.state, recomputed.zip5, recomputed.country,
            ) != fields
        ):
            raise IntegrityError("mapping normalization/routing fields do not recompute exactly")
        if not row["direct_eligible"]:
            continue
        request = GeocoderRequest(
            expected_id,
            " ".join(line for line in fields[:2] if line),
            row["city"],
            row["state"],
            row["zip5"],
        )
        prior_request = requests.setdefault(expected_id, request)
        if prior_request != request:
            raise IntegrityError("one request ID reconstructs to multiple request rows")
        prior_state = states.setdefault(expected_id, row["state"])
        if prior_state != row["state"]:
            raise IntegrityError("one request ID maps to multiple source states")
    ordered = tuple(requests[address_id] for address_id in sorted(requests))
    if not ordered:
        raise IntegrityError("frozen mappings reconstruct to zero direct requests")
    return ordered, states


def verify_frozen_request_chain(
    bundle: FrozenBundle,
    *,
    provider_mapping_source: str,
    hospital_mapping_source: str,
    request_manifest_source: str,
    request_chunk_sources: list[str],
) -> VerifiedRequestChain:
    requests, states = reconstruct_requests_from_mappings(
        bundle, (provider_mapping_source, hospital_mapping_source)
    )
    expected_chunks = tuple(chunk_requests(requests))
    if len(expected_chunks) != len(request_chunk_sources):
        raise IntegrityError("reconstructed request chunk count differs from frozen sources")
    for expected, source_id in zip(expected_chunks, request_chunk_sources):
        entry = bundle.inputs[source_id]
        body = bundle.verify_file(source_id).read_bytes()
        if (
            entry.relative_path.rsplit("/", 1)[-1] != expected.filename
            or body != expected.body
            or entry.bytes != expected.byte_count
            or entry.sha256 != expected.sha256
            or expected.row_count > 5_000
            or expected.byte_count > 4_500_000
        ):
            raise IntegrityError("frozen request chunk differs from reconstructed exact bytes")
    expected_manifest = request_manifest_bytes(expected_chunks)
    actual_manifest = bundle.verify_file(request_manifest_source).read_bytes()
    if actual_manifest != expected_manifest:
        raise IntegrityError("request manifest differs from reconstructed global request order")
    return VerifiedRequestChain(
        requests,
        expected_chunks,
        states,
        hashlib.sha256(expected_manifest).hexdigest(),
    )
