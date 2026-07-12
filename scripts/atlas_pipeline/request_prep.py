"""Phase-2D-II provider/hospital address mapping and request preparation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .addresses import EntityAddressMapping
from .bundle import FrozenBundle, atomic_write, canonical_json_bytes, write_output_receipt
from .control_receipts import ReceiptContext, validate_control_receipt
from .errors import IntegrityError
from .geocoder import (
    MAX_CHUNK_BYTES,
    MAX_CHUNK_ROWS,
    GeocoderRequest,
    request_csv_bytes,
)
from .hospitals import deduplicate_hospitals, in_scope_hospitals
from .nppes import classify_disposition
from .streaming_nppes import StreamingNppesStore, iter_selected_source_rows

MAPPING_KEYS = (
    "entity_kind", "entity_id", "address_id", "street1", "street2", "city",
    "state", "zip5", "country", "classification", "direct_eligible",
    "hud_eligible",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class RequestAddressStore:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE addresses (
                address_id TEXT PRIMARY KEY,
                fields_json BLOB NOT NULL,
                direct_eligible INTEGER NOT NULL,
                street TEXT NOT NULL,
                city TEXT NOT NULL,
                state TEXT NOT NULL,
                zip5 TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE entities (
                entity_kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                address_id TEXT NOT NULL,
                mapping_json BLOB NOT NULL,
                PRIMARY KEY(entity_kind, entity_id)
            ) WITHOUT ROWID;
            """
        )

    def close(self) -> None:
        self.connection.close()

    def add(self, mapping: EntityAddressMapping) -> None:
        row = asdict(mapping)
        if tuple(row) != MAPPING_KEYS:
            raise IntegrityError("entity mapping schema/order drift")
        mapping_body = canonical_json_bytes(row).rstrip(b"\n")
        fields = (
            mapping.street1, mapping.street2, mapping.city, mapping.state,
            mapping.zip5, mapping.country,
        )
        fields_body = canonical_json_bytes(list(fields)).rstrip(b"\n")
        prior = self.connection.execute(
            "SELECT fields_json FROM addresses WHERE address_id = ?", (mapping.address_id,)
        ).fetchone()
        if prior is not None and prior[0] != fields_body:
            raise IntegrityError(f"address_id collision in request store: {mapping.address_id}")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO addresses(address_id, fields_json, direct_eligible, "
                "street, city, state, zip5) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    mapping.address_id,
                    fields_body,
                    int(mapping.direct_eligible),
                    " ".join(line for line in (mapping.street1, mapping.street2) if line),
                    mapping.city,
                    mapping.state,
                    mapping.zip5,
                ),
            )
            try:
                self.connection.execute(
                    "INSERT INTO entities(entity_kind, entity_id, address_id, mapping_json) "
                    "VALUES (?, ?, ?, ?)",
                    (mapping.entity_kind, mapping.entity_id, mapping.address_id, mapping_body),
                )
            except sqlite3.IntegrityError as exc:
                raise IntegrityError(
                    f"duplicate entity in request store: {mapping.entity_kind}/{mapping.entity_id}"
                ) from exc

    def write_mapping(self, kind: str, path: Path) -> None:
        temporary = path.with_name(path.name + ".part")
        if temporary.exists():
            raise IntegrityError(f"stale mapping partial: {temporary}")
        with temporary.open("xb") as handle:
            for (body,) in self.connection.execute(
                "SELECT mapping_json FROM entities WHERE entity_kind = ? "
                "ORDER BY entity_id, address_id",
                (kind,),
            ):
                handle.write(body + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def iter_direct_requests(self) -> Iterator[GeocoderRequest]:
        for row in self.connection.execute(
            "SELECT address_id, street, city, state, zip5 FROM addresses "
            "WHERE direct_eligible = 1 ORDER BY address_id"
        ):
            yield GeocoderRequest(*row)

    def counts(self) -> dict[str, int]:
        provider = self.connection.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_kind = 'P'"
        ).fetchone()[0]
        hospital = self.connection.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_kind = 'H'"
        ).fetchone()[0]
        direct = self.connection.execute(
            "SELECT COUNT(*) FROM addresses WHERE direct_eligible = 1"
        ).fetchone()[0]
        return {
            "provider_entities": provider,
            "hospital_entities": hospital,
            "direct_unique_addresses": direct,
        }


def _mapping(kind: str, entity_id: str, address) -> EntityAddressMapping:
    return EntityAddressMapping(
        kind,
        entity_id,
        address.address_id,
        address.street1,
        address.street2,
        address.city,
        address.state,
        address.zip5,
        address.country,
        address.classification,
        address.direct_eligible,
        address.hud_eligible,
    )


def write_request_chunks(store: RequestAddressStore, output_dir: Path) -> tuple[Path, list[Path]]:
    metadata: list[dict[str, object]] = []
    chunk_paths: list[Path] = []
    current: list[GeocoderRequest] = []
    current_bytes = 0

    def flush() -> None:
        nonlocal current, current_bytes
        if not current:
            return
        body = request_csv_bytes(current)
        index = len(metadata) + 1
        filename = f"geocoder_request_{index:04d}.csv"
        path = output_dir / filename
        atomic_write(path, body)
        metadata.append(
            {
                "filename": filename,
                "first_id": current[0].address_id,
                "last_id": current[-1].address_id,
                "row_count": len(current),
                "byte_count": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
        chunk_paths.append(path)
        current = []
        current_bytes = 0

    for request in store.iter_direct_requests():
        row_bytes = request_csv_bytes([request])
        if len(row_bytes) > MAX_CHUNK_BYTES:
            raise IntegrityError(f"one geocoder request exceeds byte limit: {request.address_id}")
        if current and (
            len(current) >= MAX_CHUNK_ROWS
            or current_bytes + len(row_bytes) > MAX_CHUNK_BYTES
        ):
            flush()
        current.append(request)
        current_bytes += len(row_bytes)
    flush()
    manifest_path = output_dir / "request_manifest.json"
    atomic_write(manifest_path, canonical_json_bytes(metadata))
    return manifest_path, chunk_paths


def prepare_requests(
    bundle: FrozenBundle,
    output_dir: Path,
    *,
    chunk_rows: int = 10_000,
    storage_preflight_receipt_path: Path,
    completed_at_utc: str | None = None,
) -> Path:
    if bundle.phase != "2D-II":
        raise IntegrityError("prepare-requests requires a Phase 2D-II bundle")
    storage_body = storage_preflight_receipt_path.read_bytes()
    validate_control_receipt(
        "storage_preflight",
        storage_body,
        context=ReceiptContext(
            bundle.source_tree_id, bundle.manifest_sha256, bundle.authorization_id
        ),
    )
    roles = bundle.expectation("source_roles", dict)
    if set(roles) != {"nppes", "cms_hospitals"}:
        raise IntegrityError("2D-II source_roles must be exactly nppes/cms_hospitals")
    nppes_source = roles["nppes"]
    cms_source = roles["cms_hospitals"]
    if not isinstance(nppes_source, str) or not isinstance(cms_source, str):
        raise IntegrityError("2D-II source role values must be source IDs")
    bundle.require_sources((nppes_source, cms_source))
    # NPPES is validated exactly once through the approved central-manifest ->
    # streaming CRC -> analytical parse sequence below.  A separate schema
    # pre-pass would decompress the multi-GB member redundantly.
    bundle.validate_registered_schema(cms_source)
    output_dir.mkdir(parents=True, exist_ok=False)
    work_dir = output_dir / "work"
    work_dir.mkdir()

    nppes_dir = output_dir / "nppes"
    nppes_dir.mkdir()
    with StreamingNppesStore(work_dir / "nppes.sqlite") as nppes_store:
        with bundle.open_nppes_member_with_identity(nppes_source) as (
            handle,
            nppes_container_identity,
        ):
            nppes_store.ingest_csv(handle, chunk_rows=chunk_rows)
        nppes_outputs = nppes_store.finalize(
            nppes_dir, source_snapshot_id=bundle.manifest_sha256
        )

    request_store = RequestAddressStore(work_dir / "requests.sqlite")
    try:
        for row in iter_selected_source_rows(nppes_outputs.selected_path):
            disposition = classify_disposition(row)
            if disposition.bucket != "final_unmatched":
                raise IntegrityError("selected NPPES stream contains an ineligible row")
            request_store.add(_mapping("P", disposition.npi, disposition.address))

        hospitals = in_scope_hospitals(
            deduplicate_hospitals(bundle.iter_csv(cms_source))
        )
        for hospital in hospitals:
            request_store.add(_mapping("H", hospital.entity_id, hospital.address))

        provider_mapping = output_dir / "provider_entity_address_mapping.jsonl"
        hospital_mapping = output_dir / "hospital_entity_address_mapping.jsonl"
        request_store.write_mapping("P", provider_mapping)
        request_store.write_mapping("H", hospital_mapping)
        request_manifest, chunks = write_request_chunks(request_store, output_dir)
        counts = request_store.counts()
    finally:
        request_store.close()

    artifacts: dict[str, Path] = {
        "nppes_selected": nppes_outputs.selected_path,
        "nppes_dispositions": nppes_outputs.dispositions_path,
        "nppes_audit": nppes_outputs.audit_path,
        "nppes_stream_receipt": nppes_outputs.receipt_path,
        "provider_mapping": provider_mapping,
        "hospital_mapping": hospital_mapping,
        "request_manifest": request_manifest,
    }
    for index, path in enumerate(chunks, start=1):
        artifacts[f"request_chunk_{index:04d}"] = path
    # Counts are non-analytical request-formation accounting and are bound to
    # the same bundle/request manifest in a separate deterministic receipt.
    accounting = {
        "schema_version": "atlas-v19-request-accounting-1",
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "request_manifest_sha256": hashlib.sha256(request_manifest.read_bytes()).hexdigest(),
        **counts,
    }
    accounting_path = output_dir / "request_accounting.json"
    atomic_write(accounting_path, canonical_json_bytes(accounting))
    artifacts["request_accounting"] = accounting_path
    shutil.rmtree(work_dir)
    receipt = write_output_receipt(
        output_dir,
        command="prepare-requests",
        bundle=bundle,
        artifacts=artifacts,
        completed_at_utc=completed_at_utc or _utc_now(),
    )
    chunk_identities = [
        {
            "filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in chunks
    ]
    control_receipt = {
        "schema_version": "atlas-v19-d2ii-handoff-receipt-1",
        "receipt_id": "d2ii_handoff",
        "status": "complete",
        "source_tree_id": bundle.source_tree_id,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "authorization_id": bundle.authorization_id,
        "command": "prepare-requests",
        "completed_at_utc": completed_at_utc or _utc_now(),
        "evidence": {
            "phase2dii_bundle_manifest_sha256": bundle.manifest_sha256,
            "nppes_archive_sha256": nppes_container_identity.archive_sha256,
            "nppes_candidate_manifest_sha256": (
                nppes_container_identity.candidate_manifest_sha256
            ),
            "nppes_approval_receipt_sha256": (
                nppes_container_identity.approval_receipt_sha256
            ),
            "nppes_approval_trust_root_sha256": (
                nppes_container_identity.approval_trust_root_sha256
            ),
            "nppes_selected_member_filename": (
                nppes_container_identity.selected_member_filename
            ),
            "nppes_selected_member_sha256": (
                nppes_container_identity.selected_member_sha256
            ),
            "provider_mapping_sha256": hashlib.sha256(provider_mapping.read_bytes()).hexdigest(),
            "hospital_mapping_sha256": hashlib.sha256(hospital_mapping.read_bytes()).hexdigest(),
            "nppes_audit_sha256": hashlib.sha256(nppes_outputs.audit_path.read_bytes()).hexdigest(),
            "nppes_dispositions_sha256": hashlib.sha256(nppes_outputs.dispositions_path.read_bytes()).hexdigest(),
            "request_manifest_sha256": hashlib.sha256(request_manifest.read_bytes()).hexdigest(),
            "request_chunks_sha256": hashlib.sha256(
                canonical_json_bytes(chunk_identities)
            ).hexdigest(),
            "request_count": counts["direct_unique_addresses"],
            "chunk_count": len(chunks),
            "storage_preflight_receipt_sha256": hashlib.sha256(storage_body).hexdigest(),
        },
    }
    control_body = canonical_json_bytes(control_receipt)
    validate_control_receipt(
        "d2ii_handoff",
        control_body,
        context=ReceiptContext(
            bundle.source_tree_id, bundle.manifest_sha256, bundle.authorization_id
        ),
    )
    atomic_write(output_dir / "d2ii_handoff_receipt.json", control_body)
    return receipt
