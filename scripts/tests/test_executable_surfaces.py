from __future__ import annotations

import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path


class ExecutableSurfaceTests(unittest.TestCase):
    def test_run_all_describe_is_cwd_independent_and_offline(self):
        scripts = Path(__file__).resolve().parents[1]
        repo = scripts.parent
        result = subprocess.run(
            [sys.executable, str(scripts / "run_all.py"), "describe"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertIn("single identity HTTP 200", payload["phase_2c"])
        self.assertIn("central manifest", payload["phase_2d_i"])
        self.assertIn("network-denied", payload["phase_2f"])

    def test_legacy_mortality_and_dynamic_pipeline_are_not_runnable(self):
        scripts = Path(__file__).resolve().parents[1]
        self.assertFalse((scripts / "03_download_cdc_mortality.py").exists())
        self.assertTrue((scripts / "03_download_cdc_places_chd.py").is_file())
        for filename in (
            "01_download_nppes.py",
            "02_download_cms_pos.py",
            "04_download_census.py",
            "05_download_hrsa_hpsa.py",
        ):
            text = (scripts / filename).read_text()
            self.assertNotIn("requests.get", text)
            self.assertNotIn("discover_latest", text)
            self.assertNotIn("phase2a_acquisition_blocker", text)
        for filename in (
            "02_download_cms_pos.py",
            "03_download_cdc_places_chd.py",
            "04_download_census.py",
            "05_download_hrsa_hpsa.py",
        ):
            self.assertIn(
                "materialize_main", (scripts / filename).read_text(encoding="utf-8")
            )
        self.assertIn(
            "acquire_nppes_main",
            (scripts / "01_download_nppes.py").read_text(encoding="utf-8"),
        )
        orchestrator = (scripts / "atlas_pipeline" / "pipeline_cli.py").read_text(
            encoding="utf-8"
        )
        for command in (
            "materialize-known-hash",
            "acquire-nppes-transport",
            "inspect-nppes-container",
            "verify-nppes-container",
            "prepare-requests",
            "derive-places-county",
            "acquire-census",
            "acquire-hud",
            "build",
            "clean-room",
        ):
            self.assertIn(f'"{command}"', orchestrator)
        self.assertEqual((scripts / ".python-version").read_text(), "3.12.12\n")
        project = tomllib.loads((scripts / "pyproject.toml").read_text())
        self.assertEqual(project["project"]["requires-python"], "==3.12.12")
        self.assertEqual(project["tool"]["atlas"]["environment"]["uv"], "0.10.4")


if __name__ == "__main__":
    unittest.main()
