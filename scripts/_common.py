"""Compatibility imports for the V19 offline-first pipeline.

Legacy callers cannot download or append mutable manifests through this module.
All acquisition is content-addressed and requires a later, item-specific gate.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from atlas_pipeline.acquisition import (
    FrozenInput,
    execute_frozen_response,
    execute_pending_digest_response,
    phase2a_acquisition_blocker,
    sha256_file,
)
from atlas_pipeline.canonical import haversine_miles
from atlas_pipeline.contracts import FIPS_TO_STATE, STATE_FIPS
from atlas_pipeline.errors import IntegrityError

SCRIPTS_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPTS_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
INTERIM_DIR = DATA_DIR / "interim"
PROCESSED_DIR = DATA_DIR / "processed"


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def download(*_args, **_kwargs):
    phase2a_acquisition_blocker()


def record_manifest(*_args, **_kwargs):
    raise IntegrityError("mutable append-only manifests are forbidden in Atlas V19")


def require_path(path: Path, hint: str) -> Path:
    if not path.is_file():
        raise IntegrityError(f"missing frozen input {path}: {hint}")
    return path


__all__ = [
    "DATA_DIR", "FIPS_TO_STATE", "FrozenInput", "INTERIM_DIR",
    "PROCESSED_DIR", "RAW_DIR", "SCRIPTS_DIR", "STATE_FIPS", "download",
    "execute_frozen_response", "execute_pending_digest_response", "haversine_miles",
    "log", "record_manifest", "require_path", "sha256_file",
]
