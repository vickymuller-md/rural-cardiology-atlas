"""Census Batch Geocoder request, response, and snapshot contracts.

This module contains no HTTP client.  Live acquisition must inject a transport
in the separately authorized acquisition command; offline builds consume only
the validated canonical snapshot produced here.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from datetime import datetime, timedelta
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping
from urllib.parse import urlparse

from .addresses import NormalizedAddress
from .contracts import (
    CENSUS_BENCHMARK_ID,
    CENSUS_BENCHMARK_NAME,
    CENSUS_GEOCODER_ENDPOINT,
    CENSUS_VINTAGE_ID,
    CENSUS_VINTAGE_NAME,
    STATE_FIPS,
)
from .errors import AcquisitionError, ContractError, IntegrityError

MAX_CHUNK_ROWS = 5_000
MAX_CHUNK_BYTES = 4_500_000
RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})
CSV_MEDIA_TYPES = frozenset({"text/csv", "application/csv"})
CENSUS_TEXT_ATTACHMENT_DISPOSITION = "attachment; filename=GeocodeResults.csv"

RESPONSE_FIELDS = (
    "request_id",
    "input_address",
    "match_indicator",
    "match_type",
    "matched_address",
    "coordinates",
    "tiger_line_id",
    "tiger_side",
    "state_code",
    "county_code",
    "tract_code",
    "block_code",
)


def census_response_representation_error(
    response_headers: Mapping[str, str] | Iterable[tuple[str, str]],
) -> str | None:
    """Return the frozen media-contract error for one HTTP 200 response.

    Census currently serves valid batch CSV bytes as ``text/plain``.  That
    representation is accepted only when the exact official CSV attachment
    disposition is also present; generic text responses remain forbidden.
    """

    items = (
        response_headers.items()
        if isinstance(response_headers, Mapping)
        else response_headers
    )
    headers: dict[str, str] = {}
    for key, value in items:
        lowered = key.lower()
        if lowered in headers:
            return "HTTP 200 response headers are duplicated case-insensitively"
        headers[lowered] = value
    raw_content_type = headers.get("content-type", "").strip()
    base_content_type = raw_content_type.split(";", 1)[0].strip().lower()
    if base_content_type in CSV_MEDIA_TYPES:
        return None
    if raw_content_type.lower() == "text/plain":
        disposition = headers.get("content-disposition", "").strip()
        if disposition == CENSUS_TEXT_ATTACHMENT_DISPOSITION:
            return None
        return "HTTP 200 text/plain response lacks exact Census CSV attachment disposition"
    return "HTTP 200 response Content-Type is not CSV"


@dataclass(frozen=True)
class GeocoderRequest:
    address_id: str
    street: str
    city: str
    state: str
    zip5: str


def request_from_address(address: NormalizedAddress) -> GeocoderRequest:
    if not address.direct_eligible:
        raise ContractError(f"address is not direct-eligible: {address.address_id}")
    return GeocoderRequest(
        address.address_id,
        address.submitted_street,
        address.city,
        address.state,
        address.zip5,
    )


def request_csv_bytes(requests: Iterable[GeocoderRequest]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for request in requests:
        if not request.address_id or not request.street:
            raise ContractError("Census request ID and street are required")
        writer.writerow(
            [request.address_id, request.street, request.city, request.state, request.zip5]
        )
    return output.getvalue().encode("utf-8")


@dataclass(frozen=True)
class RequestChunk:
    filename: str
    first_id: str
    last_id: str
    row_count: int
    byte_count: int
    sha256: str
    body: bytes


def chunk_requests(requests: Iterable[GeocoderRequest]) -> list[RequestChunk]:
    ordered = sorted(requests, key=lambda item: item.address_id)
    if len({request.address_id for request in ordered}) != len(ordered):
        raise IntegrityError("duplicate Census request ID")
    chunks: list[list[GeocoderRequest]] = []
    current: list[GeocoderRequest] = []
    current_size = 0
    for request in ordered:
        row = request_csv_bytes([request])
        if len(row) > MAX_CHUNK_BYTES:
            raise ContractError(f"single request exceeds byte limit: {request.address_id}")
        if current and (
            len(current) >= MAX_CHUNK_ROWS or current_size + len(row) > MAX_CHUNK_BYTES
        ):
            chunks.append(current)
            current = []
            current_size = 0
        current.append(request)
        current_size += len(row)
    if current:
        chunks.append(current)

    result: list[RequestChunk] = []
    for index, rows in enumerate(chunks, start=1):
        body = request_csv_bytes(rows)
        result.append(
            RequestChunk(
                filename=f"geocoder_request_{index:04d}.csv",
                first_id=rows[0].address_id,
                last_id=rows[-1].address_id,
                row_count=len(rows),
                byte_count=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                body=body,
            )
        )
    return result


def request_manifest_bytes(chunks: Iterable[RequestChunk]) -> bytes:
    ordered = sorted(chunks, key=lambda item: item.filename)
    rows = []
    for index, chunk in enumerate(ordered, start=1):
        if chunk.filename != f"geocoder_request_{index:04d}.csv":
            raise IntegrityError("geocoder chunk names are not consecutive")
        if (
            len(chunk.body) != chunk.byte_count
            or hashlib.sha256(chunk.body).hexdigest() != chunk.sha256
            or chunk.row_count > MAX_CHUNK_ROWS
            or chunk.byte_count > MAX_CHUNK_BYTES
        ):
            raise IntegrityError("geocoder request chunk metadata/body mismatch")
        rows.append({
            "filename": chunk.filename,
            "first_id": chunk.first_id,
            "last_id": chunk.last_id,
            "row_count": chunk.row_count,
            "byte_count": chunk.byte_count,
            "sha256": chunk.sha256,
        })
    return (json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class RawGeocoderRow:
    request_id: str
    input_address: str
    match_indicator: str
    match_type: str
    matched_address: str
    coordinates: str
    tiger_line_id: str
    tiger_side: str
    state_code: str
    county_code: str
    tract_code: str
    block_code: str


def parse_response(body: bytes, expected_ids: set[str]) -> list[RawGeocoderRow]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AcquisitionError("geocoder response is not UTF-8") from exc
    rows: list[RawGeocoderRow] = []
    try:
        for line_number, values in enumerate(csv.reader(io.StringIO(text)), start=1):
            if len(values) not in {3, len(RESPONSE_FIELDS)}:
                raise AcquisitionError(
                    f"geocoder row {line_number} has {len(values)} fields, expected 3 or 12"
                )
            values = [value.strip() for value in values]
            indicator = values[2]
            canonical = {"match": "Match", "tie": "Tie", "no_match": "No_Match"}.get(
                indicator.lower()
            )
            if canonical is None:
                raise AcquisitionError(f"unknown match indicator: {indicator!r}")
            if len(values) == 3:
                if canonical == "Match":
                    raise AcquisitionError(
                        f"geocoder Match row {line_number} must have 12 fields"
                    )
                values.extend([""] * (len(RESPONSE_FIELDS) - 3))
            values[2] = canonical
            rows.append(RawGeocoderRow(*values))
    except csv.Error as exc:
        raise AcquisitionError("unparseable geocoder CSV") from exc

    ids = [row.request_id for row in rows]
    if len(ids) != len(set(ids)):
        raise AcquisitionError("duplicate geocoder response ID")
    actual = set(ids)
    missing = expected_ids - actual
    extra = actual - expected_ids
    if missing or extra:
        raise AcquisitionError(
            f"geocoder ID coverage mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
        )
    return rows


@dataclass(frozen=True)
class CanonicalGeocode:
    request_id: str
    outcome: str
    match_indicator: str
    match_type: str
    matched_address: str
    longitude: str | None
    latitude: str | None
    county_fips: str | None
    accepted_attempt: int
    source_response_sha256: str


def validate_canonical_geocodes(
    rows: Iterable[CanonicalGeocode],
    *,
    expected_ids: set[str],
    request_states: Mapping[str, str],
    county_universe: set[str],
) -> tuple[CanonicalGeocode, ...]:
    """Validate a frozen canonical snapshot without trusting its producer."""

    items = tuple(rows)
    ids = [row.request_id for row in items]
    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
        raise IntegrityError("canonical geocoder ID coverage/uniqueness mismatch")
    if set(request_states) != expected_ids:
        raise IntegrityError("canonical geocoder request-state keys differ from requests")
    allowed_outcomes = {
        "Match", "Tie", "No_Match", "Invalid_Match", "Invalid_Match_State"
    }
    for row in items:
        if (
            row.outcome not in allowed_outcomes
            or row.match_indicator not in {"Match", "Tie", "No_Match"}
            or not isinstance(row.match_type, str)
            or not isinstance(row.matched_address, str)
            or not 1 <= row.accepted_attempt <= 3
            or re.fullmatch(r"[0-9a-f]{64}", row.source_response_sha256) is None
        ):
            raise IntegrityError("canonical geocoder controlled fields are invalid")
        if row.outcome in {"Invalid_Match", "Invalid_Match_State"}:
            if row.match_indicator != "Match":
                raise IntegrityError("invalid Match outcome lacks Match source indicator")
        elif row.outcome != row.match_indicator:
            raise IntegrityError("canonical geocoder outcome/indicator mismatch")
        expected_state = STATE_FIPS.get(request_states[row.request_id])
        if expected_state is None:
            raise IntegrityError("canonical geocoder source state is invalid")
        if row.outcome == "Match":
            if (
                row.longitude is None
                or row.latitude is None
                or row.county_fips is None
                or _parse_coordinates(f"{row.longitude},{row.latitude}") is None
                or row.county_fips not in county_universe
                or row.county_fips[:2] != expected_state
            ):
                raise IntegrityError("canonical geocoder Match geography is invalid")
        elif any(
            value is not None for value in (row.longitude, row.latitude, row.county_fips)
        ):
            raise IntegrityError("non-Match canonical geocoder row carries accepted geography")
    return tuple(sorted(items, key=lambda row: row.request_id))


def _parse_coordinates(value: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        return None
    try:
        longitude, latitude = map(Decimal, parts)
    except InvalidOperation:
        return None
    if not (longitude.is_finite() and latitude.is_finite()):
        return None
    if not (
        Decimal("-180") <= longitude <= Decimal("180")
        and Decimal("-90") <= latitude <= Decimal("90")
    ):
        return None
    return parts[0], parts[1]


def canonicalize_response(
    rows: Iterable[RawGeocoderRow],
    request_states: Mapping[str, str],
    county_universe: set[str],
    *,
    accepted_attempt: int,
    source_response_sha256: str,
) -> list[CanonicalGeocode]:
    if not 1 <= accepted_attempt <= 3:
        raise IntegrityError("accepted geocoder attempt must be 1, 2, or 3")
    if len(source_response_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in source_response_sha256
    ):
        raise IntegrityError("source response SHA-256 must be lowercase hexadecimal")
    result: list[CanonicalGeocode] = []
    for row in sorted(rows, key=lambda item: item.request_id):
        if row.request_id not in request_states:
            raise IntegrityError(f"missing request-state mapping: {row.request_id}")
        source_state = request_states[row.request_id]
        expected_state = STATE_FIPS.get(source_state)
        if expected_state is None:
            raise IntegrityError(f"invalid request source state: {source_state!r}")
        outcome = row.match_indicator
        longitude = latitude = county_fips = None
        if row.match_indicator == "Match":
            coordinate_pair = _parse_coordinates(row.coordinates)
            valid_geography = (
                re.fullmatch(r"[0-9]{2}", row.state_code, flags=re.ASCII) is not None
                and re.fullmatch(r"[0-9]{3}", row.county_code, flags=re.ASCII) is not None
                and row.state_code + row.county_code in county_universe
            )
            if coordinate_pair is None or not valid_geography:
                outcome = "Invalid_Match"
            elif row.state_code != expected_state:
                outcome = "Invalid_Match_State"
            else:
                outcome = "Match"
                longitude, latitude = coordinate_pair
                county_fips = row.state_code + row.county_code
        result.append(
            CanonicalGeocode(
                request_id=row.request_id,
                outcome=outcome,
                match_indicator=row.match_indicator,
                match_type=row.match_type,
                matched_address=row.matched_address,
                longitude=longitude,
                latitude=latitude,
                county_fips=county_fips,
                accepted_attempt=accepted_attempt,
                source_response_sha256=source_response_sha256,
            )
        )
    return result


def response_is_retryable(*, http_status: int | None, integrity_error: bool, transport_error: bool) -> bool:
    """Only whole-response/transport failures retry; record outcomes never do."""

    if http_status is not None and http_status != 200 and http_status not in RETRYABLE_HTTP:
        return False
    return transport_error or integrity_error or http_status in RETRYABLE_HTTP


@dataclass(frozen=True)
class AttemptSnapshot:
    request_sha256: str
    request_method: str
    requested_url: str
    final_url: str
    request_headers: tuple[tuple[str, str], ...]
    prepared_body_bytes: int
    prepared_body_sha256: str
    redirect_chain: tuple[str, ...]
    endpoint: str
    benchmark_id: str
    benchmark_name: str
    vintage_id: str
    vintage_name: str
    attempt: int
    started_at_utc: str
    ended_at_utc: str
    http_status: int | None
    response_headers: tuple[tuple[str, str], ...]
    response_body: bytes
    response_sha256: str | None
    transport_error: str | None = None
    integrity_error: str | None = None

    def validate(self) -> None:
        if not 1 <= self.attempt <= 3:
            raise AcquisitionError("attempt number must be 1..3")
        if (
            self.endpoint,
            self.benchmark_id,
            self.benchmark_name,
            self.vintage_id,
            self.vintage_name,
        ) != (
            CENSUS_GEOCODER_ENDPOINT,
            CENSUS_BENCHMARK_ID,
            CENSUS_BENCHMARK_NAME,
            CENSUS_VINTAGE_ID,
            CENSUS_VINTAGE_NAME,
        ):
            raise AcquisitionError("attempt uses an unpinned endpoint/benchmark/vintage")
        if len(self.request_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.request_sha256
        ):
            raise AcquisitionError("request hash must be lowercase SHA-256")
        if (
            self.request_method != "POST"
            or self.endpoint != CENSUS_GEOCODER_ENDPOINT
            or re.fullmatch(r"[0-9a-f]{64}", self.prepared_body_sha256) is None
            or type(self.prepared_body_bytes) is not int
            or self.prepared_body_bytes <= 0
        ):
            raise AcquisitionError("prepared Census request identity is invalid")
        if tuple(sorted(self.request_headers)) != self.request_headers or len(
            {key.lower() for key, _value in self.request_headers}
        ) != len(self.request_headers):
            raise AcquisitionError("prepared Census request headers are not unique/sorted")
        request_headers = {key.lower(): value for key, value in self.request_headers}
        if (
            set(request_headers) != {"accept-encoding", "content-length", "content-type"}
            or request_headers["accept-encoding"].lower() != "identity"
            or not request_headers["content-length"].isdigit()
            or int(request_headers["content-length"]) != self.prepared_body_bytes
            or re.fullmatch(
                r"multipart/form-data; boundary=ATLAS-V19-[0-9a-f]{32}",
                request_headers["content-type"],
            )
            is None
        ):
            raise AcquisitionError("prepared Census request headers violate the frozen contract")
        endpoint_host = urlparse(CENSUS_GEOCODER_ENDPOINT).hostname
        redirect_urls = (*self.redirect_chain, self.final_url)
        invalid_redirect = (
            self.requested_url != CENSUS_GEOCODER_ENDPOINT
            or len(self.redirect_chain) > 3
            or any(
            urlparse(url).scheme != "https" or urlparse(url).hostname != endpoint_host
            for url in redirect_urls
            )
        )
        if self.redirect_chain:
            if self.final_url != self.redirect_chain[-1]:
                invalid_redirect = True
        elif self.final_url != self.requested_url:
            invalid_redirect = True
        if invalid_redirect and not self.integrity_error:
            raise AcquisitionError("invalid Census URL/redirect lacks integrity_error evidence")
        parsed_times = []
        for value in (self.started_at_utc, self.ended_at_utc):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise AcquisitionError("attempt timestamp is not ISO-8601") from exc
            if parsed.tzinfo is None:
                raise AcquisitionError("attempt timestamp must include a timezone")
            if parsed.utcoffset() != timedelta(0):
                raise AcquisitionError("attempt *_at_utc timestamp must use UTC offset zero")
            parsed_times.append(parsed)
        if parsed_times[1] < parsed_times[0]:
            raise AcquisitionError("attempt end precedes its start")
        actual = hashlib.sha256(self.response_body).hexdigest()
        if self.http_status is not None and self.response_sha256 is None:
            raise AcquisitionError("HTTP response requires a response SHA-256")
        if self.http_status is None and not (self.transport_error or self.integrity_error):
            raise AcquisitionError("attempt without HTTP status requires a recorded failure")
        if self.http_status is not None and self.transport_error:
            raise AcquisitionError("transport failure cannot also have an HTTP status")
        if self.response_sha256 is not None and actual != self.response_sha256:
            raise AcquisitionError("attempt response hash mismatch")
        if tuple(sorted(self.response_headers)) != self.response_headers or len(
            {key.lower() for key, _value in self.response_headers}
        ) != len(self.response_headers):
            raise AcquisitionError("Census response headers are not unique/sorted")
        response_headers = {key.lower(): value for key, value in self.response_headers}
        content_encoding = response_headers.get("content-encoding", "identity").strip().lower()
        invalid_representation = self.http_status == 200 and (
            census_response_representation_error(self.response_headers) is not None
            or content_encoding not in {"", "identity"}
        )
        if invalid_representation and not self.integrity_error:
            raise AcquisitionError("invalid Census representation lacks integrity_error evidence")


def retry_delay_after(attempt: int) -> int | None:
    if attempt == 1:
        return 5
    if attempt == 2:
        return 20
    if attempt == 3:
        return None
    raise AcquisitionError("attempt number must be 1..3")


def first_integrity_valid_attempt(
    attempts: Iterable[AttemptSnapshot], expected_ids: set[str]
) -> tuple[AttemptSnapshot, list[RawGeocoderRow]]:
    """Accept the first whole-response-valid attempt, regardless of match rate."""

    ordered = sorted(attempts, key=lambda item: item.attempt)
    if [item.attempt for item in ordered] != list(range(1, len(ordered) + 1)):
        raise AcquisitionError("attempts must be consecutive starting at 1")
    if len(ordered) > 3:
        raise AcquisitionError("more than three geocoder attempts")
    if len({item.request_sha256 for item in ordered}) > 1:
        raise AcquisitionError("geocoder attempts changed request bytes")
    for index, snapshot in enumerate(ordered):
        snapshot.validate()
        if snapshot.http_status is not None and snapshot.http_status != 200 and snapshot.http_status not in RETRYABLE_HTTP:
            raise AcquisitionError(f"terminal geocoder HTTP {snapshot.http_status}")
        if snapshot.http_status == 200:
            if snapshot.transport_error:
                raise AcquisitionError("attempt cannot have HTTP 200 and a transport error")
            try:
                rows = parse_response(snapshot.response_body, expected_ids)
            except AcquisitionError:
                continue
            # Parser/coverage validity controls acceptance.  An externally
            # supplied integrity label cannot suppress a valid No_Match/Tie and
            # cause a retry toward a more favorable result.
            if index != len(ordered) - 1:
                raise AcquisitionError("attempts continued after the first integrity-valid response")
            return snapshot, rows
        if snapshot.transport_error or snapshot.integrity_error or snapshot.http_status in RETRYABLE_HTTP:
            continue
    raise AcquisitionError("no whole-response-valid geocoder attempt")


def canonicalize_accepted_attempt(
    snapshot: AttemptSnapshot,
    *,
    expected_ids: set[str],
    request_states: Mapping[str, str],
    county_universe: set[str],
) -> list[CanonicalGeocode]:
    snapshot.validate()
    if snapshot.http_status != 200 or snapshot.transport_error:
        raise AcquisitionError("accepted geocoder attempt is not a completed HTTP 200")
    rows = parse_response(snapshot.response_body, expected_ids)
    return canonicalize_response(
        rows,
        request_states,
        county_universe,
        accepted_attempt=snapshot.attempt,
        source_response_sha256=snapshot.response_sha256 or "",
    )


def canonical_snapshot_bytes(rows: Iterable[CanonicalGeocode]) -> bytes:
    lines: list[str] = []
    for row in sorted(rows, key=lambda item: item.request_id):
        normalized = {
            key: unicodedata.normalize("NFC", value) if isinstance(value, str) else value
            for key, value in asdict(row).items()
        }
        lines.append(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
