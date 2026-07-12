"""Content-addressed transfer state machine and frozen manifest schema.

The state machine accepts already-returned HTTP response bytes so it can be
tested without a socket.  No concrete network transport is provided in Phase
2A.  A later acquisition command must inject one after item-specific approval.
"""

from __future__ import annotations

import hashlib
import csv
import json
import os
import re
import stat
import unicodedata
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable, Iterator, Mapping
from urllib.parse import urlparse
from xml.etree import ElementTree

from .errors import AcquisitionError, IntegrityError
from .strict_json import (
    canonical_json_bytes,
    reject_control_sentinels,
    require_canonical_json,
    validate_safe_id,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VALIDATOR_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CONTENT_RANGE_206 = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
_CONTENT_RANGE_416 = re.compile(r"^bytes \*/([0-9]+)$")
SUPPORTED_ARTIFACT_TYPES = frozenset(
    {"BINARY", "CSV", "JSON", "PDF", "TEXT", "XLSX", "ZIP"}
)
_SAFE_ZIP_COMPRESSION_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_UNSAFE_ZIP_FLAG_BITS = (1 << 0) | (1 << 5) | (1 << 6) | (1 << 13)
ZIP_APPROVAL_TRUST_ROOT_SCHEMA_VERSION = "atlas-v19-zip-approval-trust-root-1"
_ZIP_APPROVAL_AUTHORIZATION_TOKEN = object()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    if path.is_symlink():
        raise IntegrityError(f"refusing to hash symlink: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FrozenInput:
    source_id: str
    release: str
    url: str
    filename: str
    bytes: int
    sha256: str
    allowed_hosts: tuple[str, ...]
    artifact_type: str | None = None
    validator_ids: tuple[str, ...] = ()
    content_types: tuple[str, ...] = ()
    magic_hex: str | None = None

    def validate(self, *, release_mode: bool = False) -> None:
        parsed = urlparse(self.url)
        if (
            Path(self.source_id).name != self.source_id
            or not self.source_id
            or not self.release
            or not self.allowed_hosts
        ):
            raise IntegrityError("frozen input identity fields are unsafe or blank")
        if parsed.scheme != "https" or not parsed.hostname:
            raise IntegrityError(f"canonical URL must be HTTPS: {self.url}")
        if parsed.hostname not in self.allowed_hosts:
            raise IntegrityError(f"source host is not allowlisted: {parsed.hostname}")
        if len(self.allowed_hosts) != len(set(self.allowed_hosts)) or any(
            not host or host != host.lower() or "/" in host for host in self.allowed_hosts
        ):
            raise IntegrityError("allowed hosts must be unique lowercase hostnames")
        if Path(self.filename).name != self.filename or not self.filename:
            raise IntegrityError(f"unsafe manifest filename: {self.filename!r}")
        if self.bytes <= 0 or not _SHA256.fullmatch(self.sha256):
            raise IntegrityError(f"invalid byte/hash pin for {self.source_id}")
        if self.artifact_type is not None and self.artifact_type not in SUPPORTED_ARTIFACT_TYPES:
            raise IntegrityError(f"unsupported artifact type: {self.artifact_type!r}")
        if len(set(self.validator_ids)) != len(self.validator_ids) or any(
            _VALIDATOR_ID.fullmatch(item) is None for item in self.validator_ids
        ):
            raise IntegrityError("validator IDs must be unique canonical identifiers")
        if len(self.content_types) != len(set(self.content_types)) or any(
            not item
            or item != item.lower()
            or ";" in item
            or "/" not in item
            for item in self.content_types
        ):
            raise IntegrityError("Content-Type allowlist entries must be unique canonical media types")
        if release_mode and (self.artifact_type is None or not self.validator_ids):
            raise IntegrityError(
                "release-mode frozen inputs require artifact_type and validator_ids"
            )
        if self.magic_hex is not None:
            if (
                not self.magic_hex
                or len(self.magic_hex) % 2
                or self.magic_hex != self.magic_hex.lower()
            ):
                raise IntegrityError("media-signature hex must be nonempty and even-length")
            try:
                bytes.fromhex(self.magic_hex)
            except ValueError as exc:
                raise IntegrityError("invalid manifest media-signature hex") from exc

    def content_path(self, root: Path) -> Path:
        self.validate()
        return root / "sha256" / self.sha256 / self.filename


@dataclass(frozen=True)
class PendingDigestInput:
    """Exact URL/size contract used only to compute a candidate first digest."""

    source_id: str
    release: str
    url: str
    filename: str
    bytes: int
    allowed_hosts: tuple[str, ...]

    def validate(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise IntegrityError("pending-digest URL/host contract failed")
        if (
            Path(self.source_id).name != self.source_id
            or not self.source_id
            or not self.release
            or not self.allowed_hosts
            or len(self.allowed_hosts) != len(set(self.allowed_hosts))
            or any(
                not host or host != host.lower() or "/" in host
                for host in self.allowed_hosts
            )
        ):
            raise IntegrityError("pending-digest identity fields are unsafe or blank")
        if Path(self.filename).name != self.filename or self.bytes <= 0:
            raise IntegrityError("pending-digest filename/size contract failed")

    def candidate_path(self, root: Path) -> Path:
        self.validate()
        identity = json.dumps(
            {
                "source_id": self.source_id,
                "release": self.release,
                "url": self.url,
                "filename": self.filename,
                "bytes": self.bytes,
                "allowed_hosts": list(self.allowed_hosts),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        identity_sha256 = hashlib.sha256(identity).hexdigest()
        return (
            root
            / "candidate-digest"
            / self.source_id
            / identity_sha256
            / (self.filename + ".candidate")
        )


@dataclass(frozen=True)
class CandidateDigestResult:
    state: str
    candidate_path: Path
    partial_bytes: int
    candidate_sha256: str
    receipt: "AcquisitionReceipt"
    receipt_path: Path | None = None


def apply_pending_digest_response(
    root: Path,
    spec: PendingDigestInput,
    response: "HttpResponse",
    *,
    range_requested: bool,
) -> CandidateDigestResult:
    """Hash one complete identity-encoded 200 response without opening its body."""

    spec.validate()
    root = _prepare_root(root)
    candidate = spec.candidate_path(root)
    part = candidate.with_name(candidate.name + ".part")
    _assert_safe_managed_path(root, candidate.parent)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_managed_path(root, candidate.parent)
    candidate_exists = _safe_existing_regular(candidate, root)
    _safe_existing_regular(part, root)
    # Pending-digest acquisition is deliberately non-resumable.  Any prior
    # candidate partial is stale and removed before response acceptance checks.
    _discard_partial(part, root)
    response.validate()
    if response.requested_url != spec.url or response.final_url != spec.url:
        raise AcquisitionError("pending-digest URL drift")
    for hop in response.redirect_chain:
        parsed = urlparse(hop)
        if parsed.scheme != "https" or parsed.hostname not in spec.allowed_hosts:
            raise AcquisitionError("pending-digest redirect escaped the HTTPS host allowlist")
    if (_header(response.headers, "Content-Encoding") or "identity").lower() != "identity":
        raise AcquisitionError("pending digest requires identity encoding")
    if range_requested or _request_header(response.request_headers, "Range") is not None:
        _discard_partial(part, root)
        raise AcquisitionError("pending digest forbids Range/resume requests")
    if response.status != 200:
        _discard_partial(part, root)
        raise AcquisitionError("pending digest accepts only one complete HTTP 200 response")
    if len(response.body) != spec.bytes:
        _discard_partial(part, root)
        raise IntegrityError("pending digest HTTP 200 body is not the exact pinned size")
    receipt = receipt_from_response(
        response,
        source_id=spec.source_id,
        release=spec.release,
        artifact_type="PENDING_DIGEST",
    )
    response_digest = sha256_bytes(response.body)
    if candidate_exists:
        if candidate.stat().st_size != spec.bytes:
            _remove_corrupt_regular(candidate, root)
            raise IntegrityError("candidate quarantine byte length drift")
        digest = sha256_file(candidate)
        if digest != response_digest:
            raise IntegrityError("pending-digest candidate changed for the frozen URL/size identity")
        complete_receipt = _finalize_receipt(
            receipt, "candidate_complete", local_path=candidate, final=True
        )
        return CandidateDigestResult(
            "candidate_complete", candidate, 0, digest, complete_receipt
        )
    _write_new(part, response.body, root=root)
    if part.stat().st_size != spec.bytes:
        _discard_partial(part, root)
        raise IntegrityError("pending candidate write did not preserve exact response bytes")
    digest = sha256_file(part)
    if digest != response_digest:
        _discard_partial(part, root)
        raise IntegrityError("pending candidate write changed the response SHA-256")
    _assert_safe_managed_path(root, candidate)
    os.replace(part, candidate)
    _fsync_directory(candidate.parent)
    # Deliberately do not open or move into frozen sha256 storage.  The
    # integrator must approve this candidate digest first.
    complete_receipt = _finalize_receipt(
        receipt, "candidate_complete", local_path=candidate, final=True
    )
    return CandidateDigestResult(
        "candidate_complete", candidate, 0, digest, complete_receipt
    )


def execute_pending_digest_response(
    root: Path,
    spec: PendingDigestInput,
    response: "HttpResponse",
    *,
    range_requested: bool,
    receipt_root: Path | None = None,
) -> CandidateDigestResult:
    """Pending-digest executor with mandatory atomic response-receipt persistence."""

    spec.validate()
    base_receipt = receipt_from_response(
        response,
        source_id=spec.source_id,
        release=spec.release,
        artifact_type="PENDING_DIGEST",
        allow_noncanonical=True,
    )
    destination = receipt_root or root
    try:
        result = apply_pending_digest_response(
            root, spec, response, range_requested=range_requested
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")
        rejected = _finalize_receipt(
            base_receipt, "rejected", rejection_reason=reason[:1000]
        )
        receipt_path = persist_receipt_atomic(destination, rejected)
        exc.add_note(f"rejected pending-digest receipt: {receipt_path}")
        raise
    receipt_path = persist_receipt_atomic(destination, result.receipt)
    return replace(result, receipt_path=receipt_path)


def canonical_manifest_bytes(entries: list[FrozenInput]) -> bytes:
    seen: set[str] = set()
    rows = []
    for entry in sorted(entries, key=lambda item: item.source_id):
        entry.validate(release_mode=True)
        if entry.source_id in seen:
            raise IntegrityError(f"duplicate source ID: {entry.source_id}")
        seen.add(entry.source_id)
        row = asdict(entry)
        row["allowed_hosts"] = list(entry.allowed_hosts)
        row["validator_ids"] = list(entry.validator_ids)
        row["content_types"] = list(entry.content_types)
        rows.append(row)
    return (json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


@dataclass(frozen=True)
class CacheIdentity:
    source_id: str
    url: str
    bytes: int
    sha256: str


def validate_cache_identity(identity: CacheIdentity, spec: FrozenInput) -> None:
    expected = CacheIdentity(spec.source_id, spec.url, spec.bytes, spec.sha256)
    if identity != expected:
        raise IntegrityError(
            f"stale cache identity for {spec.source_id}: {identity!r} != {expected!r}"
        )


def validate_final(path: Path, spec: FrozenInput) -> None:
    if path.is_symlink() or not path.is_file():
        raise IntegrityError(f"missing content-addressed file: {path}")
    if path.stat().st_size != spec.bytes:
        raise IntegrityError(f"byte-length mismatch for {spec.source_id}")
    if sha256_file(path) != spec.sha256:
        raise IntegrityError(f"SHA-256 mismatch for {spec.source_id}")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...] = ()
    request_method: str = ""
    request_headers: tuple[tuple[str, str], ...] = ()
    started_at_utc: str = ""
    ended_at_utc: str = ""

    def validate_capture(self) -> None:
        """Validate that observed provenance is safely recordable, not that it was accepted."""

        if not self.request_method or any(
            character in self.request_method for character in ("\r", "\n")
        ):
            raise AcquisitionError("captured HTTP request method is blank/malformed")
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise AcquisitionError("HTTP status must be an integer in 100..599")
        if not self.requested_url or not self.final_url:
            raise AcquisitionError("HTTP response URL provenance is blank")
        names: set[str] = set()
        for name, value in self.request_headers:
            lowered = name.lower()
            if (
                not name
                or lowered in names
                or "\r" in name
                or "\n" in name
                or "\r" in value
                or "\n" in value
            ):
                raise AcquisitionError("actual request headers are malformed or duplicated")
            names.add(lowered)
        started = _parse_utc_timestamp(self.started_at_utc, "request start")
        ended = _parse_utc_timestamp(self.ended_at_utc, "request end")
        if ended < started:
            raise AcquisitionError("HTTP request end precedes its start")
        content_length = _header(self.headers, "Content-Length")
        if content_length is not None:
            if not content_length.isdigit() or int(content_length) != len(self.body):
                raise AcquisitionError("HTTP Content-Length does not match captured body")

    def validate(self) -> None:
        self.validate_capture()
        if self.request_method != "GET":
            raise AcquisitionError("canonical acquisition request method must be GET")
        request_encoding = _request_header(self.request_headers, "Accept-Encoding")
        if request_encoding != "identity":
            raise AcquisitionError("actual request must send Accept-Encoding: identity")


def canonical_request_headers(*, range_start: int | None = None) -> dict[str, str]:
    """Headers whose exact representation is frozen for canonical transfers."""

    headers = {"Accept-Encoding": "identity"}
    if range_start is not None:
        if range_start < 0:
            raise AcquisitionError("Range start cannot be negative")
        headers["Range"] = f"bytes={range_start}-"
    return headers


def _parse_utc_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcquisitionError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise AcquisitionError(f"{label} must use UTC offset zero")
    return parsed


def _request_header(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    target = name.lower()
    for key, value in headers:
        if key.lower() == target:
            return value
    return None


@dataclass(frozen=True)
class AcquisitionReceipt:
    source_id: str
    release: str
    artifact_type: str
    request_method: str
    requested_url: str
    request_headers: tuple[tuple[str, str], ...]
    started_at_utc: str
    ended_at_utc: str
    redirect_chain: tuple[str, ...]
    final_url: str
    http_status: int
    content_type: str
    content_encoding: str
    content_range: str | None
    response_bytes: int
    response_sha256: str
    segment_start: int | None
    segment_end: int | None
    representation_total_bytes: int | None
    etag: str | None
    last_modified: str | None
    result_state: str
    rejection_reason: str | None
    local_bytes_after: int | None
    local_sha256_after: str | None
    final_bytes: int | None
    final_sha256: str | None

    def validate(self) -> None:
        if not self.source_id or not self.release or not self.artifact_type:
            raise IntegrityError("receipt source identity is incomplete")
        rejected = self.result_state == "rejected"
        if (not rejected and self.request_method != "GET") or not self.request_method:
            raise IntegrityError("accepted receipt request method is not GET")
        if not self.requested_url or not self.final_url:
            raise IntegrityError("receipt URL provenance is incomplete")
        header_names: set[str] = set()
        for name, value in self.request_headers:
            lowered = name.lower()
            if (
                not name
                or lowered in header_names
                or any(character in name or character in value for character in ("\r", "\n"))
            ):
                raise IntegrityError("receipt request headers are malformed or duplicated")
            header_names.add(lowered)
        if (
            not rejected
            and _request_header(self.request_headers, "Accept-Encoding") != "identity"
        ):
            raise IntegrityError("receipt request did not bind Accept-Encoding: identity")
        started = _parse_utc_timestamp(self.started_at_utc, "receipt start")
        ended = _parse_utc_timestamp(self.ended_at_utc, "receipt end")
        if ended < started:
            raise IntegrityError("receipt end precedes start")
        if not rejected and self.content_encoding != "identity":
            raise IntegrityError("receipt is not bound to an identity representation")
        if self.response_bytes < 0 or not _SHA256.fullmatch(self.response_sha256):
            raise IntegrityError("receipt response byte/hash identity is invalid")
        if (self.segment_start is None) != (self.segment_end is None):
            if not (
                self.http_status == 200
                and self.response_bytes == 0
                and self.segment_start == 0
                and self.segment_end is None
            ):
                raise IntegrityError("receipt response segment bounds are incoherent")
        if self.segment_start is not None and self.segment_end is not None:
            if (
                self.segment_start < 0
                or self.segment_end < self.segment_start
                or self.response_bytes != self.segment_end - self.segment_start + 1
                or (
                    self.representation_total_bytes is not None
                    and self.segment_end >= self.representation_total_bytes
                )
            ):
                raise IntegrityError("receipt response segment does not bind captured bytes")
        if self.representation_total_bytes is not None and self.representation_total_bytes < 0:
            raise IntegrityError("receipt representation total cannot be negative")
        if self.result_state not in {
            "unapplied",
            "rejected",
            "partial",
            "complete",
            "already_valid",
            "candidate_complete",
        }:
            raise IntegrityError("receipt result state is invalid")
        if rejected:
            if (
                not self.rejection_reason
                or "\r" in self.rejection_reason
                or "\n" in self.rejection_reason
            ):
                raise IntegrityError("rejected receipt lacks a single-line rejection reason")
        elif self.rejection_reason is not None:
            raise IntegrityError("accepted/unapplied receipt cannot carry a rejection reason")
        if (self.local_bytes_after is None) != (self.local_sha256_after is None):
            raise IntegrityError("receipt local byte/hash fields are incoherent")
        if self.local_bytes_after is not None and (
            self.local_bytes_after < 0
            or self.local_sha256_after is None
            or not _SHA256.fullmatch(self.local_sha256_after)
        ):
            raise IntegrityError("receipt local byte/hash identity is invalid")
        if (self.final_bytes is None) != (self.final_sha256 is None):
            raise IntegrityError("receipt final byte/hash fields are incoherent")
        if self.final_bytes is not None and (
            self.final_bytes <= 0
            or self.final_sha256 is None
            or not _SHA256.fullmatch(self.final_sha256)
        ):
            raise IntegrityError("receipt final byte/hash identity is invalid")
        if self.result_state in {"complete", "already_valid", "candidate_complete"} and (
            self.final_bytes is None or self.final_sha256 is None
        ):
            raise IntegrityError("completed receipt lacks final byte/hash identity")
        if self.final_bytes is not None and (
            self.final_bytes != self.local_bytes_after
            or self.final_sha256 != self.local_sha256_after
        ):
            raise IntegrityError("receipt final identity disagrees with local post-state")


def _response_segment(response: HttpResponse) -> tuple[int | None, int | None, int | None]:
    if response.status == 200:
        return (0, len(response.body) - 1 if response.body else None, len(response.body))
    if response.status == 206:
        match = _CONTENT_RANGE_206.fullmatch(_header(response.headers, "Content-Range") or "")
        if match is not None:
            start, end, total = map(int, match.groups())
            return (start, end, total)
    if response.status == 416:
        match = _CONTENT_RANGE_416.fullmatch(_header(response.headers, "Content-Range") or "")
        if match is not None:
            return (None, None, int(match.group(1)))
    return (None, None, None)


def receipt_from_response(
    response: HttpResponse,
    *,
    source_id: str = "UNBOUND",
    release: str = "UNBOUND",
    artifact_type: str = "UNBOUND",
    allow_noncanonical: bool = False,
) -> AcquisitionReceipt:
    if allow_noncanonical:
        response.validate_capture()
    else:
        response.validate()
    segment_start, segment_end, total = _response_segment(response)
    return AcquisitionReceipt(
        source_id=source_id,
        release=release,
        artifact_type=artifact_type,
        request_method=response.request_method,
        requested_url=response.requested_url,
        request_headers=response.request_headers,
        started_at_utc=response.started_at_utc,
        ended_at_utc=response.ended_at_utc,
        redirect_chain=response.redirect_chain,
        final_url=response.final_url,
        http_status=response.status,
        content_type=(_header(response.headers, "Content-Type") or ""),
        content_encoding=(_header(response.headers, "Content-Encoding") or "identity").lower(),
        content_range=_header(response.headers, "Content-Range"),
        response_bytes=len(response.body),
        response_sha256=sha256_bytes(response.body),
        segment_start=segment_start,
        segment_end=segment_end,
        representation_total_bytes=total,
        etag=_header(response.headers, "ETag"),
        last_modified=_header(response.headers, "Last-Modified"),
        result_state="unapplied",
        rejection_reason=None,
        local_bytes_after=None,
        local_sha256_after=None,
        final_bytes=None,
        final_sha256=None,
    )


def _finalize_receipt(
    receipt: AcquisitionReceipt,
    state: str,
    *,
    local_path: Path | None = None,
    final: bool = False,
    rejection_reason: str | None = None,
) -> AcquisitionReceipt:
    local_bytes = local_sha256 = None
    if local_path is not None:
        if local_path.is_symlink() or not local_path.is_file():
            raise IntegrityError("cannot bind receipt to a non-regular local artifact")
        local_bytes = local_path.stat().st_size
        local_sha256 = sha256_file(local_path)
    result = replace(
        receipt,
        result_state=state,
        rejection_reason=rejection_reason,
        local_bytes_after=local_bytes,
        local_sha256_after=local_sha256,
        final_bytes=local_bytes if final else None,
        final_sha256=local_sha256 if final else None,
    )
    result.validate()
    return result


def canonical_receipt_bytes(receipt: AcquisitionReceipt) -> bytes:
    receipt.validate()
    row = asdict(receipt)
    row["request_headers"] = [list(item) for item in receipt.request_headers]
    row["redirect_chain"] = list(receipt.redirect_chain)
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ZipMemberIdentity:
    filename: str
    compressed_bytes: int
    uncompressed_bytes: int
    crc32: int
    compression_method: int = zipfile.ZIP_STORED
    flag_bits: int = 0
    external_attr: int = 0
    is_directory: bool = False

    def validate(self) -> None:
        if (
            not self.filename
            or "\0" in self.filename
            or "\\" in self.filename
            or any(ord(character) < 32 or ord(character) == 127 for character in self.filename)
            or unicodedata.normalize("NFC", self.filename) != self.filename
        ):
            raise IntegrityError("ZIP member name is blank, unsafe, or non-NFC")
        raw = self.filename[:-1] if self.is_directory and self.filename.endswith("/") else self.filename
        parts = raw.split("/")
        posix = PurePosixPath(raw)
        if (
            not raw
            or posix.is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
            or re.fullmatch(r"[A-Za-z]:.*", parts[0]) is not None
            or any(":" in part for part in parts)
            or self.filename.endswith("/") != self.is_directory
        ):
            raise IntegrityError(f"ZIP member is not a safe relative name: {self.filename!r}")
        if min(self.compressed_bytes, self.uncompressed_bytes, self.crc32, self.flag_bits, self.external_attr) < 0:
            raise IntegrityError("ZIP member metadata cannot be negative")
        if self.crc32 > 0xFFFFFFFF or self.flag_bits > 0xFFFF or self.external_attr > 0xFFFFFFFF:
            raise IntegrityError("ZIP member metadata exceeds its field width")
        if self.compression_method not in _SAFE_ZIP_COMPRESSION_METHODS:
            raise IntegrityError(f"unsupported ZIP compression method: {self.compression_method}")
        if self.flag_bits & _UNSAFE_ZIP_FLAG_BITS:
            raise IntegrityError("ZIP member uses encryption/unsafe general-purpose flags")
        unix_mode = (self.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(unix_mode):
            raise IntegrityError("ZIP member is a symbolic link")
        file_type = stat.S_IFMT(unix_mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise IntegrityError("ZIP member is a device/socket/FIFO special file")
        if (self.is_directory and file_type == stat.S_IFREG) or (
            not self.is_directory and file_type == stat.S_IFDIR
        ):
            raise IntegrityError("ZIP member name/type metadata is inconsistent")
        if self.is_directory and (self.uncompressed_bytes != 0 or self.crc32 != 0):
            raise IntegrityError("ZIP directory member has content")


def _zip_member_from_info(info: zipfile.ZipInfo) -> ZipMemberIdentity:
    member = ZipMemberIdentity(
        filename=info.filename,
        compressed_bytes=info.compress_size,
        uncompressed_bytes=info.file_size,
        crc32=info.CRC,
        compression_method=info.compress_type,
        flag_bits=info.flag_bits,
        external_attr=info.external_attr,
        is_directory=info.is_dir(),
    )
    member.validate()
    return member


def _read_zip_central_directory(path: Path) -> tuple[ZipMemberIdentity, ...]:
    """Read only ZIP central-directory metadata; never open/decompress a member."""

    if path.is_symlink() or not path.is_file():
        raise IntegrityError("ZIP candidate must be a regular non-symlink file")
    try:
        with zipfile.ZipFile(path) as archive:
            members = tuple(_zip_member_from_info(info) for info in archive.infolist())
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise IntegrityError("source is not a parseable ZIP central directory") from exc
    names = [member.filename for member in members]
    if not members or len(names) != len(set(names)):
        raise IntegrityError("ZIP central directory is empty or has duplicate member names")
    if len({name.casefold() for name in names}) != len(names):
        raise IntegrityError("ZIP member names collide case-insensitively")
    return tuple(sorted(members, key=lambda item: item.filename))


@dataclass(frozen=True)
class ZipCandidateManifest:
    source_id: str
    archive_bytes: int
    archive_sha256: str
    members: tuple[ZipMemberIdentity, ...]

    def validate(self) -> None:
        if Path(self.source_id).name != self.source_id or not self.source_id:
            raise IntegrityError("ZIP candidate source ID is unsafe")
        if self.archive_bytes <= 0 or _SHA256.fullmatch(self.archive_sha256) is None:
            raise IntegrityError("ZIP candidate archive byte/hash pin is invalid")
        if not self.members:
            raise IntegrityError("ZIP candidate manifest has no members")
        for member in self.members:
            member.validate()
        names = [member.filename for member in self.members]
        if (
            names != sorted(names)
            or len(names) != len(set(names))
            or len(names) != len({name.casefold() for name in names})
        ):
            raise IntegrityError("ZIP candidate members must be uniquely filename-sorted")


def build_zip_candidate_manifest(path: Path, spec: FrozenInput) -> ZipCandidateManifest:
    """Build the pre-approval member manifest without reading member contents."""

    spec.validate(release_mode=True)
    if spec.artifact_type not in {"ZIP", "XLSX"}:
        raise IntegrityError("ZIP candidate inspection requires ZIP/XLSX artifact type")
    validate_final(path, spec)
    candidate = ZipCandidateManifest(
        spec.source_id,
        spec.bytes,
        spec.sha256,
        _read_zip_central_directory(path),
    )
    candidate.validate()
    return candidate


def canonical_zip_candidate_manifest_bytes(candidate: ZipCandidateManifest) -> bytes:
    candidate.validate()
    row = {
        "source_id": candidate.source_id,
        "archive_bytes": candidate.archive_bytes,
        "archive_sha256": candidate.archive_sha256,
        "members": [asdict(member) for member in candidate.members],
    }
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ZipManifestApproval:
    source_id: str
    archive_sha256: str
    candidate_manifest_sha256: str
    approval_id: str
    approved_by: str
    approved_at_utc: str
    selected_member_filename: str | None = None
    selected_member_central_metadata_sha256: str | None = None

    def validate(self) -> None:
        if (
            not self.source_id
            or not self.approval_id.strip()
            or not self.approved_by.strip()
            or _SHA256.fullmatch(self.archive_sha256) is None
            or _SHA256.fullmatch(self.candidate_manifest_sha256) is None
        ):
            raise IntegrityError("ZIP member-manifest approval is incomplete")
        _parse_utc_timestamp(self.approved_at_utc, "ZIP manifest approval timestamp")
        if (self.selected_member_filename is None) != (
            self.selected_member_central_metadata_sha256 is None
        ):
            raise IntegrityError("ZIP selected-member approval fields are incoherent")
        if self.selected_member_filename is not None:
            selected = PurePosixPath(self.selected_member_filename)
            if (
                len(selected.parts) != 1
                or selected.name != self.selected_member_filename
                or selected.name in {".", ".."}
                or _SHA256.fullmatch(
                    self.selected_member_central_metadata_sha256 or ""
                )
                is None
            ):
                raise IntegrityError(
                    "ZIP selected-member approval is not a safe root member binding"
                )
        if self.source_id == "NPPES-202606-V2" and self.selected_member_filename is None:
            raise IntegrityError("NPPES ZIP approval must bind one selected root member")


def canonical_zip_manifest_approval_bytes(approval: ZipManifestApproval) -> bytes:
    approval.validate()
    return canonical_json_bytes(asdict(approval))


@dataclass(frozen=True)
class ZipApprovalTrustRoot:
    """Independent pin binding a candidate manifest to its member approval.

    The candidate and approval files are ordinary candidate artifacts and
    therefore cannot establish their own authority.  This control becomes a
    trust root only after its exact bytes are compared with a digest supplied
    separately by the integrator (for example, a detached control value).
    """

    schema_version: str
    trust_root_id: str
    source_tree_id: str
    bundle_manifest_sha256: str
    authorization_id: str
    source_id: str
    archive_sha256: str
    candidate_manifest_sha256: str
    member_approval_sha256: str
    approved_by: str
    approved_at_utc: str
    selected_member_filename: str | None = None
    selected_member_central_metadata_sha256: str | None = None

    def validate(self) -> None:
        if (
            not isinstance(self.schema_version, str)
            or self.schema_version != ZIP_APPROVAL_TRUST_ROOT_SCHEMA_VERSION
        ):
            raise IntegrityError("ZIP approval trust-root schema is invalid")
        validate_safe_id(self.trust_root_id, label="ZIP approval trust-root ID")
        validate_safe_id(self.authorization_id, label="ZIP approval authorization ID")
        validate_safe_id(self.source_id, label="ZIP approval source ID")
        for label, value in (
            ("source-tree ID", self.source_tree_id),
            ("bundle-manifest hash", self.bundle_manifest_sha256),
            ("archive hash", self.archive_sha256),
            ("candidate-manifest hash", self.candidate_manifest_sha256),
            ("member-approval hash", self.member_approval_sha256),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise IntegrityError(f"ZIP approval trust root has an invalid {label}")
        if not isinstance(self.approved_by, str) or not self.approved_by.strip():
            raise IntegrityError("ZIP approval trust root requires an approver")
        if not isinstance(self.approved_at_utc, str):
            raise IntegrityError("ZIP approval trust-root timestamp is invalid")
        _parse_utc_timestamp(self.approved_at_utc, "ZIP approval trust-root timestamp")
        if (self.selected_member_filename is None) != (
            self.selected_member_central_metadata_sha256 is None
        ):
            raise IntegrityError("ZIP trust-root selected-member fields are incoherent")
        if self.selected_member_filename is not None:
            selected = PurePosixPath(self.selected_member_filename)
            if (
                len(selected.parts) != 1
                or selected.name != self.selected_member_filename
                or selected.name in {".", ".."}
                or _SHA256.fullmatch(
                    self.selected_member_central_metadata_sha256 or ""
                )
                is None
            ):
                raise IntegrityError(
                    "ZIP trust root does not bind a safe selected root member"
                )
        if self.source_id == "NPPES-202606-V2" and self.selected_member_filename is None:
            raise IntegrityError("NPPES ZIP trust root must bind one selected root member")


def zip_member_central_metadata_sha256(member: ZipMemberIdentity) -> str:
    """Hash the exact approved central-directory identity for one ZIP member."""

    member.validate()
    return sha256_bytes(canonical_json_bytes(asdict(member)))


@dataclass(frozen=True)
class TrustedZipApproval:
    """A member approval proven against an independently pinned trust root."""

    approval: ZipManifestApproval
    trust_root: ZipApprovalTrustRoot
    trust_root_sha256: str
    _authorization_token: object = field(repr=False, compare=False)

    @property
    def approval_id(self) -> str:
        return self.approval.approval_id

    def validate(self, candidate: ZipCandidateManifest) -> None:
        if self._authorization_token is not _ZIP_APPROVAL_AUTHORIZATION_TOKEN:
            raise IntegrityError("ZIP approval did not pass the external-pin verifier")
        candidate.validate()
        self.approval.validate()
        self.trust_root.validate()
        if _SHA256.fullmatch(self.trust_root_sha256) is None:
            raise IntegrityError("independently approved ZIP trust-root hash is absent")
        candidate_sha256 = sha256_bytes(
            canonical_zip_candidate_manifest_bytes(candidate)
        )
        approval_sha256 = sha256_bytes(
            canonical_zip_manifest_approval_bytes(self.approval)
        )
        if (
            self.trust_root.source_id != candidate.source_id
            or self.trust_root.archive_sha256 != candidate.archive_sha256
            or self.trust_root.candidate_manifest_sha256 != candidate_sha256
            or self.trust_root.member_approval_sha256 != approval_sha256
            or self.approval.source_id != candidate.source_id
            or self.approval.archive_sha256 != candidate.archive_sha256
            or self.approval.candidate_manifest_sha256 != candidate_sha256
            or self.trust_root.selected_member_filename
            != self.approval.selected_member_filename
            or self.trust_root.selected_member_central_metadata_sha256
            != self.approval.selected_member_central_metadata_sha256
        ):
            raise IntegrityError(
                "independently pinned ZIP trust root does not bind this candidate/approval"
            )
        if self.approval.selected_member_filename is not None:
            selected = next(
                (
                    member
                    for member in candidate.members
                    if not member.is_directory
                    and member.filename == self.approval.selected_member_filename
                ),
                None,
            )
            if (
                selected is None
                or zip_member_central_metadata_sha256(selected)
                != self.approval.selected_member_central_metadata_sha256
            ):
                raise IntegrityError(
                    "approved ZIP selected-member central metadata differs from candidate"
                )


def authorize_zip_approval(
    candidate: ZipCandidateManifest,
    member_approval_body: bytes,
    trust_root_body: bytes,
    *,
    independently_approved_trust_root_sha256: str,
    source_tree_id: str,
    bundle_manifest_sha256: str,
    authorization_id: str,
) -> TrustedZipApproval:
    """Authorize CRC/payload access from an external trust-root digest.

    This function performs no archive member access.  An absent, malformed, or
    mismatched external pin fails before a :class:`TrustedZipApproval` exists.
    """

    if _SHA256.fullmatch(independently_approved_trust_root_sha256) is None:
        raise IntegrityError(
            "independently approved ZIP trust-root SHA-256 is required"
        )
    if sha256_bytes(trust_root_body) != independently_approved_trust_root_sha256:
        raise IntegrityError(
            "ZIP approval trust root differs from the independently supplied pin"
        )
    raw_approval = require_canonical_json(
        member_approval_body, label="ZIP member approval"
    )
    approval_fields = tuple(ZipManifestApproval.__dataclass_fields__)
    if not isinstance(raw_approval, dict) or tuple(raw_approval) != approval_fields:
        raise IntegrityError("ZIP member approval schema mismatch")
    approval = ZipManifestApproval(**raw_approval)
    approval.validate()

    raw_trust = require_canonical_json(
        trust_root_body, label="ZIP approval trust root"
    )
    trust_fields = tuple(ZipApprovalTrustRoot.__dataclass_fields__)
    if not isinstance(raw_trust, dict) or tuple(raw_trust) != trust_fields:
        raise IntegrityError("ZIP approval trust-root keys/order mismatch")
    reject_control_sentinels(raw_trust, label="ZIP approval trust root")
    trust_root = ZipApprovalTrustRoot(**raw_trust)
    trust_root.validate()
    if (
        trust_root.source_tree_id != source_tree_id
        or trust_root.bundle_manifest_sha256 != bundle_manifest_sha256
        or trust_root.authorization_id != authorization_id
    ):
        raise IntegrityError(
            "ZIP approval trust root is not bound to this frozen runtime"
        )
    trusted = TrustedZipApproval(
        approval,
        trust_root,
        independently_approved_trust_root_sha256,
        _ZIP_APPROVAL_AUTHORIZATION_TOKEN,
    )
    trusted.validate(candidate)
    return trusted


@dataclass(frozen=True)
class VerifiedZipArchive:
    path: Path
    source_id: str
    archive_bytes: int
    archive_sha256: str
    candidate_manifest_sha256: str
    approval_id: str
    members: tuple[ZipMemberIdentity, ...]
    member_sha256s: tuple[tuple[str, str], ...]

    def validate(self) -> None:
        if self.path.is_symlink() or not self.path.is_file():
            raise IntegrityError("verified ZIP path is no longer a regular file")
        if self.path.stat().st_size != self.archive_bytes or sha256_file(self.path) != self.archive_sha256:
            raise IntegrityError("verified ZIP bytes changed after CRC verification")
        if _SHA256.fullmatch(self.candidate_manifest_sha256) is None or not self.approval_id:
            raise IntegrityError("verified ZIP approval identity is incomplete")
        expected_names = sorted(
            member.filename for member in self.members if not member.is_directory
        )
        if (
            [name for name, _digest in self.member_sha256s] != expected_names
            or any(_SHA256.fullmatch(digest) is None for _name, digest in self.member_sha256s)
        ):
            raise IntegrityError("verified ZIP member SHA-256 set is incomplete")


def verify_zip_after_approval(
    path: Path,
    candidate: ZipCandidateManifest,
    trusted_approval: TrustedZipApproval | ZipManifestApproval,
    *,
    chunk_size: int = 1024 * 1024,
) -> VerifiedZipArchive:
    """After explicit approval, recheck central metadata and stream every member CRC."""

    candidate.validate()
    if isinstance(trusted_approval, TrustedZipApproval):
        trusted_approval.validate(candidate)
        approval = trusted_approval.approval
    elif candidate.source_id in {
        "NPPES-202606-V2",
        "HUD-ZIP-COUNTY-2025Q4",
    }:
        raise IntegrityError(
            "NPPES/HUD ZIP payload access requires an independently pinned trust root"
        )
    elif isinstance(trusted_approval, ZipManifestApproval):
        # Other source-specific gates (currently TIGER) retain their separate
        # externally receipted approval contract.  NPPES and HUD may never use
        # this compatibility branch.
        approval = trusted_approval
        approval.validate()
    else:
        raise IntegrityError("ZIP approval object type is invalid")
    candidate_hash = sha256_bytes(canonical_zip_candidate_manifest_bytes(candidate))
    if (
        approval.source_id != candidate.source_id
        or approval.archive_sha256 != candidate.archive_sha256
        or approval.candidate_manifest_sha256 != candidate_hash
    ):
        raise IntegrityError("ZIP approval does not bind this candidate manifest")
    if path.is_symlink() or not path.is_file():
        raise IntegrityError("approved ZIP path is not a regular file")
    if path.stat().st_size != candidate.archive_bytes or sha256_file(path) != candidate.archive_sha256:
        raise IntegrityError("approved ZIP archive byte/hash identity changed")

    # Approval is fully checked before this second central-directory open and
    # before the first archive.open() member decompression.
    actual = _read_zip_central_directory(path)
    if actual != candidate.members:
        raise IntegrityError("ZIP central directory changed after manifest approval")
    try:
        member_sha256s: list[tuple[str, str]] = []
        with zipfile.ZipFile(path) as archive:
            infos = {info.filename: info for info in archive.infolist()}
            for member in candidate.members:
                if member.is_directory:
                    continue
                size = 0
                crc = 0
                digest = hashlib.sha256()
                with archive.open(infos[member.filename], "r") as handle:
                    for chunk in iter(lambda: handle.read(chunk_size), b""):
                        size += len(chunk)
                        crc = zlib.crc32(chunk, crc)
                        digest.update(chunk)
                if size != member.uncompressed_bytes or (crc & 0xFFFFFFFF) != member.crc32:
                    raise IntegrityError(f"ZIP streaming CRC/size mismatch: {member.filename}")
                member_sha256s.append((member.filename, digest.hexdigest()))
    except IntegrityError:
        raise
    except (OSError, RuntimeError, NotImplementedError, EOFError, zipfile.BadZipFile) as exc:
        raise IntegrityError("ZIP streaming CRC verification failed") from exc
    return VerifiedZipArchive(
        path,
        candidate.source_id,
        candidate.archive_bytes,
        candidate.archive_sha256,
        candidate_hash,
        approval.approval_id,
        candidate.members,
        tuple(sorted(member_sha256s)),
    )


@contextmanager
def open_verified_zip_member(
    verified: VerifiedZipArchive, filename: str
) -> Iterator[BinaryIO]:
    """Open a member only from a previously approval- and CRC-verified archive."""

    verified.validate()
    allowed = {member.filename for member in verified.members if not member.is_directory}
    if filename not in allowed:
        raise IntegrityError(f"member is not in the approved ZIP manifest: {filename}")
    try:
        with zipfile.ZipFile(verified.path) as archive:
            if _read_zip_central_directory(verified.path) != verified.members:
                raise IntegrityError("ZIP central directory changed before member parsing")
            with archive.open(filename, "r") as handle:
                yield handle
    except IntegrityError:
        raise
    except (OSError, RuntimeError, NotImplementedError, EOFError, zipfile.BadZipFile) as exc:
        raise IntegrityError("approved ZIP member could not be opened") from exc


def validate_zip_container(
    _path: Path, _expected_members: tuple[ZipMemberIdentity, ...]
) -> None:
    """Retired unsafe one-step API; callers must use the explicit approval gate."""

    raise IntegrityError(
        "one-step ZIP validation is forbidden; build, approve, then stream-verify the manifest"
    )


@dataclass(frozen=True)
class ArtifactValidator:
    validator_id: str
    artifact_types: tuple[str, ...]
    callback: Callable[[Path], None]

    def validate_registration(self) -> None:
        if _VALIDATOR_ID.fullmatch(self.validator_id) is None:
            raise IntegrityError("artifact validator ID is invalid")
        if not self.artifact_types or any(
            item not in SUPPORTED_ARTIFACT_TYPES for item in self.artifact_types
        ):
            raise IntegrityError("artifact validator type contract is invalid")
        if len(self.artifact_types) != len(set(self.artifact_types)) or not callable(self.callback):
            raise IntegrityError("artifact validator registration is duplicated/non-callable")

    def __call__(self, path: Path) -> None:
        self.validate_registration()
        self.callback(path)


def csv_artifact_validator(
    validator_id: str,
    *,
    required_fields: tuple[str, ...],
    unique_fields: tuple[str, ...] = (),
    expected_header: tuple[str, ...] | None = None,
    delimiter: str = ",",
    encoding: str = "utf-8-sig",
    minimum_rows: int = 1,
) -> ArtifactValidator:
    if (
        len(delimiter) != 1
        or encoding not in {"utf-8-sig", "cp1252"}
        or minimum_rows < 1
        or not required_fields
        or len(required_fields) != len(set(required_fields))
        or len(unique_fields) != len(set(unique_fields))
        or (expected_header is not None and len(expected_header) != len(set(expected_header)))
    ):
        raise IntegrityError("CSV validator encoding/delimiter/minimum row contract is invalid")

    def validate(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise IntegrityError("CSV artifact must be a regular non-symlink file")
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.reader(handle, delimiter=delimiter, strict=True)
                header = next(reader)
                if (
                    not header
                    or any(not field or "\0" in field for field in header)
                    or len(header) != len(set(header))
                ):
                    raise IntegrityError("CSV header is blank, duplicated, or unsafe")
                if expected_header is not None and tuple(header) != expected_header:
                    raise IntegrityError("CSV header does not equal the frozen ordered schema")
                missing = [field for field in required_fields if field not in header]
                missing_unique = [field for field in unique_fields if field not in header]
                if missing or missing_unique:
                    raise IntegrityError(
                        f"CSV source missing required fields: {missing + missing_unique}"
                    )
                indexes = tuple(header.index(field) for field in unique_fields)
                seen: set[tuple[str, ...]] = set()
                row_count = 0
                for row_number, row in enumerate(reader, start=2):
                    row_count += 1
                    if len(row) != len(header) or any("\0" in value for value in row):
                        raise IntegrityError(f"CSV row width/content mismatch at row {row_number}")
                    if indexes:
                        key = tuple(row[index] for index in indexes)
                        if any(value == "" for value in key) or key in seen:
                            raise IntegrityError(f"CSV unique key is blank/duplicated at row {row_number}")
                        seen.add(key)
                if row_count < minimum_rows:
                    raise IntegrityError("CSV source has no required data rows")
        except IntegrityError:
            raise
        except (OSError, UnicodeError, csv.Error, StopIteration) as exc:
            raise IntegrityError("source is not a complete parseable nonempty CSV") from exc

    return ArtifactValidator(validator_id, ("CSV", "TEXT"), validate)


def csv_schema_validator(required_fields: tuple[str, ...]) -> Callable[[Path], None]:
    """Compatibility wrapper retaining strict full-file validation."""

    return csv_artifact_validator(
        "legacy-csv-schema", required_fields=required_fields
    ).callback


def _reject_json_constant(value: str) -> None:
    raise IntegrityError(f"non-finite JSON number is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def json_artifact_validator(
    validator_id: str,
    *,
    top_level: str,
    required_fields: tuple[str, ...] = (),
    unique_field: str | None = None,
    minimum_items: int = 1,
) -> ArtifactValidator:
    if (
        top_level not in {"array", "object"}
        or minimum_items < 1
        or not required_fields
        or len(required_fields) != len(set(required_fields))
    ):
        raise IntegrityError("JSON validator shape contract is invalid")

    def validate(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise IntegrityError("JSON artifact must be a regular non-symlink file")
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8-sig"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object,
            )
        except IntegrityError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IntegrityError("source is not complete strict UTF-8 JSON") from exc
        if top_level == "object":
            if not isinstance(payload, dict) or len(payload) < minimum_items:
                raise IntegrityError("JSON top-level object is empty or wrong type")
            missing = [field for field in required_fields if field not in payload]
            if missing:
                raise IntegrityError(f"JSON object missing required fields: {missing}")
            return
        if not isinstance(payload, list) or len(payload) < minimum_items:
            raise IntegrityError("JSON top-level array is empty or wrong type")
        seen: set[object] = set()
        for index, row in enumerate(payload):
            if not isinstance(row, dict):
                raise IntegrityError(f"JSON array row {index} is not an object")
            missing = [field for field in required_fields if field not in row]
            if missing:
                raise IntegrityError(f"JSON row {index} missing required fields: {missing}")
            if unique_field is not None:
                key = row.get(unique_field)
                try:
                    duplicate = key in seen
                    blank = key is None or key == ""
                except TypeError as exc:
                    raise IntegrityError("JSON unique key is not scalar/hashable") from exc
                if blank or duplicate:
                    raise IntegrityError(f"JSON unique key is blank/duplicated at row {index}")
                seen.add(key)

    return ArtifactValidator(validator_id, ("JSON",), validate)


def zip_artifact_validator(
    validator_id: str,
    candidate: ZipCandidateManifest,
    approval: TrustedZipApproval,
) -> ArtifactValidator:
    def validate(path: Path) -> None:
        verify_zip_after_approval(path, candidate, approval)

    return ArtifactValidator(validator_id, ("ZIP",), validate)


def xlsx_artifact_validator(
    validator_id: str,
    candidate: ZipCandidateManifest,
    approval: TrustedZipApproval,
    *,
    required_sheet_names: tuple[str, ...] = (),
    required_headers: tuple[str, ...] | None = None,
    unique_fields: tuple[str, ...] = (),
) -> ArtifactValidator:
    if not required_sheet_names or len(required_sheet_names) != len(set(required_sheet_names)):
        raise IntegrityError("XLSX validator requires unique frozen sheet names")
    if required_headers is not None and (
        not required_headers
        or len(required_headers) != len(set(required_headers))
        or any(not field for field in required_headers)
        or any(field not in required_headers for field in unique_fields)
    ):
        raise IntegrityError("XLSX ordered-header/unique-key contract is invalid")
    required_members = {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
    }

    def local_name(element: ElementTree.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    def parse_member(verified: VerifiedZipArchive, filename: str) -> ElementTree.Element:
        try:
            with open_verified_zip_member(verified, filename) as handle:
                return ElementTree.fromstring(handle.read())
        except ElementTree.ParseError as exc:
            raise IntegrityError(f"XLSX OOXML member is not parseable: {filename}") from exc

    def relationship_map(root: ElementTree.Element, label: str) -> dict[str, tuple[str, str]]:
        relationships: dict[str, tuple[str, str]] = {}
        for element in root:
            if local_name(element) != "Relationship":
                continue
            relationship_id = element.attrib.get("Id", "")
            target = element.attrib.get("Target", "")
            relationship_type = element.attrib.get("Type", "")
            if (
                not relationship_id
                or not target
                or not relationship_type
                or relationship_id in relationships
                or element.attrib.get("TargetMode", "Internal") != "Internal"
            ):
                raise IntegrityError(f"XLSX {label} relationships are unsafe/duplicated")
            relationships[relationship_id] = (target, relationship_type)
        if not relationships:
            raise IntegrityError(f"XLSX {label} relationships are empty")
        return relationships

    def worksheet_member(target: str) -> str:
        if target.startswith("/"):
            member_name = target[1:]
        else:
            target_parts = target.split("/")
            if any(part in {"", ".", ".."} for part in target_parts):
                raise IntegrityError("XLSX worksheet relationship target is unsafe")
            member_name = str(PurePosixPath("xl", *target_parts))
        if not member_name.startswith("xl/worksheets/") or not member_name.endswith(".xml"):
            raise IntegrityError("XLSX relationship does not target a worksheet member")
        return member_name

    def shared_strings(verified: VerifiedZipArchive, names: set[str]) -> tuple[str, ...]:
        if "xl/sharedStrings.xml" not in names:
            return ()
        root = parse_member(verified, "xl/sharedStrings.xml")
        if local_name(root) != "sst":
            raise IntegrityError("XLSX shared-strings root is invalid")
        values: list[str] = []
        for item in root:
            if local_name(item) != "si":
                continue
            values.append("".join(node.text or "" for node in item.iter() if local_name(node) == "t"))
        return tuple(values)

    def column_index(reference: str, expected_row: int) -> int:
        match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", reference)
        if match is None or int(match.group(2)) != expected_row:
            raise IntegrityError("XLSX cell reference is malformed or on the wrong row")
        index = 0
        for character in match.group(1):
            index = index * 26 + ord(character) - ord("A") + 1
        return index - 1

    def cell_text(cell: ElementTree.Element, strings: tuple[str, ...]) -> str:
        if any(local_name(child) == "f" for child in cell):
            raise IntegrityError("XLSX formulas are forbidden in a frozen HUD table")
        cell_type = cell.attrib.get("t", "n")
        values = [child.text or "" for child in cell if local_name(child) == "v"]
        if cell_type == "inlineStr":
            return "".join(
                node.text or "" for node in cell.iter() if local_name(node) == "t"
            )
        if len(values) > 1:
            raise IntegrityError("XLSX cell has multiple values")
        value = values[0] if values else ""
        if cell_type == "s":
            if not value.isdigit() or int(value) >= len(strings):
                raise IntegrityError("XLSX shared-string index is invalid")
            return strings[int(value)]
        if cell_type not in {"n", "str"}:
            raise IntegrityError(f"unsupported XLSX cell type: {cell_type!r}")
        return value

    def table_rows(
        worksheet: ElementTree.Element,
        strings: tuple[str, ...],
        expected_width: int | None,
    ) -> tuple[tuple[str, ...], ...]:
        sheet_data = [element for element in worksheet if local_name(element) == "sheetData"]
        if len(sheet_data) != 1:
            raise IntegrityError("XLSX worksheet must contain exactly one sheetData")
        rows: list[tuple[str, ...]] = []
        previous_row = 0
        for row in sheet_data[0]:
            if local_name(row) != "row":
                continue
            row_ref = row.attrib.get("r", "")
            if not row_ref.isdigit() or int(row_ref) <= previous_row:
                raise IntegrityError("XLSX row numbers are absent, duplicated, or unsorted")
            row_number = int(row_ref)
            previous_row = row_number
            cells: dict[int, str] = {}
            for cell in row:
                if local_name(cell) != "c":
                    continue
                index = column_index(cell.attrib.get("r", ""), row_number)
                if index in cells:
                    raise IntegrityError("XLSX row has a duplicated cell reference")
                cells[index] = cell_text(cell, strings)
            width = expected_width if expected_width is not None else (
                max(cells) + 1 if cells else 0
            )
            if width <= 0 or any(index >= width for index in cells):
                raise IntegrityError("XLSX row width exceeds the frozen table schema")
            rows.append(tuple(cells.get(index, "") for index in range(width)))
        if len(rows) < 2:
            raise IntegrityError("XLSX table is empty or header-only")
        return tuple(rows)

    def validate(path: Path) -> None:
        verified = verify_zip_after_approval(path, candidate, approval)
        names = {member.filename for member in verified.members}
        if not required_members <= names:
            raise IntegrityError("XLSX archive lacks required OOXML members")
        content_types = parse_member(verified, "[Content_Types].xml")
        package_relationships = parse_member(verified, "_rels/.rels")
        workbook_relationships = parse_member(verified, "xl/_rels/workbook.xml.rels")
        workbook = parse_member(verified, "xl/workbook.xml")
        if (
            local_name(content_types) != "Types"
            or local_name(package_relationships) != "Relationships"
            or local_name(workbook_relationships) != "Relationships"
            or local_name(workbook) != "workbook"
        ):
            raise IntegrityError("XLSX OOXML metadata has unexpected root elements")
        overrides = {
            element.attrib.get("PartName", ""): element.attrib.get("ContentType", "")
            for element in content_types
            if local_name(element) == "Override"
        }
        if (
            overrides.get("/xl/workbook.xml")
            != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
        ):
            raise IntegrityError("XLSX Content_Types does not bind the workbook")
        package = relationship_map(package_relationships, "package")
        office_documents = [
            target.lstrip("/")
            for target, relationship_type in package.values()
            if relationship_type.endswith("/officeDocument")
        ]
        if office_documents != ["xl/workbook.xml"]:
            raise IntegrityError("XLSX package relationship does not uniquely bind the workbook")
        relationships = relationship_map(workbook_relationships, "workbook")
        sheets = [
            element
            for element in workbook.iter()
            if local_name(element) == "sheet"
        ]
        sheet_names = [element.attrib.get("name", "") for element in sheets]
        if (
            not sheet_names
            or any(not name for name in sheet_names)
            or len(sheet_names) != len(set(sheet_names))
            or any(name not in sheet_names for name in required_sheet_names)
        ):
            raise IntegrityError("XLSX sheet-name schema is empty, duplicated, or incomplete")
        strings = shared_strings(verified, names)
        parsed_targets: set[str] = set()
        for sheet in sheets:
            relationship_ids = [
                value for key, value in sheet.attrib.items() if key.rsplit("}", 1)[-1] == "id"
            ]
            if len(relationship_ids) != 1 or relationship_ids[0] not in relationships:
                raise IntegrityError("XLSX sheet lacks one bound workbook relationship")
            target, relationship_type = relationships[relationship_ids[0]]
            if not relationship_type.endswith("/worksheet"):
                raise IntegrityError("XLSX workbook relation is not a worksheet")
            member_name = worksheet_member(target)
            if member_name not in names or member_name in parsed_targets:
                raise IntegrityError("XLSX worksheet member is absent or multiply referenced")
            if (
                overrides.get("/" + member_name)
                != "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
            ):
                raise IntegrityError("XLSX Content_Types does not bind the worksheet")
            parsed_targets.add(member_name)
            worksheet = parse_member(verified, member_name)
            if local_name(worksheet) != "worksheet":
                raise IntegrityError("XLSX worksheet relationship targets a non-worksheet")
            if sheet.attrib.get("name") not in required_sheet_names:
                continue
            rows = table_rows(
                worksheet,
                strings,
                len(required_headers) if required_headers is not None else None,
            )
            if required_headers is not None:
                if rows[0] != required_headers:
                    raise IntegrityError("XLSX header does not equal the frozen ordered schema")
                indexes = tuple(required_headers.index(field) for field in unique_fields)
                seen: set[tuple[str, ...]] = set()
                normalized_hud_keys: set[tuple[str, str]] = set()
                for row_number, row in enumerate(rows[1:], start=2):
                    key = tuple(row[index] for index in indexes)
                    if any(not value for value in key) or key in seen:
                        raise IntegrityError(
                            f"XLSX unique key is blank/duplicated at row {row_number}"
                        )
                    seen.add(key)
                    if required_headers == (
                        "ZIP", "COUNTY", "BUS_RATIO", "RES_RATIO", "TOT_RATIO",
                        "USPS_ZIP_PREF_STATE",
                    ):
                        zip5, county, *ratios, state = row
                        if (
                            not 1 <= len(zip5) <= 5
                            or not zip5.isascii()
                            or not zip5.isdigit()
                            or zip5.zfill(5) == "00000"
                            or not 1 <= len(county) <= 5
                            or not county.isascii()
                            or not county.isdigit()
                            or (
                                state
                                and (
                                    len(state) != 2
                                    or not state.isascii()
                                    or state.upper() != state
                                    or not state.isalpha()
                                )
                            )
                        ):
                            raise IntegrityError(f"HUD XLSX identifier/state type failure at row {row_number}")
                        normalized_key = (zip5.zfill(5), county.zfill(5))
                        if normalized_key in normalized_hud_keys:
                            raise IntegrityError(
                                f"HUD XLSX normalized ZIP/county key is duplicated at row {row_number}"
                            )
                        normalized_hud_keys.add(normalized_key)
                        for lexeme in ratios:
                            if lexeme == "":
                                continue
                            try:
                                ratio = Decimal(lexeme)
                            except InvalidOperation as exc:
                                raise IntegrityError(
                                    f"HUD XLSX ratio is not decimal at row {row_number}"
                                ) from exc
                            if not ratio.is_finite() or not Decimal(0) <= ratio <= Decimal(1):
                                raise IntegrityError(
                                    f"HUD XLSX ratio is outside [0,1] at row {row_number}"
                                )

    return ArtifactValidator(validator_id, ("XLSX",), validate)


def _run_artifact_validators(
    path: Path,
    spec: FrozenInput,
    validators: Iterable[Callable[[Path], None] | ArtifactValidator],
) -> None:
    if spec.magic_hex is not None:
        magic = bytes.fromhex(spec.magic_hex)
        with path.open("rb") as handle:
            if handle.read(len(magic)) != magic:
                raise IntegrityError("media signature mismatch")
    for validator in validators:
        if isinstance(validator, ArtifactValidator):
            validator.validate_registration()
            if spec.artifact_type not in validator.artifact_types:
                raise IntegrityError(
                    f"validator {validator.validator_id} cannot validate {spec.artifact_type}"
                )
        validator(path)


@dataclass(frozen=True)
class TransferResult:
    state: str
    final_path: Path | None
    partial_bytes: int
    receipt: AcquisitionReceipt
    receipt_path: Path | None = None


def _header(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value.strip()
    return None


def _validate_response_identity(
    response: HttpResponse,
    spec: FrozenInput,
    *,
    range_requested: bool,
    range_start: int,
) -> None:
    response.validate()
    if response.requested_url != spec.url:
        raise AcquisitionError("response does not belong to canonical URL")
    final = urlparse(response.final_url)
    if final.scheme != "https" or final.hostname not in spec.allowed_hosts:
        raise AcquisitionError("redirect escaped the HTTPS host allowlist")
    if response.final_url != spec.url:
        raise AcquisitionError("final URL drifted from the frozen canonical URL")
    for hop in response.redirect_chain:
        parsed = urlparse(hop)
        if parsed.scheme != "https" or parsed.hostname not in spec.allowed_hosts:
            raise AcquisitionError("redirect chain escaped the HTTPS host allowlist")
    actual_range = _request_header(response.request_headers, "Range")
    expected_range = f"bytes={range_start}-" if range_requested else None
    if actual_range != expected_range:
        raise AcquisitionError("actual Range request does not match local partial state")
    content_encoding = (_header(response.headers, "Content-Encoding") or "identity").lower()
    if content_encoding != "identity":
        raise AcquisitionError("canonical acquisition requires identity encoding")
    content_type = (_header(response.headers, "Content-Type") or "").split(";", 1)[0].strip()
    if spec.content_types and content_type not in spec.content_types:
        raise AcquisitionError(f"unexpected Content-Type: {content_type!r}")


def _prepare_root(root: Path) -> Path:
    root = root.absolute()
    if root.is_symlink():
        raise IntegrityError(f"managed root cannot be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise IntegrityError(f"managed root is not a regular directory: {root}")
    return root


def _assert_safe_managed_path(root: Path, path: Path) -> None:
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise IntegrityError(f"managed path escapes root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise IntegrityError(f"managed path contains a symlink: {current}")
        if current != path and not stat.S_ISDIR(mode):
            raise IntegrityError(f"managed path parent is not a directory: {current}")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_existing_regular(path: Path, root: Path, *, remove_symlink: bool = True) -> bool:
    _assert_safe_managed_path(root, path.parent)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        if remove_symlink:
            path.unlink()
            _fsync_directory(path.parent)
        raise IntegrityError(f"managed artifact path was a symlink: {path}")
    if not stat.S_ISREG(mode):
        raise IntegrityError(f"managed artifact path is not a regular file: {path}")
    return True


def _remove_corrupt_regular(path: Path, root: Path) -> None:
    if not _safe_existing_regular(path, root):
        return
    path.unlink()
    _fsync_directory(path.parent)


def _write_new(path: Path, body: bytes, *, root: Path | None = None) -> None:
    if root is not None:
        _assert_safe_managed_path(root, path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    if root is not None:
        _assert_safe_managed_path(root, path.parent)
        _safe_existing_regular(path, root) if path.exists() or path.is_symlink() else None
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise IntegrityError("write target is not a regular file")
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def _append(path: Path, body: bytes, *, root: Path | None = None) -> None:
    if root is not None:
        _assert_safe_managed_path(root, path)
        _safe_existing_regular(path, root, remove_symlink=False)
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise IntegrityError("append target is not a regular file")
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def _discard_partial(part: Path, root: Path | None = None) -> None:
    try:
        mode = os.lstat(part).st_mode
    except FileNotFoundError:
        return
    if root is not None:
        _assert_safe_managed_path(root, part.parent)
    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        raise IntegrityError("refusing to remove non-file partial path")
    part.unlink()
    _fsync_directory(part.parent)


def apply_http_response(
    root: Path,
    spec: FrozenInput,
    response: HttpResponse,
    *,
    range_requested: bool,
    validators: Iterable[Callable[[Path], None] | ArtifactValidator] = (),
) -> TransferResult:
    """Apply one 200/206/416 response using preregistered transfer semantics."""

    spec.validate()
    root = _prepare_root(root)
    final = spec.content_path(root)
    part = final.with_name(final.name + ".part")
    _assert_safe_managed_path(root, final.parent)
    final.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_managed_path(root, final.parent)
    final_exists = _safe_existing_regular(final, root)
    part_exists = _safe_existing_regular(part, root)
    existing = part.stat().st_size if part_exists else 0
    _validate_response_identity(
        response,
        spec,
        range_requested=range_requested,
        range_start=existing,
    )
    receipt = receipt_from_response(
        response,
        source_id=spec.source_id,
        release=spec.release,
        artifact_type=spec.artifact_type or "UNSPECIFIED",
    )
    if final_exists:
        try:
            validate_final(final, spec)
            _run_artifact_validators(final, spec, validators)
        except Exception:
            _remove_corrupt_regular(final, root)
            raise
        complete_receipt = _finalize_receipt(
            receipt, "already_valid", local_path=final, final=True
        )
        return TransferResult("already_valid", final, 0, complete_receipt)
    if existing > spec.bytes:
        _discard_partial(part, root)
        raise IntegrityError("stale partial exceeds expected byte length")

    if response.status == 200:
        # A server may ignore Range; the complete representation replaces the
        # old partial and is never appended to it.
        _write_new(part, response.body, root=root)
    elif response.status == 206:
        if not range_requested:
            _discard_partial(part, root)
            raise AcquisitionError("206 without a Range request")
        content_range = _header(response.headers, "Content-Range") or ""
        match = _CONTENT_RANGE_206.fullmatch(content_range)
        if match is None:
            _discard_partial(part, root)
            raise AcquisitionError("invalid 206 Content-Range")
        start, end, total = map(int, match.groups())
        if start != existing or total != spec.bytes or end < start or end >= total:
            _discard_partial(part, root)
            raise AcquisitionError("206 Content-Range disagrees with partial/manifest")
        if len(response.body) != end - start + 1:
            _discard_partial(part, root)
            raise AcquisitionError("206 body length disagrees with Content-Range")
        _append(part, response.body, root=root)
    elif response.status == 416:
        content_range = _header(response.headers, "Content-Range") or ""
        match = _CONTENT_RANGE_416.fullmatch(content_range)
        valid = (
            range_requested
            and match is not None
            and int(match.group(1)) == spec.bytes
            and existing == spec.bytes
            and sha256_file(part) == spec.sha256
        )
        if not valid:
            _discard_partial(part, root)
            raise AcquisitionError("invalid 416 completion claim")
    else:
        raise AcquisitionError(f"terminal HTTP status: {response.status}")

    size = part.stat().st_size
    if size > spec.bytes:
        _discard_partial(part, root)
        raise IntegrityError("partial exceeds expected byte length")
    if size < spec.bytes:
        partial_receipt = _finalize_receipt(receipt, "partial", local_path=part)
        return TransferResult("partial", None, size, partial_receipt)
    if sha256_file(part) != spec.sha256:
        _discard_partial(part, root)
        raise IntegrityError("completed transfer has wrong SHA-256")
    try:
        _run_artifact_validators(part, spec, validators)
    except Exception:
        _discard_partial(part, root)
        raise
    _assert_safe_managed_path(root, final)
    os.replace(part, final)
    _fsync_directory(final.parent)
    validate_final(final, spec)
    complete_receipt = _finalize_receipt(receipt, "complete", local_path=final, final=True)
    return TransferResult("complete", final, 0, complete_receipt)


def persist_receipt_atomic(root: Path, receipt: AcquisitionReceipt) -> Path:
    """Persist one canonical receipt by its own content hash via fsync + rename."""

    body = canonical_receipt_bytes(receipt)
    digest = sha256_bytes(body)
    root = _prepare_root(root)
    target = root / "receipts" / receipt.source_id / f"{digest}.json"
    _assert_safe_managed_path(root, target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_managed_path(root, target.parent)
    if _safe_existing_regular(target, root):
        if target.read_bytes() != body:
            _remove_corrupt_regular(target, root)
            raise IntegrityError("content-addressed receipt path contained different bytes")
        return target
    temporary = target.with_name(f".{target.name}.{os.getpid()}.part")
    _discard_partial(temporary, root)
    try:
        _write_new(temporary, body, root=root)
        _assert_safe_managed_path(root, target)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except Exception:
        _discard_partial(temporary, root)
        raise
    if target.read_bytes() != body:
        _remove_corrupt_regular(target, root)
        raise IntegrityError("receipt bytes changed during atomic persistence")
    return target


def execute_frozen_response(
    root: Path,
    spec: FrozenInput,
    response: HttpResponse,
    *,
    range_requested: bool,
    validation_context: object | None = None,
    receipt_root: Path | None = None,
) -> TransferResult:
    """Release executor with an immutable internal validator registry.

    Callers may pass approved data-only validation context, but cannot inject a
    callback or replace the source-specific validator set.
    """

    spec.validate(release_mode=True)
    base_receipt = receipt_from_response(
        response,
        source_id=spec.source_id,
        release=spec.release,
        artifact_type=spec.artifact_type or "UNSPECIFIED",
        allow_noncanonical=True,
    )
    destination = receipt_root or root
    try:
        from .source_registry import ReleaseValidationContext, resolve_release_validators

        if validation_context is not None and not isinstance(
            validation_context, ReleaseValidationContext
        ):
            raise IntegrityError("release validation context has the wrong immutable type")
        validators = resolve_release_validators(
            spec, context=validation_context
        )
        result = apply_http_response(
            root,
            spec,
            response,
            range_requested=range_requested,
            validators=validators,
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")
        rejected = _finalize_receipt(
            base_receipt, "rejected", rejection_reason=reason[:1000]
        )
        receipt_path = persist_receipt_atomic(destination, rejected)
        exc.add_note(f"rejected acquisition receipt: {receipt_path}")
        raise
    receipt_path = persist_receipt_atomic(destination, result.receipt)
    return replace(result, receipt_path=receipt_path)


def phase2a_acquisition_blocker() -> None:
    """Fail explicitly if a Phase-2A wrapper attempts live acquisition."""

    raise AcquisitionError(
        "live acquisition is disabled in Phase 2A; use approved Phase 2B/2C/2E tooling"
    )
