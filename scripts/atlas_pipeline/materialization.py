"""Executable frozen Phase-2B/2C/2D-I/2E acquisition surfaces.

All public entrypoints first verify an independently hashed integrator control.
The transport abstractions exist only so synthetic tests can exercise byte
streaming without a socket; production uses :class:`RequestsStreamingTransport`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, ContextManager, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlparse

from .acquisition import (
    FrozenInput,
    PendingDigestInput,
    ZipCandidateManifest,
    ZipManifestApproval,
    _assert_safe_managed_path,
    _discard_partial,
    _fsync_directory,
    _header,
    _prepare_root,
    _read_zip_central_directory,
    _run_artifact_validators,
    authorize_zip_approval,
    build_zip_candidate_manifest,
    canonical_zip_candidate_manifest_bytes,
    sha256_file,
    verify_zip_after_approval,
    zip_member_central_metadata_sha256,
)
from .errors import AcquisitionError, IntegrityError
from .control_receipts import ReceiptContext, validate_control_receipt
from .source_registry import (
    SOURCE_POLICIES,
    ReleaseValidationContext,
    resolve_release_validators,
    validate_source_identity,
)
from .strict_json import (
    canonical_json_bytes,
    reject_control_sentinels,
    require_canonical_json,
    validate_safe_id,
    validate_safe_relative_posix,
)

MATERIALIZATION_SCHEMA_VERSION = "atlas-v19-materialization-manifest-1"
NPPES_TRANSPORT_SCHEMA_VERSION = "atlas-v19-nppes-transport-plan-1"
STREAM_RECEIPT_SCHEMA_VERSION = "atlas-v19-stream-acquisition-receipt-2"
NPPES_CANDIDATE_RECEIPT_SCHEMA_VERSION = "atlas-v19-nppes-candidate-receipt-1"
NPPES_CONTAINER_RECEIPT_SCHEMA_VERSION = "atlas-v19-nppes-container-receipt-1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

PHASE2B_SOURCE_IDS = frozenset(
    source_id
    for source_id in SOURCE_POLICIES
    if source_id not in {
        "NPPES-202606-V2", "HUD-ZIP-COUNTY-2025Q4",
        "HUD-ZIP-COUNTY-API-2025Q4",
        "SYNTHETIC-IMMUTABLE-CSV",
    }
)


@dataclass(frozen=True)
class NppesTransportPlan:
    path: Path
    source_tree_id: str
    spec: PendingDigestInput
    body: bytes
    sha256: str

    @classmethod
    def load(cls, path: Path) -> "NppesTransportPlan":
        if not path.is_file() or path.is_symlink():
            raise IntegrityError("NPPES transport plan must be a regular non-symlink file")
        body = path.read_bytes()
        value = require_canonical_json(body, label="NPPES transport plan")
        if not isinstance(value, dict) or tuple(value) != (
            "schema_version", "phase", "source_tree_id", "source"
        ):
            raise IntegrityError("NPPES transport plan keys/order mismatch")
        reject_control_sentinels(value, label="NPPES transport plan")
        source = value["source"]
        if not isinstance(source, dict) or tuple(source) != (
            "source_id", "release", "url", "filename", "bytes", "allowed_hosts"
        ) or not isinstance(source["allowed_hosts"], list):
            raise IntegrityError("NPPES transport source schema mismatch")
        spec = PendingDigestInput(
            source["source_id"], source["release"], source["url"], source["filename"],
            source["bytes"], tuple(source["allowed_hosts"]),
        )
        policy = SOURCE_POLICIES["NPPES-202606-V2"]
        if (
            value["schema_version"] != NPPES_TRANSPORT_SCHEMA_VERSION
            or value["phase"] != "2C"
            or not isinstance(value["source_tree_id"], str)
            or _SHA256.fullmatch(value["source_tree_id"]) is None
            or (
                spec.source_id, spec.release, spec.url, spec.filename, spec.bytes,
                spec.allowed_hosts,
            )
            != (
                policy.source_id, policy.release, policy.url, policy.filename,
                policy.bytes, (urlparse(policy.url).hostname,),
            )
        ):
            raise IntegrityError("NPPES transport plan differs from the frozen June-2026 V2 identity")
        spec.validate()
        return cls(
            path.resolve(), value["source_tree_id"], spec, body,
            hashlib.sha256(body).hexdigest(),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        raise IntegrityError(f"atomic output already exists: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    if temporary.exists() or temporary.is_symlink():
        raise IntegrityError(f"stale atomic output partial: {temporary}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class MaterializationManifest:
    path: Path
    phase: str
    source_tree_id: str
    expectations: Mapping[str, object]
    sources: Mapping[str, FrozenInput]
    body: bytes
    sha256: str

    @classmethod
    def load(cls, path: Path, *, required_phase: str) -> "MaterializationManifest":
        if not path.is_file() or path.is_symlink():
            raise IntegrityError("materialization manifest must be a regular non-symlink file")
        body = path.read_bytes()
        value = require_canonical_json(body, label="materialization manifest")
        if not isinstance(value, dict) or tuple(value) != (
            "schema_version", "phase", "source_tree_id", "expectations", "sources"
        ):
            raise IntegrityError("materialization manifest keys/order mismatch")
        reject_control_sentinels(value, label="materialization manifest")
        if (
            value["schema_version"] != MATERIALIZATION_SCHEMA_VERSION
            or value["phase"] != required_phase
            or required_phase not in {"2B", "2D-I", "2E"}
            or not isinstance(value["source_tree_id"], str)
            or _SHA256.fullmatch(value["source_tree_id"]) is None
            or not isinstance(value["expectations"], dict)
            or not isinstance(value["sources"], list)
        ):
            raise IntegrityError("materialization manifest phase/schema is invalid")
        fields = (
            "source_id", "release", "url", "filename", "bytes", "sha256",
            "allowed_hosts", "artifact_type", "validator_ids", "content_types",
            "magic_hex",
        )
        sources: dict[str, FrozenInput] = {}
        for raw in value["sources"]:
            if not isinstance(raw, dict) or tuple(raw) != fields:
                raise IntegrityError("materialization source keys/order mismatch")
            if not isinstance(raw["allowed_hosts"], list) or not isinstance(
                raw["validator_ids"], list
            ) or not isinstance(raw["content_types"], list):
                raise IntegrityError("materialization source tuple fields must be arrays")
            source = FrozenInput(
                raw["source_id"], raw["release"], raw["url"], raw["filename"],
                raw["bytes"], raw["sha256"], tuple(raw["allowed_hosts"]),
                raw["artifact_type"], tuple(raw["validator_ids"]),
                tuple(raw["content_types"]), raw["magic_hex"],
            )
            source.validate(release_mode=True)
            validate_source_identity(source)
            if source.source_id in sources:
                raise IntegrityError("materialization source IDs are duplicated")
            sources[source.source_id] = source
        if [row["source_id"] for row in value["sources"]] != sorted(sources):
            raise IntegrityError("materialization sources are not sorted by source_id")
        required_ids = {
            "2B": PHASE2B_SOURCE_IDS,
            "2D-I": frozenset({"NPPES-202606-V2"}),
            "2E": None,
        }[required_phase]
        allowed_2e_sets = {
            frozenset({"HUD-ZIP-COUNTY-2025Q4"}),
            frozenset({"HUD-ZIP-COUNTY-API-2025Q4"}),
        }
        if (
            required_phase == "2E" and frozenset(sources) not in allowed_2e_sets
        ) or (
            required_phase != "2E" and set(sources) != required_ids
        ):
            expected = sorted(required_ids or set().union(*allowed_2e_sets))
            raise IntegrityError(
                f"{required_phase} source set is incomplete: "
                f"expected one approved set from={expected}, observed={sorted(sources)}"
            )
        expected_expectation_keys = {
            "2B": (),
            "2D-I": (),
            "2E": None,
        }[required_phase]
        if required_phase == "2E":
            expected_expectation_keys = (
                (
                    "county_universe_sha256", "county_count", "required_sheet_name",
                )
                if "HUD-ZIP-COUNTY-2025Q4" in sources
                else (
                    "county_universe_sha256", "county_count",
                    "acquisition_receipt_sha256", "result_row_count",
                )
            )
        if tuple(value["expectations"]) != expected_expectation_keys:
            raise IntegrityError(f"{required_phase} expectation keys/order mismatch")
        if required_phase == "2E":
            expectations = value["expectations"]
            if (
                not isinstance(expectations["county_universe_sha256"], str)
                or _SHA256.fullmatch(expectations["county_universe_sha256"]) is None
                or expectations["county_count"] != 3_144
            ):
                raise IntegrityError("Phase-2E HUD county expectations are not frozen")
            if "HUD-ZIP-COUNTY-2025Q4" in sources:
                if (
                    not isinstance(expectations["required_sheet_name"], str)
                    or not expectations["required_sheet_name"].strip()
                ):
                    raise IntegrityError("Phase-2E HUD XLSX sheet expectation is not frozen")
            elif (
                expectations["acquisition_receipt_sha256"]
                != "013d18d4019534e28bb2306d0d0fd1afce6b9b911e8a37da7d0489405a2d6782"
                or expectations["result_row_count"] != 54_571
            ):
                raise IntegrityError("Phase-2E HUD API expectations differ from Amendment 03")
        return cls(
            path.resolve(), required_phase, value["source_tree_id"],
            value["expectations"], sources,
            body, hashlib.sha256(body).hexdigest(),
        )


@dataclass
class StreamingResponse:
    status: int
    headers: Mapping[str, str]
    body: BinaryIO
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    request_method: str
    request_headers: tuple[tuple[str, str], ...]
    started_at_utc: str
    ended_at_utc: str | None = None


class StreamingTransport(Protocol):
    def get(
        self, url: str, headers: Mapping[str, str]
    ) -> ContextManager[StreamingResponse]: ...


class RequestsStreamingTransport:
    """Requests transport preserving raw identity-encoded on-wire bytes."""

    def __init__(self, *, authorization_header: str | None = None) -> None:
        import requests

        self._session = requests.Session()
        self._authorization_header = authorization_header

    @contextmanager
    def get(self, url: str, headers: Mapping[str, str]) -> Iterator[StreamingResponse]:
        request_headers = dict(headers)
        if self._authorization_header is not None:
            request_headers["Authorization"] = self._authorization_header
        started = _now()
        response = self._session.get(
            url,
            headers=request_headers,
            stream=True,
            timeout=(30, 300),
            allow_redirects=True,
        )
        response.raw.decode_content = False
        # Never persist an authentication secret; bind its digest instead.
        receipt_headers = []
        for name, value in request_headers.items():
            if name.lower() == "authorization":
                value = "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
                name = "Authorization-Digest"
            receipt_headers.append((name, value))
        exchange = StreamingResponse(
            response.status_code,
            dict(response.headers),
            response.raw,
            url,
            response.url,
            tuple(item.url for item in response.history),
            response.request.method,
            tuple(receipt_headers),
            started,
        )
        try:
            yield exchange
        finally:
            exchange.ended_at_utc = _now()
            response.close()


def _validate_exchange(
    response: StreamingResponse,
    spec: FrozenInput | PendingDigestInput,
    *,
    expected_range: str | None,
) -> None:
    if response.request_method != "GET" or response.requested_url != spec.url:
        raise AcquisitionError("streaming response request identity drift")
    headers = {name.lower(): value for name, value in response.request_headers}
    if headers.get("accept-encoding") != "identity" or headers.get("range") != expected_range:
        raise AcquisitionError("streaming request headers differ from the frozen request")
    if response.final_url != spec.url:
        raise AcquisitionError("streaming response final URL drift")
    for url in response.redirect_chain:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in spec.allowed_hosts:
            raise AcquisitionError("streaming redirect escaped the frozen host allowlist")
    encoding = (_header(dict(response.headers), "Content-Encoding") or "identity").lower()
    if encoding != "identity":
        raise AcquisitionError("streaming acquisition requires identity on-wire bytes")
    if isinstance(spec, FrozenInput) and spec.content_types:
        content_type = (
            _header(dict(response.headers), "Content-Type") or ""
        ).split(";", 1)[0].strip().lower()
        if content_type not in spec.content_types:
            raise AcquisitionError(
                f"streaming acquisition Content-Type drift: {content_type!r}"
            )


def _stream_body(
    source: BinaryIO,
    target: BinaryIO,
    *,
    maximum_bytes: int,
    initial_bytes: int = 0,
    chunk_size: int = 1024 * 1024,
) -> tuple[int, str, bytes]:
    if chunk_size <= 0:
        raise IntegrityError("stream chunk size must be positive")
    digest = hashlib.sha256()
    total = 0
    prefix = bytearray()
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise AcquisitionError("stream transport returned non-byte content")
        total += len(chunk)
        if initial_bytes + total > maximum_bytes:
            raise IntegrityError("streamed response exceeds the frozen expected size")
        if len(prefix) < 8:
            prefix.extend(chunk[: 8 - len(prefix)])
        digest.update(chunk)
        target.write(chunk)
    return total, digest.hexdigest(), bytes(prefix)


def _stream_receipt(
    *,
    spec: FrozenInput | PendingDigestInput,
    response: StreamingResponse,
    response_bytes: int,
    response_sha256: str,
    result_state: str,
    final_bytes: int | None,
    final_sha256: str | None,
    rejection_reason: str | None = None,
    receipt_kind: str = "attempt",
) -> dict[str, object]:
    ended = response.ended_at_utc or _now()
    range_request = next(
        (
            value
            for name, value in response.request_headers
            if name.lower() == "range"
        ),
        None,
    )
    content_range = _header(dict(response.headers), "Content-Range")
    segment_start: int | None = None
    segment_end: int | None = None
    representation_total_bytes: int | None = None
    if response.status == 200:
        segment_start = 0
        segment_end = response_bytes - 1 if response_bytes else None
        representation_total_bytes = response_bytes
    elif response.status == 206:
        match = re.fullmatch(
            r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range or ""
        )
        if match is not None:
            segment_start, segment_end, representation_total_bytes = map(
                int, match.groups()
            )
    elif response.status == 416:
        match = re.fullmatch(r"bytes \*/([0-9]+)", content_range or "")
        if match is not None:
            representation_total_bytes = int(match.group(1))
    return {
        "schema_version": STREAM_RECEIPT_SCHEMA_VERSION,
        "receipt_kind": receipt_kind,
        "source_id": spec.source_id,
        "release": spec.release,
        "request_method": response.request_method,
        "requested_url": response.requested_url,
        "request_headers": [list(item) for item in response.request_headers],
        "range_request": range_request,
        "started_at_utc": response.started_at_utc,
        "ended_at_utc": ended,
        "redirect_chain": list(response.redirect_chain),
        "final_url": response.final_url,
        "http_status": response.status,
        "content_type": _header(dict(response.headers), "Content-Type") or "",
        "content_encoding": (_header(dict(response.headers), "Content-Encoding") or "identity").lower(),
        "content_range": content_range,
        "segment_start": segment_start,
        "segment_end": segment_end,
        "representation_total_bytes": representation_total_bytes,
        "segment_bytes": response_bytes,
        "segment_sha256": response_sha256,
        "response_bytes": response_bytes,
        "response_sha256": response_sha256,
        "result_state": result_state,
        "rejection_reason": rejection_reason,
        "final_bytes": final_bytes,
        "final_sha256": final_sha256,
    }


def _persist_content_addressed_receipt(root: Path, receipt: Mapping[str, object]) -> Path:
    body = canonical_json_bytes(receipt)
    digest = hashlib.sha256(body).hexdigest()
    source_id = validate_safe_id(receipt.get("source_id"), label="receipt source ID")
    path = root / "receipts" / source_id / f"{digest}.json"
    if path.is_file() and not path.is_symlink():
        if path.read_bytes() != body:
            raise IntegrityError("content-addressed receipt collision")
        return path
    _atomic_write(path, body)
    return path


def validate_stream_acquisition_receipt(
    body: bytes,
    spec: FrozenInput,
    *,
    final_path: Path | None = None,
) -> Mapping[str, object]:
    """Validate an exact live, local-import, or clean-cache source receipt."""

    value = require_canonical_json(body, label="stream acquisition receipt")
    keys = (
        "schema_version", "receipt_kind", "source_id", "release",
        "request_method", "requested_url", "request_headers", "range_request",
        "started_at_utc", "ended_at_utc", "redirect_chain", "final_url", "http_status",
        "content_type", "content_encoding", "content_range", "segment_start",
        "segment_end", "representation_total_bytes", "segment_bytes",
        "segment_sha256", "response_bytes", "response_sha256",
        "result_state", "rejection_reason", "final_bytes", "final_sha256",
    )
    if not isinstance(value, dict) or tuple(value) != keys:
        raise IntegrityError("stream acquisition receipt keys/order mismatch")
    reject_control_sentinels(value, label="stream acquisition receipt")
    string_fields = (
        "schema_version", "receipt_kind", "source_id", "release",
        "request_method", "requested_url", "final_url", "content_type",
        "content_encoding", "segment_sha256", "response_sha256", "result_state",
        "final_sha256",
    )
    if (
        any(not isinstance(value[field], str) for field in string_fields)
        or not isinstance(value["request_headers"], list)
        or not isinstance(value["redirect_chain"], list)
        or type(value["http_status"]) is not int
        or type(value["segment_bytes"]) is not int
        or type(value["response_bytes"]) is not int
        or type(value["final_bytes"]) is not int
        or value["range_request"] is not None
        and not isinstance(value["range_request"], str)
        or value["content_range"] is not None
        and not isinstance(value["content_range"], str)
        or any(
            item is not None and type(item) is not int
            for item in (
                value["segment_start"],
                value["segment_end"],
                value["representation_total_bytes"],
            )
        )
    ):
        raise IntegrityError("stream acquisition receipt field types are invalid")
    if (
        value["schema_version"] != STREAM_RECEIPT_SCHEMA_VERSION
        or value["source_id"] != spec.source_id
        or value["release"] != spec.release
        or value["requested_url"] != spec.url
        or value["redirect_chain"] != []
        or value["final_url"] != spec.url
        or value["content_encoding"] != "identity"
        or value["rejection_reason"] is not None
        or value["final_bytes"] != spec.bytes
        or value["final_sha256"] != spec.sha256
        or value["content_type"].split(";", 1)[0] not in spec.content_types
        or value["segment_bytes"] != value["response_bytes"]
        or value["segment_sha256"] != value["response_sha256"]
        or value["segment_bytes"] < 0
        or _SHA256.fullmatch(value["segment_sha256"]) is None
    ):
        raise IntegrityError("stream acquisition receipt semantic binding mismatch")
    branch = value["receipt_kind"]
    if branch == "attempt":
        headers = value["request_headers"]
        if (
            not isinstance(headers, list)
            or any(
                not isinstance(row, list)
                or len(row) != 2
                or not all(isinstance(item, str) and item for item in row)
                for row in headers
            )
        ):
            raise IntegrityError("stream acquisition receipt headers are malformed")
        normalized = {name.lower(): item for name, item in headers}
        if len(normalized) != len(headers) or normalized.get("accept-encoding") != "identity":
            raise IntegrityError("stream acquisition receipt headers are duplicated/incomplete")
        allowed_headers = {"accept-encoding", "range"}
        if spec.source_id == "HUD-ZIP-COUNTY-2025Q4":
            allowed_headers.add("authorization-digest")
            if (
                re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    normalized.get("authorization-digest", ""),
                )
                is None
            ):
                raise IntegrityError(
                    "authenticated HUD receipt lacks the redacted authorization digest"
                )
        if set(normalized) - allowed_headers:
            raise IntegrityError("stream acquisition receipt contains unapproved headers")
        if value["range_request"] != normalized.get("range"):
            raise IntegrityError("stream acquisition receipt Range provenance mismatch")
        empty_sha = hashlib.sha256(b"").hexdigest()
        status = value["http_status"]
        if status == 200:
            if value["range_request"] is not None and re.fullmatch(
                r"bytes=([1-9][0-9]*)-", value["range_request"]
            ) is None:
                raise IntegrityError("HTTP 200 resume Range is malformed")
            segment_contract = (
                None, 0, spec.bytes - 1, spec.bytes, spec.bytes, spec.sha256
            )
        elif status == 206:
            match = re.fullmatch(
                r"bytes ([0-9]+)-([0-9]+)/([0-9]+)",
                value["content_range"] or "",
            )
            range_match = re.fullmatch(
                r"bytes=([0-9]+)-", value["range_request"] or ""
            )
            if match is None or range_match is None:
                raise IntegrityError("HTTP 206 receipt lacks Range/Content-Range")
            start, end, total = map(int, match.groups())
            if (
                start <= 0
                or int(range_match.group(1)) != start
                or end != spec.bytes - 1
                or total != spec.bytes
            ):
                raise IntegrityError("HTTP 206 segment does not complete the frozen artifact")
            segment_contract = (
                value["content_range"], start, end, total, end - start + 1, None
            )
        elif status == 416:
            if (
                value["range_request"] != f"bytes={spec.bytes}-"
                or value["content_range"] != f"bytes */{spec.bytes}"
            ):
                raise IntegrityError("HTTP 416 receipt does not bind the assembled artifact")
            segment_contract = (
                value["content_range"], None, None, spec.bytes, 0, empty_sha
            )
        else:
            raise IntegrityError("stream acquisition attempt status is not 200/206/416")
        expected_segment = segment_contract[:5]
        observed_segment = (
            value["content_range"], value["segment_start"], value["segment_end"],
            value["representation_total_bytes"], value["segment_bytes"],
        )
        if observed_segment != expected_segment:
            raise IntegrityError("stream acquisition response-segment provenance mismatch")
        expected_segment_sha = segment_contract[5]
        if expected_segment_sha is not None and value["segment_sha256"] != expected_segment_sha:
            raise IntegrityError("stream acquisition response-segment hash mismatch")
        expected = ("GET", status, "complete")
        observed = (
            value["request_method"], value["http_status"], value["result_state"]
        )
    elif branch == "local_import":
        expected = (
            "LOCAL_IMPORT", [], None, 0, None, 0, spec.bytes - 1,
            spec.bytes, spec.bytes, spec.sha256, "complete",
        )
        observed = (
            value["request_method"], value["request_headers"], value["range_request"],
            value["http_status"], value["content_range"], value["segment_start"],
            value["segment_end"], value["representation_total_bytes"],
            value["segment_bytes"], value["segment_sha256"], value["result_state"],
        )
    elif branch == "cache_validation":
        expected = (
            "NONE", [], None, 0, None, None, None, None, 0,
            hashlib.sha256(b"").hexdigest(), "already_valid",
        )
        observed = (
            value["request_method"], value["request_headers"], value["range_request"],
            value["http_status"], value["content_range"], value["segment_start"],
            value["segment_end"], value["representation_total_bytes"],
            value["segment_bytes"], value["segment_sha256"], value["result_state"],
        )
    else:
        raise IntegrityError("stream acquisition receipt kind is not registered")
    if observed != expected:
        raise IntegrityError(
            f"stream acquisition receipt {branch} branch contract mismatch"
        )
    if final_path is not None:
        if final_path.is_symlink() or not final_path.is_file():
            raise IntegrityError("stream acquisition final artifact is not a regular file")
        if final_path.stat().st_size != spec.bytes or sha256_file(final_path) != spec.sha256:
            raise IntegrityError("stream acquisition final artifact bytes/hash mismatch")
        if branch == "attempt" and value["http_status"] == 206:
            start = value["segment_start"]
            segment_bytes = value["segment_bytes"]
            digest = hashlib.sha256()
            observed_bytes = 0
            with final_path.open("rb") as handle:
                handle.seek(start)
                remaining = segment_bytes
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    digest.update(chunk)
                    observed_bytes += len(chunk)
                    remaining -= len(chunk)
            if (
                observed_bytes != segment_bytes
                or digest.hexdigest() != value["segment_sha256"]
            ):
                raise IntegrityError(
                    "HTTP 206 segment hash does not match the assembled artifact range"
                )
    elif branch == "attempt" and value["http_status"] in {206, 416}:
        raise IntegrityError(
            "HTTP 206/416 receipt validation requires the assembled artifact path"
        )
    for field in ("started_at_utc", "ended_at_utc"):
        timestamp = value[field]
        if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
            raise IntegrityError("stream acquisition timestamp is not UTC Z")
        try:
            datetime.fromisoformat(timestamp[:-1] + "+00:00")
        except ValueError as exc:
            raise IntegrityError("stream acquisition timestamp is invalid") from exc
    if value["ended_at_utc"] < value["started_at_utc"]:
        raise IntegrityError("stream acquisition receipt timestamps are reversed")
    return value


def _validate_promoted(
    path: Path,
    spec: FrozenInput,
    *,
    context: ReleaseValidationContext | None = None,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise IntegrityError("materialized artifact is not a regular file")
    if path.stat().st_size != spec.bytes or sha256_file(path) != spec.sha256:
        raise IntegrityError("materialized artifact byte/hash pin failed")
    validators = resolve_release_validators(spec, context=context)
    _run_artifact_validators(path, spec, validators)


@dataclass(frozen=True)
class MaterializationResult:
    source_id: str
    final_path: Path
    receipt_path: Path
    bytes: int
    sha256: str


def finalize_known_hash_materialization(
    manifest: MaterializationManifest,
    output_root: Path,
    *,
    authorization_id: str,
    completed_at_utc: str | None = None,
) -> Path:
    """Verify the entire Phase-2B set and emit its typed chain receipt."""

    if manifest.phase != "2B":
        raise IntegrityError("input-materialization finalization is Phase 2B only")
    validate_safe_id(authorization_id, label="Phase-2B authorization ID")
    root = output_root.resolve(strict=True)
    rows: list[dict[str, object]] = []
    for source_id in sorted(manifest.sources):
        spec = manifest.sources[source_id]
        path = spec.content_path(root)
        _validate_promoted(path, spec)
        rows.append(
            {
                "source_id": source_id,
                "relative_path": path.relative_to(root).as_posix(),
                "bytes": spec.bytes,
                "sha256": spec.sha256,
                "validator_ids": list(spec.validator_ids),
            }
        )
    identity_body = canonical_json_bytes(rows)
    registry_path = Path(__file__).with_name("source_registry.py")
    receipt = {
        "schema_version": "atlas-v19-input-materialization-receipt-1",
        "receipt_id": "input_materialization",
        "status": "verified_frozen",
        "source_tree_id": manifest.source_tree_id,
        "bundle_manifest_sha256": manifest.sha256,
        "authorization_id": authorization_id,
        "command": "materialize-inputs",
        "completed_at_utc": completed_at_utc or _now(),
        "evidence": {
            "input_manifest_sha256": manifest.sha256,
            "artifact_count": len(rows),
            "materialization_root_sha256": hashlib.sha256(identity_body).hexdigest(),
            "validator_registry_sha256": sha256_file(registry_path),
        },
    }
    body = canonical_json_bytes(receipt)
    validate_control_receipt(
        "input_materialization",
        body,
        context=ReceiptContext(
            manifest.source_tree_id, manifest.sha256, authorization_id
        ),
    )
    path = root / "phase2b_input_materialization_receipt.json"
    _atomic_write(path, body)
    return path


def import_known_hash(
    manifest: MaterializationManifest,
    source_id: str,
    source_path: Path,
    output_root: Path,
    *,
    context: ReleaseValidationContext | None = None,
) -> MaterializationResult:
    """Stream-import one independently acquired file into immutable storage."""

    try:
        spec = manifest.sources[source_id]
    except KeyError as exc:
        raise IntegrityError(f"source is not in the approved manifest: {source_id}") from exc
    if source_path.is_symlink() or not source_path.is_file():
        raise IntegrityError("import source must be a regular non-symlink file")
    root = _prepare_root(output_root)
    final = spec.content_path(root)
    _assert_safe_managed_path(root, final.parent)
    final.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_managed_path(root, final.parent)
    if final.exists() or final.is_symlink():
        _validate_promoted(final, spec, context=context)
        state = "already_valid"
    else:
        part = final.with_name(final.name + ".part")
        _discard_partial(part, root)
        digest = hashlib.sha256()
        total = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(part, flags, 0o600)
        try:
            with source_path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > spec.bytes:
                        raise IntegrityError("import source exceeds frozen bytes")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if total != spec.bytes or digest.hexdigest() != spec.sha256:
                raise IntegrityError("import source byte/hash pin failed")
            _validate_promoted(part, spec, context=context)
            os.replace(part, final)
            _fsync_directory(final.parent)
        except Exception:
            _discard_partial(part, root)
            raise
        state = "complete"
    cache_validation = state == "already_valid"
    receipt = {
        "schema_version": STREAM_RECEIPT_SCHEMA_VERSION,
        "receipt_kind": "cache_validation" if cache_validation else "local_import",
        "source_id": source_id,
        "release": spec.release,
        "request_method": "NONE" if cache_validation else "LOCAL_IMPORT",
        "requested_url": spec.url,
        "request_headers": [],
        "range_request": None,
        "started_at_utc": _now(),
        "ended_at_utc": _now(),
        "redirect_chain": [],
        "final_url": spec.url,
        "http_status": 0,
        "content_type": spec.content_types[0],
        "content_encoding": "identity",
        "content_range": None,
        "segment_start": None if cache_validation else 0,
        "segment_end": None if cache_validation else spec.bytes - 1,
        "representation_total_bytes": None if cache_validation else spec.bytes,
        "segment_bytes": 0 if cache_validation else spec.bytes,
        "segment_sha256": hashlib.sha256(b"").hexdigest() if cache_validation else spec.sha256,
        "response_bytes": 0 if cache_validation else spec.bytes,
        "response_sha256": hashlib.sha256(b"").hexdigest() if cache_validation else spec.sha256,
        "result_state": state,
        "rejection_reason": None,
        "final_bytes": spec.bytes,
        "final_sha256": spec.sha256,
    }
    receipt_path = _persist_content_addressed_receipt(root, receipt)
    return MaterializationResult(source_id, final, receipt_path, spec.bytes, spec.sha256)


def acquire_known_hash(
    manifest: MaterializationManifest,
    source_id: str,
    output_root: Path,
    *,
    transport: StreamingTransport,
    context: ReleaseValidationContext | None = None,
) -> MaterializationResult:
    """Stream one known-hash source with exact 200/206/416 semantics."""

    try:
        spec = manifest.sources[source_id]
    except KeyError as exc:
        raise IntegrityError(f"source is not in the approved manifest: {source_id}") from exc
    root = _prepare_root(output_root)
    final = spec.content_path(root)
    final.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_managed_path(root, final.parent)
    if final.exists() or final.is_symlink():
        _validate_promoted(final, spec, context=context)
        receipt = {
            "schema_version": STREAM_RECEIPT_SCHEMA_VERSION,
            "receipt_kind": "cache_validation",
            "source_id": source_id,
            "release": spec.release,
            "request_method": "NONE",
            "requested_url": spec.url,
            "request_headers": [],
            "range_request": None,
            "started_at_utc": _now(),
            "ended_at_utc": _now(),
            "redirect_chain": [],
            "final_url": spec.url,
            "http_status": 0,
            "content_type": spec.content_types[0],
            "content_encoding": "identity",
            "content_range": None,
            "segment_start": None,
            "segment_end": None,
            "representation_total_bytes": None,
            "segment_bytes": 0,
            "segment_sha256": hashlib.sha256(b"").hexdigest(),
            "response_bytes": 0,
            "response_sha256": hashlib.sha256(b"").hexdigest(),
            "result_state": "already_valid",
            "rejection_reason": None,
            "final_bytes": spec.bytes,
            "final_sha256": spec.sha256,
        }
        receipt_path = _persist_content_addressed_receipt(root, receipt)
        return MaterializationResult(source_id, final, receipt_path, spec.bytes, spec.sha256)
    part = final.with_name(final.name + ".part")
    if part.is_symlink():
        raise IntegrityError("known-hash partial cannot be a symlink")
    existing = part.stat().st_size if part.is_file() else 0
    if existing > spec.bytes:
        _discard_partial(part, root)
        raise IntegrityError("known-hash partial exceeds expected bytes")
    request_headers = {"Accept-Encoding": "identity"}
    expected_range = None
    if existing:
        expected_range = f"bytes={existing}-"
        request_headers["Range"] = expected_range
    response_bytes = 0
    response_digest = hashlib.sha256(b"").hexdigest()
    response: StreamingResponse | None = None
    try:
        with transport.get(spec.url, request_headers) as response:
            _validate_exchange(response, spec, expected_range=expected_range)
            status = response.status
            if status == 416:
                content_range = _header(dict(response.headers), "Content-Range")
                if (
                    not existing
                    or content_range != f"bytes */{spec.bytes}"
                    or existing != spec.bytes
                    or sha256_file(part) != spec.sha256
                ):
                    raise AcquisitionError("invalid known-hash 416 completion claim")
            elif status in {200, 206}:
                expected_response_bytes: int
                if status == 206:
                    content_range = _header(dict(response.headers), "Content-Range")
                    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range or "")
                    if (
                        not existing
                        or match is None
                        or int(match.group(1)) != existing
                        or int(match.group(3)) != spec.bytes
                        or int(match.group(2)) < existing
                        or int(match.group(2)) >= spec.bytes
                    ):
                        raise AcquisitionError("known-hash 206 range does not bind the partial")
                    expected_response_bytes = int(match.group(2)) - existing + 1
                    mode = "ab"
                else:
                    mode = "wb"
                    existing = 0
                    expected_response_bytes = spec.bytes
                content_length = _header(dict(response.headers), "Content-Length")
                if content_length is not None and content_length != str(expected_response_bytes):
                    raise AcquisitionError("known-hash Content-Length differs from response segment")
                with part.open(mode) as handle:
                    response_bytes, response_digest, _prefix = _stream_body(
                        response.body, handle, maximum_bytes=spec.bytes,
                        initial_bytes=existing,
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                if response_bytes != expected_response_bytes:
                    raise AcquisitionError("known-hash response body length differs from HTTP segment")
            else:
                raise AcquisitionError(f"terminal known-hash HTTP status: {status}")
            response.ended_at_utc = response.ended_at_utc or _now()
        if part.stat().st_size < spec.bytes:
            state = "partial"
            receipt = _stream_receipt(
                spec=spec, response=response, response_bytes=response_bytes,
                response_sha256=response_digest, result_state=state,
                final_bytes=None, final_sha256=None,
            )
            receipt_path = _persist_content_addressed_receipt(root, receipt)
            raise AcquisitionError(f"known-hash transfer remains partial; receipt={receipt_path}")
        _validate_promoted(part, spec, context=context)
        os.replace(part, final)
        _fsync_directory(final.parent)
        receipt = _stream_receipt(
            spec=spec, response=response, response_bytes=response_bytes,
            response_sha256=response_digest, result_state="complete",
            final_bytes=spec.bytes, final_sha256=spec.sha256,
        )
        receipt_path = _persist_content_addressed_receipt(root, receipt)
        return MaterializationResult(source_id, final, receipt_path, spec.bytes, spec.sha256)
    except Exception as exc:
        if response is not None:
            response.ended_at_utc = response.ended_at_utc or _now()
            receipt = _stream_receipt(
                spec=spec, response=response, response_bytes=response_bytes,
                response_sha256=response_digest, result_state="rejected",
                final_bytes=None, final_sha256=None,
                rejection_reason=f"{type(exc).__name__}: {exc}"[:1000],
            )
            receipt_path = _persist_content_addressed_receipt(root, receipt)
            exc.add_note(f"rejected streaming acquisition receipt: {receipt_path}")
        raise


@dataclass(frozen=True)
class NppesCandidateResult:
    candidate_path: Path
    candidate_sha256: str
    attempt_receipt_path: Path
    final_receipt_path: Path


def acquire_nppes_candidate(
    spec: PendingDigestInput,
    output_root: Path,
    *,
    transport: StreamingTransport,
    source_tree_id: str,
    plan_sha256: str,
    authorization_id: str,
    storage_preflight_receipt_sha256: str,
    chunk_size: int = 1024 * 1024,
) -> NppesCandidateResult:
    """Phase 2C: one non-resumable identity HTTP 200, then stop unopened."""

    spec.validate()
    if (
        spec.source_id != "NPPES-202606-V2"
        or spec.release != "2026-06"
        or spec.url != SOURCE_POLICIES[spec.source_id].url
        or spec.filename != SOURCE_POLICIES[spec.source_id].filename
        or spec.bytes != SOURCE_POLICIES[spec.source_id].bytes
    ):
        raise IntegrityError("Phase-2C NPPES pending-digest identity drift")
    return _acquire_pending_candidate(
        spec,
        output_root,
        transport=transport,
        source_tree_id=source_tree_id,
        plan_sha256=plan_sha256,
        authorization_id=authorization_id,
        storage_preflight_receipt_sha256=storage_preflight_receipt_sha256,
        chunk_size=chunk_size,
    )


def _acquire_pending_candidate(
    spec: PendingDigestInput,
    output_root: Path,
    *,
    transport: StreamingTransport,
    source_tree_id: str,
    plan_sha256: str,
    authorization_id: str,
    storage_preflight_receipt_sha256: str,
    chunk_size: int = 1024 * 1024,
) -> NppesCandidateResult:
    """Synthetic-testable implementation behind the exact public NPPES gate."""

    spec.validate()
    validate_safe_id(authorization_id, label="Phase-2C authorization ID")
    if _SHA256.fullmatch(source_tree_id) is None or _SHA256.fullmatch(plan_sha256) is None:
        raise IntegrityError("Phase-2C runtime identity is invalid")
    if _SHA256.fullmatch(storage_preflight_receipt_sha256) is None:
        raise IntegrityError("Phase-2C storage-preflight receipt hash is invalid")
    root = _prepare_root(output_root)
    candidate = spec.candidate_path(root)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_managed_path(root, candidate.parent)
    if candidate.exists() or candidate.is_symlink():
        raise IntegrityError("Phase-2C candidate already exists; no implicit reacquisition")
    part = candidate.with_name(candidate.name + ".part")
    _discard_partial(part, root)
    response: StreamingResponse | None = None
    response_bytes = 0
    response_digest = hashlib.sha256(b"").hexdigest()
    try:
        with transport.get(spec.url, {"Accept-Encoding": "identity"}) as response:
            _validate_exchange(response, spec, expected_range=None)
            if response.status != 200:
                raise AcquisitionError("Phase-2C NPPES accepts exactly one complete HTTP 200")
            content_length = _header(dict(response.headers), "Content-Length")
            if content_length != str(spec.bytes):
                raise AcquisitionError("Phase-2C NPPES Content-Length differs from pinned bytes")
            content_type = (_header(dict(response.headers), "Content-Type") or "").split(";", 1)[0]
            if content_type not in {"application/zip", "application/octet-stream"}:
                raise AcquisitionError("Phase-2C NPPES Content-Type is not ZIP/binary")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(part, flags, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                response_bytes, response_digest, prefix = _stream_body(
                    response.body, target, maximum_bytes=spec.bytes, chunk_size=chunk_size
                )
                target.flush()
                os.fsync(target.fileno())
            if response_bytes != spec.bytes or not prefix.startswith(b"PK"):
                raise IntegrityError("Phase-2C NPPES bytes/signature are incomplete")
            response.ended_at_utc = response.ended_at_utc or _now()
        if part.stat().st_size != spec.bytes or sha256_file(part) != response_digest:
            raise IntegrityError("Phase-2C NPPES local write changed on-wire bytes")
        os.replace(part, candidate)
        _fsync_directory(candidate.parent)
        receipt_context = ReceiptContext(source_tree_id, plan_sha256, authorization_id)
        attempt = {
            "schema_version": "atlas-v19-source-acquisition-attempt-receipt-1",
            "receipt_id": "source_acquisition_attempt",
            "status": "complete",
            "source_tree_id": source_tree_id,
            "bundle_manifest_sha256": plan_sha256,
            "authorization_id": authorization_id,
            "command": "acquire-known-hash-source",
            "completed_at_utc": response.ended_at_utc,
            "evidence": {
                "source_id": spec.source_id,
                "schema_id": "nppes-transport-zip",
                "url": spec.url,
                "method": "GET",
                "request_headers": [["Accept-Encoding", "identity"]],
                "started_at_utc": response.started_at_utc,
                "ended_at_utc": response.ended_at_utc,
                "response_status": 200,
                "content_encoding": "identity",
                "response_bytes": response_bytes,
                "response_sha256": response_digest,
                "promoted_relative_path": candidate.relative_to(root).as_posix(),
            },
        }
        attempt_body = canonical_json_bytes(attempt)
        validate_control_receipt(
            "source_acquisition_attempt", attempt_body, context=receipt_context
        )
        attempt_path = root / "phase2c_source_acquisition_attempt_receipt.json"
        _atomic_write(attempt_path, attempt_body)
        final = {
            "schema_version": "atlas-v19-nppes-transport-receipt-1",
            "receipt_id": "nppes_transport",
            "status": "candidate_complete_stopped_before_container_open",
            "source_tree_id": source_tree_id,
            "bundle_manifest_sha256": plan_sha256,
            "authorization_id": authorization_id,
            "command": "acquire-nppes-transport",
            "completed_at_utc": response.ended_at_utc,
            "evidence": {
                "url": spec.url,
                "filename": spec.filename,
                "expected_bytes": spec.bytes,
                "candidate_sha256": response_digest,
                "attempt_receipt_sha256": hashlib.sha256(attempt_body).hexdigest(),
                "storage_preflight_receipt_sha256": storage_preflight_receipt_sha256,
                "response_status": 200,
                "content_encoding": "identity",
                "container_opened": False,
                "completed_transfer_count": 1,
            },
        }
        final_body = canonical_json_bytes(final)
        validate_control_receipt(
            "nppes_transport", final_body, context=receipt_context
        )
        final_path = root / "phase2c_nppes_transport_receipt.json"
        _atomic_write(final_path, final_body)
        return NppesCandidateResult(candidate, response_digest, attempt_path, final_path)
    except Exception as exc:
        _discard_partial(part, root)
        if response is not None:
            response.ended_at_utc = response.ended_at_utc or _now()
            rejected = _stream_receipt(
                spec=spec, response=response, response_bytes=response_bytes,
                response_sha256=response_digest, result_state="rejected",
                final_bytes=None, final_sha256=None,
                rejection_reason=f"{type(exc).__name__}: {exc}"[:1000],
            )
            receipt_path = _persist_content_addressed_receipt(root, rejected)
            exc.add_note(f"rejected Phase-2C receipt: {receipt_path}")
        raise


def inspect_nppes_container(
    manifest: MaterializationManifest,
    archive_path: Path,
    output_dir: Path,
    *,
    authorization_id: str,
) -> tuple[Path, Path]:
    """Phase 2D-I step one: central-directory metadata only, no payload open."""

    if manifest.phase != "2D-I":
        raise IntegrityError("NPPES container inspection requires Phase 2D-I manifest")
    spec = manifest.sources["NPPES-202606-V2"]
    candidate = build_zip_candidate_manifest(archive_path, spec)
    validate_safe_id(authorization_id, label="Phase-2D-I authorization ID")
    body = canonical_zip_candidate_manifest_bytes(candidate)
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = output_dir / "nppes_candidate_member_manifest.json"
    _atomic_write(manifest_path, body)
    receipt = {
        "schema_version": "atlas-v19-nppes-zip-candidate-receipt-1",
        "receipt_id": "nppes_zip_candidate",
        "status": "candidate_manifest_complete_stopped_before_payload",
        "source_tree_id": manifest.source_tree_id,
        "bundle_manifest_sha256": manifest.sha256,
        "authorization_id": authorization_id,
        "command": "inspect-nppes-container",
        "completed_at_utc": _now(),
        "evidence": {
            "archive_sha256": candidate.archive_sha256,
            "candidate_manifest_sha256": hashlib.sha256(body).hexdigest(),
            "member_count": len(candidate.members),
            "payload_opened": False,
            "analytical_rows_parsed": False,
        },
    }
    receipt_path = output_dir / "receipt.json"
    receipt_body = canonical_json_bytes(receipt)
    validate_control_receipt(
        "nppes_zip_candidate",
        receipt_body,
        context=ReceiptContext(
            manifest.source_tree_id, manifest.sha256, authorization_id
        ),
    )
    _atomic_write(receipt_path, receipt_body)
    return manifest_path, receipt_path


def _load_zip_candidate(path: Path) -> ZipCandidateManifest:
    if not path.is_file() or path.is_symlink():
        raise IntegrityError("ZIP candidate manifest must be a regular non-symlink file")
    value = require_canonical_json(path.read_bytes(), label="ZIP candidate manifest")
    if not isinstance(value, dict) or tuple(value) != (
        "source_id", "archive_bytes", "archive_sha256", "members"
    ) or not isinstance(value["members"], list):
        raise IntegrityError("ZIP candidate manifest schema mismatch")
    from .acquisition import ZipMemberIdentity

    fields = tuple(ZipMemberIdentity.__dataclass_fields__)
    members = []
    for raw in value["members"]:
        if not isinstance(raw, dict) or tuple(raw) != fields:
            raise IntegrityError("ZIP member identity schema mismatch")
        members.append(ZipMemberIdentity(**raw))
    candidate = ZipCandidateManifest(
        value["source_id"], value["archive_bytes"], value["archive_sha256"],
        tuple(members),
    )
    candidate.validate()
    return candidate


def _load_zip_approval(path: Path) -> ZipManifestApproval:
    if not path.is_file() or path.is_symlink():
        raise IntegrityError("ZIP manifest approval must be a regular non-symlink file")
    value = require_canonical_json(path.read_bytes(), label="ZIP manifest approval")
    fields = tuple(ZipManifestApproval.__dataclass_fields__)
    if not isinstance(value, dict) or tuple(value) != fields:
        raise IntegrityError("ZIP manifest approval schema mismatch")
    approval = ZipManifestApproval(**value)
    approval.validate()
    return approval


def approve_nppes_container(
    manifest: MaterializationManifest,
    archive_path: Path,
    candidate_manifest_path: Path,
    approval_path: Path,
    approval_trust_root_path: Path,
    output_dir: Path,
    *,
    independently_approved_trust_root_sha256: str,
    authorization_id: str,
    selected_member_filename: str,
) -> Path:
    """Phase 2D-I step two: consume approval, then stream every member CRC."""

    if manifest.phase != "2D-I":
        raise IntegrityError("NPPES CRC approval consumer requires Phase 2D-I manifest")
    candidate = _load_zip_candidate(candidate_manifest_path)
    spec = manifest.sources["NPPES-202606-V2"]
    if (
        candidate.source_id != spec.source_id
        or candidate.archive_bytes != spec.bytes
        or candidate.archive_sha256 != spec.sha256
    ):
        raise IntegrityError("approved NPPES candidate differs from Phase 2D-I pin")
    validate_safe_id(authorization_id, label="Phase-2D-I authorization ID")
    for path, label in (
        (approval_path, "NPPES member approval"),
        (approval_trust_root_path, "NPPES approval trust root"),
    ):
        if not path.is_file() or path.is_symlink():
            raise IntegrityError(f"{label} must be a regular non-symlink file")
    trusted_approval = authorize_zip_approval(
        candidate,
        approval_path.read_bytes(),
        approval_trust_root_path.read_bytes(),
        independently_approved_trust_root_sha256=(
            independently_approved_trust_root_sha256
        ),
        source_tree_id=manifest.source_tree_id,
        bundle_manifest_sha256=manifest.sha256,
        authorization_id=authorization_id,
    )
    selected_member = validate_safe_relative_posix(
        selected_member_filename, label="NPPES selected member filename"
    )
    if len(selected_member.parts) != 1:
        raise IntegrityError("NPPES selected member must be a root ZIP filename")
    if selected_member_filename != trusted_approval.approval.selected_member_filename:
        raise IntegrityError(
            "NPPES selected member differs from the externally trusted approval"
        )
    if selected_member_filename not in {
        member.filename for member in candidate.members if not member.is_directory
    }:
        raise IntegrityError("approved NPPES selected analytical member is absent")
    selected_identity = next(
        member
        for member in candidate.members
        if not member.is_directory and member.filename == selected_member_filename
    )
    if (
        zip_member_central_metadata_sha256(selected_identity)
        != trusted_approval.approval.selected_member_central_metadata_sha256
    ):
        raise IntegrityError("NPPES selected-member central metadata changed after approval")
    verified = verify_zip_after_approval(
        archive_path, candidate, trusted_approval
    )
    member_hashes = dict(verified.member_sha256s)
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt = {
        "schema_version": "atlas-v19-nppes-container-receipt-1",
        "receipt_id": "nppes_container",
        "status": "crc_verified_stopped_before_analytical_parse",
        "source_tree_id": manifest.source_tree_id,
        "bundle_manifest_sha256": manifest.sha256,
        "authorization_id": authorization_id,
        "command": "verify-nppes-container",
        "completed_at_utc": _now(),
        "evidence": {
            "archive_sha256": verified.archive_sha256,
            "candidate_manifest_sha256": verified.candidate_manifest_sha256,
            "approval_id": verified.approval_id,
            "approval_receipt_sha256": hashlib.sha256(
                approval_path.read_bytes()
            ).hexdigest(),
            "approval_trust_root_sha256": (
                independently_approved_trust_root_sha256
            ),
            "selected_member_filename": selected_member_filename,
            "selected_member_central_metadata_sha256": (
                zip_member_central_metadata_sha256(selected_identity)
            ),
            "selected_member_bytes": selected_identity.uncompressed_bytes,
            "selected_member_sha256": member_hashes[selected_member_filename],
            "member_count": len(verified.members),
            "crc_verified": True,
            "analytical_rows_parsed": False,
        },
    }
    path = output_dir / "receipt.json"
    receipt_body = canonical_json_bytes(receipt)
    validate_control_receipt(
        "nppes_container",
        receipt_body,
        context=ReceiptContext(
            manifest.source_tree_id, manifest.sha256, authorization_id
        ),
    )
    _atomic_write(path, receipt_body)
    return path
