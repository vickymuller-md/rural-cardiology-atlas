"""Strict canonical JSON and path primitives for frozen Atlas controls.

These helpers deliberately reject the permissive corners of Python's JSON
decoder (duplicate keys and non-finite numbers) and the control sentinels that
must never enter an authorized release bundle.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Mapping

from .errors import IntegrityError

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SENTINEL = re.compile(r"(?:PENDING|PLACEHOLDER)", re.IGNORECASE)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise IntegrityError(f"non-finite JSON number is forbidden: {value}")


def strict_json_loads(body: bytes, *, label: str = "JSON") -> object:
    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except IntegrityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is not strict UTF-8 JSON") from exc


def _canonical_json_bytes(value: object, *, normalize_strings: bool) -> bytes:
    def normalize(item: object) -> object:
        if isinstance(item, float) and not math.isfinite(item):
            raise IntegrityError("non-finite values are forbidden in canonical JSON")
        if isinstance(item, str):
            return unicodedata.normalize("NFC", item) if normalize_strings else item
        if isinstance(item, tuple):
            return [normalize(child) for child in item]
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, Mapping):
            if not all(isinstance(key, str) for key in item):
                raise IntegrityError("canonical JSON object keys must be strings")
            normalized: dict[str, object] = {}
            for key, child in item.items():
                normalized_key = unicodedata.normalize("NFC", key) if normalize_strings else key
                if normalized_key in normalized:
                    raise IntegrityError("JSON keys collide after canonical normalization")
                normalized[normalized_key] = normalize(child)
            return normalized
        return item

    try:
        text = json.dumps(
            normalize(value),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise IntegrityError("value cannot be encoded as canonical JSON") from exc
    return (text + "\n").encode("utf-8")


def canonical_json_bytes(value: object) -> bytes:
    """Canonical control JSON: strings are normalized to NFC."""

    return _canonical_json_bytes(value, normalize_strings=True)


def canonical_data_json_bytes(value: object) -> bytes:
    """Canonical data-row JSON preserving every source string code point."""

    return _canonical_json_bytes(value, normalize_strings=False)


def require_canonical_json(body: bytes, *, label: str = "JSON") -> object:
    value = strict_json_loads(body, label=label)
    if body != canonical_json_bytes(value):
        raise IntegrityError(f"{label} is not canonical UTF-8/LF JSON")
    return value


def reject_control_sentinels(value: object, *, label: str = "control") -> None:
    """Recursively reject unresolved values in an authorization control."""

    if isinstance(value, str) and _SENTINEL.search(value):
        raise IntegrityError(f"{label} contains an unresolved control sentinel")
    if isinstance(value, list):
        for index, child in enumerate(value):
            reject_control_sentinels(child, label=f"{label}[{index}]")
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if _SENTINEL.search(key):
                raise IntegrityError(f"{label} contains an unresolved control-sentinel key")
            reject_control_sentinels(child, label=f"{label}.{key}")


def validate_safe_id(value: object, *, label: str = "ID") -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or _SAFE_ID.fullmatch(value) is None
    ):
        raise IntegrityError(f"{label} is unsafe")
    return value


def validate_safe_relative_posix(value: object, *, label: str = "path") -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise IntegrityError(f"{label} is not a safe relative POSIX path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or value != relative.as_posix()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise IntegrityError(f"{label} is not a safe relative POSIX path")
    return relative


def resolve_regular_contained(
    root: Path,
    relative: PurePosixPath,
    *,
    label: str = "artifact",
) -> Path:
    """Resolve a regular file while rejecting symlinks in every parent."""

    root = root.resolve(strict=True)
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError as exc:
            raise IntegrityError(f"{label} does not exist") from exc
        if candidate.is_symlink():
            raise IntegrityError(f"{label} has a symlink path component")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise IntegrityError(f"{label} escapes its root") from exc
    if not resolved.is_file():
        raise IntegrityError(f"{label} is not a regular file")
    return resolved
