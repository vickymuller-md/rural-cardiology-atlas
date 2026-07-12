"""HUD-USPS same-state unique-maximum fallback rules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable, Mapping

from .contracts import STATE_FIPS
from .errors import ContractError, IntegrityError


def _ratio(value: object, field: str) -> Decimal:
    text = "" if value is None else str(value).strip()
    if not text:
        raise ContractError(f"HUD {field} is required")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ContractError(f"invalid HUD {field}: {text!r}") from exc
    if not result.is_finite() or result < 0 or result > 1:
        raise ContractError(f"HUD {field} outside [0,1]: {text!r}")
    return result


@dataclass(frozen=True)
class HudCandidate:
    zip5: str
    county_fips: str
    bus_ratio: Decimal
    res_ratio: Decimal
    tot_ratio: Decimal
    oth_ratio: Decimal
    usps_zip_pref_city: str
    usps_zip_pref_state: str


def parse_candidates(
    rows: Iterable[Mapping[str, object]], county_universe: set[str]
) -> list[HudCandidate]:
    required = {
        "ZIP", "COUNTY", "BUS_RATIO", "RES_RATIO", "TOT_RATIO", "OTH_RATIO",
        "USPS_ZIP_PREF_CITY", "USPS_ZIP_PREF_STATE",
    }
    seen: set[tuple[str, str]] = set()
    result: list[HudCandidate] = []
    for row in rows:
        if set(row) != required:
            raise ContractError(
                "HUD canonical row schema mismatch: "
                f"missing={sorted(required - set(row))}, extra={sorted(set(row) - required)}"
            )
        zip5 = str(row.get("ZIP") or "").strip()
        county = str(row.get("COUNTY") or "").strip()
        if not (len(zip5) == 5 and zip5.isdigit()) or zip5 == "00000":
            raise ContractError(f"invalid HUD ZIP: {zip5!r}")
        if not (len(county) == 5 and county.isdigit()) or county not in county_universe:
            raise ContractError(f"HUD county outside frozen universe: {county!r}")
        key = (zip5, county)
        if key in seen:
            raise IntegrityError(f"duplicate HUD ZIP/county: {key}")
        seen.add(key)
        ratios = (
            _ratio(row.get("BUS_RATIO"), "BUS_RATIO"),
            _ratio(row.get("RES_RATIO"), "RES_RATIO"),
            _ratio(row.get("TOT_RATIO"), "TOT_RATIO"),
            _ratio(row.get("OTH_RATIO"), "OTH_RATIO"),
        )
        city = str(row.get("USPS_ZIP_PREF_CITY") or "").strip()
        preferred_state = str(row.get("USPS_ZIP_PREF_STATE") or "").strip().upper()
        if not city or re.fullmatch(r"[A-Z]{2}", preferred_state, flags=re.ASCII) is None:
            raise ContractError("HUD preferred city/state metadata is invalid")
        result.append(
            HudCandidate(
                zip5,
                county,
                *ratios,
                city,
                preferred_state,
            )
        )
    return result


@dataclass(frozen=True)
class HudAssignment:
    outcome: str
    county_fips: str | None
    ratio_basis: str
    selected_ratio: Decimal | None
    bus_ratio: Decimal | None
    res_ratio: Decimal | None
    tot_ratio: Decimal | None
    same_state_candidate_count: int
    second_highest_ratio: Decimal | None
    margin_to_second: Decimal | None


def assign_hud(
    zip5: str,
    source_state: str,
    candidates: Iterable[HudCandidate],
    *,
    basis: str = "BUS_RATIO",
    restrict_state: bool = True,
) -> HudAssignment:
    if basis not in {"BUS_RATIO", "RES_RATIO", "TOT_RATIO"}:
        raise ContractError(f"unsupported HUD ratio basis: {basis}")
    state_fips = STATE_FIPS.get(source_state)
    if state_fips is None:
        return HudAssignment("HUD_INVALID_SOURCE_STATE", None, basis, None, None, None, None, 0, None, None)
    if not (len(zip5) == 5 and zip5.isdigit()) or zip5 == "00000":
        return HudAssignment("HUD_INVALID_OR_MISSING_ZIP", None, basis, None, None, None, None, 0, None, None)
    zip_rows = [item for item in candidates if item.zip5 == zip5]
    if not zip_rows:
        return HudAssignment("HUD_ZIP_ABSENT", None, basis, None, None, None, None, 0, None, None)
    eligible = (
        [item for item in zip_rows if item.county_fips[:2] == state_fips]
        if restrict_state
        else zip_rows
    )
    if not eligible:
        return HudAssignment("HUD_NO_SAME_STATE_CANDIDATE", None, basis, None, None, None, None, 0, None, None)
    accessor = {
        "BUS_RATIO": lambda item: item.bus_ratio,
        "RES_RATIO": lambda item: item.res_ratio,
        "TOT_RATIO": lambda item: item.tot_ratio,
    }[basis]
    present = list(eligible)
    positive = [item for item in present if accessor(item) > 0]
    if not positive:
        return HudAssignment("HUD_NO_POSITIVE_RATIO", None, basis, None, None, None, None, len(eligible), None, None)
    maximum = max(accessor(item) for item in positive)
    winners = [item for item in positive if accessor(item) == maximum]
    if len(winners) != 1:
        tie_code = {
            "BUS_RATIO": "HUD_BUS_TIE",
            "RES_RATIO": "HUD_RES_TIE",
            "TOT_RATIO": "HUD_TOT_TIE",
        }[basis]
        return HudAssignment(tie_code, None, basis, maximum, None, None, None, len(eligible), maximum, Decimal("0"))
    winner = winners[0]
    lower = sorted((accessor(item) for item in present if item is not winner), reverse=True)
    second = lower[0] if lower else None
    margin = maximum - second if second is not None else None
    return HudAssignment(
        "HUD_ASSIGNED",
        winner.county_fips,
        basis,
        maximum,
        winner.bus_ratio,
        winner.res_ratio,
        winner.tot_ratio,
        len(eligible),
        second,
        margin,
    )
