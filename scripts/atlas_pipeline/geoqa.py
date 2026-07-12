"""Pinned adjacency and full-resolution TIGER manual-QA contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, Mapping

from .errors import ContractError, IntegrityError


def sorted_fips_bytes(values: Iterable[str]) -> bytes:
    return ("\n".join(sorted(values)) + "\n").encode("ascii")


def derive_state_border_counties(
    adjacency: Mapping[str, Iterable[str]],
    county_universe: set[str],
    *,
    expected_count: int,
    expected_sha256: str,
) -> frozenset[str]:
    if set(adjacency) != county_universe:
        raise IntegrityError("adjacency primary-FIPS set does not equal county universe")
    borders = {
        primary
        for primary, neighbors in adjacency.items()
        if any(neighbor in county_universe and neighbor[:2] != primary[:2] for neighbor in neighbors)
    }
    payload = sorted_fips_bytes(borders)
    if len(borders) != expected_count:
        raise IntegrityError("state-border county count mismatch")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise IntegrityError("state-border FIPS-list hash mismatch")
    return frozenset(borders)


@dataclass(frozen=True)
class AdjacencyReceipt:
    source_id: str
    source_sha256: str
    county_universe_sha256: str
    primary_fips_count: int
    state_border_count: int
    state_border_fips_sha256: str
    canonical_bytes: bytes
    sha256: str


def build_adjacency_receipt(
    adjacency: Mapping[str, Iterable[str]],
    county_universe: set[str],
    *,
    source_id: str,
    source_sha256: str,
    expected_source_sha256: str,
    expected_border_count: int,
    expected_border_sha256: str,
) -> AdjacencyReceipt:
    if source_id != "CENSUS-COUNTY-ADJ-2025":
        raise ContractError("adjacency receipt uses an unapproved source ID")
    if source_sha256 != expected_source_sha256:
        raise IntegrityError("adjacency source SHA-256 differs from approved bundle pin")
    borders = derive_state_border_counties(
        adjacency,
        county_universe,
        expected_count=expected_border_count,
        expected_sha256=expected_border_sha256,
    )
    universe_hash = hashlib.sha256(sorted_fips_bytes(county_universe)).hexdigest()
    payload = {
        "source_id": source_id,
        "source_sha256": source_sha256,
        "county_universe_sha256": universe_hash,
        "primary_fips_count": len(county_universe),
        "state_border_count": len(borders),
        "state_border_fips_sha256": expected_border_sha256,
    }
    canonical = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    return AdjacencyReceipt(
        **payload,
        canonical_bytes=canonical,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


@dataclass(frozen=True)
class PolygonSource:
    source_id: str
    scale: str
    county_fips: frozenset[str]
    sha256: str

    def validate_for_point_qa(self, county_universe: set[str]) -> None:
        if self.source_id != "CENSUS-TIGER-COUNTY-2025" or self.scale != "FULL_RESOLUTION":
            raise ContractError("point QA requires pinned full-resolution TIGER; display geometry is forbidden")
        if set(self.county_fips) != county_universe:
            raise IntegrityError("TIGER polygon FIPS do not equal county universe")
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise IntegrityError("TIGER source identity is not a lowercase SHA-256")


@dataclass(frozen=True)
class GeometryEngineIdentity:
    package: str
    version: str
    package_sha256: str
    algorithm: str

    def validate(self) -> None:
        if not self.package or not self.version or not self.algorithm:
            raise IntegrityError("geometry engine identity contains a blank field")
        if any(value.upper().startswith("PENDING") for value in (self.package, self.version, self.algorithm)):
            raise IntegrityError("geometry engine identity is not frozen")
        if not re.fullmatch(r"[0-9a-f]{64}", self.package_sha256):
            raise IntegrityError("geometry engine package hash is not lowercase SHA-256")


@dataclass(frozen=True)
class BoundPointLocator:
    engine: GeometryEngineIdentity
    locate: Callable[[float, float, str], str] = field(compare=False, repr=False)


@dataclass(frozen=True)
class PointQaSetup:
    polygon_source: PolygonSource
    geometry_engine: GeometryEngineIdentity
    county_universe_sha256: str
    canonical_bytes: bytes
    sha256: str


def build_point_qa_setup(
    source: PolygonSource,
    engine: GeometryEngineIdentity,
    county_universe: set[str],
) -> PointQaSetup:
    source.validate_for_point_qa(county_universe)
    engine.validate()
    universe_hash = hashlib.sha256(sorted_fips_bytes(county_universe)).hexdigest()
    payload = {
        "polygon_source": asdict(source) | {"county_fips": sorted(source.county_fips)},
        "geometry_engine": asdict(engine),
        "county_universe_sha256": universe_hash,
    }
    canonical = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    return PointQaSetup(
        source,
        engine,
        universe_hash,
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )


def point_county_qa_code(
    longitude: float,
    latitude: float,
    assigned_fips: str,
    county_universe: set[str],
    source: PolygonSource,
    locator: BoundPointLocator,
    expected_engine: GeometryEngineIdentity,
) -> str:
    source.validate_for_point_qa(county_universe)
    expected_engine.validate()
    if locator.engine != expected_engine:
        raise IntegrityError("point locator engine identity differs from frozen QA setup")
    if assigned_fips not in county_universe:
        raise IntegrityError("assigned FIPS outside QA universe")
    relation = locator.locate(longitude, latitude, assigned_fips)
    mapping = {
        "inside": "POINT_INSIDE_ASSIGNED_COUNTY",
        "boundary": "POINT_ON_ASSIGNED_COUNTY_BOUNDARY",
        "outside": "POINT_OUTSIDE_ASSIGNED_COUNTY",
    }
    if relation not in mapping:
        raise ContractError(f"unknown point/polygon relation: {relation}")
    return mapping[relation]
