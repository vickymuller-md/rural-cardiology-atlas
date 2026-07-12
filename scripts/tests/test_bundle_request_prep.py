from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path

from atlas_pipeline.bundle import (
    FrozenBundle,
    atomic_write,
    canonical_json_bytes,
    verify_output_receipt,
    write_output_receipt,
)
from atlas_pipeline.acquisition import (
    ZipCandidateManifest,
    ZipManifestApproval,
    ZipMemberIdentity,
    canonical_zip_candidate_manifest_bytes,
    canonical_manifest_bytes,
    zip_member_central_metadata_sha256,
)
from atlas_pipeline.addresses import normalize_address
from atlas_pipeline.geocoder import CanonicalGeocode
from atlas_pipeline.offline_bundle import load_phase2f_inputs
from atlas_pipeline.streaming_nppes import StreamingNppesStore
from atlas_pipeline.contracts import (
    HOSPITAL_FIELDS,
    METHOD_LINEAGE_ID,
    NPPES_ANALYTICAL_FIELDS,
)
from atlas_pipeline.errors import IntegrityError
from atlas_pipeline.places import PLACES_REQUIRED_FIELDS
from atlas_pipeline.request_prep import prepare_requests
from tests.bundle_helpers import write_bundle
from tests.synthetic import hospital_row, make_npi, nppes_row

STORAGE_FILESYSTEM_SHA256 = hashlib.sha256(
    b"synthetic-storage-filesystem-identity"
).hexdigest()
SCHEMA_REQUEST_ADDRESS_SHA256 = hashlib.sha256(
    b"synthetic-schema-request-address"
).hexdigest()
SCHEMA_RESPONSE_SHA256 = hashlib.sha256(
    b"synthetic-schema-geocoder-response"
).hexdigest()


def csv_bytes(fieldnames, rows, *, delimiter=",") -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=fieldnames, delimiter=delimiter, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def rucc_bytes(counties) -> bytes:
    rows = []
    for county in counties:
        base = {
            "FIPS": county["FIPS"],
            "State": county["State"],
            "County_Name": county["County_Name"],
        }
        rows.extend(
            (
                {**base, "Attribute": "Population_2020", "Value": county["Population_2020"]},
                {**base, "Attribute": "RUCC_2023", "Value": county["RUCC_2023"]},
                {**base, "Attribute": "Description", "Value": county["Description"]},
            )
        )
    return csv_bytes(("FIPS", "State", "County_Name", "Attribute", "Value"), rows)


def write_storage_preflight(path: Path, bundle: FrozenBundle, output_parent: Path) -> Path:
    minimum = 25 * 1024**3
    receipt = {
        "schema_version": "atlas-v19-storage-preflight-receipt-1",
        "receipt_id": "storage_preflight",
        "status": "approved",
        "source_tree_id": bundle.source_tree_id,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "authorization_id": bundle.authorization_id,
        "command": "approve-storage-preflight",
        "completed_at_utc": "2026-07-10T00:00:01Z",
        "evidence": {
            "work_root": str(output_parent.parent.resolve()),
            "output_parent": str(output_parent.resolve()),
            "device": "synthetic-volume",
            "st_dev": os.stat(output_parent).st_dev,
            "checked_at_utc": "2026-07-10T00:00:00Z",
            "free_bytes": minimum + 1,
            "worst_case_bytes": minimum,
            "minimum_required_bytes": minimum,
            "filesystem_identity_sha256": STORAGE_FILESYSTEM_SHA256,
            "integrator_control_id": "SYNTHETIC-CONTROL",
            "approved_by": "Synthetic Independent Integrator",
            "approved": True,
        },
    }
    path.write_bytes(canonical_json_bytes(receipt))
    return path


def nppes_zip(rows):
    payload = csv_bytes(NPPES_ANALYTICAL_FIELDS, rows)
    output = io.BytesIO()
    info = zipfile.ZipInfo("npidata_pfile_synthetic.csv", date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(info, payload)
    body = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        member = archive.getinfo(info.filename)
        identity = ZipMemberIdentity(
            member.filename,
            member.compress_size,
            member.file_size,
            member.CRC,
            member.compress_type,
            member.flag_bits,
            member.external_attr,
            member.is_dir(),
        )
    candidate = ZipCandidateManifest(
        "NPPES-202606-V2", len(body), hashlib.sha256(body).hexdigest(), (identity,)
    )
    candidate_body = canonical_zip_candidate_manifest_bytes(candidate)
    approval = ZipManifestApproval(
        "NPPES-202606-V2",
        candidate.archive_sha256,
        hashlib.sha256(candidate_body).hexdigest(),
        "SYNTHETIC-NPPES-MEMBER-APPROVAL",
        "Independent Synthetic Integrator",
        "2026-07-10T00:00:00Z",
        identity.filename,
        zip_member_central_metadata_sha256(identity),
    )
    approval_body = canonical_json_bytes(asdict(approval))
    synthetic_source_manifest = canonical_json_bytes(
        {
            "path": "scripts/tests/synthetic-fixture.py",
            "bytes": len(b"synthetic-source-tree\n"),
            "sha256": hashlib.sha256(b"synthetic-source-tree\n").hexdigest(),
        }
    )
    source_tree_id = hashlib.sha256(synthetic_source_manifest).hexdigest()
    phase2di_bundle = hashlib.sha256(
        b"synthetic independently approved Phase-2D-I bundle"
    ).hexdigest()
    phase2di_authorization = "SYNTHETIC-2DI-AUTHORIZATION"
    trust_root = {
        "schema_version": "atlas-v19-zip-approval-trust-root-1",
        "trust_root_id": "SYNTHETIC-NPPES-ZIP-TRUST",
        "source_tree_id": source_tree_id,
        "bundle_manifest_sha256": phase2di_bundle,
        "authorization_id": phase2di_authorization,
        "source_id": candidate.source_id,
        "archive_sha256": candidate.archive_sha256,
        "candidate_manifest_sha256": hashlib.sha256(candidate_body).hexdigest(),
        "member_approval_sha256": hashlib.sha256(approval_body).hexdigest(),
        "approved_by": "Independent Synthetic Integrator",
        "approved_at_utc": "2026-07-10T00:00:00Z",
        "selected_member_filename": identity.filename,
        "selected_member_central_metadata_sha256": (
            zip_member_central_metadata_sha256(identity)
        ),
    }
    trust_root_body = canonical_json_bytes(trust_root)
    container_receipt = canonical_json_bytes(
        {
            "schema_version": "atlas-v19-nppes-container-receipt-1",
            "receipt_id": "nppes_container",
            "status": "crc_verified_stopped_before_analytical_parse",
            "source_tree_id": source_tree_id,
            "bundle_manifest_sha256": phase2di_bundle,
            "authorization_id": phase2di_authorization,
            "command": "verify-nppes-container",
            "completed_at_utc": "2026-07-10T00:00:01Z",
            "evidence": {
                "archive_sha256": candidate.archive_sha256,
                "candidate_manifest_sha256": hashlib.sha256(candidate_body).hexdigest(),
                "approval_id": approval.approval_id,
                "approval_receipt_sha256": hashlib.sha256(approval_body).hexdigest(),
                "approval_trust_root_sha256": hashlib.sha256(
                    trust_root_body
                ).hexdigest(),
                "selected_member_filename": identity.filename,
                "selected_member_central_metadata_sha256": (
                    zip_member_central_metadata_sha256(identity)
                ),
                "selected_member_bytes": len(payload),
                "selected_member_sha256": hashlib.sha256(payload).hexdigest(),
                "member_count": 1,
                "crc_verified": True,
                "analytical_rows_parsed": False,
            },
        }
    )
    gate = {
        "candidate_manifest_source": "NPPES-CANDIDATE",
        "approval_source": "NPPES-APPROVAL",
        "approval_trust_root_source": "NPPES-APPROVAL-TRUST",
        "container_receipt_source": "NPPES-CONTAINER-RECEIPT",
        "selected_member_filename": identity.filename,
    }
    return (
        body,
        gate,
        candidate_body,
        approval_body,
        trust_root_body,
        container_receipt,
    )


class BundleContractTests(unittest.TestCase):
    def test_all_registered_source_schemas_and_receipt_binding(self):
        (
            nppes, gate, candidate_body, approval_body,
            trust_root_body, container_receipt,
        ) = nppes_zip([nppes_row(make_npi("500000001"))])
        cms = csv_bytes(HOSPITAL_FIELDS, [hospital_row()])
        hud = csv_bytes(
            ("ZIP", "COUNTY", "BUS_RATIO", "RES_RATIO", "TOT_RATIO", "USPS_ZIP_PREF_STATE"),
            [{"ZIP": "01001", "COUNTY": "01001", "BUS_RATIO": "1", "RES_RATIO": "1", "TOT_RATIO": "1", "USPS_ZIP_PREF_STATE": "AL"}],
        )
        acs_pop = csv_bytes(
            ("GEO_ID", "B01003_E001", "B01003_M001"),
            [{"GEO_ID": "0500000US01001", "B01003_E001": "1", "B01003_M001": "0"}],
            delimiter="|",
        )
        acs_income = csv_bytes(
            ("GEO_ID", "B19013_E001", "B19013_M001"),
            [{"GEO_ID": "0500000US01001", "B19013_E001": "1", "B19013_M001": "0"}],
            delimiter="|",
        )
        gaz = csv_bytes(("USPS", "GEOID", "NAME", "INTPTLAT", "INTPTLONG"), [{"USPS": "AL", "GEOID": "01001", "NAME": "Synthetic", "INTPTLAT": "32", "INTPTLONG": "-86"}])
        rucc = rucc_bytes(
            [{
                "FIPS": "01001", "State": "AL", "County_Name": "Synthetic",
                "Population_2020": "1", "RUCC_2023": "4", "Description": "Synthetic",
            }]
        )
        hrsa = csv_bytes(("Common State County FIPS Code", "HPSA Status"), [{"Common State County FIPS Code": "01001", "HPSA Status": "DESIGNATED"}])
        places_source = {field: "" for field in PLACES_REQUIRED_FIELDS}
        places_raw = csv_bytes(PLACES_REQUIRED_FIELDS, [places_source])
        places_row = {
            "fips": "01001", "chd_age_adjusted_prevalence_pct": "5.0",
            "places_release": "2025", "estimate_year": "2023", "measure_id": "CHD",
            "data_value_type_id": "AgeAdjPrv", "unit": "%",
            "low_confidence_limit": "4.0", "high_confidence_limit": "6.0",
            "footnote": None,
        }
        geocoder_row = {
            "request_id": "A-" + SCHEMA_REQUEST_ADDRESS_SHA256,
            "outcome": "Match",
            "match_indicator": "Match", "match_type": "Exact", "matched_address": "X",
            "longitude": "-86", "latitude": "32", "county_fips": "01001",
            "accepted_attempt": 1,
            "source_response_sha256": SCHEMA_RESPONSE_SHA256,
        }
        request = (
            f"A-{SCHEMA_REQUEST_ADDRESS_SHA256},100 TEST ST,CITY,AL,01001\n"
        ).encode()
        files = [
            ("NPPES-202606-V2", "nppes_zip", "nppes.zip", nppes, "application/zip"),
            ("NPPES-CANDIDATE", "zip_candidate_manifest_json", "nppes-candidate.json", candidate_body, "application/json"),
            ("NPPES-APPROVAL", "zip_manifest_approval_json", "nppes-approval.json", approval_body, "application/json"),
            ("NPPES-APPROVAL-TRUST", "zip_approval_trust_root_json", "nppes-approval-trust.json", trust_root_body, "application/json"),
            ("NPPES-CONTAINER-RECEIPT", "nppes_container_receipt_json", "nppes-container-receipt.json", container_receipt, "application/json"),
            ("CMS", "cms_hospital_csv", "cms.csv", cms, "text/csv"),
            ("HUD", "hud_zip_county_csv", "hud.csv", hud, "text/csv"),
            ("ACSPOP", "acs_population_csv", "pop.csv", acs_pop, "text/csv"),
            ("ACSINC", "acs_income_csv", "income.csv", acs_income, "text/csv"),
            ("GAZ", "gazetteer_csv", "gaz.csv", gaz, "text/csv"),
            ("RUCC", "rucc_csv", "rucc.csv", rucc, "text/csv"),
            ("HRSA", "hrsa_hpsa_csv", "hrsa.csv", hrsa, "text/csv"),
            ("PLACESRAW", "places_raw_csv", "places.csv", places_raw, "text/csv"),
            ("PLACES", "places_canonical_jsonl", "places.jsonl", canonical_json_bytes(places_row), "application/jsonl"),
            ("GEO", "geocoder_canonical_jsonl", "geo.jsonl", canonical_json_bytes(geocoder_row), "application/jsonl"),
            ("REQ", "geocoder_request_csv", "geocoder_request_0001.csv", request, "text/csv"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_bundle(
                root / "bundle", phase="2F",
                expectations={
                    "method_lineage_id": METHOD_LINEAGE_ID,
                    "nppes_container_gate": gate,
                },
                files=files,
            )
            bundle = FrozenBundle.load(root / "bundle", required_phase="2F")
            bundle.validate_all_schemas()
            output = root / "output"
            output.mkdir()
            artifact = output / "artifact.bin"
            atomic_write(artifact, b"fixture")
            receipt = write_output_receipt(
                output,
                command="synthetic",
                bundle=bundle,
                artifacts={"artifact": artifact},
                completed_at_utc="2026-07-10T00:00:01Z",
            )
            verify_output_receipt(
                receipt,
                bundle=bundle,
                artifact_root=output,
                required_artifacts={"artifact"},
                expected_command="synthetic",
            )
            self.assertEqual(
                json.loads(receipt.read_bytes())["method_lineage_id"],
                METHOD_LINEAGE_ID,
            )
            artifact.write_bytes(b"drifted")
            with self.assertRaises(IntegrityError):
                verify_output_receipt(
                    receipt,
                    bundle=bundle,
                    artifact_root=output,
                    required_artifacts={"artifact"},
                    expected_command="synthetic",
                )

    def test_manifest_and_source_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            write_bundle(
                root,
                phase="2D-II",
                expectations={},
                files=[("ONE", "nppes_audit_json", "one.json", b"{}\n", "application/json")],
            )
            manifest = root / "bundle.json"
            manifest.write_bytes(manifest.read_bytes().replace(b"\n", b"\r\n"))
            with self.assertRaises(IntegrityError):
                FrozenBundle.load(root)

    def test_phase2f_adapter_constructs_every_production_input_from_frozen_bundle(self):
        provider_rows = [
            nppes_row(make_npi("510000001")),
            nppes_row(
                make_npi("510000002"),
                **{
                    "Provider First Line Business Practice Location Address": "200 TEST STREET",
                    "Provider Business Practice Location Address City Name": "SECOND CITY",
                    "Provider Business Practice Location Address Postal Code": "01003",
                },
            ),
        ]
        hospital_rows = [
            hospital_row("000001", **{"Hospital Type": "Critical Access Hospitals"}),
            hospital_row(
                "000002",
                **{
                    "Address": "400 HOSPITAL ROAD",
                    "City/Town": "SECOND CITY",
                    "ZIP Code": "01003",
                },
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with StreamingNppesStore(root / "stream.sqlite") as store:
                store.ingest_rows(provider_rows, chunk_rows=1)
                streamed = store.finalize(
                    root / "streamed",
                    source_snapshot_id=hashlib.sha256(
                        b"synthetic-streamed-nppes-source"
                    ).hexdigest(),
                )

            geocodes = []
            for row, county, longitude, latitude in (
                (provider_rows[0], "01001", "-86", "32"),
                (provider_rows[1], "01003", "-87", "33"),
            ):
                address = normalize_address(
                    row["Provider First Line Business Practice Location Address"],
                    row["Provider Second Line Business Practice Location Address"],
                    row["Provider Business Practice Location Address City Name"],
                    row["Provider Business Practice Location Address State Name"],
                    row["Provider Business Practice Location Address Postal Code"],
                    row["Provider Business Practice Location Address Country Code (If outside U.S.)"],
                )
                geocodes.append(
                    CanonicalGeocode(
                        address.address_id, "Match", "Match", "Exact", "SYNTHETIC",
                        longitude,
                        latitude,
                        county,
                        1,
                        hashlib.sha256(
                            b"synthetic-provider-geocoder-response"
                        ).hexdigest(),
                    )
                )
            for row, county, longitude, latitude in (
                (hospital_rows[0], "01001", "-86.1", "32.1"),
                (hospital_rows[1], "01003", "-87.1", "33.1"),
            ):
                address = normalize_address(
                    row["Address"], "", row["City/Town"], row["State"], row["ZIP Code"], "US"
                )
                geocodes.append(
                    CanonicalGeocode(
                        address.address_id, "Match", "Match", "Exact", "SYNTHETIC",
                        longitude,
                        latitude,
                        county,
                        1,
                        hashlib.sha256(
                            b"synthetic-hospital-geocoder-response"
                        ).hexdigest(),
                    )
                )
            geocoder_bytes = b"".join(canonical_json_bytes(asdict(row)) for row in sorted(geocodes, key=lambda row: row.request_id))
            places_rows = [
                {
                    "fips": "01001", "chd_age_adjusted_prevalence_pct": "5.0",
                    "places_release": "2025", "estimate_year": "2023", "measure_id": "CHD",
                    "data_value_type_id": "AgeAdjPrv", "unit": "%",
                    "low_confidence_limit": "4.0", "high_confidence_limit": "6.0",
                    "footnote": None,
                },
                {
                    "fips": "01003", "chd_age_adjusted_prevalence_pct": None,
                    "places_release": "2025", "estimate_year": "2023", "measure_id": "CHD",
                    "data_value_type_id": "AgeAdjPrv", "unit": "%",
                    "low_confidence_limit": None, "high_confidence_limit": None,
                    "footnote": "Population less than 50",
                },
            ]
            places_bytes = b"".join(canonical_json_bytes(row) for row in places_rows)
            county_topology = canonical_json_bytes(
                {
                    "type": "Topology",
                    "objects": {
                        "counties": {"type": "GeometryCollection", "geometries": []}
                    },
                    "arcs": [],
                }
            )
            state_topology = canonical_json_bytes(
                {
                    "type": "Topology",
                    "objects": {
                        "states": {"type": "GeometryCollection", "geometries": []}
                    },
                    "arcs": [],
                }
            )
            files = [
                ("NPPES-SELECTED", "nppes_selected_jsonl", "nppes-selected.jsonl", streamed.selected_path.read_bytes(), "application/jsonl"),
                ("NPPES-DISPOSITIONS", "nppes_dispositions_jsonl", "nppes-dispositions.jsonl", streamed.dispositions_path.read_bytes(), "application/jsonl"),
                ("NPPES-AUDIT", "nppes_audit_json", "nppes-audit.json", streamed.audit_path.read_bytes(), "application/json"),
                ("NPPES-RECEIPT", "build_receipt_json", "nppes-receipt.json", streamed.receipt_path.read_bytes(), "application/json"),
                ("CMS", "cms_hospital_csv", "cms.csv", csv_bytes(HOSPITAL_FIELDS, hospital_rows), "text/csv"),
                ("HUD", "hud_zip_county_csv", "hud.csv", csv_bytes(("ZIP", "COUNTY", "BUS_RATIO", "RES_RATIO", "TOT_RATIO", "USPS_ZIP_PREF_STATE"), [{"ZIP": "01001", "COUNTY": "01001", "BUS_RATIO": "1", "RES_RATIO": "1", "TOT_RATIO": "1", "USPS_ZIP_PREF_STATE": "AL"}]), "text/csv"),
                ("ACS-POP", "acs_population_csv", "population.dat", csv_bytes(("GEO_ID", "B01003_E001", "B01003_M001"), [{"GEO_ID": "0500000US01001", "B01003_E001": "100", "B01003_M001": "1"}, {"GEO_ID": "0500000US01003", "B01003_E001": "50", "B01003_M001": "1"}], delimiter="|"), "text/plain"),
                ("ACS-INCOME", "acs_income_csv", "income.dat", csv_bytes(("GEO_ID", "B19013_E001", "B19013_M001"), [{"GEO_ID": "0500000US01001", "B19013_E001": "50000", "B19013_M001": "1"}, {"GEO_ID": "0500000US01003", "B19013_E001": "-1", "B19013_M001": "1"}], delimiter="|"), "text/plain"),
                ("GAZ", "gazetteer_csv", "gazetteer.csv", csv_bytes(("USPS", "GEOID", "NAME", "INTPTLAT", "INTPTLONG"), [{"USPS": "AL", "GEOID": "01001", "NAME": "Synthetic One", "INTPTLAT": "32", "INTPTLONG": "-86"}, {"USPS": "AL", "GEOID": "01003", "NAME": "Synthetic Two", "INTPTLAT": "33", "INTPTLONG": "-87"}]), "text/csv"),
                ("RUCC", "rucc_csv", "rucc.csv", rucc_bytes([{"FIPS": "01001", "State": "AL", "County_Name": "Synthetic One", "Population_2020": "100", "RUCC_2023": "2", "Description": "Metro"}, {"FIPS": "01003", "State": "AL", "County_Name": "Synthetic Two", "Population_2020": "50", "RUCC_2023": "4", "Description": "Nonmetro"}]), "text/csv"),
                ("HRSA", "hrsa_hpsa_csv", "hrsa.csv", csv_bytes(("Common State County FIPS Code", "HPSA Status"), [{"Common State County FIPS Code": "01001", "HPSA Status": "DESIGNATED"}, {"Common State County FIPS Code": "01003", "HPSA Status": "WITHDRAWN"}]), "text/csv"),
                ("PLACES", "places_canonical_jsonl", "places.jsonl", places_bytes, "application/jsonl"),
                ("GEOCODER", "geocoder_canonical_jsonl", "geocoder.jsonl", geocoder_bytes, "application/jsonl"),
                ("ADJACENCY", "adjacency_canonical_jsonl", "adjacency.jsonl", canonical_json_bytes({"primary_fips": "01001", "neighbor_fips": ["01003"]}) + canonical_json_bytes({"primary_fips": "01003", "neighbor_fips": ["01001"]}), "application/jsonl"),
                ("COUNTY-TOPOLOGY", "topology_json", "counties.topo.json", county_topology, "application/topo+json"),
                ("STATE-TOPOLOGY", "topology_json", "states.topo.json", state_topology, "application/topo+json"),
            ]
            roles = {
                "nppes_selected": "NPPES-SELECTED",
                "nppes_dispositions": "NPPES-DISPOSITIONS",
                "nppes_audit": "NPPES-AUDIT",
                "nppes_stream_receipt": "NPPES-RECEIPT",
                "cms_hospitals": "CMS",
                "hud": "HUD",
                "acs_population": "ACS-POP",
                "acs_income": "ACS-INCOME",
                "gazetteer": "GAZ",
                "rucc": "RUCC",
                "hrsa": "HRSA",
                "places_canonical": "PLACES",
                "geocoder_canonical": "GEOCODER",
                "adjacency_canonical": "ADJACENCY",
                "county_topology": "COUNTY-TOPOLOGY",
                "state_topology": "STATE-TOPOLOGY",
            }
            expectations = {
                "method_lineage_id": METHOD_LINEAGE_ID,
                "source_roles": roles,
                "county_universe": ["01001", "01003"],
                "county_count": 2,
                "population_total": 150,
                "build_expectations": {
                    "county_count": 2,
                    "population_total": 150,
                    "places_county_rows": 2,
                    "places_nonnull": 1,
                    "places_public_nulls": 1,
                    "places_snapshot_sha256": hashlib.sha256(places_bytes).hexdigest(),
                    "input_manifest_sha256": hashlib.sha256(
                        b"pre-binding-input-manifest"
                    ).hexdigest(),
                    "required_input_hashes": {},
                    "method_lineage_id": METHOD_LINEAGE_ID,
                    "places_suppressed_fips": "01003",
                },
            }
            write_bundle(root / "bundle", phase="2F", expectations=expectations, files=files)
            bundle = FrozenBundle.load(root / "bundle", required_phase="2F")
            manifest_sha = hashlib.sha256(
                canonical_manifest_bytes(list(bundle.frozen_inputs()))
            ).hexdigest()
            manifest_path = root / "bundle" / "bundle.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["expectations"]["build_expectations"]["input_manifest_sha256"] = manifest_sha
            manifest["expectations"]["build_expectations"]["required_input_hashes"] = {
                source_id: entry.sha256 for source_id, entry in bundle.inputs.items()
            }
            manifest_path.write_bytes(canonical_json_bytes(manifest))
            bundle = FrozenBundle.load(root / "bundle", required_phase="2F")
            (
                inputs,
                build_expectations,
                loaded_places,
                adjacency,
                loaded_county_topology,
                loaded_state_topology,
            ) = load_phase2f_inputs(bundle, require_chain=False)
            self.assertEqual(len(inputs.nppes_rows), 2)
            self.assertEqual(len(inputs.hospital_rows), 2)
            self.assertEqual(len(inputs.geocodes), 4)
            self.assertEqual(build_expectations.input_manifest_sha256, manifest_sha)
            self.assertEqual(loaded_places, places_bytes)
            self.assertEqual(len(adjacency), 2)
            self.assertEqual(loaded_county_topology, county_topology)
            self.assertEqual(loaded_state_topology, state_topology)
            drifted = json.loads(manifest_path.read_text())
            del drifted["expectations"]["source_roles"]["adjacency_canonical"]
            manifest_path.write_bytes(canonical_json_bytes(drifted))
            with self.assertRaises(IntegrityError):
                load_phase2f_inputs(FrozenBundle.load(root / "bundle", required_phase="2F"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            write_bundle(
                root,
                phase="2D-II",
                expectations={},
                files=[("ONE", "nppes_audit_json", "one.json", b"{}\n", "application/json")],
            )
            manifest = json.loads((root / "bundle.json").read_text())
            (root / manifest["inputs"][0]["relative_path"]).write_bytes(b"changed\n")
            with self.assertRaises(IntegrityError):
                FrozenBundle.load(root)


class RequestPreparationTests(unittest.TestCase):
    def test_phase2dii_rejects_rehashed_wrong_container_member_count(self):
        nppes_row_value = nppes_row(make_npi("500000099"))
        (
            nppes, gate, candidate_body, approval_body,
            trust_root_body, container_receipt,
        ) = nppes_zip([nppes_row_value])
        tampered_container = json.loads(container_receipt)
        tampered_container["evidence"]["member_count"] = 2
        cms = csv_bytes(HOSPITAL_FIELDS, [hospital_row()])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_bundle(
                root / "bundle",
                phase="2D-II",
                expectations={
                    "nppes_container_gate": gate,
                    "source_roles": {
                        "nppes": "NPPES-202606-V2",
                        "cms_hospitals": "CMS",
                    },
                },
                files=[
                    ("NPPES-202606-V2", "nppes_zip", "nppes.zip", nppes, "application/zip"),
                    ("NPPES-CANDIDATE", "zip_candidate_manifest_json", "nppes-candidate.json", candidate_body, "application/json"),
                    ("NPPES-APPROVAL", "zip_manifest_approval_json", "nppes-approval.json", approval_body, "application/json"),
                    ("NPPES-APPROVAL-TRUST", "zip_approval_trust_root_json", "nppes-approval-trust.json", trust_root_body, "application/json"),
                    ("NPPES-CONTAINER-RECEIPT", "nppes_container_receipt_json", "nppes-container-receipt.json", canonical_json_bytes(tampered_container), "application/json"),
                    ("CMS", "cms_hospital_csv", "cms.csv", cms, "text/csv"),
                ],
            )
            bundle = FrozenBundle.load(root / "bundle", required_phase="2D-II")
            work_parent = root / "work-root" / "outputs"
            work_parent.mkdir(parents=True)
            storage = write_storage_preflight(
                root / "storage-preflight.json", bundle, work_parent
            )
            with self.assertRaisesRegex(
                IntegrityError, "reverified D-II container"
            ):
                prepare_requests(
                    bundle,
                    work_parent / "prepared",
                    storage_preflight_receipt_path=storage,
                )

    def test_phase2dii_streams_nppes_and_freezes_combined_requests(self):
        direct = nppes_row(make_npi("500000101"))
        po_box = nppes_row(
            make_npi("500000102"),
            **{
                "Provider First Line Business Practice Location Address": "PO BOX 2",
                "Provider Second Line Business Practice Location Address": "",
            },
        )
        (
            nppes, gate, candidate_body, approval_body,
            trust_root_body, container_receipt,
        ) = nppes_zip([direct, po_box])
        cms = csv_bytes(HOSPITAL_FIELDS, [hospital_row()])
        expectations = {
            "nppes_container_gate": gate,
            "source_roles": {"nppes": "NPPES-202606-V2", "cms_hospitals": "CMS"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_bundle(
                root / "bundle",
                phase="2D-II",
                expectations=expectations,
                files=[
                    ("NPPES-202606-V2", "nppes_zip", "nppes.zip", nppes, "application/zip"),
                    ("NPPES-CANDIDATE", "zip_candidate_manifest_json", "nppes-candidate.json", candidate_body, "application/json"),
                    ("NPPES-APPROVAL", "zip_manifest_approval_json", "nppes-approval.json", approval_body, "application/json"),
                    ("NPPES-APPROVAL-TRUST", "zip_approval_trust_root_json", "nppes-approval-trust.json", trust_root_body, "application/json"),
                    ("NPPES-CONTAINER-RECEIPT", "nppes_container_receipt_json", "nppes-container-receipt.json", container_receipt, "application/json"),
                    ("CMS", "cms_hospital_csv", "cms.csv", cms, "text/csv"),
                ],
            )
            bundle = FrozenBundle.load(root / "bundle", required_phase="2D-II")
            work_parent = root / "work-root" / "outputs"
            work_parent.mkdir(parents=True)
            storage = write_storage_preflight(
                root / "storage-preflight.json", bundle, work_parent
            )
            receipt = prepare_requests(
                bundle,
                work_parent / "prepared",
                chunk_rows=1,
                storage_preflight_receipt_path=storage,
                completed_at_utc="2026-07-10T00:00:02Z",
            )
            self.assertTrue(receipt.is_file())
            manifest = json.loads((work_parent / "prepared" / "request_manifest.json").read_text())
            self.assertEqual(sum(row["row_count"] for row in manifest), 2)
            accounting = json.loads((work_parent / "prepared" / "request_accounting.json").read_text())
            self.assertEqual(accounting["provider_entities"], 2)
            self.assertEqual(accounting["hospital_entities"], 1)
            self.assertEqual(accounting["direct_unique_addresses"], 2)
            self.assertFalse((work_parent / "prepared" / "work").exists())
            self.assertTrue(
                (work_parent / "prepared" / "d2ii_handoff_receipt.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
