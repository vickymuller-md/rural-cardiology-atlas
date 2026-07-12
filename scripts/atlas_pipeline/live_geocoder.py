"""Phase-2E gated Census acquisition with injectable synthetic transport."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.parse import urljoin, urlparse

from .bundle import (
    FrozenBundle,
    _load_json_bytes,
    _require_exact_keys,
    _validate_utc_z,
    atomic_write,
    canonical_json_bytes,
    write_output_receipt,
)
from .contracts import (
    CENSUS_BENCHMARK_ID,
    CENSUS_BENCHMARK_NAME,
    CENSUS_GEOCODER_ENDPOINT,
    CENSUS_VINTAGE_ID,
    CENSUS_VINTAGE_NAME,
)
from .control_receipts import ReceiptContext, validate_control_receipt
from .errors import AcquisitionError, IntegrityError
from .geocoder import (
    AttemptSnapshot,
    CanonicalGeocode,
    canonical_snapshot_bytes,
    canonicalize_accepted_attempt,
    census_response_representation_error,
    parse_response,
    response_is_retryable,
    retry_delay_after,
)
from .request_verification import verify_frozen_request_chain

APPROVAL_SCHEMA_VERSION = "atlas-v19-census-live-approval-1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class LiveApproval:
    authorization_id: str
    bundle_manifest_sha256: str
    request_manifest_sha256: str
    approved_at_utc: str
    approval_bytes_sha256: str

    @classmethod
    def load(cls, path: Path, bundle: FrozenBundle) -> "LiveApproval":
        body = path.read_bytes()
        value = _load_json_bytes(body)
        if not isinstance(value, dict):
            raise AcquisitionError("live approval must be a JSON object")
        _require_exact_keys(
            value,
            (
                "schema_version", "phase", "authorization_id",
                "bundle_manifest_sha256", "request_manifest_sha256",
                "endpoint", "benchmark_id", "benchmark_name", "vintage_id",
                "vintage_name", "network_allowed", "approved_at_utc",
            ),
            "live approval",
        )
        if body != canonical_json_bytes(value):
            raise AcquisitionError("live approval is not canonical JSON")
        expected = (
            value["schema_version"],
            value["phase"],
            value["authorization_id"],
            value["bundle_manifest_sha256"],
            value["endpoint"],
            value["benchmark_id"],
            value["benchmark_name"],
            value["vintage_id"],
            value["vintage_name"],
            value["network_allowed"],
        )
        required = (
            APPROVAL_SCHEMA_VERSION,
            "2E",
            bundle.authorization_id,
            bundle.manifest_sha256,
            CENSUS_GEOCODER_ENDPOINT,
            CENSUS_BENCHMARK_ID,
            CENSUS_BENCHMARK_NAME,
            CENSUS_VINTAGE_ID,
            CENSUS_VINTAGE_NAME,
            True,
        )
        if expected != required:
            raise AcquisitionError("live approval is not bound to the fixed bundle/method")
        _validate_utc_z(value["approved_at_utc"], "approval approved_at_utc")
        request_hash = value["request_manifest_sha256"]
        if not isinstance(request_hash, str) or len(request_hash) != 64:
            raise AcquisitionError("approval request-manifest hash is invalid")
        return cls(
            bundle.authorization_id,
            bundle.manifest_sha256,
            request_hash,
            value["approved_at_utc"],
            hashlib.sha256(body).hexdigest(),
        )


@dataclass(frozen=True)
class PreparedCensusRequest:
    filename: str
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def prepare_census_request(filename: str, csv_body: bytes) -> PreparedCensusRequest:
    if not filename or not csv_body:
        raise IntegrityError("Census prepared request requires a filename and CSV bytes")
    boundary = "ATLAS-V19-" + hashlib.sha256(csv_body).hexdigest()[:32]
    parts = []
    for name, value in (
        ("benchmark", CENSUS_BENCHMARK_ID),
        ("vintage", CENSUS_VINTAGE_ID),
    ):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("ascii")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="addressFile"; filename="{filename}"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode("ascii")
        + csv_body
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    body = b"".join(parts)
    headers = tuple(
        sorted(
            (
                ("Accept-Encoding", "identity"),
                ("Content-Length", str(len(body))),
                ("Content-Type", f"multipart/form-data; boundary={boundary}"),
            )
        )
    )
    return PreparedCensusRequest(filename, "POST", CENSUS_GEOCODER_ENDPOINT, headers, body)


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...] = ()


class CensusTransport(Protocol):
    def execute(self, request: PreparedCensusRequest) -> TransportResponse: ...


class RequestsCensusTransport:
    """Concrete live transport; construction/calls require CLI approval gates."""

    def __init__(self) -> None:
        import requests

        self.session = requests.Session()

    def execute(self, request: PreparedCensusRequest) -> TransportResponse:
        current_url = request.url
        redirect_chain: list[str] = []
        for _redirect_count in range(4):
            prepared = self.session.prepare_request(
                __import__("requests").Request(
                    method=request.method,
                    url=current_url,
                    headers=dict(request.headers),
                    data=request.body,
                )
            )
            prepared_body = prepared.body
            if isinstance(prepared_body, str):
                prepared_body = prepared_body.encode("utf-8")
            prepared_headers = tuple(
                sorted(
                    (key, value)
                    for key, value in prepared.headers.items()
                    if key.lower() in {"accept-encoding", "content-length", "content-type"}
                )
            )
            if (
                prepared.method != request.method
                or prepared.url != current_url
                or prepared_body != request.body
                or prepared_headers != request.headers
            ):
                raise AcquisitionError("requests altered the frozen prepared Census request")
            response = self.session.send(
                prepared,
                timeout=(30, 300),
                allow_redirects=False,
                stream=True,
            )
            response.raw.decode_content = False
            response_body = response.raw.read()
            if response.status_code not in {307, 308}:
                return TransportResponse(
                    response.status_code,
                    dict(response.headers),
                    response_body,
                    request.url,
                    current_url,
                    tuple(redirect_chain),
                )
            location = response.headers.get("Location", "")
            target = urljoin(current_url, location)
            parsed = urlparse(target)
            if (
                parsed.scheme != "https"
                or parsed.hostname != urlparse(CENSUS_GEOCODER_ENDPOINT).hostname
            ):
                return TransportResponse(
                    response.status_code,
                    dict(response.headers),
                    response_body,
                    request.url,
                    target,
                    tuple((*redirect_chain, target)),
                )
            redirect_chain.append(target)
            current_url = target
        raise AcquisitionError("Census redirect chain exceeds three controlled redirects")


def acquire_census(
    bundle: FrozenBundle,
    approval: LiveApproval,
    output_dir: Path,
    *,
    transport: CensusTransport,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], str] = _now,
) -> Path:
    if bundle.phase != "2E" or approval.bundle_manifest_sha256 != bundle.manifest_sha256:
        raise AcquisitionError("Census acquisition lacks a matching Phase-2E approval")
    roles = bundle.expectation("source_roles", dict)
    if set(roles) != {
        "request_manifest", "provider_mapping", "hospital_mapping", "request_chunks"
    }:
        raise IntegrityError("Phase-2E source_roles are incomplete")
    chunk_ids = roles["request_chunks"]
    if not isinstance(chunk_ids, list) or not all(isinstance(item, str) for item in chunk_ids):
        raise IntegrityError("request_chunks role must be an ordered source-ID array")
    bundle.require_sources(
        (roles["request_manifest"], roles["provider_mapping"], roles["hospital_mapping"], *chunk_ids)
    )
    bundle.validate_registered_schema(roles["provider_mapping"])
    bundle.validate_registered_schema(roles["hospital_mapping"])
    request_chain = verify_frozen_request_chain(
        bundle,
        provider_mapping_source=roles["provider_mapping"],
        hospital_mapping_source=roles["hospital_mapping"],
        request_manifest_source=roles["request_manifest"],
        request_chunk_sources=chunk_ids,
    )
    if request_chain.manifest_sha256 != approval.request_manifest_sha256:
        raise AcquisitionError("approval request-manifest hash mismatch")
    request_states = request_chain.request_states
    output_dir.mkdir(parents=True, exist_ok=False)
    canonical_rows: list[CanonicalGeocode] = []
    artifact_paths: dict[str, Path] = {}
    attempts_manifest_rows: list[dict[str, object]] = []
    all_request_ids: set[str] = set()

    for index, (chunk, source_id) in enumerate(
        zip(request_chain.chunks, chunk_ids), start=1
    ):
        body = chunk.body
        ids = [request.address_id for request in request_chain.requests if chunk.first_id <= request.address_id <= chunk.last_id]
        if len(ids) != chunk.row_count:
            raise IntegrityError("reconstructed request chunk ID interval is not exact")
        if all_request_ids.intersection(ids):
            raise IntegrityError("request ID appears in multiple chunks")
        all_request_ids.update(ids)
        if any(request_id not in request_states for request_id in ids):
            raise IntegrityError("request ID lacks an approved entity/address-state mapping")
        prepared_request = prepare_census_request(chunk.filename, body)

        accepted: AttemptSnapshot | None = None
        chunk_attempts: list[tuple[AttemptSnapshot, Path, Path | None]] = []
        for attempt_number in range(1, 4):
            started = now()
            status: int | None = None
            headers: dict[str, str] = {}
            response_body = b""
            transport_error: str | None = None
            requested_url = prepared_request.url
            final_url = prepared_request.url
            redirect_chain: tuple[str, ...] = ()
            integrity_errors: list[str] = []
            try:
                response = transport.execute(prepared_request)
                status = response.status
                headers = dict(response.headers)
                response_body = response.body
                requested_url = response.requested_url
                final_url = response.final_url
                redirect_chain = tuple(response.redirect_chain)
            except Exception as exc:  # transport evidence, not source-row logic
                transport_error = f"{type(exc).__name__}: {exc}"
            ended = now()
            if transport_error is None:
                if requested_url != prepared_request.url:
                    integrity_errors.append("transport requested URL differs from prepared URL")
                endpoint_host = urlparse(CENSUS_GEOCODER_ENDPOINT).hostname
                if len(redirect_chain) > 3 or any(
                    urlparse(url).scheme != "https"
                    or urlparse(url).hostname != endpoint_host
                    for url in (*redirect_chain, final_url)
                ):
                    integrity_errors.append("redirect chain escapes controlled Census host")
                if redirect_chain and final_url != redirect_chain[-1]:
                    integrity_errors.append("final URL differs from redirect chain")
                if not redirect_chain and final_url != requested_url:
                    integrity_errors.append("final URL changed without recorded redirect")
                response_header_map = {key.lower(): value for key, value in headers.items()}
                content_encoding = response_header_map.get("content-encoding", "identity").strip().lower()
                representation_error = census_response_representation_error(headers)
                if status == 200 and representation_error is not None:
                    integrity_errors.append(representation_error)
                if status == 200 and content_encoding not in {"", "identity"}:
                    integrity_errors.append("HTTP 200 response is not identity encoded on wire")
                if status == 200 and not integrity_errors:
                    try:
                        parse_response(response_body, set(ids))
                    except AcquisitionError as exc:
                        integrity_errors.append(f"{type(exc).__name__}: {exc}")
            response_hash = (
                hashlib.sha256(response_body).hexdigest() if status is not None else None
            )
            snapshot = AttemptSnapshot(
                request_sha256=chunk.sha256,
                request_method=prepared_request.method,
                requested_url=requested_url,
                final_url=final_url,
                request_headers=prepared_request.headers,
                prepared_body_bytes=len(prepared_request.body),
                prepared_body_sha256=prepared_request.body_sha256,
                redirect_chain=redirect_chain,
                endpoint=CENSUS_GEOCODER_ENDPOINT,
                benchmark_id=CENSUS_BENCHMARK_ID,
                benchmark_name=CENSUS_BENCHMARK_NAME,
                vintage_id=CENSUS_VINTAGE_ID,
                vintage_name=CENSUS_VINTAGE_NAME,
                attempt=attempt_number,
                started_at_utc=started,
                ended_at_utc=ended,
                http_status=status,
                response_headers=tuple(sorted(headers.items())),
                response_body=response_body,
                response_sha256=response_hash,
                transport_error=transport_error,
                integrity_error=("; ".join(integrity_errors) or None),
            )
            snapshot.validate()
            attempt_dir = output_dir / f"chunk_{index:04d}"
            attempt_dir.mkdir(exist_ok=True)
            raw_path = attempt_dir / f"attempt_{attempt_number}.response"
            meta_path = attempt_dir / f"attempt_{attempt_number}.json"
            metadata = {
                "attempt": attempt_number,
                "request_method": prepared_request.method,
                "requested_url": requested_url,
                "final_url": final_url,
                "redirect_chain": list(redirect_chain),
                "request_headers": [list(item) for item in prepared_request.headers],
                "request_csv_sha256": chunk.sha256,
                "prepared_body_bytes": len(prepared_request.body),
                "prepared_body_sha256": prepared_request.body_sha256,
                "endpoint": CENSUS_GEOCODER_ENDPOINT,
                "benchmark_id": CENSUS_BENCHMARK_ID,
                "benchmark_name": CENSUS_BENCHMARK_NAME,
                "vintage_id": CENSUS_VINTAGE_ID,
                "vintage_name": CENSUS_VINTAGE_NAME,
                "started_at_utc": started,
                "ended_at_utc": ended,
                "http_status": status,
                "response_headers": dict(sorted(headers.items())),
                "response_bytes": len(response_body),
                "response_sha256": response_hash,
                "transport_error": transport_error,
                "integrity_error": snapshot.integrity_error,
            }
            atomic_write(meta_path, canonical_json_bytes(metadata))
            artifact_paths[f"chunk_{index:04d}_attempt_{attempt_number}_meta"] = meta_path
            persisted_raw: Path | None = None
            if response_body:
                atomic_write(raw_path, response_body)
                artifact_paths[f"chunk_{index:04d}_attempt_{attempt_number}_raw"] = raw_path
                persisted_raw = raw_path
            else:
                raw_path.unlink(missing_ok=True)
            chunk_attempts.append((snapshot, meta_path, persisted_raw))

            terminal_status = status is not None and status != 200 and not response_is_retryable(
                http_status=status,
                integrity_error=bool(snapshot.integrity_error),
                transport_error=False,
            )
            if terminal_status:
                raise AcquisitionError(f"terminal Census HTTP status {status}")
            if (
                status == 200
                and transport_error is None
                and snapshot.integrity_error is None
            ):
                accepted = snapshot
                break
            if attempt_number < 3:
                delay = retry_delay_after(attempt_number)
                sleep(float(delay))
        if accepted is None:
            raise AcquisitionError(f"no integrity-valid Census response for chunk {index}")
        for snapshot, meta_path, raw_path in chunk_attempts:
            attempts_manifest_rows.append(
                {
                    "chunk_index": index,
                    "request_csv_sha256": chunk.sha256,
                    "attempt": snapshot.attempt,
                    "accepted": snapshot.attempt == accepted.attempt,
                    "metadata_filename": meta_path.relative_to(output_dir).as_posix(),
                    "metadata_bytes": meta_path.stat().st_size,
                    "metadata_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
                    "response_filename": (
                        None if raw_path is None else raw_path.relative_to(output_dir).as_posix()
                    ),
                    "response_bytes": len(snapshot.response_body),
                    "response_sha256": snapshot.response_sha256,
                }
            )
        canonical_rows.extend(
            canonicalize_accepted_attempt(
                accepted,
                expected_ids=set(ids),
                request_states={request_id: request_states[request_id] for request_id in ids},
                county_universe=set(bundle.expectation("county_universe", list)),
            )
        )

    if set(request_states) != all_request_ids:
        raise IntegrityError("direct mapping/request ID sets are not equal")
    snapshot_path = output_dir / "geocoder_canonical.jsonl"
    atomic_write(snapshot_path, canonical_snapshot_bytes(canonical_rows))
    artifact_paths["geocoder_canonical"] = snapshot_path
    attempts_manifest_path = output_dir / "attempts_manifest.json"
    atomic_write(
        attempts_manifest_path,
        canonical_json_bytes(attempts_manifest_rows),
    )
    artifact_paths["attempts_manifest"] = attempts_manifest_path
    completed = now()
    acquisition_receipt = output_dir / "census_handoff_receipt.json"
    acquisition_body = canonical_json_bytes(
        {
            "schema_version": "atlas-v19-census-handoff-receipt-1",
            "receipt_id": "census_handoff",
            "status": "complete",
            "source_tree_id": bundle.source_tree_id,
            "bundle_manifest_sha256": bundle.manifest_sha256,
            "authorization_id": bundle.authorization_id,
            "command": "acquire-census",
            "completed_at_utc": completed,
            "evidence": {
                "phase2e_bundle_manifest_sha256": bundle.manifest_sha256,
                "request_manifest_sha256": request_chain.manifest_sha256,
                "attempts_manifest_sha256": hashlib.sha256(
                    attempts_manifest_path.read_bytes()
                ).hexdigest(),
                "canonical_snapshot_sha256": hashlib.sha256(
                    snapshot_path.read_bytes()
                ).hexdigest(),
                "benchmark_id": int(CENSUS_BENCHMARK_ID),
                "vintage_id": int(CENSUS_VINTAGE_ID),
                "attempt_count": len(attempts_manifest_rows),
            },
        }
    )
    validate_control_receipt(
        "census_handoff",
        acquisition_body,
        context=ReceiptContext(
            bundle.source_tree_id, bundle.manifest_sha256, bundle.authorization_id
        ),
    )
    atomic_write(acquisition_receipt, acquisition_body)
    artifact_paths["census_handoff_receipt"] = acquisition_receipt
    return write_output_receipt(
        output_dir,
        command="acquire-census",
        bundle=bundle,
        artifacts=artifact_paths,
        completed_at_utc=completed,
    )
