"""Disk-backed, chunked NPPES reconciliation and Phase-2D-II outputs."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, TextIO

from .bundle import atomic_write, canonical_data_json_bytes, canonical_json_bytes
from .contracts import NPPES_ANALYTICAL_FIELDS
from .errors import ContractError, IntegrityError
from .nppes import classify_disposition, valid_npi

SELECTED_SCHEMA_VERSION = "atlas-v19-nppes-selected-1"
DISPOSITION_SCHEMA_VERSION = "atlas-v19-nppes-dispositions-1"
STREAM_RECEIPT_SCHEMA_VERSION = "atlas-v19-nppes-stream-receipt-1"

SELECTED_KEYS = (
    "schema_version",
    "npi",
    "source_row",
    "primary_taxonomy",
    "active_status",
    "address_id",
    "address_classification",
    "direct_eligible",
    "hud_eligible",
)

DISPOSITION_KEYS = (
    "schema_version",
    "npi",
    "pre_geography_disposition",
    "diagnostics",
    "address_id",
    "direct_eligible",
    "hud_eligible",
)


def _source_row(row: Mapping[str, object]) -> dict[str, str]:
    missing = [field for field in NPPES_ANALYTICAL_FIELDS if field not in row]
    if missing:
        raise IntegrityError(f"NPPES source row missing analytical fields: {missing}")
    result: dict[str, str] = {}
    for field in NPPES_ANALYTICAL_FIELDS:
        value = row[field]
        if not isinstance(value, str):
            raise IntegrityError(f"NPPES field is not an exact string: {field}")
        result[field] = value
    return result


def _positional_row_bytes(row: Mapping[str, str]) -> bytes:
    """Exact field strings in frozen positional order; no Unicode normalization."""

    return json.dumps(
        [row[field] for field in NPPES_ANALYTICAL_FIELDS],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _exact_json_line(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _row_from_positional(body: bytes) -> dict[str, str]:
    try:
        values = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("stored positional NPPES row is invalid") from exc
    if (
        not isinstance(values, list)
        or len(values) != len(NPPES_ANALYTICAL_FIELDS)
        or any(not isinstance(value, str) for value in values)
    ):
        raise IntegrityError("stored positional NPPES row schema is invalid")
    return dict(zip(NPPES_ANALYTICAL_FIELDS, values))


@dataclass(frozen=True)
class StreamAudit:
    raw_rows: int
    retained_unique_npis: int
    identical_duplicate_extra_rows: int


@dataclass(frozen=True)
class StreamingOutputs:
    selected_path: Path
    dispositions_path: Path
    audit_path: Path
    receipt_path: Path
    selected_sha256: str
    dispositions_sha256: str
    audit_sha256: str


class StreamingNppesStore:
    """SQLite uniqueness index; memory use is bounded by ``chunk_rows``."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS providers (
                npi TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                bucket TEXT NOT NULL,
                disposition_json BLOB NOT NULL,
                selected_source_values BLOB,
                selected_metadata_json BLOB,
                occurrences INTEGER NOT NULL CHECK (occurrences >= 1)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS counters (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            ) WITHOUT ROWID;
            INSERT OR IGNORE INTO counters(key, value) VALUES ('raw_rows', 0);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StreamingNppesStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def ingest_rows(
        self, rows: Iterable[Mapping[str, object]], *, chunk_rows: int = 10_000
    ) -> StreamAudit:
        if chunk_rows <= 0:
            raise ContractError("chunk_rows must be positive")
        chunk: list[Mapping[str, object]] = []
        for row in rows:
            chunk.append(row)
            if len(chunk) >= chunk_rows:
                self._ingest_chunk(chunk)
                chunk.clear()
        if chunk:
            self._ingest_chunk(chunk)
        return self.audit()

    def ingest_csv(self, handle: TextIO, *, chunk_rows: int = 10_000) -> StreamAudit:
        reader = csv.DictReader(handle, strict=True)
        if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise IntegrityError("NPPES CSV header is missing or duplicated")
        missing = [field for field in NPPES_ANALYTICAL_FIELDS if field not in reader.fieldnames]
        if missing:
            raise IntegrityError(f"NPPES CSV missing analytical columns: {missing}")

        row_count = 0

        def rows() -> Iterator[dict[str, str]]:
            nonlocal row_count
            try:
                for raw in reader:
                    row_count += 1
                    if None in raw or any(raw[field] is None for field in reader.fieldnames):
                        raise IntegrityError("NPPES CSV row does not match its header")
                    yield {field: raw[field] for field in NPPES_ANALYTICAL_FIELDS}
            except csv.Error as exc:
                raise IntegrityError("NPPES CSV is malformed") from exc

        audit = self.ingest_rows(rows(), chunk_rows=chunk_rows)
        if row_count == 0:
            raise IntegrityError("NPPES CSV is empty or header-only")
        return audit

    def _ingest_chunk(self, rows: list[Mapping[str, object]]) -> None:
        with self.connection:
            for raw in rows:
                row = _source_row(raw)
                npi = row["NPI"]
                if not valid_npi(npi):
                    raise IntegrityError(f"invalid NPI in streaming source: {npi!r}")
                body = _positional_row_bytes(row)
                fingerprint = hashlib.sha256(body).hexdigest()
                existing = self.connection.execute(
                    "SELECT fingerprint FROM providers WHERE npi = ?", (npi,)
                ).fetchone()
                if existing is None:
                    disposition = classify_disposition(row)
                    bucket = (
                        "pre_geography_eligible"
                        if disposition.bucket == "final_unmatched"
                        else disposition.bucket
                    )
                    disposition_record = {
                        "schema_version": DISPOSITION_SCHEMA_VERSION,
                        "npi": disposition.npi,
                        "pre_geography_disposition": bucket,
                        "diagnostics": sorted(disposition.diagnostics),
                        "address_id": disposition.address.address_id,
                        "direct_eligible": disposition.address.direct_eligible,
                        "hud_eligible": disposition.address.hud_eligible,
                    }
                    selected_metadata = None
                    selected_source = None
                    if bucket == "pre_geography_eligible":
                        selected_source = body
                        selected_metadata = json.dumps(
                            {
                                "primary_taxonomy": disposition.primary_taxonomy,
                                "active_status": disposition.active_status,
                                "address_id": disposition.address.address_id,
                                "address_classification": disposition.address.classification,
                                "direct_eligible": disposition.address.direct_eligible,
                                "hud_eligible": disposition.address.hud_eligible,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    self.connection.execute(
                        "INSERT INTO providers("
                        "npi, fingerprint, bucket, disposition_json, "
                        "selected_source_values, selected_metadata_json, occurrences"
                        ") VALUES (?, ?, ?, ?, ?, ?, 1)",
                        (
                            npi,
                            fingerprint,
                            bucket,
                            _exact_json_line(disposition_record).rstrip(b"\n"),
                            selected_source,
                            selected_metadata,
                        ),
                    )
                elif existing[0] == fingerprint:
                    self.connection.execute(
                        "UPDATE providers SET occurrences = occurrences + 1 WHERE npi = ?",
                        (npi,),
                    )
                else:
                    raise IntegrityError(f"conflicting duplicate NPI across chunks: {npi}")
                self.connection.execute(
                    "UPDATE counters SET value = value + 1 WHERE key = 'raw_rows'"
                )

    def audit(self) -> StreamAudit:
        raw_rows = self.connection.execute(
            "SELECT value FROM counters WHERE key = 'raw_rows'"
        ).fetchone()[0]
        unique, occurrences = self.connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(occurrences), 0) FROM providers"
        ).fetchone()
        extras = occurrences - unique
        if raw_rows != unique + extras:
            raise IntegrityError("streaming NPPES raw/duplicate accounting failed")
        return StreamAudit(raw_rows, unique, extras)

    def finalize(self, output_dir: Path, *, source_snapshot_id: str) -> StreamingOutputs:
        if not re_full_sha256(source_snapshot_id):
            raise IntegrityError("source_snapshot_id must be a lowercase SHA-256")
        output_dir.mkdir(parents=True, exist_ok=True)
        selected_path = output_dir / "nppes_selected.jsonl"
        dispositions_path = output_dir / "nppes_dispositions.jsonl"
        selected_part = selected_path.with_name(selected_path.name + ".part")
        dispositions_part = dispositions_path.with_name(dispositions_path.name + ".part")
        if selected_part.exists() or dispositions_part.exists():
            raise IntegrityError("stale NPPES finalization partial exists")
        disposition_counts: Counter[str] = Counter()
        selected_count = 0
        try:
            with selected_part.open("xb") as selected_handle, dispositions_part.open("xb") as disposition_handle:
                cursor = self.connection.execute(
                    "SELECT npi, bucket, disposition_json, selected_source_values, "
                    "selected_metadata_json FROM providers ORDER BY npi"
                )
                for npi, bucket, disposition_json, source_values, metadata_json in cursor:
                    disposition_counts[bucket] += 1
                    disposition_handle.write(disposition_json + b"\n")
                    if bucket == "pre_geography_eligible":
                        if source_values is None or metadata_json is None:
                            raise IntegrityError("eligible NPPES row lacks compact selected payload")
                        selected_count += 1
                        row = _row_from_positional(source_values)
                        metadata = json.loads(metadata_json)
                        selected_record = {
                            "schema_version": SELECTED_SCHEMA_VERSION,
                            "npi": npi,
                            "source_row": row,
                            **metadata,
                        }
                        # Selected rows are analytical data, not controls.  Preserve
                        # the exact source Unicode code points while retaining strict
                        # canonical JSON ordering/LF and non-finite rejection.
                        selected_handle.write(canonical_data_json_bytes(selected_record))
                    elif source_values is not None or metadata_json is not None:
                        raise IntegrityError("excluded NPPES row retained a forbidden full source payload")
                for handle in (selected_handle, disposition_handle):
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(selected_part, selected_path)
            os.replace(dispositions_part, dispositions_path)
        except Exception:
            for part in (selected_part, dispositions_part):
                try:
                    part.unlink()
                except FileNotFoundError:
                    pass
            raise
        audit = self.audit()
        if sum(disposition_counts.values()) != audit.retained_unique_npis:
            raise IntegrityError("streaming terminal disposition accounting failed")
        audit_record = {
            "schema_version": "atlas-v19-nppes-audit-1",
            "source_snapshot_id": source_snapshot_id,
            "raw_rows": audit.raw_rows,
            "retained_unique_npis": audit.retained_unique_npis,
            "identical_duplicate_extra_rows": audit.identical_duplicate_extra_rows,
            "selected_unique_npis": selected_count,
            "pre_geography_disposition_counts": dict(sorted(disposition_counts.items())),
        }
        audit_path = output_dir / "nppes_audit.json"
        atomic_write(audit_path, canonical_json_bytes(audit_record))
        artifacts = {
            "nppes_selected": file_identity(selected_path),
            "nppes_dispositions": file_identity(dispositions_path),
            "nppes_audit": file_identity(audit_path),
        }
        receipt_record = {
            "schema_version": STREAM_RECEIPT_SCHEMA_VERSION,
            "source_snapshot_id": source_snapshot_id,
            "database_schema": "sqlite-providers-v2-compact-positional",
            "artifacts": artifacts,
        }
        receipt_path = output_dir / "nppes_stream_receipt.json"
        atomic_write(receipt_path, canonical_json_bytes(receipt_record))
        return StreamingOutputs(
            selected_path,
            dispositions_path,
            audit_path,
            receipt_path,
            artifacts["nppes_selected"]["sha256"],
            artifacts["nppes_dispositions"]["sha256"],
            artifacts["nppes_audit"]["sha256"],
        )


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def file_identity(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def iter_selected_source_rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.endswith(b"\n"):
                raise IntegrityError(f"selected NPPES JSONL missing LF at line {line_number}")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IntegrityError("selected NPPES JSONL is malformed") from exc
            if tuple(record) != SELECTED_KEYS:
                raise IntegrityError("selected NPPES JSONL schema/order mismatch")
            row = _source_row(record["source_row"])
            if row["NPI"] != record["npi"]:
                raise IntegrityError("selected NPPES NPI/source-row mismatch")
            yield row
