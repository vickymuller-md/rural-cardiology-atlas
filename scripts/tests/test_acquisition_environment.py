from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas_pipeline.acquisition_environment import (
    NODE_VERSION,
    NPM_LOCK_PINS,
    NPM_OFFLINE_INSTALL_COMMAND,
    NPM_VERSION,
    validate_npm_command,
    validate_npm_lock_pins,
)
from atlas_pipeline.errors import IntegrityError


class AcquisitionEnvironmentTests(unittest.TestCase):
    def test_exact_offline_npm_policy_rejects_install_cache_exec_and_mutations(self):
        self.assertEqual(
            NPM_OFFLINE_INSTALL_COMMAND,
            ("npm", "ci", "--offline", "--ignore-scripts"),
        )
        validate_npm_command(NPM_OFFLINE_INSTALL_COMMAND)
        self.assertEqual((NODE_VERSION, NPM_VERSION), ("22.17.0", "10.9.2"))
        for command in (
            ("npm", "install"),
            ("npm", "ci"),
            ("npm", "ci", "--offline"),
            ("npm", "ci", "--offline", "--ignore-scripts", "--no-audit"),
            ("npm", "cache", "add", "mapshaper"),
            ("npm", "exec", "mapshaper"),
            ("npx", "mapshaper"),
        ):
            with self.subTest(command=command), self.assertRaises(IntegrityError):
                validate_npm_command(command)

    def test_app_and_topology_locks_have_distinct_frozen_hashes(self):
        repo_root = Path(__file__).resolve().parents[2]
        pins = validate_npm_lock_pins(repo_root)
        self.assertEqual(pins, NPM_LOCK_PINS)
        self.assertEqual(len({pin.package_lock_sha256 for pin in pins}), len(pins))
        self.assertEqual(
            {pin.surface_id for pin in pins},
            {"atlas-app", "atlas-topology-tooling"},
        )

    def test_lock_hash_drift_fails_without_invoking_npm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pin in NPM_LOCK_PINS:
                package = root / pin.package_json
                lock = root / pin.package_lock
                package.parent.mkdir(parents=True, exist_ok=True)
                package.write_text(
                    '{"packageManager":"npm@10.9.2","engines":{"node":"22.17.0","npm":"10.9.2"}}',
                    encoding="utf-8",
                )
                lock.write_text('{"lockfileVersion":3,"requires":true}', encoding="utf-8")
            with self.assertRaises(IntegrityError):
                validate_npm_lock_pins(root)


if __name__ == "__main__":
    unittest.main()
