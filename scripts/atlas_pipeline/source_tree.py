"""Canonical source-tree freeze generator and runtime verifier."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import marshal
import os
import stat
import struct
import subprocess
import sys
import argparse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import CodeType, ModuleType
from typing import Iterable, Mapping

from .errors import IntegrityError
from .strict_json import canonical_json_bytes, strict_json_loads

_ROOT_FILES = frozenset({".node-version", ".python-version"})
_TREE_SUFFIXES: Mapping[str, frozenset[str]] = {
    "scripts": frozenset({".py", ".toml", ".lock", ".csv"}),
    "app": frozenset(
        {".ts", ".tsx", ".mts", ".mjs", ".js", ".json", ".css", ".svg", ".d.ts", ".map", ".md"}
    ),
    "environment": frozenset({".mjs", ".json"}),
    "tooling/topology": frozenset({".mjs", ".json", ".txt"}),
}
_NOMINAL_SOURCE_PATHS = frozenset(
    {
        "app/vendor/heartland-ui.tree.jsonl",
        "app/vendor/heartland-ui/README.md",
    }
)
_APPROVED_ATLAS_IMPORT_SUFFIXES = frozenset({".py"})
_PYTHON_CACHE_RULE = "current-magic-active-cache-must-compile-equal-manifested-source"
_CODE_OR_EXECUTABLE_SUFFIXES = frozenset(
    {
        ".bash",
        ".bin",
        ".cjs",
        ".cts",
        ".dll",
        ".dylib",
        ".exe",
        ".fish",
        ".js",
        ".jsx",
        ".mjs",
        ".mts",
        ".node",
        ".pyd",
        ".py",
        ".pyc",
        ".pyo",
        ".sh",
        ".so",
        ".ts",
        ".tsx",
        ".wasm",
        ".zsh",
    }
)
_SKIP_PARTS = frozenset(
    {
        ".git",
        ".next",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "coverage",
        "graphify-out",
    }
)
_FORBIDDEN_PREFIXES = (
    "app/public/",
    "scripts/data/",
    "scripts/raw/",
    "scripts/interim/",
    "scripts/processed/",
)
_FORBIDDEN_NAMES = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "NIW_INTEGRATION.md",
        "CITATION.cff",
        ".zenodo.json",
        "tsconfig.tsbuildinfo",
        "next-env.d.ts",
    }
)
_SPECIAL_SOURCE_NAMES = frozenset({".node-version", ".python-version"})


def source_tree_policy_bytes() -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": "atlas-v19-source-tree-policy-1",
            "root_files": sorted(_ROOT_FILES),
            "trees": {
                root: sorted(suffixes) for root, suffixes in sorted(_TREE_SUFFIXES.items())
            },
            "nominal_source_paths": sorted(_NOMINAL_SOURCE_PATHS),
            "python_cache_rule": _PYTHON_CACHE_RULE,
            "special_source_names": sorted(_SPECIAL_SOURCE_NAMES),
            "skipped_parts": sorted(_SKIP_PARTS),
            "forbidden_prefixes": list(_FORBIDDEN_PREFIXES),
            "forbidden_names": sorted(_FORBIDDEN_NAMES),
        }
    )


SOURCE_TREE_POLICY_SHA256 = hashlib.sha256(source_tree_policy_bytes()).hexdigest()


@dataclass(frozen=True)
class SourceTreeIdentity:
    root: Path
    manifest_bytes: bytes
    source_tree_id: str
    paths: tuple[str, ...]


def _suffix_allowed(path: Path, relative: str, suffixes: frozenset[str]) -> bool:
    name = path.name
    return (
        relative in _NOMINAL_SOURCE_PATHS
        or name in _SPECIAL_SOURCE_NAMES
        or any(name.endswith(suffix) for suffix in suffixes)
    )


def _is_unallowlisted_code_or_executable(path: Path, mode: int) -> bool:
    lower_name = path.name.lower()
    return bool(stat.S_IMODE(mode) & 0o111) or any(
        lower_name.endswith(suffix) for suffix in _CODE_OR_EXECUTABLE_SUFFIXES
    )


def _validate_python_cache_derivative(cache_path: Path, source_path: Path) -> None:
    """Allow only an active current-runtime cache compiled from the exact source."""

    cache_mode = cache_path.lstat().st_mode
    if stat.S_ISLNK(cache_mode) or not stat.S_ISREG(cache_mode):
        raise IntegrityError(f"Python cache must be a regular non-symlink file: {cache_path}")
    body = cache_path.read_bytes()
    if len(body) < 4 or body[:4] != importlib.util.MAGIC_NUMBER:
        # A foreign-runtime cache cannot be loaded by this pinned interpreter.
        return
    if len(body) < 16:
        raise IntegrityError(f"current-runtime Python cache is truncated: {cache_path}")
    flags = struct.unpack_from("<I", body, 4)[0]
    if flags & ~0b11:
        raise IntegrityError(f"current-runtime Python cache has invalid flags: {cache_path}")
    if source_path.is_symlink() or not source_path.is_file():
        raise IntegrityError(f"current-runtime Python cache lacks exact source: {cache_path}")
    source_mode = source_path.lstat().st_mode
    if not stat.S_ISREG(source_mode) or source_path.suffix != ".py":
        raise IntegrityError(f"current-runtime Python cache source is invalid: {cache_path}")

    active = bool(flags & 0b1)
    if not active:
        cached_mtime, cached_size = struct.unpack_from("<II", body, 8)
        source_stat = source_path.stat()
        active = (
            cached_mtime == int(source_stat.st_mtime) & 0xFFFFFFFF
            and cached_size == source_stat.st_size & 0xFFFFFFFF
        )
    if not active:
        return

    stream = io.BytesIO(body[16:])
    try:
        cached_code = marshal.load(stream)
    except (EOFError, TypeError, ValueError) as exc:
        raise IntegrityError(f"active Python cache payload is invalid: {cache_path}") from exc
    if stream.read(1):
        raise IntegrityError(f"active Python cache has trailing payload bytes: {cache_path}")
    try:
        source_code = compile(
            source_path.read_bytes(),
            str(source_path),
            "exec",
            dont_inherit=True,
            optimize=sys.flags.optimize,
        )
    except (OSError, SyntaxError, ValueError) as exc:
        raise IntegrityError(f"manifested Python source cannot be compiled: {source_path}") from exc
    if not isinstance(cached_code, CodeType) or cached_code != source_code:
        raise IntegrityError(
            f"active Python cache differs from manifested source: {cache_path}"
        )


def _validate_python_cache_directory(cache_directory: Path) -> None:
    mode = cache_directory.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise IntegrityError(
            f"Python cache directory must be a regular non-symlink directory: {cache_directory}"
        )
    for cache_path in sorted(cache_directory.iterdir(), key=lambda path: path.name):
        cache_mode = cache_path.lstat().st_mode
        if stat.S_ISLNK(cache_mode) or not stat.S_ISREG(cache_mode):
            raise IntegrityError(f"special entry is forbidden in Python cache: {cache_path}")
        if cache_path.suffix != ".pyc":
            if _is_unallowlisted_code_or_executable(cache_path, cache_mode):
                raise IntegrityError(
                    f"unallowlisted executable/code file in Python cache: {cache_path}"
                )
            continue
        body = cache_path.read_bytes()
        if len(body) < 4 or body[:4] != importlib.util.MAGIC_NUMBER:
            continue
        cache_tag = sys.implementation.cache_tag
        if not isinstance(cache_tag, str) or not cache_tag:
            raise IntegrityError("active Python runtime has no cache tag")
        optimization = "" if sys.flags.optimize == 0 else f".opt-{sys.flags.optimize}"
        active_suffix = f".{cache_tag}{optimization}.pyc"
        if not cache_path.name.endswith(active_suffix):
            # Pytest-tagged and other non-importable cache variants are inert.
            continue
        source_name = cache_path.name[: -len(active_suffix)] + ".py"
        source_path = cache_directory.parent / source_name
        _validate_python_cache_derivative(cache_path, source_path)


def _iter_tree_files(root: Path, relative_root: str, suffixes: frozenset[str]):
    tree = root.joinpath(*PurePosixPath(relative_root).parts)
    if not tree.is_dir() or tree.is_symlink():
        raise IntegrityError(f"required source directory is absent or a symlink: {relative_root}")
    for current, dirs, files in os.walk(tree, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for name in dirs:
            candidate = current_path / name
            relative_directory = candidate.relative_to(root).as_posix() + "/"
            if name == "__pycache__":
                _validate_python_cache_directory(candidate)
                continue
            if name in _SKIP_PARTS or any(
                relative_directory.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES
            ):
                continue
            if candidate.is_symlink():
                raise IntegrityError(f"symlink is forbidden in source scope: {candidate}")
            if not stat.S_ISDIR(candidate.lstat().st_mode):
                raise IntegrityError(f"special directory entry in source scope: {candidate}")
            kept_dirs.append(name)
        dirs[:] = sorted(kept_dirs)
        for name in sorted(files):
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            if any(relative.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES):
                continue
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise IntegrityError(f"source entry must be a regular non-symlink file: {relative}")
            if name in _FORBIDDEN_NAMES and relative not in _NOMINAL_SOURCE_PATHS:
                continue
            if not _suffix_allowed(candidate, relative, suffixes):
                if _is_unallowlisted_code_or_executable(candidate, mode):
                    raise IntegrityError(
                        f"unallowlisted executable/code file in managed source tree: {relative}"
                    )
                continue
            yield candidate


def discover_source_files(root: Path) -> tuple[Path, ...]:
    root = root.resolve(strict=True)
    paths: list[Path] = []
    for name in sorted(_ROOT_FILES):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise IntegrityError(f"required root source file is absent or a symlink: {name}")
        paths.append(path)
    for relative_root, suffixes in _TREE_SUFFIXES.items():
        paths.extend(_iter_tree_files(root, relative_root, suffixes))
    ordered = sorted(paths, key=lambda path: path.relative_to(root).as_posix())
    relatives = [path.relative_to(root).as_posix() for path in ordered]
    if len(relatives) != len(set(relatives)) or not relatives:
        raise IntegrityError("source-tree discovery is empty or duplicated")
    missing_nominal = sorted(_NOMINAL_SOURCE_PATHS.difference(relatives))
    if missing_nominal:
        raise IntegrityError(f"required nominal source files are absent: {missing_nominal}")
    if (root / ".git").exists():
        checked = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin"],
            input=("\n".join(relatives) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if checked.returncode not in {0, 1}:
            raise IntegrityError("git ignored-file verification failed")
        ignored = [line for line in checked.stdout.decode("utf-8").splitlines() if line]
        if ignored:
            raise IntegrityError(f"ignored files are forbidden in source manifest: {ignored}")
    return tuple(ordered)


def generate_source_tree_manifest(root: Path) -> SourceTreeIdentity:
    root = root.resolve(strict=True)
    files = discover_source_files(root)
    rows = []
    for path in files:
        body = path.read_bytes()
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    # Section 18.2 freezes the manifest itself as canonical JSONL: exactly one
    # fixed-key row per source file and no envelope/schema row.  The exact
    # concatenated bytes, including every terminal LF, are the source-tree ID.
    body = b"".join(canonical_json_bytes(row) for row in rows)
    return SourceTreeIdentity(
        root,
        body,
        hashlib.sha256(body).hexdigest(),
        tuple(row["path"] for row in rows),
    )


def parse_source_tree_manifest(manifest_bytes: bytes) -> tuple[dict[str, object], ...]:
    """Parse the preregistered canonical UTF-8/LF source-tree JSONL format."""

    if not manifest_bytes or not manifest_bytes.endswith(b"\n"):
        raise IntegrityError("source-tree manifest must be non-empty JSONL ending in LF")
    if b"\r" in manifest_bytes:
        raise IntegrityError("source-tree manifest must use LF, not CRLF")
    raw_lines = manifest_bytes[:-1].split(b"\n")
    if not raw_lines or any(not line for line in raw_lines):
        raise IntegrityError("source-tree manifest must contain one non-empty JSON row per line")

    rows: list[dict[str, object]] = []
    prior_path: str | None = None
    for index, raw_line in enumerate(raw_lines, start=1):
        line = raw_line + b"\n"
        value = strict_json_loads(line, label=f"source-tree manifest row {index}")
        if not isinstance(value, dict) or tuple(value) != ("path", "bytes", "sha256"):
            raise IntegrityError(f"source-tree manifest row {index} keys/order mismatch")
        if line != canonical_json_bytes(value):
            raise IntegrityError(
                f"source-tree manifest row {index} is not canonical UTF-8/LF JSON"
            )

        relative_value = value["path"]
        byte_count = value["bytes"]
        sha256_value = value["sha256"]
        if not isinstance(relative_value, str) or not relative_value or "\\" in relative_value:
            raise IntegrityError(f"source-tree manifest row {index} path is invalid")
        relative = PurePosixPath(relative_value)
        if (
            relative.is_absolute()
            or relative.as_posix() != relative_value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise IntegrityError(f"source-tree manifest row {index} path is invalid")
        if type(byte_count) is not int or byte_count < 0:
            raise IntegrityError(f"source-tree manifest row {index} byte count is invalid")
        if (
            not isinstance(sha256_value, str)
            or len(sha256_value) != 64
            or any(character not in "0123456789abcdef" for character in sha256_value)
        ):
            raise IntegrityError(f"source-tree manifest row {index} SHA-256 is invalid")
        if prior_path is not None and prior_path >= relative_value:
            raise IntegrityError(
                "source-tree manifest paths must be unique and strictly ascending"
            )
        prior_path = relative_value
        rows.append(value)
    return tuple(rows)


def verify_source_tree_manifest(root: Path, manifest_bytes: bytes) -> SourceTreeIdentity:
    parse_source_tree_manifest(manifest_bytes)
    generated = generate_source_tree_manifest(root)
    if manifest_bytes != generated.manifest_bytes:
        raise IntegrityError("source-tree files/bytes/hash differ from the frozen manifest")
    return generated


def verify_imported_module_containment(
    root: Path,
    *,
    modules: Iterable[tuple[str, ModuleType]] | None = None,
) -> tuple[str, ...]:
    """Reject Atlas modules imported from outside the recomputed frozen root."""

    root = root.resolve(strict=True)
    identity = generate_source_tree_manifest(root)
    manifested_paths = frozenset(identity.paths)
    package_root = (root / "scripts" / "atlas_pipeline").resolve(strict=True)
    candidates = modules or (
        (name, module)
        for name, module in sys.modules.items()
        if name == "atlas_pipeline" or name.startswith("atlas_pipeline.")
    )
    verified: list[str] = []
    for name, module in candidates:
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise IntegrityError(f"imported Atlas module lacks a file: {name}")
        lexical = Path(module_file).absolute()
        if lexical.is_symlink() or not lexical.is_file():
            raise IntegrityError(f"imported Atlas module is not a regular file: {name}")
        resolved = lexical.resolve(strict=True)
        try:
            relative_to_package = resolved.relative_to(package_root)
        except ValueError as exc:
            raise IntegrityError(f"imported Atlas module escapes frozen root: {name}") from exc
        relative = (PurePosixPath("scripts/atlas_pipeline") / relative_to_package).as_posix()
        if (
            PurePosixPath(relative).suffix not in _APPROVED_ATLAS_IMPORT_SUFFIXES
            or relative not in manifested_paths
        ):
            raise IntegrityError(
                f"imported Atlas module is not an approved exact manifested source path: {name}"
            )
        current = package_root
        for part in relative_to_package.parts:
            current = current / part
            if current.is_symlink():
                raise IntegrityError(f"imported Atlas module has a symlink parent: {name}")
        cached_file = getattr(module, "__cached__", None)
        if cached_file is not None:
            if not isinstance(cached_file, str):
                raise IntegrityError(f"imported Atlas module cache path is invalid: {name}")
            cached_path = Path(cached_file).absolute()
            if cached_path.exists() or cached_path.is_symlink():
                _validate_python_cache_derivative(cached_path, resolved)
        verified.append(name)
    if not verified:
        raise IntegrityError("no imported atlas_pipeline module was verified")
    return tuple(sorted(verified))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Atlas canonical source-tree freeze")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--repo-root", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--repo-root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "generate":
        identity = generate_source_tree_manifest(args.repo_root)
        try:
            args.output.absolute().relative_to(args.repo_root.absolute())
        except ValueError:
            pass
        else:
            raise IntegrityError("source-tree manifest control must be written outside the source root")
        if args.output.exists():
            raise IntegrityError("source-tree manifest output already exists")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".part")
        with temporary.open("xb") as handle:
            handle.write(identity.manifest_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.output)
    else:
        if not args.manifest.is_file() or args.manifest.is_symlink():
            raise IntegrityError("source-tree manifest must be a regular non-symlink file")
        identity = verify_source_tree_manifest(args.repo_root, args.manifest.read_bytes())
    print(identity.source_tree_id)


if __name__ == "__main__":
    main()
