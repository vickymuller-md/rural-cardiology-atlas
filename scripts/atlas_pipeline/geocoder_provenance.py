"""Offline revalidation of every Census HTTP attempt and its handoff chain."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

from .bundle import FrozenBundle, _load_json_bytes, _require_exact_keys, canonical_json_bytes
from .contracts import (
    CENSUS_BENCHMARK_ID,
    CENSUS_GEOCODER_ENDPOINT,
    CENSUS_VINTAGE_ID,
)
from .control_receipts import ReceiptContext, validate_control_receipt
from .errors import AcquisitionError, IntegrityError
from .geocoder import (
    AttemptSnapshot,
    canonical_snapshot_bytes,
    canonicalize_accepted_attempt,
    census_response_representation_error,
    parse_response,
    response_is_retryable,
)
from .live_geocoder import prepare_census_request
from .predecessor_bridge import require_same_tree_or_predecessor_body
from .request_verification import VerifiedRequestChain
from .strict_json import validate_safe_relative_posix

ATTEMPT_MANIFEST_KEYS = (
    "chunk_index", "request_csv_sha256", "attempt", "accepted",
    "metadata_filename", "metadata_bytes", "metadata_sha256",
    "response_filename", "response_bytes", "response_sha256",
)
ATTEMPT_METADATA_KEYS = (
    "attempt", "request_method", "requested_url", "final_url", "redirect_chain",
    "request_headers", "request_csv_sha256", "prepared_body_bytes",
    "prepared_body_sha256", "endpoint", "benchmark_id", "benchmark_name",
    "vintage_id", "vintage_name", "started_at_utc", "ended_at_utc",
    "http_status", "response_headers", "response_bytes", "response_sha256",
    "transport_error", "integrity_error",
)


@dataclass(frozen=True)
class VerifiedCensusProvenance:
    attempts: tuple[AttemptSnapshot, ...]
    accepted_by_chunk: Mapping[int, AttemptSnapshot]
    attempts_manifest_sha256: str
    acquisition_receipt_sha256: str


def _integrity_errors(snapshot: AttemptSnapshot, expected_ids: set[str]) -> list[str]:
    errors: list[str] = []
    if snapshot.requested_url != CENSUS_GEOCODER_ENDPOINT:
        errors.append("transport requested URL differs from prepared URL")
    endpoint_host = urlparse(CENSUS_GEOCODER_ENDPOINT).hostname
    if len(snapshot.redirect_chain) > 3 or any(
        urlparse(url).scheme != "https" or urlparse(url).hostname != endpoint_host
        for url in (*snapshot.redirect_chain, snapshot.final_url)
    ):
        errors.append("redirect chain escapes controlled Census host")
    if snapshot.redirect_chain and snapshot.final_url != snapshot.redirect_chain[-1]:
        errors.append("final URL differs from redirect chain")
    if not snapshot.redirect_chain and snapshot.final_url != snapshot.requested_url:
        errors.append("final URL changed without recorded redirect")
    headers = {key.lower(): value for key, value in snapshot.response_headers}
    content_encoding = headers.get("content-encoding", "identity").strip().lower()
    representation_error = census_response_representation_error(snapshot.response_headers)
    if snapshot.http_status == 200 and representation_error is not None:
        errors.append(representation_error)
    if snapshot.http_status == 200 and content_encoding not in {"", "identity"}:
        errors.append("HTTP 200 response is not identity encoded on wire")
    if snapshot.http_status == 200 and not errors:
        try:
            parse_response(snapshot.response_body, expected_ids)
        except AcquisitionError as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return errors


def _snapshot(metadata: Mapping[str, object], response_body: bytes) -> AttemptSnapshot:
    headers = metadata["request_headers"]
    redirects = metadata["redirect_chain"]
    response_headers = metadata["response_headers"]
    if (
        not isinstance(headers, list)
        or any(not isinstance(row, list) or len(row) != 2 or not all(isinstance(item, str) for item in row) for row in headers)
        or not isinstance(redirects, list)
        or not all(isinstance(item, str) for item in redirects)
        or not isinstance(response_headers, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in response_headers.items())
    ):
        raise IntegrityError("Census attempt metadata collections are malformed")
    snapshot = AttemptSnapshot(
        request_sha256=metadata["request_csv_sha256"],
        request_method=metadata["request_method"],
        requested_url=metadata["requested_url"],
        final_url=metadata["final_url"],
        request_headers=tuple((row[0], row[1]) for row in headers),
        prepared_body_bytes=metadata["prepared_body_bytes"],
        prepared_body_sha256=metadata["prepared_body_sha256"],
        redirect_chain=tuple(redirects),
        endpoint=metadata["endpoint"],
        benchmark_id=metadata["benchmark_id"],
        benchmark_name=metadata["benchmark_name"],
        vintage_id=metadata["vintage_id"],
        vintage_name=metadata["vintage_name"],
        attempt=metadata["attempt"],
        started_at_utc=metadata["started_at_utc"],
        ended_at_utc=metadata["ended_at_utc"],
        http_status=metadata["http_status"],
        response_headers=tuple(sorted(response_headers.items())),
        response_body=response_body,
        response_sha256=metadata["response_sha256"],
        transport_error=metadata["transport_error"],
        integrity_error=metadata["integrity_error"],
    )
    snapshot.validate()
    return snapshot


def verify_census_provenance(
    bundle: FrozenBundle,
    *,
    request_chain: VerifiedRequestChain,
    attempts_manifest_source: str,
    attempt_metadata_sources: list[str],
    attempt_response_sources: list[str | None],
    acquisition_receipt_source: str,
    canonical_snapshot_source: str,
    control_receipt_evidence: Mapping[str, object],
    county_universe: set[str],
) -> VerifiedCensusProvenance:
    if len(attempt_metadata_sources) != len(attempt_response_sources):
        raise IntegrityError("Census attempt metadata/response role lengths differ")
    source_ids = [
        attempts_manifest_source,
        acquisition_receipt_source,
        canonical_snapshot_source,
        *attempt_metadata_sources,
        *(source for source in attempt_response_sources if source is not None),
    ]
    if len(source_ids) != len(set(source_ids)):
        raise IntegrityError("Census provenance roles reuse one frozen source")
    bundle.require_sources(source_ids)
    expected_schemas = {
        attempts_manifest_source: "geocoder_attempts_manifest_json",
        acquisition_receipt_source: "census_handoff_receipt_json",
        canonical_snapshot_source: "geocoder_canonical_jsonl",
        **{source: "geocoder_attempt_json" for source in attempt_metadata_sources},
        **{
            source: "geocoder_response_bytes"
            for source in attempt_response_sources
            if source is not None
        },
    }
    for source_id, schema_id in expected_schemas.items():
        if bundle.inputs[source_id].schema_id != schema_id:
            raise IntegrityError(f"Census provenance role has wrong schema: {source_id}")

    manifest_body = bundle.verify_file(attempts_manifest_source).read_bytes()
    manifest = _load_json_bytes(manifest_body)
    if (
        not isinstance(manifest, list)
        or not manifest
        or manifest_body != canonical_json_bytes(manifest)
        or len(manifest) != len(attempt_metadata_sources)
    ):
        raise IntegrityError("Census attempts manifest is empty/noncanonical/wrong length")
    attempts: list[AttemptSnapshot] = []
    accepted_by_chunk: dict[int, AttemptSnapshot] = {}
    position = 0
    for row, metadata_source, response_source in zip(
        manifest, attempt_metadata_sources, attempt_response_sources
    ):
        if not isinstance(row, dict):
            raise IntegrityError("Census attempt-manifest row must be an object")
        _require_exact_keys(row, ATTEMPT_MANIFEST_KEYS, "Census attempt-manifest row")
        if (
            type(row["chunk_index"]) is not int
            or row["chunk_index"] < 1
            or row["chunk_index"] > len(request_chain.chunks)
            or type(row["attempt"]) is not int
            or not 1 <= row["attempt"] <= 3
            or type(row["accepted"]) is not bool
            or type(row["metadata_bytes"]) is not int
            or row["metadata_bytes"] <= 0
            or type(row["response_bytes"]) is not int
            or row["response_bytes"] < 0
        ):
            raise IntegrityError("Census attempt-manifest scalar fields are invalid")
        validate_safe_relative_posix(
            row["metadata_filename"], label="Census attempt metadata filename"
        )
        chunk = request_chain.chunks[row["chunk_index"] - 1]
        if row["request_csv_sha256"] != chunk.sha256:
            raise IntegrityError("Census attempt manifest changed request bytes")
        metadata_path = bundle.verify_file(metadata_source)
        metadata_body = metadata_path.read_bytes()
        if (
            metadata_path.stat().st_size != row["metadata_bytes"]
            or hashlib.sha256(metadata_body).hexdigest() != row["metadata_sha256"]
        ):
            raise IntegrityError("Census attempt metadata identity mismatch")
        metadata = _load_json_bytes(metadata_body)
        if not isinstance(metadata, dict) or metadata_body != canonical_json_bytes(metadata):
            raise IntegrityError("Census attempt metadata is not canonical JSON")
        _require_exact_keys(metadata, ATTEMPT_METADATA_KEYS, "Census attempt metadata")

        if row["response_bytes"] == 0:
            if response_source is not None or row["response_filename"] is not None:
                raise IntegrityError("zero-byte Census response unexpectedly has a source")
            response_body = b""
        else:
            if response_source is None:
                raise IntegrityError("nonempty Census response source is missing")
            validate_safe_relative_posix(
                row["response_filename"], label="Census response filename"
            )
            response_body = bundle.verify_file(response_source).read_bytes()
        if (
            len(response_body) != row["response_bytes"]
            or row["response_sha256"] != metadata["response_sha256"]
            or len(response_body) != metadata["response_bytes"]
            or (
                row["response_sha256"] is not None
                and hashlib.sha256(response_body).hexdigest() != row["response_sha256"]
            )
        ):
            raise IntegrityError("Census raw response/metadata identity mismatch")
        snapshot = _snapshot(metadata, response_body)
        if snapshot.attempt != row["attempt"] or snapshot.request_sha256 != chunk.sha256:
            raise IntegrityError("Census attempt number/request binding mismatch")
        prepared = prepare_census_request(chunk.filename, chunk.body)
        if (
            snapshot.request_method != prepared.method
            or snapshot.request_headers != prepared.headers
            or snapshot.prepared_body_bytes != len(prepared.body)
            or snapshot.prepared_body_sha256 != prepared.body_sha256
        ):
            raise IntegrityError("Census attempt differs from reconstructed prepared request")
        start = sum(previous.row_count for previous in request_chain.chunks[: row["chunk_index"] - 1])
        expected_ids = {
            request.address_id
            for request in request_chain.requests[start : start + chunk.row_count]
        }
        objective = "; ".join(_integrity_errors(snapshot, expected_ids)) or None
        if snapshot.integrity_error != objective:
            raise IntegrityError("Census attempt integrity_error was not objectively rederived")
        if row["accepted"]:
            if (
                snapshot.http_status != 200
                or snapshot.transport_error is not None
                or objective is not None
                or row["chunk_index"] in accepted_by_chunk
            ):
                raise IntegrityError("Census attempts manifest marks an invalid/duplicate acceptance")
            accepted_by_chunk[row["chunk_index"]] = snapshot
        elif not response_is_retryable(
            http_status=snapshot.http_status,
            integrity_error=objective is not None,
            transport_error=snapshot.transport_error is not None,
        ):
            raise IntegrityError("Census successful handoff contains a terminal rejected attempt")
        expected_position = (row["chunk_index"], row["attempt"])
        if position and expected_position <= (
            manifest[position - 1]["chunk_index"], manifest[position - 1]["attempt"]
        ):
            raise IntegrityError("Census attempts manifest is not globally ordered")
        attempts.append(snapshot)
        position += 1

    for chunk_index in range(1, len(request_chain.chunks) + 1):
        chunk_rows = [
            (row, attempt)
            for row, attempt in zip(manifest, attempts)
            if row["chunk_index"] == chunk_index
        ]
        if (
            [row["attempt"] for row, _attempt in chunk_rows]
            != list(range(1, len(chunk_rows) + 1))
            or chunk_index not in accepted_by_chunk
            or chunk_rows[-1][0]["accepted"] is not True
        ):
            raise IntegrityError("Census attempts are nonconsecutive or continued after acceptance")

    derived_canonical = []
    offset = 0
    for chunk_index, chunk in enumerate(request_chain.chunks, start=1):
        chunk_requests = request_chain.requests[offset : offset + chunk.row_count]
        offset += chunk.row_count
        expected_ids = {request.address_id for request in chunk_requests}
        derived_canonical.extend(
            canonicalize_accepted_attempt(
                accepted_by_chunk[chunk_index],
                expected_ids=expected_ids,
                request_states={
                    request_id: request_chain.request_states[request_id]
                    for request_id in expected_ids
                },
                county_universe=county_universe,
            )
        )
    canonical_body = bundle.verify_file(canonical_snapshot_source).read_bytes()
    if canonical_body != canonical_snapshot_bytes(derived_canonical):
        raise IntegrityError("canonical Census snapshot differs from accepted attempts")

    acquisition_body = bundle.verify_file(acquisition_receipt_source).read_bytes()
    acquisition = _load_json_bytes(acquisition_body)
    if not isinstance(acquisition, dict) or acquisition_body != canonical_json_bytes(acquisition):
        raise IntegrityError("Census handoff receipt is not canonical JSON")
    _require_exact_keys(
        acquisition,
        (
            "schema_version", "receipt_id", "status", "source_tree_id",
            "bundle_manifest_sha256", "authorization_id", "command",
            "completed_at_utc", "evidence",
        ),
        "Census handoff receipt",
    )
    require_same_tree_or_predecessor_body(
        bundle,
        observed_source_tree_id=acquisition["source_tree_id"],
        artifact_id=acquisition_receipt_source,
        body=acquisition_body,
    )
    acquisition = validate_control_receipt(
        "census_handoff",
        acquisition_body,
        context=ReceiptContext(
            acquisition["source_tree_id"],
            acquisition["bundle_manifest_sha256"],
            acquisition["authorization_id"],
        ),
    )
    acquisition_evidence = acquisition["evidence"]
    canonical_hash = hashlib.sha256(canonical_body).hexdigest()
    manifest_hash = hashlib.sha256(manifest_body).hexdigest()
    if (
        acquisition_evidence["phase2e_bundle_manifest_sha256"]
        != control_receipt_evidence.get("phase2e_bundle_manifest_sha256")
        or acquisition_evidence["request_manifest_sha256"]
        != request_chain.manifest_sha256
        or acquisition_evidence["attempt_count"] != len(attempts)
        or acquisition_evidence["attempts_manifest_sha256"] != manifest_hash
        or acquisition_evidence["canonical_snapshot_sha256"] != canonical_hash
        or str(acquisition_evidence["benchmark_id"]) != CENSUS_BENCHMARK_ID
        or str(acquisition_evidence["vintage_id"]) != CENSUS_VINTAGE_ID
    ):
        raise IntegrityError("Census handoff receipt chain mismatch")
    if (
        control_receipt_evidence.get("request_manifest_sha256")
        != request_chain.manifest_sha256
        or control_receipt_evidence.get("attempts_manifest_sha256") != manifest_hash
        or control_receipt_evidence.get("canonical_snapshot_sha256") != canonical_hash
        or str(control_receipt_evidence.get("benchmark_id")) != CENSUS_BENCHMARK_ID
        or str(control_receipt_evidence.get("vintage_id")) != CENSUS_VINTAGE_ID
        or control_receipt_evidence.get("attempt_count") != len(attempts)
        or control_receipt_evidence.get("geocoder_acquisition_receipt_sha256")
        != hashlib.sha256(acquisition_body).hexdigest()
    ):
        raise IntegrityError("Census control receipt differs from verified attempt chain")
    return VerifiedCensusProvenance(
        tuple(attempts),
        accepted_by_chunk,
        manifest_hash,
        hashlib.sha256(acquisition_body).hexdigest(),
    )
