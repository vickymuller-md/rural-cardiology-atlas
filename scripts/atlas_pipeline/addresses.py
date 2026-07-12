"""Frozen NPPES/CMS address normalization, classifier, and routing rules."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass

from .contracts import STATE_FIPS

_SPACE = re.compile(r"\s+")
_ZIP = re.compile(r"^(?:[0-9]{5}|[0-9]{9}|[0-9]{5}-[0-9]{4})$")
_BOX_ID = r"(?:#\s*)?[A-Z0-9]+(?:-[A-Z0-9]+)?"

_MAIL_ONLY = tuple(
    re.compile(pattern.replace("BOX_ID", _BOX_ID))
    for pattern in (
        r"^(?:P O BOX|PO BOX|POST OFFICE BOX|BOX)\s+BOX_ID$",
        r"^(?:RR|RURAL ROUTE)\s+[A-Z0-9]+(?:\s+BOX\s+BOX_ID)?$",
        r"^(?:HC|HIGHWAY CONTRACT(?: ROUTE)?)\s+[A-Z0-9]+(?:\s+BOX\s+BOX_ID)?$",
        r"^STAR ROUTE\s+[A-Z0-9]+(?:\s+BOX\s+BOX_ID)?$",
        r"^GENERAL DELIVERY$",
        r"^(?:PMB|PRIVATE MAILBOX)\s+BOX_ID$",
    )
)
_PO_BOX_ONLY = re.compile(
    (r"^(?:P O BOX|PO BOX|POST OFFICE BOX|BOX)\s+" + _BOX_ID + r"$")
)

_AUX_ONLY = tuple(
    re.compile(pattern.replace("BOX_ID", _BOX_ID))
    for pattern in (
        r"^(?:APT|APARTMENT|STE|SUITE|UNIT|RM|ROOM|FL|FLOOR|DEPT|DEPARTMENT)\s+BOX_ID$",
        r"^#\s*[A-Z0-9]+(?:-[A-Z0-9]+)?$",
        r"^(?:ATTN|C O|CARE OF)\s+[A-Z0-9][A-Z0-9 '&-]*$",
    )
)


def normalize_text(value: object) -> str:
    """NFKC, trim, collapse Unicode whitespace, uppercase; preserve punctuation."""

    if value is None:
        return ""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", str(value)).strip()).upper()


def normalize_zip5(value: object) -> str | None:
    """Return a syntactically valid ZIP5 without coercion or padding."""

    if value is None:
        return None
    raw = str(value).strip(" \t\r\n\f\v")
    if not _ZIP.fullmatch(raw):
        return None
    zip5 = raw[:5]
    return None if zip5 == "00000" else zip5


def classifier_text(value: str) -> str:
    normalized = normalize_text(value)
    return _SPACE.sub(" ", re.sub(r"[.,/]", " ", normalized).strip())


def line_kind(value: str) -> str:
    """Classify one normalized line as blank, mail, auxiliary, or street."""

    if not value:
        return "blank"
    candidate = classifier_text(value)
    if any(pattern.fullmatch(candidate) for pattern in _MAIL_ONLY):
        return "mail_only"
    if any(pattern.fullmatch(candidate) for pattern in _AUX_ONLY):
        return "auxiliary_only"
    return "street_capable"


@dataclass(frozen=True)
class NormalizedAddress:
    street1: str
    street2: str
    city: str
    state: str
    zip5: str
    zip_status: str
    country: str
    address_id: str
    classification: str
    direct_eligible: bool
    hud_eligible: bool
    out_of_scope_reason: str | None

    @property
    def submitted_street(self) -> str:
        return " ".join(line for line in (self.street1, self.street2) if line)

    @property
    def diagnostic_flags(self) -> frozenset[str]:
        nonblank = [line for line in (self.street1, self.street2) if line]
        kinds = [line_kind(line) for line in nonblank]
        classifier_lines = [classifier_text(line) for line in nonblank]
        flags = {
            self.classification,
            "city_present" if self.city else "missing_city",
            f"zip_{self.zip_status}",
            "country_domestic" if self.country in {"", "US", "USA"} else "foreign_country",
            "state_in_scope" if self.state in STATE_FIPS else "invalid_or_out_of_scope_state",
            "direct_eligible" if self.direct_eligible else "direct_ineligible",
            "hud_eligible" if self.hud_eligible else "hud_ineligible",
        }
        if not nonblank:
            flags.add("both_street_lines_blank")
        if any(kind == "mail_only" for kind in kinds):
            flags.add("mail_only_line_present")
        if any(kind == "auxiliary_only" for kind in kinds):
            flags.add("auxiliary_only_line_present")
        if any(kind == "street_capable" for kind in kinds):
            flags.add("street_capable_line_present")
        if nonblank and all(_PO_BOX_ONLY.fullmatch(line) for line in classifier_lines):
            flags.add("po_box_only")
        if "mail_only" in kinds and "street_capable" in kinds:
            flags.add("mixed_mail_street")
        return frozenset(flags)

    @property
    def direct_reason(self) -> str:
        if self.direct_eligible:
            components = []
            if self.city:
                components.append("CITY")
            if self.zip5:
                components.append("ZIP")
            return "DIRECT_ELIGIBLE_STREET_" + "_AND_".join(components)
        if self.classification != "street_capable":
            return "DIRECT_INELIGIBLE_" + self.classification.upper()
        return "DIRECT_INELIGIBLE_STREET_MISSING_CITY_AND_MISSING_OR_INVALID_ZIP"


def canonical_address_bytes(fields: tuple[str, str, str, str, str, str]) -> bytes:
    nfc = [unicodedata.normalize("NFC", item) for item in fields]
    return json.dumps(nfc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def make_address_id(fields: tuple[str, str, str, str, str, str]) -> str:
    return "A-" + hashlib.sha256(canonical_address_bytes(fields)).hexdigest()


def normalize_address(
    street1: object,
    street2: object,
    city: object,
    state: object,
    postal: object,
    country: object,
) -> NormalizedAddress:
    """Normalize, classify, and route one address without inspecting outcomes."""

    s1, s2, cty, st, nation = map(
        normalize_text, (street1, street2, city, state, country)
    )
    raw_postal = "" if postal is None else str(postal).strip(" \t\r\n\f\v")
    normalized_zip5 = normalize_zip5(postal)
    zip_status = "missing" if not raw_postal else "valid" if normalized_zip5 else "invalid"
    zip5 = normalized_zip5 or ""
    fields = (s1, s2, cty, st, zip5, nation)

    domestic = nation in {"", "US", "USA"}
    if not domestic:
        classification = "country_out_of_scope"
        direct = hud = False
        scope_reason = classification
    elif st not in STATE_FIPS:
        classification = "state_out_of_scope_or_invalid"
        direct = hud = False
        scope_reason = classification
    else:
        kinds = [line_kind(line) for line in (s1, s2) if line]
        if not kinds:
            classification = "missing_street"
        elif all(kind in {"mail_only", "auxiliary_only"} for kind in kinds):
            classification = "nonstreet_only"
        else:
            classification = "street_capable"
        direct = classification == "street_capable" and bool(cty or zip5)
        hud = bool(zip5)
        scope_reason = None

    return NormalizedAddress(
        street1=s1,
        street2=s2,
        city=cty,
        state=st,
        zip5=zip5,
        zip_status=zip_status,
        country=nation,
        address_id=make_address_id(fields),
        classification=classification,
        direct_eligible=direct,
        hud_eligible=hud,
        out_of_scope_reason=scope_reason,
    )


def assert_address_id_bijection(addresses: list[NormalizedAddress]) -> None:
    """Fail if a digest maps to more than one normalized tuple."""

    seen: dict[str, tuple[str, ...]] = {}
    for address in addresses:
        fields = (
            address.street1,
            address.street2,
            address.city,
            address.state,
            address.zip5,
            address.country,
        )
        prior = seen.setdefault(address.address_id, fields)
        if prior != fields:
            from .errors import IntegrityError

            raise IntegrityError(f"address hash collision: {address.address_id}")


@dataclass(frozen=True)
class EntityAddressMapping:
    entity_kind: str
    entity_id: str
    address_id: str
    street1: str
    street2: str
    city: str
    state: str
    zip5: str
    country: str
    classification: str
    direct_eligible: bool
    hud_eligible: bool


def entity_address_mapping_bytes(rows: list[EntityAddressMapping]) -> bytes:
    keys: set[tuple[str, str]] = set()
    lines = []
    for row in sorted(rows, key=lambda item: (item.entity_kind, item.entity_id, item.address_id)):
        if row.entity_kind not in {"P", "H"} or (row.entity_kind, row.entity_id) in keys:
            from .errors import IntegrityError

            raise IntegrityError("entity/address mapping key contract failed")
        keys.add((row.entity_kind, row.entity_id))
        expected_id = make_address_id((row.street1, row.street2, row.city, row.state, row.zip5, row.country))
        if row.address_id != expected_id:
            from .errors import IntegrityError

            raise IntegrityError("entity/address mapping address_id mismatch")
        normalized = {
            key: unicodedata.normalize("NFC", value) if isinstance(value, str) else value
            for key, value in asdict(row).items()
        }
        lines.append(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
