"""Fail-closed parsers for Census, RUCC, and HRSA frozen snapshots."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping

from .addresses import normalize_text
from .canonical import quantize_coordinate
from .contracts import STATE_FIPS
from .errors import ContractError, IntegrityError

_INTEGER = re.compile(r"^-?[0-9]+$")
_FIPS = re.compile(r"^[0-9]{5}$", re.ASCII)
_COUNTY_GEO_ID = re.compile(r"^0500000US([0-9]{5})$", re.ASCII)
_IN_SCOPE_PREFIXES = frozenset(STATE_FIPS.values())


def _integer(value: object, field: str) -> int:
    if not isinstance(value, str) or not _INTEGER.fullmatch(value.strip(" \t\r\n\f\v")):
        raise ContractError(f"{field} is not an exact integer lexeme: {value!r}")
    return int(value)


def parse_gazetteer(
    rows: Iterable[Mapping[str, object]], *, expected_count: int = 3_144
) -> dict[str, tuple[str, str]]:
    required = {"USPS", "GEOID", "NAME", "INTPTLAT", "INTPTLONG"}
    result: dict[str, tuple[str, str]] = {}
    for row in rows:
        missing = required - set(row)
        if missing:
            raise ContractError(f"Gazetteer source missing columns: {sorted(missing)}")
        state = str(row["USPS"]).strip(" \t\r\n\f\v")
        fips = str(row["GEOID"]).strip(" \t\r\n\f\v")
        if not _FIPS.fullmatch(fips):
            raise ContractError(f"malformed Gazetteer FIPS: {fips!r}")
        if state not in STATE_FIPS:
            if fips[:2] in _IN_SCOPE_PREFIXES:
                raise ContractError(
                    f"Gazetteer has unknown USPS for an in-scope FIPS: {state}/{fips}"
                )
            # Valid territory/out-of-scope rows in the national file are
            # explicitly excluded before universe equality.
            continue
        if STATE_FIPS[state] != fips[:2]:
            raise ContractError(f"Gazetteer state/FIPS mismatch: {state}/{fips}")
        if fips in result:
            raise IntegrityError(f"duplicate Gazetteer FIPS: {fips}")
        result[fips] = (
            quantize_coordinate(row["INTPTLAT"], latitude=True),
            quantize_coordinate(row["INTPTLONG"], latitude=False),
        )
    if len(result) != expected_count:
        raise IntegrityError(f"Gazetteer universe count {len(result)} != {expected_count}")
    return dict(sorted(result.items()))


def parse_acs_population(
    rows: Iterable[Mapping[str, object]],
    county_universe: set[str],
    *,
    expected_total: int = 334_922_499,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        for field in ("GEO_ID", "B01003_E001"):
            if field not in row:
                raise ContractError(f"ACS population missing column {field}")
        geo_id = str(row["GEO_ID"])
        match = _COUNTY_GEO_ID.fullmatch(geo_id)
        if match is None:
            if geo_id.startswith("0500000US"):
                raise ContractError(f"malformed ACS county GEO_ID: {geo_id!r}")
            continue
        fips = match.group(1)
        if fips not in county_universe:
            if fips[:2] in _IN_SCOPE_PREFIXES:
                raise ContractError(f"in-scope ACS population FIPS outside universe: {fips}")
            continue
        if fips in result:
            raise IntegrityError(f"duplicate ACS population FIPS: {fips}")
        value = _integer(row["B01003_E001"], "B01003_E001")
        if value < 0:
            raise ContractError(f"negative ACS population for {fips}")
        result[fips] = value
    if set(result) != county_universe:
        raise IntegrityError("ACS population FIPS do not equal Gazetteer universe")
    if sum(result.values()) != expected_total:
        raise IntegrityError("ACS population total does not equal frozen denominator")
    return dict(sorted(result.items()))


def parse_acs_income(
    rows: Iterable[Mapping[str, object]], county_universe: set[str]
) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for row in rows:
        for field in ("GEO_ID", "B19013_E001"):
            if field not in row:
                raise ContractError(f"ACS income missing column {field}")
        geo_id = str(row["GEO_ID"])
        match = _COUNTY_GEO_ID.fullmatch(geo_id)
        if match is None:
            if geo_id.startswith("0500000US"):
                raise ContractError(f"malformed ACS county GEO_ID: {geo_id!r}")
            continue
        fips = match.group(1)
        if fips not in county_universe:
            if fips[:2] in _IN_SCOPE_PREFIXES:
                raise IntegrityError(f"in-scope ACS income FIPS outside universe: {fips}")
            continue
        if fips in result:
            raise IntegrityError(f"duplicate ACS income FIPS: {fips}")
        value = _integer(row["B19013_E001"], "B19013_E001")
        result[fips] = None if value < 0 else value
    if set(result) != county_universe:
        raise IntegrityError("ACS income FIPS do not equal Gazetteer universe")
    return dict(sorted(result.items()))


def parse_rucc(
    rows: Iterable[Mapping[str, object]], county_universe: set[str]
) -> dict[str, int]:
    required = {"FIPS", "State", "County_Name", "Attribute", "Value"}
    expected_attributes = {"Population_2020", "RUCC_2023", "Description"}
    result: dict[str, int] = {}
    identities: dict[str, tuple[str, str]] = {}
    observed_attributes: dict[str, set[str]] = {}
    for row in rows:
        if set(row) != required:
            raise ContractError(
                "RUCC source requires exact long-form "
                "FIPS,State,County_Name,Attribute,Value columns"
            )
        fips = str(row["FIPS"])
        if not _FIPS.fullmatch(fips):
            raise ContractError(f"malformed RUCC FIPS: {fips!r}")
        state = str(row["State"])
        county = str(row["County_Name"])
        attribute = str(row["Attribute"])
        value = str(row["Value"])
        if not re.fullmatch(r"[A-Z]{2}", state) or not county:
            raise ContractError(f"malformed RUCC county identity for {fips}")
        identity = (state, county)
        prior_identity = identities.setdefault(fips, identity)
        if prior_identity != identity:
            raise IntegrityError(f"RUCC county identity drift for {fips}")
        if attribute not in expected_attributes:
            raise ContractError(f"unsupported RUCC attribute for {fips}: {attribute!r}")
        attributes = observed_attributes.setdefault(fips, set())
        if attribute in attributes:
            raise IntegrityError(f"duplicate RUCC attribute for {fips}: {attribute}")
        attributes.add(attribute)
        code: int | None = None
        if attribute == "Population_2020":
            population = _integer(value, "Population_2020")
            if population < 0:
                raise ContractError(f"negative RUCC population for {fips}")
        elif attribute == "Description" and not value:
            raise ContractError(f"blank RUCC description for {fips}")
        elif attribute == "RUCC_2023":
            code = _integer(value, "RUCC_2023")
            if not 1 <= code <= 9:
                raise ContractError(f"RUCC outside 1..9 for {fips}")
        if fips not in county_universe:
            if fips[:2] in _IN_SCOPE_PREFIXES:
                raise IntegrityError(f"in-scope RUCC FIPS outside universe: {fips}")
            continue
        if state not in STATE_FIPS or STATE_FIPS[state] != fips[:2]:
            raise ContractError(f"RUCC state/FIPS mismatch: {state}/{fips}")
        if attribute == "RUCC_2023":
            if fips in result:
                raise IntegrityError(f"duplicate RUCC FIPS: {fips}")
            if code is None:
                raise IntegrityError(f"RUCC code was not parsed for {fips}")
            result[fips] = code
    for fips in county_universe:
        if observed_attributes.get(fips) != expected_attributes:
            raise IntegrityError(f"RUCC long-form attributes are incomplete for {fips}")
    if set(result) != county_universe:
        raise IntegrityError("RUCC FIPS do not equal Gazetteer universe")
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class HrsaAudit:
    raw_rows: int
    retained_rows: int
    qualifying_retained_rows: int
    malformed_fips: int
    out_of_scope_fips: int
    normalized_status_counts: dict[str, int]
    duplicate_designations: int


def parse_hrsa_hpsa(
    rows: Iterable[Mapping[str, object]], county_universe: set[str]
) -> tuple[dict[str, bool], HrsaAudit]:
    values = list(rows)
    if not values:
        raise ContractError("HRSA source is empty or header-only")
    required = {"Common State County FIPS Code", "HPSA Status"}
    if any(not required.issubset(row) for row in values):
        raise ContractError("HRSA source missing exact required columns")
    retained = 0
    qualifying_retained = 0
    malformed = 0
    out_scope = 0
    statuses: Counter[str] = Counter()
    designation_counts: Counter[str] = Counter()
    qualifying = {"DESIGNATED", "PROPOSED FOR WITHDRAWAL"}
    for row in values:
        fips = str(row["Common State County FIPS Code"] or "").strip(" \t\r\n\f\v")
        status = normalize_text(row["HPSA Status"])
        statuses[status] += 1
        if not re.fullmatch(r"[0-9]{5}", fips):
            malformed += 1
            continue
        if fips not in county_universe:
            out_scope += 1
            continue
        retained += 1
        if status in qualifying:
            qualifying_retained += 1
            designation_counts[fips] += 1
    result = {fips: designation_counts[fips] > 0 for fips in sorted(county_universe)}
    duplicates = sum(max(0, count - 1) for count in designation_counts.values())
    return result, HrsaAudit(
        len(values), retained, qualifying_retained, malformed, out_scope,
        dict(sorted(statuses.items())), duplicates
    )
