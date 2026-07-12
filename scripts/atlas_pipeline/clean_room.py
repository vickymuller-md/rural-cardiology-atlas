"""Two-process Phase-2F clean-room orchestration and comparison receipts.

The orchestrator never treats Python monkey-patching as network isolation.  It
requires a separately frozen OS-level network-denial receipt, creates two
empty output/cache roots, launches every gate as a distinct subprocess, and
compares the complete generated dataset manifests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .bundle import atomic_write
from .control_receipts import ReceiptContext, validate_control_receipt
from .errors import IntegrityError
from .strict_json import (
    canonical_json_bytes,
    strict_json_loads,
    validate_safe_relative_posix,
)
from .trust import RuntimeControl
from .source_tree import parse_source_tree_manifest, verify_source_tree_manifest

NODE_MODULES_TREE_SHA256 = "2bc4911cec85aa3414e1fd2697f409b28afb88e6709bafe3d0093591fb6cf352"
NEXT_ENV_SHA256 = "b8c5f92f7f59e547b43cd558aac101ffcb0b6e8375afc0fdc3f6b08bc6339dc5"
FROZEN_APP_SUBSET_SHA256 = "25d72705f2ea12a60eb95e22673325ae46c8f8a7995e94fb99030f73519bcb35"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class CleanRoomInputs:
    repo_root: Path
    python_executable: Path
    uv_executable: Path
    node_executable: Path
    npm_executable: Path
    bundle_root: Path
    review_config: Path
    source_tree_manifest: Path
    integrator_control: Path
    approved_control_sha256: str
    authorization_receipt: Path
    control_receipts_manifest: Path
    runtime_control: RuntimeControl
    context: ReceiptContext
    input_manifest_sha256: str
    environment_receipt_bytes: bytes
    network_denied_receipt_bytes: bytes

    def validate(self) -> None:
        self.context.validate()
        if (
            self.input_manifest_sha256 == self.context.source_tree_id
            or len(self.input_manifest_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.input_manifest_sha256)
        ):
            raise IntegrityError("clean-room input-manifest identity is invalid or conflated")
        for path, label in (
            (self.repo_root, "repository root"),
            (self.bundle_root, "bundle root"),
        ):
            if not path.is_dir() or path.is_symlink():
                raise IntegrityError(f"clean-room {label} is not a regular directory")
        for path, label in (
            (self.python_executable, "Python executable"),
            (self.uv_executable, "uv executable"),
            (self.node_executable, "Node executable"),
            (self.npm_executable, "npm executable"),
            (self.review_config, "review config"),
            (self.source_tree_manifest, "source-tree manifest"),
            (self.integrator_control, "integrator control"),
            (self.authorization_receipt, "authorization receipt"),
            (self.control_receipts_manifest, "control-receipts manifest"),
        ):
            if not path.is_file() or path.is_symlink():
                raise IntegrityError(f"clean-room {label} is not a regular file")
        runtime = self.runtime_control
        bundle_manifest = self.bundle_root / "bundle.json"
        if not bundle_manifest.is_file() or bundle_manifest.is_symlink():
            raise IntegrityError("clean-room bundle manifest is absent")
        bindings = (
            (runtime.phase == "2F", "runtime phase"),
            (runtime.source_tree.root.resolve() == self.repo_root.resolve(), "runtime source root"),
            (runtime.source_tree_id == self.context.source_tree_id, "source_tree_id"),
            (runtime.approved_bundle_sha256 == _sha(bundle_manifest.read_bytes()), "bundle hash"),
            (runtime.approved_authorization_sha256 == _sha(self.authorization_receipt.read_bytes()), "authorization hash"),
            (runtime.approved_review_config_sha256 == _sha(self.review_config.read_bytes()), "review-config hash"),
            (runtime.approved_control_receipts_manifest_sha256 == _sha(self.control_receipts_manifest.read_bytes()), "control-receipts hash"),
            (runtime.approved_storage_preflight_receipt_sha256 is None, "inapplicable storage receipt"),
            (runtime.authorization_id == self.context.authorization_id, "authorization ID"),
            (runtime.trust_root_sha256 == _sha(self.integrator_control.read_bytes()), "trust-root hash"),
            (runtime.trust_root_sha256 == self.approved_control_sha256, "external trust pin"),
        )
        failed = [label for passed, label in bindings if not passed]
        if failed:
            raise IntegrityError(f"clean-room runtime authority binding mismatch: {failed}")
        validate_control_receipt(
            "network_denied", self.network_denied_receipt_bytes, context=self.context
        )
        dependency = validate_control_receipt(
            "dependency_materialization",
            self.environment_receipt_bytes,
            context=self.context,
        )
        if dependency["evidence"]["network_denied_receipt_sha256"] != _sha(
            self.network_denied_receipt_bytes
        ):
            raise IntegrityError("clean-room environment does not bind OS network denial")
        expected_versions = {
            "python": "3.12.12",
            "uv": "0.10.4",
            "node": "22.17.0",
            "npm": "10.9.2",
        }
        receipt_versions = {
            "python": dependency["evidence"]["python_version"],
            "uv": dependency["evidence"]["uv_version"],
            "node": dependency["evidence"]["node_version"],
            "npm": dependency["evidence"]["npm_version"],
        }
        observed_versions = _probe_runtime_versions(self)
        if receipt_versions != expected_versions or observed_versions != expected_versions:
            raise IntegrityError(
                "clean-room executable/receipt versions differ from frozen toolchain"
            )


@dataclass(frozen=True)
class ProcessCommand:
    label: str
    cwd: Path
    argv: tuple[str, ...]


@dataclass(frozen=True)
class CleanRoomResult:
    receipt_path: Path
    comparison_receipt_path: Path
    run_receipts: tuple[Path, Path]
    output_manifest_sha256: str


def _probe_runtime_versions(inputs: CleanRoomInputs) -> dict[str, str]:
    commands = {
        "python": (inputs.python_executable, re.compile(r"^Python ([0-9.]+)$")),
        "uv": (inputs.uv_executable, re.compile(r"^uv ([0-9.]+)")),
        "node": (inputs.node_executable, re.compile(r"^v([0-9.]+)$")),
        "npm": (inputs.npm_executable, re.compile(r"^([0-9.]+)$")),
    }
    environment = dict(os.environ)
    environment["PATH"] = str(inputs.node_executable.parent) + os.pathsep + environment.get("PATH", "")
    observed: dict[str, str] = {}
    for name, (executable, pattern) in commands.items():
        completed = subprocess.run(
            [str(executable), "--version"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        match = pattern.search(completed.stdout.strip()) if completed.returncode == 0 else None
        if match is None:
            raise IntegrityError(f"cannot verify clean-room {name} version")
        observed[name] = match.group(1)
    return observed


def frozen_commands(inputs: CleanRoomInputs, run_root: Path) -> tuple[ProcessCommand, ...]:
    output = run_root / "output"
    typescript_build_info = (
        run_root / "cache" / "typescript" / "typecheck.tsbuildinfo"
    )
    authority = (
        "--repo-root", str(inputs.repo_root),
        "--source-tree-manifest", str(inputs.source_tree_manifest),
        "--integrator-control", str(inputs.integrator_control),
        "--approved-control-sha256", inputs.approved_control_sha256,
        "--authorization-receipt", str(inputs.authorization_receipt),
        "--control-receipts-manifest", str(inputs.control_receipts_manifest),
    )
    return (
        ProcessCommand(
            "phase2f_build",
            inputs.repo_root,
            (
                str(inputs.python_executable), "scripts/run_all.py", "build", "--offline",
                "--bundle", str(inputs.bundle_root), "--review-config",
                str(inputs.review_config), "--output", str(output), *authority,
            ),
        ),
        ProcessCommand(
            "python_tests",
            inputs.repo_root / "scripts",
            (str(inputs.python_executable), "-m", "unittest", "discover", "-s", "tests", "-t", "."),
        ),
        ProcessCommand(
            "app_tests",
            inputs.repo_root / "app",
            (
                str(inputs.npm_executable), "test", "--", "--run",
                "--config", "vitest.clean-room.config.mjs",
                "--configLoader", "native",
            ),
        ),
        ProcessCommand(
            "app_typecheck",
            inputs.repo_root / "app",
            (
                str(inputs.npm_executable), "run", "typecheck", "--",
                "--project", "tsconfig.clean-room.json",
                "--tsBuildInfoFile", str(typescript_build_info),
            ),
        ),
        ProcessCommand(
            "production_app_build",
            run_root / "cache" / "next" / "project" / "app",
            (str(inputs.npm_executable), "run", "build"),
        ),
    )


def _output_manifest(root: Path) -> bytes:
    if not root.is_dir() or root.is_symlink():
        raise IntegrityError("clean-room generated output root is absent or a symlink")
    release_manifest = root / "app" / "release-manifest.json"
    if not release_manifest.is_file() or release_manifest.is_symlink():
        raise IntegrityError("clean-room build did not generate the approved app release manifest")
    rows = []
    for path in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise IntegrityError(f"clean-room output contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise IntegrityError(f"clean-room output contains a special file: {relative}")
        validate_safe_relative_posix(relative, label="clean-room output path")
        body = path.read_bytes()
        rows.append({"path": relative, "bytes": len(body), "sha256": _sha(body)})
    if not rows:
        raise IntegrityError("clean-room output manifest is empty")
    return canonical_json_bytes(
        {"schema_version": "atlas-v19-clean-room-output-manifest-1", "files": rows}
    )


def _cache_layout(run_root: Path) -> dict[str, Path]:
    cache = run_root / "cache"
    return {
        "home": cache / "home",
        "npm": cache / "npm",
        "uv": cache / "uv",
        "python_bytecode": cache / "python" / "bytecode",
        "python_user_base": cache / "python" / "user-base",
        "vite": cache / "vite",
        "next": cache / "next",
        "typescript": cache / "typescript",
        "node_compile": cache / "node-compile",
        "corepack": cache / "corepack",
        "tmp": cache / "tmp",
    }


def _dependency_tree_bytes(root: Path) -> bytes:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise IntegrityError("node_modules root must be an absolute regular directory")
    if root != root.resolve(strict=True):
        raise IntegrityError("node_modules root is not physically canonical")
    app_root = root.parent.resolve(strict=True)
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if stat.S_ISLNK(mode):
            target = os.readlink(path)
            if os.path.isabs(target):
                raise IntegrityError(f"node_modules link is absolute: {relative}")
            resolved = (path.parent / target).resolve(strict=True)
            if not resolved.is_relative_to(app_root):
                raise IntegrityError(f"node_modules link escapes ephemeral app: {relative}")
            rows.append({"path": relative, "type": "symlink", "target": target})
        elif stat.S_ISREG(mode):
            body = path.read_bytes()
            rows.append({"path": relative, "type": "file", "bytes": len(body), "sha256": _sha(body)})
        else:
            raise IntegrityError(f"node_modules special entry: {relative}")
    return canonical_json_bytes(
        {"schema_version": "atlas-v19-node-modules-tree-1", "entry_count": len(rows), "rows": rows}
    )


def _app_manifest_rows(inputs: CleanRoomInputs) -> tuple[dict[str, object], ...]:
    rows = parse_source_tree_manifest(inputs.source_tree_manifest.read_bytes())
    app_rows = tuple(row for row in rows if str(row["path"]).startswith("app/"))
    if not app_rows:
        raise IntegrityError("source-tree manifest has no app rows")
    subset = canonical_json_bytes(
        {"schema_version": "atlas-v19-frozen-app-subset-1", "rows": list(app_rows)}
    )
    if _sha(subset) != FROZEN_APP_SUBSET_SHA256:
        raise IntegrityError("active app subset differs from the frozen app subset")
    return app_rows


def _verify_authoritative_source(inputs: CleanRoomInputs) -> None:
    verify_source_tree_manifest(
        inputs.repo_root, inputs.source_tree_manifest.read_bytes()
    )


def _verify_independent_regular_files(original: Path, copied: Path) -> None:
    for source in sorted(original.rglob("*")):
        if not stat.S_ISREG(source.lstat().st_mode):
            continue
        relative = source.relative_to(original)
        target = copied / relative
        if not target.is_file() or target.is_symlink():
            raise IntegrityError(f"copied dependency is not regular: {relative}")
        if source.stat().st_ino == target.stat().st_ino:
            raise IntegrityError(f"copied dependency shares source inode: {relative}")


def _materialize_production_app(inputs: CleanRoomInputs, run_root: Path) -> tuple[Path, bytes]:
    _verify_authoritative_source(inputs)
    cache_root = (run_root / "cache").resolve(strict=True)
    project = (cache_root / "next" / "project" / "app").resolve()
    if not project.is_relative_to(cache_root):
        raise IntegrityError("ephemeral production app escapes the run cache")
    if project.exists():
        raise IntegrityError("ephemeral production app already exists")
    project.mkdir(parents=True)
    for row in _app_manifest_rows(inputs):
        relative = Path(str(row["path"])).relative_to("app")
        source = inputs.repo_root / "app" / relative
        target = project / relative
        if source.is_symlink() or not source.is_file():
            raise IntegrityError(f"manifested app source is not regular: {relative}")
        body = source.read_bytes()
        if len(body) != row["bytes"] or _sha(body) != row["sha256"]:
            raise IntegrityError(f"manifested app source drift: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    original_modules = inputs.repo_root / "app" / "node_modules"
    original_tree = _dependency_tree_bytes(original_modules)
    if _sha(original_tree) != NODE_MODULES_TREE_SHA256:
        raise IntegrityError("approved node_modules tree drifted before production")
    shutil.copytree(original_modules, project / "node_modules", symlinks=True)
    copied_tree = _dependency_tree_bytes(project / "node_modules")
    if copied_tree != original_tree:
        raise IntegrityError("ephemeral node_modules copy differs from approved tree")
    _verify_independent_regular_files(original_modules, project / "node_modules")
    return project, original_tree


def _expected_tsconfig_bytes(inputs: CleanRoomInputs) -> bytes:
    original = json.loads((inputs.repo_root / "app" / "tsconfig.json").read_bytes())
    expected_include = list(original["include"])
    for item in (".next-atlas/types/**/*.ts", ".next-atlas/dev/types/**/*.ts"):
        if item not in expected_include:
            expected_include.append(item)
    original["include"] = expected_include
    return (json.dumps(original, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _verify_generated_tree(project: Path) -> None:
    generated = project / ".next-atlas"
    if generated.is_symlink() or not generated.is_dir():
        raise IntegrityError("generated Next tree is absent or not a regular directory")
    canonical_generated = generated.resolve(strict=True)
    if canonical_generated != generated or not canonical_generated.is_relative_to(project):
        raise IntegrityError("generated Next tree escapes the ephemeral app")
    for path in generated.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise IntegrityError(f"generated Next tree contains a symlink: {path}")
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise IntegrityError(f"generated Next tree contains a special entry: {path}")
        if not path.resolve(strict=True).is_relative_to(canonical_generated):
            raise IntegrityError(f"generated Next entry escapes its tree: {path}")


def _verify_production_app_after(
    inputs: CleanRoomInputs, project: Path, original_modules_tree: bytes
) -> None:
    _verify_authoritative_source(inputs)
    if _dependency_tree_bytes(project / "node_modules") != original_modules_tree:
        raise IntegrityError("ephemeral node_modules changed during production build")
    if _dependency_tree_bytes(inputs.repo_root / "app" / "node_modules") != original_modules_tree:
        raise IntegrityError("approved node_modules changed during production build")
    expected = {str(row["path"])[4:]: row for row in _app_manifest_rows(inputs)}
    for relative, row in expected.items():
        path = project / relative
        if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.lstat().st_mode):
            raise IntegrityError(f"ephemeral app source is not regular: {relative}")
        if relative == "tsconfig.json":
            if path.read_bytes() != _expected_tsconfig_bytes(inputs):
                raise IntegrityError("ephemeral tsconfig delta is not the exact Next rewrite")
            continue
        body = path.read_bytes()
        if len(body) != row["bytes"] or _sha(body) != row["sha256"]:
            raise IntegrityError(f"ephemeral app source changed: {relative}")
    next_env = project / "next-env.d.ts"
    if (
        not next_env.is_file()
        or next_env.is_symlink()
        or not stat.S_ISREG(next_env.lstat().st_mode)
        or len(next_env.read_bytes()) != 253
        or _sha(next_env.read_bytes()) != NEXT_ENV_SHA256
    ):
        raise IntegrityError("ephemeral next-env.d.ts differs from the approved Next output")
    expected_files = set(expected) | {"next-env.d.ts"}
    expected_dirs = {
        parent.as_posix()
        for relative in expected_files
        for parent in Path(relative).parents
        if parent.as_posix() != "."
    }
    observed_files: set[str] = set()
    observed_dirs: set[str] = set()
    for path in project.rglob("*"):
        relative = path.relative_to(project)
        if relative.parts[0] in {"node_modules", ".next-atlas"}:
            continue
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            observed_dirs.add(relative.as_posix())
        elif stat.S_ISREG(mode):
            observed_files.add(relative.as_posix())
        else:
            raise IntegrityError(f"unexpected ephemeral app special entry: {relative}")
    if observed_files != expected_files or observed_dirs != expected_dirs:
        raise IntegrityError("ephemeral app inventory differs from the exact approved set")
    _verify_generated_tree(project)


def _base_environment(
    inputs: CleanRoomInputs, run_root: Path, reproducible_completed_at_utc: str
) -> dict[str, str]:
    cache = run_root / "cache"
    layout = _cache_layout(run_root)
    cache.mkdir(parents=True, exist_ok=False)
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=False)
        if tuple(path.iterdir()):
            raise IntegrityError("clean-room cache directory was not initialized empty")
    environment = dict(os.environ)
    environment.pop("ATLAS_RELEASE_MANIFEST_SHA256", None)
    environment.pop("JITI_FS_CACHE", None)
    # Override both canonical and commonly inherited aliases.  Otherwise a
    # parent shell can silently redirect one child back to a shared cache.
    environment.update(
        {
            "HOME": str(layout["home"]),
            "XDG_CACHE_HOME": str(cache),
            "npm_config_cache": str(layout["npm"]),
            "NPM_CONFIG_CACHE": str(layout["npm"]),
            "NPM_CONFIG_USERCONFIG": str(cache / "npmrc-does-not-exist"),
            "UV_CACHE_DIR": str(layout["uv"]),
            "PYTHONPYCACHEPREFIX": str(layout["python_bytecode"]),
            "PYTHONUSERBASE": str(layout["python_user_base"]),
            "PYTHONNOUSERSITE": "1",
            "ATLAS_VITE_CACHE_DIR": str(layout["vite"]),
            "ATLAS_RELEASE_DATA_DIR": str(run_root / "output" / "app"),
            "ATLAS_NEXT_DIST_DIR": str((layout["next"] / "project" / "app" / ".next-atlas").resolve()),
            "ATLAS_TYPESCRIPT_BUILDINFO": str(
                layout["typescript"] / "typecheck.tsbuildinfo"
            ),
            "NODE_COMPILE_CACHE": str(layout["node_compile"]),
            "COREPACK_HOME": str(layout["corepack"]),
            "TMPDIR": str(layout["tmp"]),
            "TMP": str(layout["tmp"]),
            "TEMP": str(layout["tmp"]),
            "ATLAS_CLEAN_ROOM_NETWORK_RECEIPT_SHA256": _sha(
                inputs.network_denied_receipt_bytes
            ),
            "ATLAS_PHASE2F_COMPLETED_AT_UTC": reproducible_completed_at_utc,
            "NEXT_TELEMETRY_DISABLED": "1",
            "NO_UPDATE_NOTIFIER": "1",
        }
    )
    environment["PATH"] = (
        str(inputs.node_executable.parent)
        + os.pathsep
        + environment.get("PATH", "")
    )
    return environment


def _persist_rejection(
    root: Path,
    inputs: CleanRoomInputs,
    *,
    run_id: str,
    step: str,
    reason: str,
) -> Path:
    body = canonical_json_bytes(
        {
            "schema_version": "atlas-v19-clean-room-rejection-receipt-1",
            "status": "REJECTED",
            "source_tree_id": inputs.context.source_tree_id,
            "input_manifest_sha256": inputs.input_manifest_sha256,
            "environment_receipt_sha256": _sha(inputs.environment_receipt_bytes),
            "network_denied_receipt_sha256": _sha(inputs.network_denied_receipt_bytes),
            "run_id": run_id,
            "failed_step": step,
            "reason_sha256": _sha(reason.encode("utf-8", errors="replace")),
            "recorded_at_utc": _now(),
        }
    )
    path = root / f"rejection-{run_id.lower()}.json"
    atomic_write(path, body)
    return path


def _run_once(
    inputs: CleanRoomInputs,
    run_root: Path,
    run_id: str,
    reproducible_completed_at_utc: str,
) -> tuple[Path, bytes]:
    run_root.mkdir(parents=True, exist_ok=False)
    if tuple(run_root.iterdir()):
        raise IntegrityError("clean-room run root was not empty")
    environment = _base_environment(
        inputs, run_root, reproducible_completed_at_utc
    )
    process_rows = []
    approved_release_manifest_sha256: str | None = None
    production_project: Path | None = None
    original_modules_tree: bytes | None = None
    for command in frozen_commands(inputs, run_root):
        if command.label in {"python_tests", "app_tests", "app_typecheck"}:
            if "ATLAS_RELEASE_MANIFEST_SHA256" in environment:
                raise IntegrityError(
                    "clean-room tests/typecheck inherited the production release hash"
                )
        if command.label == "production_app_build":
            if approved_release_manifest_sha256 is None:
                raise IntegrityError(
                    "clean-room production build lacks the approved release hash"
                )
            environment["ATLAS_RELEASE_MANIFEST_SHA256"] = (
                approved_release_manifest_sha256
            )
            environment["JITI_FS_CACHE"] = "false"
            production_project, original_modules_tree = _materialize_production_app(
                inputs, run_root
            )
            if command.cwd.resolve() != production_project:
                raise IntegrityError("production cwd differs from the ephemeral app")
            cache_root = (run_root / "cache").resolve(strict=True)
            dist = Path(environment["ATLAS_NEXT_DIST_DIR"])
            expected_dist = production_project / ".next-atlas"
            if (
                not dist.is_absolute()
                or dist.resolve() != expected_dist
                or not dist.resolve().is_relative_to(cache_root)
            ):
                raise IntegrityError("production dist path escapes the run cache")
        elif "JITI_FS_CACHE" in environment:
            raise IntegrityError("JITI_FS_CACHE leaked before production build")
        started = _now()
        try:
            completed = subprocess.run(
                command.argv,
                cwd=command.cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        finally:
            if command.label == "production_app_build":
                _verify_authoritative_source(inputs)
                if production_project is None or original_modules_tree is None:
                    raise IntegrityError("production dependency evidence is absent")
                if (
                    _dependency_tree_bytes(production_project / "node_modules")
                    != original_modules_tree
                ):
                    raise IntegrityError(
                        "ephemeral node_modules changed during failed production build"
                    )
                if (
                    _dependency_tree_bytes(inputs.repo_root / "app" / "node_modules")
                    != original_modules_tree
                ):
                    raise IntegrityError("approved node_modules changed during production build")
        ended = _now()
        row = {
            "label": command.label,
            "argv": list(command.argv),
            "cwd": str(command.cwd),
            "started_at_utc": started,
            "ended_at_utc": ended,
            "exit_code": completed.returncode,
            "stdout_sha256": _sha(completed.stdout),
            "stderr_sha256": _sha(completed.stderr),
            "release_manifest_sha256": environment.get(
                "ATLAS_RELEASE_MANIFEST_SHA256"
            ),
        }
        process_rows.append(row)
        if completed.returncode != 0:
            _persist_rejection(
                run_root.parent,
                inputs,
                run_id=run_id,
                step=command.label,
                reason=completed.stderr.decode("utf-8", errors="replace"),
            )
            raise IntegrityError(f"clean-room subprocess failed: {command.label}")
        if command.label == "phase2f_build":
            release_manifest = run_root / "output" / "app" / "release-manifest.json"
            if not release_manifest.is_file() or release_manifest.is_symlink():
                _persist_rejection(
                    run_root.parent,
                    inputs,
                    run_id=run_id,
                    step="phase2f-release-manifest",
                    reason="approved release manifest is absent after Phase-2F build",
                )
                raise IntegrityError("Phase-2F did not emit a regular release manifest")
            release_manifest_sha256 = _sha(release_manifest.read_bytes())
            approved_release_manifest_sha256 = release_manifest_sha256
            row["release_manifest_sha256"] = release_manifest_sha256
        if command.label == "production_app_build" and (
            row["release_manifest_sha256"] != approved_release_manifest_sha256
        ):
            raise IntegrityError(
                "clean-room production process row did not bind the release hash"
            )
        if command.label == "production_app_build":
            if production_project is None or original_modules_tree is None:
                raise IntegrityError("production app materialization evidence is absent")
            _verify_production_app_after(
                inputs, production_project, original_modules_tree
            )
    manifest = _output_manifest(run_root / "output")
    manifest_path = run_root / "output-manifest.json"
    atomic_write(manifest_path, manifest)
    receipt = canonical_json_bytes(
        {
            "schema_version": "atlas-v19-clean-room-run-receipt-1",
            "status": "COMPLETE",
            "run_id": run_id,
            "source_tree_id": inputs.context.source_tree_id,
            "input_manifest_sha256": inputs.input_manifest_sha256,
            "environment_receipt_sha256": _sha(inputs.environment_receipt_bytes),
            "network_denied_receipt_sha256": _sha(inputs.network_denied_receipt_bytes),
            "reproducible_completed_at_utc": reproducible_completed_at_utc,
            "empty_output_and_cache_roots": True,
            "isolated_cache_paths": {
                name: path.relative_to(run_root).as_posix()
                for name, path in sorted(_cache_layout(run_root).items())
            },
            "processes": process_rows,
            "output_manifest_sha256": _sha(manifest),
        }
    )
    receipt_path = run_root / "run-receipt.json"
    atomic_write(receipt_path, receipt)
    return receipt_path, manifest


def orchestrate_clean_rooms(inputs: CleanRoomInputs, root: Path) -> CleanRoomResult:
    inputs.validate()
    if root.exists():
        raise IntegrityError("clean-room orchestration root must not preexist")
    root.mkdir(parents=True)
    reproducible_completed_at_utc = _now()
    try:
        run_a_path, manifest_a = _run_once(
            inputs, root / "run-a", "A", reproducible_completed_at_utc
        )
        run_b_path, manifest_b = _run_once(
            inputs, root / "run-b", "B", reproducible_completed_at_utc
        )
        if manifest_a != manifest_b:
            _persist_rejection(
                root,
                inputs,
                run_id="COMPARISON",
                step="compare-output-manifests",
                reason=f"{_sha(manifest_a)} != {_sha(manifest_b)}",
            )
            raise IntegrityError("independent clean-room outputs differ")
        comparison = canonical_json_bytes(
            {
                "schema_version": "atlas-v19-phase2f-comparison-receipt-1",
                "receipt_id": "phase2f_comparison",
                "status": "equal",
                "source_tree_id": inputs.context.source_tree_id,
                "bundle_manifest_sha256": inputs.context.bundle_manifest_sha256,
                "authorization_id": inputs.context.authorization_id,
                "command": "compare-clean-room",
                "completed_at_utc": reproducible_completed_at_utc,
                "evidence": {
                    "run_a_receipt_sha256": _sha(run_a_path.read_bytes()),
                    "run_b_receipt_sha256": _sha(run_b_path.read_bytes()),
                    "compared_artifacts_sha256": _sha(manifest_a),
                    "artifact_count": len(strict_json_loads(manifest_a)["files"]),
                    "equal": True,
                },
            }
        )
        comparison_path = root / "comparison-receipt.json"
        atomic_write(comparison_path, comparison)
        receipt = canonical_json_bytes(
            {
                "schema_version": "atlas-v19-phase2f-clean-room-receipt-1",
                "receipt_id": "phase2f_clean_room",
                "status": "complete",
                "source_tree_id": inputs.context.source_tree_id,
                "bundle_manifest_sha256": inputs.context.bundle_manifest_sha256,
                "authorization_id": inputs.context.authorization_id,
                "command": "run-clean-room",
                "completed_at_utc": reproducible_completed_at_utc,
                "evidence": {
                    "input_manifest_sha256": inputs.input_manifest_sha256,
                    "environment_receipt_sha256": _sha(inputs.environment_receipt_bytes),
                    "network_denied_receipt_sha256": _sha(inputs.network_denied_receipt_bytes),
                    "run_a_receipt_sha256": _sha(run_a_path.read_bytes()),
                    "run_b_receipt_sha256": _sha(run_b_path.read_bytes()),
                    "comparison_receipt_sha256": _sha(comparison),
                    "process_count": 2,
                    "empty_root_count": 2,
                },
            }
        )
        receipt_path = root / "clean-room-receipt.json"
        atomic_write(receipt_path, receipt)
        return CleanRoomResult(
            receipt_path,
            comparison_path,
            (run_a_path, run_b_path),
            _sha(manifest_a),
        )
    except Exception:
        if not list(root.glob("rejection-*.json")):
            _persist_rejection(
                root,
                inputs,
                run_id="ORCHESTRATOR",
                step="orchestrate-clean-rooms",
                reason="unclassified clean-room failure",
            )
        raise
