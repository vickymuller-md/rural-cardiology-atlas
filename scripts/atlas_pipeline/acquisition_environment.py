"""Frozen npm acquisition/install policy for later network-denied clean rooms.

This module validates tracked package/lock identities only.  It never invokes
npm, creates a cache, installs dependencies, or mutates either lockfile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .acquisition import sha256_file
from .errors import IntegrityError

NODE_VERSION = "22.17.0"
NPM_VERSION = "10.9.2"
NPM_OFFLINE_INSTALL_COMMAND = (
    "npm",
    "ci",
    "--offline",
    "--ignore-scripts",
)


@dataclass(frozen=True)
class NpmLockPin:
    surface_id: str
    working_directory: str
    package_json: str
    package_lock: str
    package_lock_sha256: str
    install_command: tuple[str, ...] = NPM_OFFLINE_INSTALL_COMMAND

    def validate(self) -> None:
        if not self.surface_id or Path(self.working_directory).is_absolute():
            raise IntegrityError("npm surface identity/path is invalid")
        for value in (self.working_directory, self.package_json, self.package_lock):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise IntegrityError("npm plan paths must be safe repository-relative paths")
        if len(self.package_lock_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.package_lock_sha256
        ):
            raise IntegrityError("npm lock pin is not a lowercase SHA-256")
        validate_npm_command(self.install_command)


NPM_LOCK_PINS = (
    NpmLockPin(
        "atlas-app",
        "app",
        "app/package.json",
        "app/package-lock.json",
        "ebd3ae6323c0cc4046fcccdbfd8a98132614809c29e4575cd2975b9cd1c660e0",
    ),
    NpmLockPin(
        "atlas-topology-tooling",
        "tooling/topology",
        "tooling/topology/package.json",
        "tooling/topology/package-lock.json",
        "89f33ff6eee602303122263260f8b3bda3222e526dfecc16edd6b9e908278125",
    ),
)


def validate_npm_command(command: Sequence[str]) -> None:
    """Allow only the exact no-network/no-lifecycle clean-install command."""

    if tuple(command) != NPM_OFFLINE_INSTALL_COMMAND:
        raise IntegrityError(
            "npm command is not the frozen offline ci --ignore-scripts policy"
        )


def validate_npm_lock_pins(repo_root: Path) -> tuple[NpmLockPin, ...]:
    """Validate distinct app/tooling locks without running npm or touching caches."""

    root = repo_root.absolute()
    observed_hashes: set[str] = set()
    for pin in NPM_LOCK_PINS:
        pin.validate()
        package_path = root / pin.package_json
        lock_path = root / pin.package_lock
        for path in (package_path, lock_path):
            if path.is_symlink() or not path.is_file():
                raise IntegrityError(f"npm plan input is not a regular tracked file: {path}")
        actual_hash = sha256_file(lock_path)
        if actual_hash != pin.package_lock_sha256:
            raise IntegrityError(f"npm lock hash drift: {pin.surface_id}")
        if actual_hash in observed_hashes:
            raise IntegrityError("app and topology npm surfaces must retain distinct lock identities")
        observed_hashes.add(actual_hash)
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IntegrityError("npm package/lock JSON is not parseable") from exc
        if package.get("packageManager") != f"npm@{NPM_VERSION}":
            raise IntegrityError(f"npm packageManager pin drift: {pin.surface_id}")
        engines = package.get("engines")
        if not isinstance(engines, dict) or engines.get("node") != NODE_VERSION or engines.get("npm") != NPM_VERSION:
            raise IntegrityError(f"Node/npm engine pin drift: {pin.surface_id}")
        if lock.get("lockfileVersion") != 3 or lock.get("requires") is not True:
            raise IntegrityError(f"npm lockfile contract drift: {pin.surface_id}")
        lock_text = lock_path.read_text(encoding="utf-8")
        if "git+ssh:" in lock_text or "github:" in lock_text:
            raise IntegrityError(f"network/Git dependency resolution remains in {pin.surface_id}")
    return NPM_LOCK_PINS
