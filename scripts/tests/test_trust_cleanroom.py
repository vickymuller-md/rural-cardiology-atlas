from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from atlas_pipeline.bundle import (
    FrozenBundle,
    atomic_write,
    canonical_json_bytes,
    verify_output_receipt,
    write_output_receipt,
)
from atlas_pipeline.clean_room import (
    CleanRoomInputs,
    ProcessCommand,
    _app_manifest_rows,
    _dependency_tree_bytes,
    _expected_tsconfig_bytes,
    _materialize_production_app,
    _run_once,
    _verify_production_app_after,
    orchestrate_clean_rooms,
)
from atlas_pipeline.control_receipts import (
    PHASE2F_CROSS_PHASE_ORIGINAL_IDS,
    ReceiptContext,
    validate_control_receipt,
    validate_receipt_set,
)
from atlas_pipeline.errors import IntegrityError
from atlas_pipeline.predecessor_bridge import (
    AMENDMENT04_ADDENDUM01_SHA256,
    AMENDMENT04_SHA256,
    PREDECESSOR_ARTIFACT_SHA256,
    PREDECESSOR_NEGATIVE_MATRIX_SHA256,
    PREDECESSOR_VERIFICATION_PATHS,
    R4B_SOURCE_TREE_ID,
    R4_INPUT_COUNT,
    R4_INPUT_MANIFEST_SHA256,
    R4_METHOD_LINEAGE_ID,
)
from atlas_pipeline.source_tree import (
    SOURCE_TREE_POLICY_SHA256,
    SourceTreeIdentity,
    generate_source_tree_manifest,
    parse_source_tree_manifest,
    source_tree_policy_bytes,
    verify_imported_module_containment,
    verify_source_tree_manifest,
)
from atlas_pipeline.strict_json import (
    canonical_data_json_bytes,
    canonical_json_bytes as strict_canonical_json_bytes,
    reject_control_sentinels,
    strict_json_loads,
    validate_safe_id,
)
from atlas_pipeline.trust import RuntimeControl, TRUST_ROOT_SCHEMA_VERSION, verify_entrypoint_authority
from tests.bundle_helpers import write_bundle


REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-07-10T12:00:00Z"


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def receipt_body(
    receipt_id: str,
    context: ReceiptContext,
    *,
    status: str,
    command: str,
    evidence: dict,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": f"atlas-v19-{receipt_id.replace('_', '-')}-receipt-1",
            "receipt_id": receipt_id,
            "status": status,
            "source_tree_id": context.source_tree_id,
            "bundle_manifest_sha256": context.bundle_manifest_sha256,
            "authorization_id": context.authorization_id,
            "command": command,
            "completed_at_utc": NOW,
            "evidence": evidence,
        }
    )


def predecessor_bridge_evidence(active_source_tree_id: str) -> dict[str, object]:
    predecessor_rows = [
        {
            "artifact_id": artifact_id,
            "sha256": PREDECESSOR_ARTIFACT_SHA256[artifact_id],
            "body_sha256": PREDECESSOR_ARTIFACT_SHA256[artifact_id],
            "body_verified": True,
        }
        for artifact_id in sorted(PREDECESSOR_ARTIFACT_SHA256)
    ]
    allowlist = {
        "schema_version": "atlas-v19-predecessor-allowlist-1",
        "rows": [
            {"artifact_id": row["artifact_id"], "sha256": row["sha256"]}
            for row in predecessor_rows
        ],
    }
    ledger = {
        "schema_version": "atlas-v19-predecessor-encounter-ledger-1",
        "rows": [
            {
                "artifact_id": row["artifact_id"],
                "sha256": row["sha256"],
                "encounter_count": 1,
                "verification_path": PREDECESSOR_VERIFICATION_PATHS[row["artifact_id"]],
            }
            for row in predecessor_rows
        ],
    }
    return {
        "amendment04_sha256": AMENDMENT04_SHA256,
        "amendment04_addendum01_sha256": AMENDMENT04_ADDENDUM01_SHA256,
        "active_source_tree_id": active_source_tree_id,
        "method_lineage_id": R4_METHOD_LINEAGE_ID,
        "input_manifest_sha256": R4_INPUT_MANIFEST_SHA256,
        "input_count": R4_INPUT_COUNT,
        "predecessor_source_tree_id": R4B_SOURCE_TREE_ID,
        "predecessor_rows": predecessor_rows,
        "predecessor_allowlist_sha256": digest(canonical_json_bytes(allowlist)),
        "integration_harness_sha256": digest(b"frozen integration harness"),
        "negative_matrix_sha256": PREDECESSOR_NEGATIVE_MATRIX_SHA256,
        "negative_matrix_status": "PASS",
        "load_phase2f_inputs_passed": True,
        "verify_phase2f_chain_passed": True,
        "request_count": 22635,
        "census_attempt_count": 5,
        "encounter_ledger": ledger,
        "encounter_ledger_sha256": digest(canonical_json_bytes(ledger)),
        "unlisted_encounter_count": 0,
        "network_accessed": False,
    }


def network_receipt(context: ReceiptContext) -> bytes:
    return receipt_body(
        "network_denied",
        context,
        status="verified_os_level",
        command="verify-network-denial",
        evidence={
            "mechanism": "synthetic sandbox namespace policy",
            "policy_sha256": digest(b"synthetic-network-policy"),
            "probe_command": "synthetic-network-probe",
            "probe_exit_code": 77,
            "started_at_utc": "2026-07-10T11:59:58Z",
            "ended_at_utc": "2026-07-10T11:59:59Z",
        },
    )


def dependency_receipt(context: ReceiptContext, network: bytes) -> bytes:
    return receipt_body(
        "dependency_materialization",
        context,
        status="verified_frozen",
        command="materialize-dependencies",
        evidence={
            "lockfiles": [
                {"path": "app/package-lock.json", "sha256": digest(b"app-lock")},
                {"path": "scripts/uv.lock", "sha256": digest(b"uv-lock")},
                {
                    "path": "tooling/topology/package-lock.json",
                    "sha256": digest(b"topology-lock"),
                },
            ],
            "cache_or_image_sha256": digest(b"dependency-image"),
            "operating_system": "synthetic-os",
            "platform": "synthetic-platform",
            "architecture": "synthetic-architecture",
            "python_version": "3.12.12",
            "uv_version": "0.10.4",
            "node_version": "22.17.0",
            "npm_version": "10.9.2",
            "install_command": "npm ci --offline --ignore-scripts",
            "install_exit_code": 0,
            "network_denied_receipt_sha256": digest(network),
        },
    )


class StrictJsonTests(unittest.TestCase):
    def test_nonfinite_sentinels_and_unsafe_dot_ids_are_rejected(self):
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(token=token):
                with self.assertRaises(IntegrityError):
                    strict_json_loads(b'{"value":' + token + b"}\n")
        with self.assertRaises(IntegrityError):
            strict_canonical_json_bytes({"value": float("nan")})
        with self.assertRaises(IntegrityError):
            reject_control_sentinels({"nested": ["approved", {"value": "placeholder-pin"}]})
        for value in (".", "..", "unsafe/name", ""):
            with self.subTest(value=value), self.assertRaises(IntegrityError):
                validate_safe_id(value)

    def test_data_rows_preserve_raw_unicode_while_controls_use_nfc(self):
        decomposed = "Cafe\u0301"
        data = canonical_data_json_bytes({"raw": decomposed})
        control = strict_canonical_json_bytes({"raw": decomposed})
        self.assertIn(decomposed.encode("utf-8"), data)
        self.assertNotEqual(data, control)


class SourceTreeAndTrustTests(unittest.TestCase):
    def test_manifest_is_exact_and_imported_modules_must_stay_under_root(self):
        identity = generate_source_tree_manifest(REPO_ROOT)
        verified = verify_source_tree_manifest(REPO_ROOT, identity.manifest_bytes)
        self.assertEqual(verified.source_tree_id, digest(identity.manifest_bytes))
        paths = set(identity.paths)
        self.assertIn("scripts/atlas_pipeline/trust.py", paths)
        self.assertNotIn("CLAUDE.md", paths)
        self.assertFalse(any(path.startswith("app/public/") for path in paths))
        self.assertFalse(any("node_modules" in path or ".next" in path for path in paths))

        nominal_path = "app/vendor/heartland-ui.tree.jsonl"
        self.assertIn(nominal_path, paths)
        policy = json.loads(source_tree_policy_bytes())
        self.assertIn(nominal_path, policy["nominal_source_paths"])
        self.assertIn(
            "app/vendor/heartland-ui/README.md", paths
        )
        self.assertIn(
            "app/vendor/heartland-ui/README.md", policy["nominal_source_paths"]
        )
        manifest_rows = parse_source_tree_manifest(identity.manifest_bytes)
        nominal_row = next(row for row in manifest_rows if row["path"] == nominal_path)
        nominal_body = (REPO_ROOT / nominal_path).read_bytes()
        self.assertEqual(nominal_row["bytes"], len(nominal_body))
        self.assertEqual(nominal_row["sha256"], digest(nominal_body))

        normal = types.ModuleType("atlas_pipeline.trust")
        normal.__file__ = str(REPO_ROOT / "scripts" / "atlas_pipeline" / "trust.py")
        self.assertEqual(
            verify_imported_module_containment(
                REPO_ROOT, modules=(("atlas_pipeline.trust", normal),)
            ),
            ("atlas_pipeline.trust",),
        )

        outside = types.ModuleType("atlas_pipeline.outside")
        outside.__file__ = __file__
        with self.assertRaises(IntegrityError):
            verify_imported_module_containment(
                REPO_ROOT, modules=(("atlas_pipeline.outside", outside),)
            )

    def test_manifest_is_fixed_key_sorted_canonical_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_source_tree(root)
            identity = generate_source_tree_manifest(root)
            rows = parse_source_tree_manifest(identity.manifest_bytes)
            raw_lines = identity.manifest_bytes.splitlines(keepends=True)

            self.assertEqual(len(raw_lines), len(rows))
            self.assertTrue(all(line.endswith(b"\n") for line in raw_lines))
            self.assertNotIn(b"\r", identity.manifest_bytes)
            self.assertEqual(
                [row["path"] for row in rows],
                sorted(row["path"] for row in rows),
            )
            for line, row in zip(raw_lines, rows, strict=True):
                self.assertEqual(tuple(row), ("path", "bytes", "sha256"))
                self.assertEqual(line, canonical_json_bytes(row))
            self.assertEqual(identity.source_tree_id, digest(identity.manifest_bytes))

            first = rows[0]
            invalid_manifests = (
                canonical_json_bytes({"bytes": first["bytes"], "path": first["path"], "sha256": first["sha256"]}),
                identity.manifest_bytes.replace(b"\n", b"\r\n", 1),
                identity.manifest_bytes + b"\n",
                canonical_json_bytes({"path": "/absolute", "bytes": 1, "sha256": "0" * 64}),
                canonical_json_bytes({"schema_version": "obsolete", "files": list(rows)}),
            )
            for invalid in invalid_manifests:
                with self.subTest(invalid=invalid[:80]), self.assertRaises(IntegrityError):
                    parse_source_tree_manifest(invalid)

            reversed_rows = b"".join(
                canonical_json_bytes(row) for row in reversed(rows)
            )
            with self.assertRaisesRegex(IntegrityError, "strictly ascending"):
                parse_source_tree_manifest(reversed_rows)

    @staticmethod
    def _write_minimal_source_tree(root: Path) -> None:
        (root / ".node-version").write_text("22.17.0\n")
        (root / ".python-version").write_text("3.12.12\n")
        for directory in (
            root / "scripts" / "atlas_pipeline",
            root / "app" / "vendor",
            root / "environment",
            root / "tooling" / "topology",
        ):
            directory.mkdir(parents=True)
        (root / "scripts" / "atlas_pipeline" / "trust.py").write_text("VALUE = 1\n")
        (root / "app" / "entry.ts").write_text("export {}\n")
        (root / "app" / "vendor" / "heartland-ui.tree.jsonl").write_text(
            '{"path":"synthetic"}\n'
        )
        (root / "app" / "vendor" / "heartland-ui").mkdir()
        (root / "app" / "vendor" / "heartland-ui" / "README.md").write_text(
            "# Synthetic vendored package\n"
        )
        (root / "environment" / "config.json").write_text("{}\n")
        (root / "tooling" / "topology" / "config.json").write_text("{}\n")

    def test_unmanifested_native_or_bytecode_shadow_is_rejected(self):
        for shadow_name in ("trust.cpython-312-darwin.so", "trust.pyc"):
            with self.subTest(shadow_name=shadow_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._write_minimal_source_tree(root)
                (root / "scripts" / "atlas_pipeline" / shadow_name).write_bytes(b"shadow")
                with self.assertRaisesRegex(
                    IntegrityError, "unallowlisted executable/code file"
                ):
                    generate_source_tree_manifest(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_source_tree(root)
            cached = root / "scripts" / "atlas_pipeline" / "__pycache__"
            cached.mkdir()
            bytecode = cached / "trust.cpython-312.pyc"
            bytecode.write_bytes(b"shadow")
            shadow = types.ModuleType("atlas_pipeline.trust")
            shadow.__file__ = str(bytecode)
            with self.assertRaisesRegex(IntegrityError, "approved exact manifested source path"):
                verify_imported_module_containment(
                    root, modules=(("atlas_pipeline.trust", shadow),)
                )

    @staticmethod
    def _timestamp_cache(source: Path, compiled_body: bytes) -> bytes:
        source_stat = source.stat()
        code = compile(
            compiled_body,
            str(source),
            "exec",
            dont_inherit=True,
            optimize=sys.flags.optimize,
        )
        return (
            importlib.util.MAGIC_NUMBER
            + struct.pack(
                "<III",
                0,
                int(source_stat.st_mtime) & 0xFFFFFFFF,
                source_stat.st_size & 0xFFFFFFFF,
            )
            + marshal.dumps(code)
        )

    def test_active_python_cache_is_exact_derivative_and_manifest_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_source_tree(root)
            source = root / "scripts" / "atlas_pipeline" / "trust.py"
            baseline = generate_source_tree_manifest(root)
            cache_directory = source.parent / "__pycache__"
            cache_directory.mkdir()
            cache_tag = sys.implementation.cache_tag
            self.assertIsInstance(cache_tag, str)
            optimization = "" if sys.flags.optimize == 0 else f".opt-{sys.flags.optimize}"
            cache = cache_directory / f"trust.{cache_tag}{optimization}.pyc"

            cache.write_bytes(self._timestamp_cache(source, source.read_bytes()))
            with_cache = generate_source_tree_manifest(root)
            self.assertEqual(with_cache.manifest_bytes, baseline.manifest_bytes)
            self.assertEqual(with_cache.source_tree_id, baseline.source_tree_id)
            imported = types.ModuleType("atlas_pipeline.trust")
            imported.__file__ = str(source)
            imported.__cached__ = str(cache)
            self.assertEqual(
                verify_imported_module_containment(
                    root, modules=(("atlas_pipeline.trust", imported),)
                ),
                ("atlas_pipeline.trust",),
            )

            malicious = compile(
                b"VALUE = 2\n",
                str(source),
                "exec",
                dont_inherit=True,
                optimize=sys.flags.optimize,
            )
            cache.write_bytes(
                importlib.util.MAGIC_NUMBER
                + struct.pack("<I", 1)
                + b"\0" * 8
                + marshal.dumps(malicious)
            )
            with self.assertRaisesRegex(
                IntegrityError, "active Python cache differs from manifested source"
            ):
                generate_source_tree_manifest(root)

            cache.write_bytes(b"\0\0\0\0foreign-runtime-cache")
            foreign = generate_source_tree_manifest(root)
            self.assertEqual(foreign.manifest_bytes, baseline.manifest_bytes)
            self.assertEqual(foreign.source_tree_id, baseline.source_tree_id)

            cache.write_bytes(importlib.util.MAGIC_NUMBER + b"\0")
            with self.assertRaisesRegex(IntegrityError, "Python cache is truncated"):
                generate_source_tree_manifest(root)

    def test_current_runtime_orphan_and_cache_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_source_tree(root)
            cache_directory = root / "scripts" / "atlas_pipeline" / "__pycache__"
            cache_directory.mkdir()
            cache_tag = sys.implementation.cache_tag
            optimization = "" if sys.flags.optimize == 0 else f".opt-{sys.flags.optimize}"
            orphan = cache_directory / f"orphan.{cache_tag}{optimization}.pyc"
            orphan.write_bytes(importlib.util.MAGIC_NUMBER + b"\0" * 12)
            with self.assertRaisesRegex(IntegrityError, "lacks exact source"):
                generate_source_tree_manifest(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_source_tree(root)
            cache_directory = root / "scripts" / "atlas_pipeline" / "__pycache__"
            cache_directory.mkdir()
            (cache_directory / "trust.pyc").symlink_to(
                root / "scripts" / "atlas_pipeline" / "trust.py"
            )
            with self.assertRaisesRegex(IntegrityError, "special entry"):
                generate_source_tree_manifest(root)

    def test_source_scope_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".node-version").write_text("22.17.0\n")
            (root / ".python-version").write_text("3.12.12\n")
            for directory in (
                root / "scripts",
                root / "app",
                root / "environment",
                root / "tooling" / "topology",
            ):
                directory.mkdir(parents=True)
            (root / "scripts" / "entry.py").write_text("pass\n")
            (root / "app" / "entry.ts").write_text("export {}\n")
            (root / "app" / "vendor").mkdir()
            (root / "app" / "vendor" / "heartland-ui.tree.jsonl").write_text(
                '{"path":"synthetic"}\n'
            )
            (root / "app" / "vendor" / "heartland-ui").mkdir()
            (root / "app" / "vendor" / "heartland-ui" / "README.md").write_text(
                "# Synthetic vendored package\n"
            )
            (root / "environment" / "config.json").write_text("{}\n")
            (root / "tooling" / "topology" / "config.json").write_text("{}\n")
            (root / "scripts" / "linked.py").symlink_to(root / "scripts" / "entry.py")
            with self.assertRaises(IntegrityError):
                generate_source_tree_manifest(root)

    def test_independent_control_hash_is_mandatory_and_binds_all_candidates(self):
        identity = generate_source_tree_manifest(REPO_ROOT)
        bundle = canonical_json_bytes({"synthetic_bundle": "candidate"})
        review = canonical_json_bytes({"synthetic_review_config": "approved"})
        authorization = canonical_json_bytes(
            {
                "schema_version": "atlas-v19-entrypoint-authorization-receipt-1",
                "authorization_id": "SYNTHETIC-AUTHORIZATION-2F",
                "phase": "2F",
                "status": "AUTHORIZED_PHASE_2F",
                "source_tree_id": identity.source_tree_id,
                "bundle_manifest_sha256": digest(bundle),
                "integrator_control_id": "SYNTHETIC-CONTROL-2F",
                "approved_by": "Independent synthetic integrator",
                "approved_at_utc": NOW,
            }
        )
        receipt_context = ReceiptContext(
            identity.source_tree_id,
            digest(bundle),
            "SYNTHETIC-AUTHORIZATION-2F",
        )
        network = network_receipt(receipt_context)
        control_receipts = canonical_json_bytes(
            {
                "schema_version": "atlas-v19-control-receipts-manifest-1",
                "source_tree_id": identity.source_tree_id,
                "bundle_manifest_sha256": digest(bundle),
                "authorization_id": "SYNTHETIC-AUTHORIZATION-2F",
                "receipts": [
                    {
                        "receipt_id": "network_denied",
                        "relative_path": "receipts/network-denied.json",
                        "bytes": len(network),
                        "sha256": digest(network),
                    }
                ],
            }
        )
        control = canonical_json_bytes(
            {
                "schema_version": TRUST_ROOT_SCHEMA_VERSION,
                "control_id": "SYNTHETIC-CONTROL-2F",
                "phase": "2F",
                "source_tree_manifest_sha256": identity.source_tree_id,
                "bundle_manifest_sha256": digest(bundle),
                "authorization_receipt_sha256": digest(authorization),
                "review_config_sha256": digest(review),
                "control_receipts_manifest_sha256": digest(control_receipts),
                "storage_preflight_receipt_sha256": None,
                "approved_by": "Independent synthetic integrator",
                "approved_at_utc": NOW,
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {
                "source": root / "source-tree.json",
                "bundle": root / "bundle.json",
                "authorization": root / "authorization.json",
                "control": root / "control.json",
                "review": root / "review.json",
                "control_receipts": root / "control-receipts.json",
            }
            for path, body in (
                (paths["source"], identity.manifest_bytes),
                (paths["bundle"], bundle),
                (paths["authorization"], authorization),
                (paths["control"], control),
                (paths["review"], review),
                (paths["control_receipts"], control_receipts),
            ):
                path.write_bytes(body)
            (root / "receipts").mkdir()
            (root / "receipts" / "network-denied.json").write_bytes(network)
            runtime = verify_entrypoint_authority(
                repo_root=REPO_ROOT,
                phase="2F",
                source_tree_manifest_path=paths["source"],
                integrator_control_path=paths["control"],
                approved_control_sha256=digest(control),
                bundle_manifest_path=paths["bundle"],
                authorization_receipt_path=paths["authorization"],
                control_receipts_manifest_path=paths["control_receipts"],
                review_config_path=paths["review"],
            )
            self.assertEqual(runtime.source_tree_id, identity.source_tree_id)
            with self.assertRaises(IntegrityError):
                verify_entrypoint_authority(
                    repo_root=REPO_ROOT,
                    phase="2F",
                    source_tree_manifest_path=paths["source"],
                    integrator_control_path=paths["control"],
                    approved_control_sha256="",
                    bundle_manifest_path=paths["bundle"],
                    authorization_receipt_path=paths["authorization"],
                    control_receipts_manifest_path=paths["control_receipts"],
                    review_config_path=paths["review"],
                )
            paths["bundle"].write_bytes(canonical_json_bytes({"synthetic_bundle": "self-authored"}))
            with self.assertRaises(IntegrityError):
                verify_entrypoint_authority(
                    repo_root=REPO_ROOT,
                    phase="2F",
                    source_tree_manifest_path=paths["source"],
                    integrator_control_path=paths["control"],
                    approved_control_sha256=digest(control),
                    bundle_manifest_path=paths["bundle"],
                    authorization_receipt_path=paths["authorization"],
                    control_receipts_manifest_path=paths["control_receipts"],
                    review_config_path=paths["review"],
                )


class ReceiptRegistryTests(unittest.TestCase):
    def setUp(self):
        self.context = ReceiptContext(
            digest(b"canonical-source-tree-manifest"),
            digest(b"canonical-bundle-manifest"),
            "SYNTHETIC-AUTHORIZATION",
        )

    def test_receipt_ids_have_distinct_schemas_and_semantic_cross_bindings(self):
        network = network_receipt(self.context)
        dependency = dependency_receipt(self.context, network)
        parsed = validate_receipt_set(
            {"network_denied": network, "dependency_materialization": dependency},
            required_ids={"network_denied", "dependency_materialization"},
            context=self.context,
        )
        self.assertEqual(parsed["network_denied"]["status"], "verified_os_level")
        with self.assertRaises(IntegrityError):
            validate_control_receipt(
                "dependency_materialization", network, context=self.context
            )
        with self.assertRaises(IntegrityError):
            validate_receipt_set(
                {"network_denied": network, "dependency_materialization": network},
                required_ids={"network_denied", "dependency_materialization"},
                context=self.context,
            )
        drifted = dependency.replace(digest(network).encode(), digest(b"other").encode())
        with self.assertRaises(IntegrityError):
            validate_receipt_set(
                {"network_denied": network, "dependency_materialization": drifted},
                required_ids={"network_denied", "dependency_materialization"},
                context=self.context,
            )

    def test_source_tree_receipt_binds_the_immutable_allowlist_policy(self):
        body = receipt_body(
            "source_tree_freeze", self.context,
            status="frozen_verified", command="verify-source-tree",
            evidence={
                "source_tree_manifest_sha256": self.context.source_tree_id,
                "file_count": 100,
                "allowlist_policy_sha256": SOURCE_TREE_POLICY_SHA256,
                "symlink_count": 0,
                "protected_files_excluded": True,
                "ignored_outputs_excluded": True,
            },
        )
        validate_control_receipt("source_tree_freeze", body, context=self.context)
        drift = json.loads(body)
        drift["evidence"]["allowlist_policy_sha256"] = digest(b"self-authored policy")
        with self.assertRaises(IntegrityError):
            validate_control_receipt(
                "source_tree_freeze", canonical_json_bytes(drift), context=self.context
            )

    def test_predecessor_bridge_receipt_is_typed_cross_bound_and_tamper_evident(self):
        evidence = predecessor_bridge_evidence(self.context.source_tree_id)
        body = receipt_body(
            "predecessor_bridge_audit",
            self.context,
            status="verified",
            command="verify-predecessor-bridge",
            evidence=evidence,
        )
        validate_control_receipt(
            "predecessor_bridge_audit", body, context=self.context
        )

        cases = []
        wrong_tree = json.loads(body)
        wrong_tree["evidence"]["active_source_tree_id"] = digest(b"other tree")
        cases.append(wrong_tree)
        wrong_hash = json.loads(body)
        wrong_hash["evidence"]["predecessor_rows"][0]["body_sha256"] = digest(
            b"tampered predecessor"
        )
        cases.append(wrong_hash)
        wrong_matrix = json.loads(body)
        wrong_matrix["evidence"]["negative_matrix_sha256"] = digest(
            b"different valid matrix hash"
        )
        cases.append(wrong_matrix)
        wrong_path = json.loads(body)
        wrong_path["evidence"]["encounter_ledger"]["rows"][0]["verification_path"] = (
            "synthetic.unapproved_path"
        )
        wrong_path["evidence"]["encounter_ledger_sha256"] = digest(
            canonical_json_bytes(wrong_path["evidence"]["encounter_ledger"])
        )
        cases.append(wrong_path)
        for field, invalid in (
            ("input_count", 54.0),
            ("request_count", 22635.0),
            ("census_attempt_count", 5.0),
            ("unlisted_encounter_count", False),
        ):
            wrong_type = json.loads(body)
            wrong_type["evidence"][field] = invalid
            cases.append(wrong_type)
        wrong_encounter_type = json.loads(body)
        wrong_encounter_type["evidence"]["encounter_ledger"]["rows"][0][
            "encounter_count"
        ] = True
        wrong_encounter_type["evidence"]["encounter_ledger_sha256"] = digest(
            canonical_json_bytes(
                wrong_encounter_type["evidence"]["encounter_ledger"]
            )
        )
        cases.append(wrong_encounter_type)
        for value in cases:
            with self.subTest(case=value["evidence"]), self.assertRaises(IntegrityError):
                validate_control_receipt(
                    "predecessor_bridge_audit",
                    canonical_json_bytes(value),
                    context=self.context,
                )

    def test_nppes_and_census_receipt_chains_reject_cross_phase_hash_drift(self):
        archive_sha = digest(b"synthetic NPPES archive")
        candidate_sha = digest(b"synthetic central-directory manifest")
        transport = receipt_body(
            "nppes_transport", self.context,
            status="candidate_complete_stopped_before_container_open",
            command="acquire-nppes-transport",
            evidence={
                "url": "https://example.test/nppes.zip", "filename": "nppes.zip",
                "expected_bytes": 100, "candidate_sha256": archive_sha,
                "attempt_receipt_sha256": digest(b"attempt"),
                "storage_preflight_receipt_sha256": digest(b"Phase-2C storage"),
                "response_status": 200,
                "content_encoding": "identity", "container_opened": False,
                "completed_transfer_count": 1,
            },
        )
        candidate = receipt_body(
            "nppes_zip_candidate", self.context,
            status="candidate_manifest_complete_stopped_before_payload",
            command="inspect-nppes-container",
            evidence={
                "archive_sha256": archive_sha, "candidate_manifest_sha256": candidate_sha,
                "member_count": 2, "payload_opened": False,
                "analytical_rows_parsed": False,
            },
        )
        container = receipt_body(
            "nppes_container", self.context,
            status="crc_verified_stopped_before_analytical_parse",
            command="verify-nppes-container",
            evidence={
                "archive_sha256": archive_sha, "candidate_manifest_sha256": candidate_sha,
                "approval_id": "SYNTHETIC-ZIP-APPROVAL",
                "approval_receipt_sha256": digest(b"approval"),
                "approval_trust_root_sha256": digest(b"approval trust root"),
                "selected_member_filename": "npidata_pfile_fixture.csv",
                "selected_member_central_metadata_sha256": digest(b"central metadata"),
                "selected_member_bytes": 100,
                "selected_member_sha256": digest(b"selected member"), "member_count": 2,
                "crc_verified": True, "analytical_rows_parsed": False,
            },
        )
        validate_receipt_set(
            {"nppes_transport": transport, "nppes_zip_candidate": candidate, "nppes_container": container},
            required_ids={"nppes_transport", "nppes_zip_candidate", "nppes_container"},
            context=self.context,
        )
        drift = json.loads(container)
        drift["evidence"]["archive_sha256"] = digest(b"other archive")
        with self.assertRaises(IntegrityError):
            validate_receipt_set(
                {"nppes_transport": transport, "nppes_zip_candidate": candidate,
                 "nppes_container": canonical_json_bytes(drift)},
                required_ids={"nppes_transport", "nppes_zip_candidate", "nppes_container"},
                context=self.context,
            )

    def test_phase2f_verified_imports_bind_exact_prior_phase_receipts(self):
        phase2b = ReceiptContext(
            self.context.source_tree_id,
            digest(b"approved Phase-2B manifest"),
            "SYNTHETIC-AUTHORIZATION-2B",
        )
        phase2c = ReceiptContext(
            self.context.source_tree_id,
            digest(b"approved Phase-2C plan"),
            "SYNTHETIC-AUTHORIZATION-2C",
        )
        phase2di = ReceiptContext(
            self.context.source_tree_id,
            digest(b"approved Phase-2D-I manifest"),
            "SYNTHETIC-AUTHORIZATION-2DI",
        )
        input_original = receipt_body(
            "input_materialization",
            phase2b,
            status="verified_frozen",
            command="materialize-inputs",
            evidence={
                "input_manifest_sha256": phase2b.bundle_manifest_sha256,
                "artifact_count": 12,
                "materialization_root_sha256": digest(b"materialization root"),
                "validator_registry_sha256": digest(b"validator registry"),
            },
        )
        input_import = receipt_body(
            "input_materialization_import",
            self.context,
            status="verified_import",
            command="verify-input-materialization",
            evidence={
                "phase2b_bundle_manifest_sha256": phase2b.bundle_manifest_sha256,
                "phase2b_authorization_id": phase2b.authorization_id,
                "input_materialization_receipt_sha256": digest(input_original),
                "phase2f_input_manifest_sha256": digest(b"Phase-2F input manifest"),
                "artifact_count": 12,
                "materialization_root_sha256": digest(b"materialization root"),
                "validator_registry_sha256": digest(b"validator registry"),
            },
        )
        archive_sha = digest(b"NPPES archive")
        candidate_sha = digest(b"NPPES candidate manifest")
        transport_evidence = {
            "url": "https://example.test/nppes.zip",
            "filename": "nppes.zip",
            "expected_bytes": 100,
            "candidate_sha256": archive_sha,
            "attempt_receipt_sha256": digest(b"transport attempt"),
            "storage_preflight_receipt_sha256": digest(b"Phase-2C storage"),
            "response_status": 200,
            "content_encoding": "identity",
            "container_opened": False,
            "completed_transfer_count": 1,
        }
        candidate_evidence = {
            "archive_sha256": archive_sha,
            "candidate_manifest_sha256": candidate_sha,
            "member_count": 2,
            "payload_opened": False,
            "analytical_rows_parsed": False,
        }
        container_evidence = {
            "archive_sha256": archive_sha,
            "candidate_manifest_sha256": candidate_sha,
            "approval_id": "SYNTHETIC-ZIP-APPROVAL",
            "approval_receipt_sha256": digest(b"member approval"),
            "approval_trust_root_sha256": digest(b"approval trust root"),
            "selected_member_filename": "npidata_pfile_fixture.csv",
            "selected_member_central_metadata_sha256": digest(b"central metadata"),
            "selected_member_bytes": 100,
            "selected_member_sha256": digest(b"selected member"),
            "member_count": 2,
            "crc_verified": True,
            "analytical_rows_parsed": False,
        }
        transport = receipt_body(
            "nppes_transport", phase2c,
            status="candidate_complete_stopped_before_container_open",
            command="acquire-nppes-transport", evidence=transport_evidence,
        )
        candidate = receipt_body(
            "nppes_zip_candidate", phase2di,
            status="candidate_manifest_complete_stopped_before_payload",
            command="inspect-nppes-container", evidence=candidate_evidence,
        )
        container = receipt_body(
            "nppes_container", phase2di,
            status="crc_verified_stopped_before_analytical_parse",
            command="verify-nppes-container", evidence=container_evidence,
        )
        transport_import = receipt_body(
            "nppes_transport_import", self.context,
            status="verified_import", command="verify-nppes-transport",
            evidence={
                "phase2c_bundle_manifest_sha256": phase2c.bundle_manifest_sha256,
                "phase2c_authorization_id": phase2c.authorization_id,
                "nppes_transport_receipt_sha256": digest(transport),
                **transport_evidence,
            },
        )
        candidate_import = receipt_body(
            "nppes_zip_candidate_import", self.context,
            status="verified_import", command="verify-nppes-zip-candidate",
            evidence={
                "phase2di_bundle_manifest_sha256": phase2di.bundle_manifest_sha256,
                "phase2di_authorization_id": phase2di.authorization_id,
                "nppes_zip_candidate_receipt_sha256": digest(candidate),
                **candidate_evidence,
            },
        )
        container_import = receipt_body(
            "nppes_container_import", self.context,
            status="verified_import", command="verify-nppes-container-import",
            evidence={
                "phase2di_bundle_manifest_sha256": phase2di.bundle_manifest_sha256,
                "phase2di_authorization_id": phase2di.authorization_id,
                "nppes_container_receipt_sha256": digest(container),
                **container_evidence,
            },
        )
        receipts = {
            "input_materialization": input_original,
            "input_materialization_import": input_import,
            "nppes_transport": transport,
            "nppes_transport_import": transport_import,
            "nppes_zip_candidate": candidate,
            "nppes_zip_candidate_import": candidate_import,
            "nppes_container": container,
            "nppes_container_import": container_import,
        }
        parsed = validate_receipt_set(
            receipts,
            required_ids=set(receipts),
            context=self.context,
            cross_phase_original_ids=(
                PHASE2F_CROSS_PHASE_ORIGINAL_IDS & set(receipts)
            ),
        )
        self.assertEqual(
            parsed["nppes_container_import"]["evidence"][
                "selected_member_sha256"
            ],
            container_evidence["selected_member_sha256"],
        )

        recontextualized = json.loads(transport)
        recontextualized["bundle_manifest_sha256"] = self.context.bundle_manifest_sha256
        recontextualized["authorization_id"] = self.context.authorization_id
        receipts["nppes_transport"] = canonical_json_bytes(recontextualized)
        with self.assertRaisesRegex(IntegrityError, "recontextualized"):
            validate_receipt_set(
                receipts,
                required_ids=set(receipts),
                context=self.context,
                cross_phase_original_ids=(
                    PHASE2F_CROSS_PHASE_ORIGINAL_IDS & set(receipts)
                ),
            )

        request_sha = digest(b"canonical D-II request manifest")
        d2ii = receipt_body(
            "d2ii_prepare_requests", self.context, status="verified_import", command="verify-d2ii-handoff",
            evidence={
                "phase2dii_bundle_manifest_sha256": digest(b"distinct Phase2D-II bundle"),
                "d2ii_handoff_receipt_sha256": digest(b"D-II handoff receipt"),
                "nppes_archive_sha256": archive_sha,
                "nppes_candidate_manifest_sha256": candidate_sha,
                "nppes_approval_receipt_sha256": digest(b"approval"),
                "nppes_approval_trust_root_sha256": digest(b"approval trust root"),
                "nppes_selected_member_filename": "npidata.csv",
                "nppes_selected_member_sha256": digest(b"selected member"),
                "provider_mapping_sha256": digest(b"providers"),
                "hospital_mapping_sha256": digest(b"hospitals"),
                "nppes_audit_sha256": digest(b"audit"),
                "nppes_dispositions_sha256": digest(b"dispositions"),
                "request_manifest_sha256": request_sha,
                "request_chunks_sha256": digest(b"chunks"), "request_count": 1,
                "chunk_count": 1, "storage_preflight_receipt_sha256": digest(b"storage"),
            },
        )
        census = receipt_body(
            "census_acquisition", self.context, status="verified_import", command="verify-census-handoff",
            evidence={
                "phase2e_bundle_manifest_sha256": digest(b"distinct Phase2E bundle"),
                "request_manifest_sha256": request_sha,
                "attempts_manifest_sha256": digest(b"attempts"),
                "canonical_snapshot_sha256": digest(b"snapshot"),
                "geocoder_acquisition_receipt_sha256": digest(b"geocoder receipt"),
                "benchmark_id": 4, "vintage_id": 4, "attempt_count": 1,
            },
        )
        validate_receipt_set(
            {"d2ii_prepare_requests": d2ii, "census_acquisition": census},
            required_ids={"d2ii_prepare_requests", "census_acquisition"},
            context=self.context,
        )
        census_drift = json.loads(census)
        census_drift["evidence"]["request_manifest_sha256"] = digest(b"other requests")
        with self.assertRaises(IntegrityError):
            validate_receipt_set(
                {"d2ii_prepare_requests": d2ii,
                 "census_acquisition": canonical_json_bytes(census_drift)},
                required_ids={"d2ii_prepare_requests", "census_acquisition"},
                context=self.context,
            )

    def test_storage_preflight_requires_exact_root_device_and_25_gib(self):
        body = receipt_body(
            "storage_preflight",
            self.context,
            status="approved",
            command="approve-storage-preflight",
            evidence={
                "work_root": "/synthetic/atlas-work",
                "output_parent": "/synthetic/atlas-work/output",
                "device": "synthetic-device",
                "st_dev": 42,
                "checked_at_utc": NOW,
                "free_bytes": 30 * 1024**3,
                "worst_case_bytes": 25 * 1024**3,
                "minimum_required_bytes": 25 * 1024**3,
                "filesystem_identity_sha256": digest(b"filesystem"),
                "integrator_control_id": "SYNTHETIC-STORAGE-CONTROL",
                "approved_by": "Independent synthetic integrator",
                "approved": True,
            },
        )
        validate_control_receipt("storage_preflight", body, context=self.context)
        too_small = body.replace(str(25 * 1024**3).encode(), str(24 * 1024**3).encode(), 1)
        with self.assertRaises(IntegrityError):
            validate_control_receipt("storage_preflight", too_small, context=self.context)


class OutputReceiptHardeningTests(unittest.TestCase):
    def test_receipt_requires_canonical_command_safe_contained_regular_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_bundle(
                root / "bundle",
                phase="2D-II",
                expectations={},
                files=[("SYNTHETIC", "nppes_audit_json", "audit.json", b"{}\n", "application/json")],
            )
            bundle = FrozenBundle.load(root / "bundle")
            output = root / "output"
            output.mkdir()
            artifact = output / "nested" / "artifact.bin"
            atomic_write(artifact, b"synthetic-artifact")
            receipt = write_output_receipt(
                output,
                command="synthetic-command",
                bundle=bundle,
                artifacts={"artifact": artifact},
                completed_at_utc=NOW,
            )
            verify_output_receipt(
                receipt,
                bundle=bundle,
                artifact_root=output,
                required_artifacts={"artifact"},
                expected_command="synthetic-command",
            )
            valid_receipt = receipt.read_bytes()
            receipt.write_text(json.dumps(json.loads(valid_receipt), indent=2) + "\n")
            with self.assertRaises(IntegrityError):
                verify_output_receipt(
                    receipt,
                    bundle=bundle,
                    artifact_root=output,
                    required_artifacts={"artifact"},
                    expected_command="synthetic-command",
                )
            receipt.write_bytes(valid_receipt)
            with self.assertRaises(IntegrityError):
                verify_output_receipt(
                    receipt,
                    bundle=bundle,
                    artifact_root=output,
                    required_artifacts={"artifact"},
                    expected_command="other-command",
                )

            value = json.loads(receipt.read_text())
            value["artifacts"][0]["relative_path"] = "../outside.bin"
            receipt.write_bytes(canonical_json_bytes(value))
            with self.assertRaises(IntegrityError):
                verify_output_receipt(
                    receipt,
                    bundle=bundle,
                    artifact_root=output,
                    required_artifacts={"artifact"},
                    expected_command="synthetic-command",
                )

            receipt.unlink()
            linked_parent = output / "linked"
            linked_parent.symlink_to(output / "nested", target_is_directory=True)
            linked_artifact = linked_parent / "artifact.bin"
            with self.assertRaises(IntegrityError):
                write_output_receipt(
                    output,
                    command="synthetic-command",
                    bundle=bundle,
                    artifacts={"artifact": linked_artifact},
                    completed_at_utc=NOW,
                )


class CleanRoomTests(unittest.TestCase):
    NEXT_ENV_BODY = b'''/// <reference types="next" />
/// <reference types="next/image-types/global" />
import "./.next-atlas/types/routes.d.ts";

// NOTE: This file should not be edited
// see https://nextjs.org/docs/app/api-reference/config/typescript for more information.
'''

    def make_inputs(self, root: Path) -> CleanRoomInputs:
        bundle = root / "bundle"
        bundle.mkdir()
        bundle_body = canonical_json_bytes({"synthetic_bundle": "clean-room"})
        (bundle / "bundle.json").write_bytes(bundle_body)
        files = {
            "review": root / "review.json",
            "source": root / "source.json",
            "control": root / "control.json",
            "authorization": root / "authorization.json",
            "control_receipts": root / "control-receipts.json",
        }
        for name, path in files.items():
            path.write_bytes(canonical_json_bytes({"synthetic": name}))
        source_body = files["source"].read_bytes()
        context = ReceiptContext(
            digest(source_body), digest(bundle_body), "SYNTHETIC-CLEAN-ROOM"
        )
        network = network_receipt(context)
        environment = dependency_receipt(context, network)
        runtime = RuntimeControl(
            phase="2F",
            control_id="SYNTHETIC-CLEAN-CONTROL",
            authorization_id=context.authorization_id,
            trust_root_sha256=digest(files["control"].read_bytes()),
            source_tree=SourceTreeIdentity(
                REPO_ROOT,
                source_body,
                context.source_tree_id,
                ("scripts/tests/synthetic-clean-room.py",),
            ),
            approved_bundle_sha256=context.bundle_manifest_sha256,
            approved_authorization_sha256=digest(files["authorization"].read_bytes()),
            approved_review_config_sha256=digest(files["review"].read_bytes()),
            approved_control_receipts_manifest_sha256=digest(
                files["control_receipts"].read_bytes()
            ),
            approved_storage_preflight_receipt_sha256=None,
            imported_modules=("atlas_pipeline.clean_room",),
        )
        npm = shutil.which("npm")
        node = shutil.which("node")
        uv = shutil.which("uv")
        if npm is None or node is None or uv is None:
            self.skipTest("existing Node/npm/uv executables are unavailable")
        return CleanRoomInputs(
            repo_root=REPO_ROOT,
            python_executable=Path(sys.executable).resolve(),
            uv_executable=Path(uv).resolve(),
            node_executable=Path(node).resolve(),
            npm_executable=Path(npm).resolve(),
            bundle_root=bundle,
            review_config=files["review"],
            source_tree_manifest=files["source"],
            integrator_control=files["control"],
            approved_control_sha256=digest(files["control"].read_bytes()),
            authorization_receipt=files["authorization"],
            control_receipts_manifest=files["control_receipts"],
            runtime_control=runtime,
            context=context,
            input_manifest_sha256=digest(b"input-manifest"),
            environment_receipt_bytes=environment,
            network_denied_receipt_bytes=network,
        )

    def make_materialization_fixture(self, root: Path):
        (root / "controls").mkdir()
        inputs = self.make_inputs(root / "controls")
        repo = root / "repo"
        for directory in (
            "scripts",
            "app/vendor/heartland-ui",
            "app/node_modules/pkg",
            "environment",
            "tooling/topology",
        ):
            (repo / directory).mkdir(parents=True, exist_ok=True)
        (repo / ".node-version").write_text("22.17.0\n", encoding="utf-8")
        (repo / ".python-version").write_text("3.12.12\n", encoding="utf-8")
        (repo / "scripts" / "runner.py").write_text("VALUE = 1\n", encoding="utf-8")
        (repo / "environment" / "control.json").write_bytes(canonical_json_bytes({"ok": True}))
        (repo / "tooling" / "topology" / "control.json").write_bytes(
            canonical_json_bytes({"ok": True})
        )
        (repo / "app" / "vendor" / "heartland-ui.tree.jsonl").write_bytes(
            canonical_json_bytes({"path": "README.md"})
        )
        (repo / "app" / "vendor" / "heartland-ui" / "README.md").write_text(
            "fixture\n", encoding="utf-8"
        )
        (repo / "app" / "page.ts").write_text("export const value = 1;\n", encoding="utf-8")
        (repo / "app" / "tsconfig.json").write_bytes(
            (json.dumps({"include": ["next-env.d.ts", "**/*.ts"]}, indent=2) + "\n").encode()
        )
        (repo / "app" / "node_modules" / "pkg" / "index.js").write_text(
            "module.exports = 1;\n", encoding="utf-8"
        )
        os.symlink("pkg", repo / "app" / "node_modules" / "pkg-link")
        identity = generate_source_tree_manifest(repo)
        manifest = root / "source-tree.jsonl"
        manifest.write_bytes(identity.manifest_bytes)
        inputs = replace(inputs, repo_root=repo.resolve(), source_tree_manifest=manifest)
        app_rows = [
            row
            for row in parse_source_tree_manifest(identity.manifest_bytes)
            if str(row["path"]).startswith("app/")
        ]
        app_subset_sha = digest(
            canonical_json_bytes(
                {"schema_version": "atlas-v19-frozen-app-subset-1", "rows": app_rows}
            )
        )
        dependency_sha = digest(
            _dependency_tree_bytes(inputs.repo_root / "app" / "node_modules")
        )
        return inputs, app_subset_sha, dependency_sha

    def materialize_fixture(self, root: Path):
        inputs, app_subset_sha, dependency_sha = self.make_materialization_fixture(root)
        run_root = root / "run"
        (run_root / "cache" / "next").mkdir(parents=True)
        with patch(
            "atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", app_subset_sha
        ), patch(
            "atlas_pipeline.clean_room.NODE_MODULES_TREE_SHA256", dependency_sha
        ):
            project, tree = _materialize_production_app(inputs, run_root)
        return inputs, project, tree, app_subset_sha

    def complete_ephemeral_next_output(self, inputs, project):
        (project / "tsconfig.json").write_bytes(_expected_tsconfig_bytes(inputs))
        (project / "next-env.d.ts").write_bytes(self.NEXT_ENV_BODY)
        (project / ".next-atlas" / "types").mkdir(parents=True)
        (project / ".next-atlas" / "types" / "routes.d.ts").write_text(
            "export {};\n", encoding="utf-8"
        )

    def test_production_app_materialization_is_exact_contained_and_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs, project, dependency_tree, app_subset_sha = self.materialize_fixture(root)
            self.assertEqual(project, project.resolve(strict=True))
            self.assertTrue(project.is_relative_to((root / "run" / "cache").resolve()))
            self.assertNotEqual(
                (inputs.repo_root / "app" / "node_modules" / "pkg" / "index.js").stat().st_ino,
                (project / "node_modules" / "pkg" / "index.js").stat().st_ino,
            )
            self.assertEqual(os.readlink(project / "node_modules" / "pkg-link"), "pkg")
            self.assertEqual(
                _dependency_tree_bytes(project / "node_modules"), dependency_tree
            )
            with patch("atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", app_subset_sha):
                source_paths = {str(row["path"])[4:] for row in _app_manifest_rows(inputs)}
            copied_paths = {
                path.relative_to(project).as_posix()
                for path in project.rglob("*")
                if path.is_file() and "node_modules" not in path.relative_to(project).parts
            }
            self.assertEqual(copied_paths, source_paths)

            self.complete_ephemeral_next_output(inputs, project)
            with patch("atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", app_subset_sha):
                _verify_production_app_after(inputs, project, dependency_tree)

    def test_materialization_rejects_drift_escape_and_preexisting_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs, _app_subset_sha, dependency_sha = self.make_materialization_fixture(root)
            (root / "run-subset" / "cache" / "next").mkdir(parents=True)
            with patch("atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", "0" * 64), patch(
                "atlas_pipeline.clean_room.NODE_MODULES_TREE_SHA256", dependency_sha
            ), self.assertRaisesRegex(IntegrityError, "differs from the frozen app subset"):
                _materialize_production_app(inputs, root / "run-subset")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs, app_subset_sha, dependency_sha = self.make_materialization_fixture(root)
            run_root = root / "run-drift"
            (run_root / "cache" / "next").mkdir(parents=True)
            (inputs.repo_root / "app" / "page.ts").write_text("drift\n", encoding="utf-8")
            with patch("atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", app_subset_sha), patch(
                "atlas_pipeline.clean_room.NODE_MODULES_TREE_SHA256", dependency_sha
            ), self.assertRaisesRegex(IntegrityError, "source-tree files"):
                _materialize_production_app(inputs, run_root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs, app_subset_sha, dependency_sha = self.make_materialization_fixture(root)
            external = root / "external.js"
            external.write_text("external\n", encoding="utf-8")
            os.symlink(str(external), inputs.repo_root / "app" / "node_modules" / "absolute")
            run_root = root / "run-link"
            (run_root / "cache" / "next").mkdir(parents=True)
            with patch("atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", app_subset_sha), patch(
                "atlas_pipeline.clean_room.NODE_MODULES_TREE_SHA256", dependency_sha
            ), self.assertRaisesRegex(IntegrityError, "link is absolute"):
                _materialize_production_app(inputs, run_root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs, app_subset_sha, dependency_sha = self.make_materialization_fixture(root)
            project = root / "run-existing" / "cache" / "next" / "project" / "app"
            project.mkdir(parents=True)
            (project / "unexpected").write_text("x", encoding="utf-8")
            with patch("atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", app_subset_sha), patch(
                "atlas_pipeline.clean_room.NODE_MODULES_TREE_SHA256", dependency_sha
            ), self.assertRaisesRegex(IntegrityError, "already exists"):
                _materialize_production_app(inputs, root / "run-existing")

    def test_post_build_rejects_every_unapproved_delta_and_source_mutation(self):
        mutations = (
            "nested-extra",
            "expected-symlink",
            "generated-symlink",
            "tsconfig-bytes",
            "next-env-bytes",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                inputs, project, dependency_tree, app_subset_sha = self.materialize_fixture(root)
                self.complete_ephemeral_next_output(inputs, project)
                if mutation == "nested-extra":
                    (project / "vendor" / "evil.ts").write_text("evil\n", encoding="utf-8")
                elif mutation == "expected-symlink":
                    (project / "page.ts").unlink()
                    os.symlink("tsconfig.json", project / "page.ts")
                elif mutation == "generated-symlink":
                    os.symlink("/tmp", project / ".next-atlas" / "escape")
                elif mutation == "tsconfig-bytes":
                    body = json.loads((project / "tsconfig.json").read_bytes())
                    (project / "tsconfig.json").write_text(json.dumps(body), encoding="utf-8")
                else:
                    (project / "next-env.d.ts").write_bytes(self.NEXT_ENV_BODY + b" ")
                with patch("atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", app_subset_sha), self.assertRaises(IntegrityError):
                    _verify_production_app_after(inputs, project, dependency_tree)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs, project, dependency_tree, app_subset_sha = self.materialize_fixture(root)
            self.complete_ephemeral_next_output(inputs, project)
            (inputs.repo_root / "scripts" / "runner.py").write_text("VALUE = 2\n", encoding="utf-8")
            with patch("atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", app_subset_sha), self.assertRaisesRegex(IntegrityError, "source-tree files"):
                _verify_production_app_after(inputs, project, dependency_tree)

        for target in ("copy", "source"):
            with self.subTest(dependency_drift=target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                inputs, project, dependency_tree, app_subset_sha = self.materialize_fixture(root)
                self.complete_ephemeral_next_output(inputs, project)
                dependency_root = (
                    project / "node_modules"
                    if target == "copy"
                    else inputs.repo_root / "app" / "node_modules"
                )
                (dependency_root / "pkg" / "index.js").write_text("drift\n", encoding="utf-8")
                with patch("atlas_pipeline.clean_room.FROZEN_APP_SUBSET_SHA256", app_subset_sha), self.assertRaisesRegex(IntegrityError, "node_modules"):
                    _verify_production_app_after(inputs, project, dependency_tree)

    def test_failed_production_subprocess_still_verifies_authoritative_immutability(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inputs = self.make_inputs(base)
            phase = ProcessCommand(
                "phase2f_build", REPO_ROOT, (str(inputs.python_executable), "phase")
            )
            production_project = base / "run-a" / "cache" / "next" / "project" / "app"
            production = ProcessCommand(
                "production_app_build",
                production_project,
                (str(inputs.npm_executable), "run", "build"),
            )
            dependency_copy_drifted = False

            def fail_production(argv, **_kwargs):
                nonlocal dependency_copy_drifted
                if argv[-1] == "phase":
                    output = base / "run-a" / "output" / "app"
                    output.mkdir(parents=True)
                    (output / "release-manifest.json").write_bytes(
                        canonical_json_bytes({"schema_version": 1, "files": []})
                    )
                    return subprocess.CompletedProcess(argv, 0, b"", b"")
                dependency_copy_drifted = True
                return subprocess.CompletedProcess(argv, 9, b"", b"failed")

            def dependency_tree(path):
                if (
                    path.resolve() == (production_project / "node_modules").resolve()
                    and dependency_copy_drifted
                ):
                    return b"drifted-node-tree"
                return b"synthetic-node-tree"

            with patch(
                "atlas_pipeline.clean_room.frozen_commands", return_value=(phase, production)
            ), patch(
                "atlas_pipeline.clean_room._materialize_production_app",
                side_effect=lambda _inputs, _run_root: (
                    production_project.resolve(), b"synthetic-node-tree"
                ),
            ), patch(
                "atlas_pipeline.clean_room._dependency_tree_bytes",
                side_effect=dependency_tree,
            ), patch(
                "atlas_pipeline.clean_room._verify_authoritative_source"
            ) as verify_source, patch(
                "atlas_pipeline.clean_room.subprocess.run", side_effect=fail_production
            ), self.assertRaisesRegex(
                IntegrityError, "ephemeral node_modules changed during failed"
            ):
                _run_once(inputs, base / "run-a", "A", NOW)
            self.assertGreaterEqual(verify_source.call_count, 1)

    def test_two_distinct_runs_execute_all_gates_and_compare_equal_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inputs = self.make_inputs(base)
            calls = []
            initialized_cache_paths = []

            def fake_run(argv, **kwargs):
                calls.append((tuple(argv), kwargs["env"].copy()))
                if "scripts/run_all.py" in argv:
                    environment = kwargs["env"]
                    cache_paths = {
                        Path(environment[name])
                        for name in (
                            "HOME",
                            "npm_config_cache",
                            "UV_CACHE_DIR",
                            "PYTHONPYCACHEPREFIX",
                            "PYTHONUSERBASE",
                            "ATLAS_VITE_CACHE_DIR",
                            "NODE_COMPILE_CACHE",
                            "COREPACK_HOME",
                            "TMPDIR",
                        )
                    }
                    self.assertEqual(len(cache_paths), 9)
                    self.assertTrue(all(path.is_dir() for path in cache_paths))
                    self.assertTrue(
                        all(not tuple(path.iterdir()) for path in cache_paths)
                    )
                    initialized_cache_paths.append(cache_paths)
                    output = Path(argv[argv.index("--output") + 1])
                    self.assertFalse(output.exists())
                    (output / "app").mkdir(parents=True)
                    (output / "app" / "release-manifest.json").write_bytes(
                        canonical_json_bytes({"schema_version": 1, "files": []})
                    )
                    (output / "synthetic-result.json").write_bytes(
                        canonical_json_bytes({"result": "synthetic-only"})
                    )
                return subprocess.CompletedProcess(argv, 0, b"synthetic stdout", b"")

            inherited = base / "forbidden-shared-cache"
            inherited_release_manifest = "f" * 64
            def fake_materialize(_inputs, run_root):
                project = run_root / "cache" / "next" / "project" / "app"
                project.mkdir(parents=True)
                return project.resolve(), b"synthetic-node-tree"

            with patch.dict(
                os.environ,
                {
                    "PYTHONPYCACHEPREFIX": str(inherited / "python"),
                    "npm_config_cache": str(inherited / "npm"),
                    "NPM_CONFIG_CACHE": str(inherited / "npm-upper"),
                    "UV_CACHE_DIR": str(inherited / "uv"),
                    "ATLAS_VITE_CACHE_DIR": str(inherited / "vite"),
                    "ATLAS_NEXT_DIST_DIR": str(inherited / "next"),
                    "NODE_COMPILE_CACHE": str(inherited / "node"),
                    "TMPDIR": str(inherited / "tmp"),
                    "ATLAS_RELEASE_MANIFEST_SHA256": inherited_release_manifest,
                    "JITI_FS_CACHE": "inherited-and-forbidden",
                },
            ), patch(
                "atlas_pipeline.clean_room._probe_runtime_versions",
                return_value={
                    "python": "3.12.12", "uv": "0.10.4",
                    "node": "22.17.0", "npm": "10.9.2",
                },
            ), patch(
                "atlas_pipeline.clean_room._materialize_production_app",
                side_effect=fake_materialize,
            ), patch(
                "atlas_pipeline.clean_room._verify_production_app_after"
            ), patch(
                "atlas_pipeline.clean_room._verify_authoritative_source"
            ), patch(
                "atlas_pipeline.clean_room._dependency_tree_bytes",
                return_value=b"synthetic-node-tree",
            ), patch("atlas_pipeline.clean_room.subprocess.run", side_effect=fake_run):
                result = orchestrate_clean_rooms(inputs, base / "clean")
            self.assertEqual(len(calls), 10)
            self.assertEqual(len(initialized_cache_paths), 2)
            self.assertTrue(initialized_cache_paths[0].isdisjoint(initialized_cache_paths[1]))
            self.assertFalse(inherited.exists())
            self.assertTrue(result.receipt_path.is_file())
            self.assertTrue(result.comparison_receipt_path.is_file())
            validate_control_receipt(
                "phase2f_comparison",
                result.comparison_receipt_path.read_bytes(),
                context=inputs.context,
            )
            validate_control_receipt(
                "phase2f_clean_room",
                result.receipt_path.read_bytes(),
                context=inputs.context,
            )
            release_dirs = {
                call[1]["ATLAS_RELEASE_DATA_DIR"] for call in calls
            }
            cache_dirs = {call[1]["XDG_CACHE_HOME"] for call in calls}
            reproducible_timestamps = {
                call[1]["ATLAS_PHASE2F_COMPLETED_AT_UTC"] for call in calls
            }
            self.assertEqual(len(release_dirs), 2)
            self.assertEqual(len(cache_dirs), 2)
            self.assertEqual(len(reproducible_timestamps), 1)
            self.assertTrue(all("run-a" in path or "run-b" in path for path in release_dirs))
            isolated_keys = (
                "HOME",
                "XDG_CACHE_HOME",
                "npm_config_cache",
                "NPM_CONFIG_CACHE",
                "UV_CACHE_DIR",
                "PYTHONPYCACHEPREFIX",
                "PYTHONUSERBASE",
                "ATLAS_VITE_CACHE_DIR",
                "ATLAS_NEXT_DIST_DIR",
                "ATLAS_TYPESCRIPT_BUILDINFO",
                "NODE_COMPILE_CACHE",
                "COREPACK_HOME",
                "TMPDIR",
            )
            for key in isolated_keys:
                values = {call[1][key] for call in calls}
                self.assertEqual(len(values), 2, key)
                self.assertTrue(
                    all("run-a" in value or "run-b" in value for value in values),
                    key,
                )
            self.assertTrue(all(call[1]["TMP"] == call[1]["TMPDIR"] for call in calls))
            self.assertTrue(all(call[1]["TEMP"] == call[1]["TMPDIR"] for call in calls))
            self.assertTrue(
                all(call[1]["NPM_CONFIG_CACHE"] == call[1]["npm_config_cache"] for call in calls)
            )
            typecheck_calls = [
                call for call in calls if tuple(call[0][1:4]) == ("run", "typecheck", "--")
            ]
            self.assertEqual(len(typecheck_calls), 2)
            for argv, environment in typecheck_calls:
                self.assertIn("--project", argv)
                self.assertEqual(argv[argv.index("--project") + 1], "tsconfig.clean-room.json")
                self.assertEqual(argv[-2], "--tsBuildInfoFile")
                self.assertEqual(argv[-1], environment["ATLAS_TYPESCRIPT_BUILDINFO"])
            app_test_calls = [call for call in calls if call[0][1] == "test"]
            self.assertEqual(len(app_test_calls), 2)
            self.assertTrue(
                all(
                    call[0][-4:] == (
                        "--config", "vitest.clean-room.config.mjs",
                        "--configLoader", "native",
                    )
                    for call in app_test_calls
                )
            )
            self.assertTrue(
                all("install" not in call[0] and "ci" not in call[0] for call in calls)
            )
            self.assertEqual(
                sum("ATLAS_RELEASE_MANIFEST_SHA256" in call[1] for call in calls), 2
            )
            initial_builds = [
                call for call in calls if "scripts/run_all.py" in call[0]
            ]
            self.assertEqual(len(initial_builds), 2)
            self.assertTrue(
                all("ATLAS_RELEASE_MANIFEST_SHA256" not in call[1] for call in initial_builds)
            )
            expected_release_manifest_sha256 = digest(
                canonical_json_bytes({"schema_version": 1, "files": []})
            )
            production_builds = [
                call
                for call in calls
                if len(call[0]) >= 3 and tuple(call[0][1:3]) == ("run", "build")
            ]
            self.assertEqual(len(production_builds), 2)
            self.assertTrue(
                all(call[1].get("JITI_FS_CACHE") == "false" for call in production_builds)
            )
            self.assertTrue(
                all(
                    call[1]["ATLAS_RELEASE_MANIFEST_SHA256"]
                    == expected_release_manifest_sha256
                    for call in production_builds
                )
            )
            nonproduction = [call for call in calls if call not in production_builds]
            self.assertTrue(all("JITI_FS_CACHE" not in call[1] for call in nonproduction))
            self.assertTrue(
                all(
                    "ATLAS_RELEASE_MANIFEST_SHA256" not in call[1]
                    for call in nonproduction
                )
            )
            self.assertTrue(
                all(
                    call[1].get("ATLAS_RELEASE_MANIFEST_SHA256")
                    != inherited_release_manifest
                    for call in calls
                )
            )
            for receipt_path in result.run_receipts:
                run_receipt = json.loads(receipt_path.read_bytes())
                isolated_receipt_paths = run_receipt["isolated_cache_paths"]
                self.assertEqual(len(isolated_receipt_paths), 11)
                self.assertEqual(len(set(isolated_receipt_paths.values())), 11)
                self.assertTrue(
                    all(value.startswith("cache/") for value in isolated_receipt_paths.values())
                )
                exposed_rows = [
                    row
                    for row in run_receipt["processes"]
                    if row["release_manifest_sha256"] is not None
                ]
                self.assertEqual(
                    [row["label"] for row in exposed_rows],
                    ["phase2f_build", "production_app_build"],
                )
                self.assertTrue(
                    all(
                        row["release_manifest_sha256"]
                        == expected_release_manifest_sha256
                        for row in exposed_rows
                    )
                )

    def test_production_build_rejects_missing_retained_release_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inputs = self.make_inputs(base)
            production = ProcessCommand(
                "production_app_build",
                base / "run-a" / "cache" / "next" / "project" / "app",
                (str(inputs.npm_executable), "run", "build"),
            )
            with patch(
                "atlas_pipeline.clean_room.frozen_commands",
                return_value=(production,),
            ), patch("atlas_pipeline.clean_room.subprocess.run") as run:
                with self.assertRaisesRegex(
                    IntegrityError, "production build lacks the approved release hash"
                ):
                    _run_once(inputs, base / "run-a", "A", NOW)
            run.assert_not_called()

    def test_production_process_row_rejects_release_hash_divergence(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inputs = self.make_inputs(base)
            phase = ProcessCommand(
                "phase2f_build", REPO_ROOT, (str(inputs.python_executable), "phase")
            )
            production = ProcessCommand(
                "production_app_build",
                base / "run-a" / "cache" / "next" / "project" / "app",
                (str(inputs.npm_executable), "run", "build"),
            )

            def divergent(argv, **kwargs):
                if argv[-1] == "phase":
                    output = base / "run-a" / "output" / "app"
                    output.mkdir(parents=True)
                    (output / "release-manifest.json").write_bytes(
                        canonical_json_bytes({"schema_version": 1, "files": []})
                    )
                else:
                    kwargs["env"]["ATLAS_RELEASE_MANIFEST_SHA256"] = "0" * 64
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with patch(
                "atlas_pipeline.clean_room.frozen_commands",
                return_value=(phase, production),
            ), patch(
                "atlas_pipeline.clean_room._materialize_production_app",
                side_effect=lambda _inputs, run_root: (
                    (run_root / "cache" / "next" / "project" / "app").resolve(),
                    b"synthetic-node-tree",
                ),
            ), patch(
                "atlas_pipeline.clean_room._verify_authoritative_source"
            ), patch(
                "atlas_pipeline.clean_room._dependency_tree_bytes",
                return_value=b"synthetic-node-tree",
            ), patch(
                "atlas_pipeline.clean_room.subprocess.run", side_effect=divergent
            ):
                with self.assertRaisesRegex(
                    IntegrityError, "production process row did not bind"
                ):
                    _run_once(inputs, base / "run-a", "A", NOW)

    def test_clean_room_typescript_config_excludes_shared_generated_trees(self):
        config = strict_json_loads(
            (REPO_ROOT / "app" / "tsconfig.clean-room.json").read_bytes()
        )
        self.assertEqual(config["extends"], "./tsconfig.json")
        self.assertEqual(set(config["exclude"]), {"node_modules", ".next"})
        self.assertFalse(any(".next" in pattern for pattern in config["include"]))
        vitest_config = (
            REPO_ROOT / "app" / "vitest.clean-room.config.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn("const cacheDir = process.env.ATLAS_VITE_CACHE_DIR", vitest_config)
        self.assertIn("if (!cacheDir || !path.isAbsolute(cacheDir))", vitest_config)
        self.assertIn("cacheDir,", vitest_config)

    def test_difference_and_subprocess_failure_persist_rejection_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inputs = self.make_inputs(base)
            build_count = 0

            def differing(argv, **_kwargs):
                nonlocal build_count
                if "scripts/run_all.py" in argv:
                    build_count += 1
                    output = Path(argv[argv.index("--output") + 1])
                    (output / "app").mkdir(parents=True)
                    (output / "app" / "release-manifest.json").write_bytes(
                        canonical_json_bytes({"schema_version": 1, "files": []})
                    )
                    (output / "result.json").write_bytes(
                        canonical_json_bytes({"run": build_count})
                    )
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with patch(
                "atlas_pipeline.clean_room._probe_runtime_versions",
                return_value={
                    "python": "3.12.12", "uv": "0.10.4",
                    "node": "22.17.0", "npm": "10.9.2",
                },
            ), patch(
                "atlas_pipeline.clean_room._materialize_production_app",
                side_effect=lambda _inputs, run_root: (
                    (run_root / "cache" / "next" / "project" / "app").resolve(),
                    b"synthetic-node-tree",
                ),
            ), patch(
                "atlas_pipeline.clean_room._verify_production_app_after"
            ), patch(
                "atlas_pipeline.clean_room._verify_authoritative_source"
            ), patch(
                "atlas_pipeline.clean_room._dependency_tree_bytes",
                return_value=b"synthetic-node-tree",
            ), patch("atlas_pipeline.clean_room.subprocess.run", side_effect=differing):
                with self.assertRaises(IntegrityError):
                    orchestrate_clean_rooms(inputs, base / "different")
            self.assertTrue((base / "different" / "rejection-comparison.json").is_file())

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inputs = self.make_inputs(base)
            with patch(
                "atlas_pipeline.clean_room._probe_runtime_versions",
                return_value={
                    "python": "3.12.12", "uv": "0.10.4",
                    "node": "22.17.0", "npm": "10.9.2",
                },
            ), patch(
                "atlas_pipeline.clean_room.subprocess.run",
                return_value=subprocess.CompletedProcess([], 9, b"", b"synthetic failure"),
            ):
                with self.assertRaises(IntegrityError):
                    orchestrate_clean_rooms(inputs, base / "failed")
            self.assertTrue((base / "failed" / "rejection-a.json").is_file())

    def test_clean_room_rejects_unpinned_runtime_versions_before_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = self.make_inputs(Path(tmp))
            with patch(
                "atlas_pipeline.clean_room._probe_runtime_versions",
                return_value={
                    "python": "3.12.12", "uv": "0.10.4",
                    "node": "25.5.0", "npm": "11.8.0",
                },
            ), self.assertRaisesRegex(IntegrityError, "frozen toolchain"):
                inputs.validate()


if __name__ == "__main__":
    unittest.main()
