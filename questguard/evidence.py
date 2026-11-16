"""Validated evidence records and conservative selection of usable quest facts."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

MAX_ID = 2_147_483_647
FOREVERDB_SOURCE = "ForeverDB.net"
FOREVERDB_URL = "https://foreverdb.net"
FOREVERDB_CREDIT = f"Data from {FOREVERDB_SOURCE} · {FOREVERDB_URL}"
VERIFIED_GAME = "verified-game-state"
FOREVER_SPECIFIC = "forever-specific"
STRUCTURED = "authoritative-structured"
CLASSIC = "classic-inherited"
GUIDE = "guide-derived"
UNVERIFIED = "unverified"
CONFIDENCE_ORDER = {
    VERIFIED_GAME: 6, FOREVER_SPECIFIC: 5, STRUCTURED: 4, GUIDE: 3, CLASSIC: 2, UNVERIFIED: 1,
}
_SENSITIVE = frozenset({
    "requirements", "requirementGroups", "classification", "startItem", "timerSeconds",
    "startLocation", "endLocation", "faction", "classes", "minLevel",
})
_DISPLAY_FIELDS = frozenset({"name", "zone", "questLevel", "questLine", "chain", "objectives", "startOptions", "endOptions"})
FIELDS = _SENSITIVE | _DISPLAY_FIELDS
_CLASSES = frozenset({
    "WARRIOR", "PALADIN", "HUNTER", "ROGUE", "PRIEST", "SHAMAN", "MAGE", "WARLOCK", "DRUID",
    "DEATHKNIGHT", "MONK", "DEMONHUNTER", "EVOKER",
})
_LOCAL_METHOD = "Explicit local import"


def from_foreverdb(quest: Mapping) -> bool:
    def identified(claim: Mapping) -> bool:
        provider = claim.get("provider") or claim.get("source", "")
        return isinstance(provider, str) and FOREVERDB_SOURCE.casefold() in provider.casefold()

    return (identified(quest) or any(identified(claim) for claims in quest.get("fieldEvidence", {}).values()
                                    for claim in claims)
            or any(identified(claim) for conflict in quest.get("conflicts", ())
                   for claim in conflict.get("claims", ())))


def clean_text(value, limit: int = 400) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())
    if not clean or len(clean) > limit or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in clean):
        return None
    return clean


def integer(value, maximum: int = MAX_ID) -> int | None:
    return value if type(value) is int and 1 <= value <= maximum else None


def safe_url(value) -> str | None:
    """Public HTTPS references only, without credentials, query data or fragments."""
    text = clean_text(value, 512)
    if not text or "\\" in text or any(character.isspace() for character in text):
        return None
    try:
        bits = urlsplit(text)
        if bits.scheme != "https" or not bits.hostname or bits.username or bits.password or bits.port not in (None, 443):
            return None
        return urlunsplit(("https", bits.hostname, bits.path or "/", "", ""))
    except ValueError:
        return None


def _datetime(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        return None
    return date.astimezone(timezone.utc) if date.tzinfo is not None else None


def utc_stamp(value) -> str | None:
    parsed = _datetime(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def build_number(value) -> str | None:
    if type(value) is int and 10_000 <= value <= 9_999_999:
        return str(value)
    text = clean_text(value, 100)
    if not text:
        return None
    match = re.fullmatch(r"(?:\d{1,2}\.\d{1,2}\.\d{1,2}[._ +\-])?(\d{5,7})", text)
    if not match:
        match = re.match(r"^WOW-(\d{5,7})patch", text, flags=re.IGNORECASE)
    return match[1] if match else None


def build_matches(expected, observed) -> bool | None:
    if expected is None or observed is None:
        return None
    first, second = build_number(expected), build_number(observed)
    if first is not None and second is not None:
        return first == second
    if str(expected).casefold() == str(observed).casefold():
        return True
    patch = r"^(?:WOW-\d{5,7}patch)?(\d{1,2}\.\d{1,2}\.\d{1,2})(?:\D|$)"
    expected_patch = re.match(patch, str(expected), flags=re.IGNORECASE)
    observed_patch = re.match(patch, str(observed), flags=re.IGNORECASE)
    if expected_patch and observed_patch and expected_patch[1] != observed_patch[1]:
        return False
    # An edition/patch label alone cannot prove that the builds differ.
    return None


def _position(value) -> float | None:
    if type(value) in (int, float) and 0 <= value <= 100 and math.isfinite(value):
        return round(float(value), 3)
    return None


def _location(value) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    result = {}
    for field in ("name", "zone", "source", "instance"):
        if label := clean_text(value.get(field), 160):
            result[field] = label
    if value.get("type") in ("npc", "object", "item"):
        result["type"] = value["type"]
        if entity := integer(value.get("id")):
            result["id"] = entity
    x, y = _position(value.get("x")), _position(value.get("y"))
    if x is not None and y is not None:
        result.update(x=x, y=y)
        if map_id := integer(value.get("mapId")):
            result["mapId"] = map_id
    return result or None


def _requirements(value, quest_id: int) -> list[dict] | None:
    if not isinstance(value, (list, tuple)) or len(value) > 128:
        return None
    result = set()
    for row in value:
        identifier = integer(row.get("questId")) if isinstance(row, Mapping) else None
        state = row.get("state") if isinstance(row, Mapping) else None
        if not identifier or identifier == quest_id or state not in ("active", "completed"):
            return None
        result.add((identifier, state))
    return [{"questId": identifier, "state": state} for identifier, state in sorted(result)]


def _chain(value, quest_id: int) -> list[dict] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result = []
    for row in value[:128]:
        if not isinstance(row, Mapping):
            continue
        identifier = integer(row.get("questId"))
        direction = row.get("direction")
        if not identifier or identifier == quest_id or direction not in ("previous", "next"):
            continue
        edge = {"questId": identifier, "direction": direction}
        for field in ("name", "relation", "source", "confidence"):
            if label := clean_text(row.get(field), 160):
                edge[field] = label
        if edge not in result:
            result.append(edge)
    return result or None


def _objectives(value) -> list[dict] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result = []
    for row in value[:32]:
        if not isinstance(row, Mapping):
            continue
        # Keep a concise label and typed identifiers, never quest dialogue or HTML.
        item = {}
        for field in ("index", "targetId", "count"):
            if number := integer(row.get(field)):
                item[field] = number
        for field in ("type", "name", "source"):
            if label := clean_text(row.get(field), 160):
                item[field] = label
        if item:
            result.append(item)
    return result or None


def fact_value(field: str, value, quest_id: int):
    """Whitelist supported fields and enforce their units and shape."""
    if field in {"name", "zone"}:
        return clean_text(value, 160)
    if field in {"questLevel", "minLevel"}:
        return integer(value, 255)
    if field == "timerSeconds":
        return integer(value, 7 * 24 * 60 * 60)
    if field == "classification":
        return value if value in ("mandatory", "optional", "breadcrumb") else None
    if field == "faction":
        side = clean_text(value, 20)
        return side.capitalize() if side and side.casefold() in {"horde", "alliance"} else None
    if field == "classes" and isinstance(value, (tuple, list)):
        classes = {name.upper() for name in value if isinstance(name, str) and name.upper() in _CLASSES}
        return sorted(classes) or None
    if field in {"startLocation", "endLocation"}:
        return _location(value)
    if field in {"startOptions", "endOptions"} and isinstance(value, (tuple, list)):
        locations = [location for item in value[:32] if (location := _location(item))]
        return locations or None
    if field == "startItem" and isinstance(value, Mapping) and (identifier := integer(value.get("itemId"))):
        result = {"itemId": identifier}
        if name := clean_text(value.get("name"), 160):
            result["name"] = name
        return result
    if field == "requirements":
        return _requirements(value, quest_id)
    if field == "requirementGroups" and isinstance(value, (tuple, list)) and len(value) <= 128:
        groups = [_requirements(group, quest_id) for group in value]
        if any(not group for group in groups):
            return None
        return sorted(groups, key=lambda group: [(row["questId"], row["state"]) for row in group])
    if field == "questLine" and isinstance(value, Mapping) and (name := clean_text(value.get("name"), 160)):
        result = {"name": name}
        part, total = integer(value.get("part")), integer(value.get("of"))
        if part and total and part <= total:
            result.update(part=part, of=total)
        return result
    if field == "chain":
        return _chain(value, quest_id)
    if field == "objectives":
        return _objectives(value)
    return None


def validated_evidence(quest_id: int, raw) -> dict[str, list[dict]]:
    if not isinstance(raw, Mapping):
        return {}
    result = {}
    for field, claims in raw.items():
        if field not in FIELDS or not isinstance(claims, (list, tuple)):
            continue
        accepted = []
        for claim in claims[:64]:
            if not isinstance(claim, Mapping):
                continue
            value = fact_value(field, claim.get("value"), quest_id)
            provider = clean_text(claim.get("provider"), 120)
            method = clean_text(claim.get("method"), 120)
            confidence = clean_text(claim.get("confidence"), 40)
            if value is None or not provider or not method or confidence not in CONFIDENCE_ORDER:
                continue
            if claim.get("inheritedClassic") is True:
                confidence = CLASSIC
            elif claim.get("guideDerived") is True:
                confidence = GUIDE
            row = {"value": value, "provider": provider, "method": method, "confidence": confidence}
            if url := safe_url(claim.get("sourceUrl")):
                row["sourceUrl"] = url
            for date in ("retrievedAt", "verifiedAt"):
                if valid := utc_stamp(claim.get(date)):
                    row[date] = valid
            if version := clean_text(str(claim["gameVersion"]), 100) if type(claim.get("gameVersion")) in (str, int) else None:
                row["gameVersion"] = version
            if label := clean_text(claim.get("sourceLabel"), 160):
                row["sourceLabel"] = label
            row.update(observedForever=claim.get("observedForever") is True and confidence in {FOREVER_SPECIFIC, VERIFIED_GAME},
                       inheritedClassic=confidence == CLASSIC, guideDerived=confidence == GUIDE)
            if row not in accepted:
                accepted.append(row)
        if accepted:
            result[field] = accepted
    return result


def _claimed(row: Mapping, quest_id: int) -> dict[str, list[dict]]:
    claims = validated_evidence(quest_id, row.get("fieldEvidence"))
    source = clean_text(row.get("source"), 120) or "Local metadata"
    declared = clean_text(row.get("confidence"), 40)
    label = " ".join((source, declared or "")).casefold()
    inherited = row.get("inheritedClassic") is True or "classic" in label or "cmangos" in label
    inferred = row.get("guideDerived") is True or "restedxp" in label or "guide-derived" in label
    for field in FIELDS:
        if field in claims or field not in row:
            continue
        raw = row[field]
        value = fact_value(field, raw, quest_id)
        if value is None:
            continue
        confidence = declared if declared in CONFIDENCE_ORDER else STRUCTURED
        method = _LOCAL_METHOD
        if inherited:
            confidence = CLASSIC
        elif inferred:
            confidence = GUIDE
        elif source.casefold() == FOREVERDB_SOURCE.casefold():
            method = "Legacy provider metadata"
            if field in _SENSITIVE:
                # Older API rows did not identify the origin of individual facts.
                confidence = UNVERIFIED
        elif field == "startItem":
            confidence = FOREVER_SPECIFIC if isinstance(raw, Mapping) and raw.get("verifiedForever") is True else UNVERIFIED
        elif field == "timerSeconds" and declared not in {FOREVER_SPECIFIC, VERIFIED_GAME}:
            confidence = UNVERIFIED
        claims[field] = [{"value": value, "provider": source, "method": method, "confidence": confidence,
                         "retrievedAt": row.get("observedAt"), "sourceUrl": row.get("sourceUrl"),
                         "gameVersion": row.get("gameVersion")}]
    return validated_evidence(quest_id, claims)


def _eligible(field: str, claim: Mapping, game_version) -> bool:
    if field not in _SENSITIVE:
        return True
    if build_matches(game_version, claim.get("gameVersion")) is False:
        return False
    confidence = claim["confidence"]
    if confidence in {VERIFIED_GAME, FOREVER_SPECIFIC}:
        return True
    return confidence == STRUCTURED and claim["method"] == _LOCAL_METHOD and field not in {"startItem", "timerSeconds"}


def _incompatible(field: str, first, second) -> bool:
    if field == "startItem":
        return first["itemId"] != second["itemId"]
    if field in {"startLocation", "endLocation"}:
        for key in ("type", "id", "mapId"):
            if key in first and key in second and first[key] != second[key]:
                return True
        if not ("id" in first and "id" in second) and "name" in first and "name" in second:
            if first["name"].casefold() != second["name"].casefold():
                return True
        if all(key in first and key in second for key in ("x", "y")):
            return any(abs(first[key] - second[key]) > 0.1 for key in ("x", "y"))
        return False
    if isinstance(first, str) and isinstance(second, str):
        return first.casefold() != second.casefold()
    return first != second


def _latest_claims(claims: list[dict]) -> list[dict]:
    by_source = {}
    for claim in claims:
        key = (claim["provider"], claim["method"], claim.get("gameVersion"), claim["confidence"])
        by_source.setdefault(key, []).append(claim)
    result = []
    for items in by_source.values():
        newest = max((row.get("retrievedAt", "") for row in items), default="")
        for row in items:
            if row.get("retrievedAt", "") == newest and row not in result:
                result.append(row)
    result.sort(key=lambda row: (CONFIDENCE_ORDER[row["confidence"]], row.get("retrievedAt", "")), reverse=True)
    return result[:32]


def merge_quest_evidence(
    quest_id: int, existing_row: Mapping | None, incoming_row: Mapping | None, *, game_version=None,
) -> dict:
    """Keep dissenting facts while excluding unresolved conflicting fields from gameplay."""
    existing, incoming = existing_row or {}, incoming_row or {}
    left, right = _claimed(existing, quest_id), _claimed(incoming, quest_id)
    chosen = {}
    evidence = {}
    conflicts = []
    fields = sorted(left.keys() | right.keys())
    for field in fields:
        claims = _latest_claims(left.get(field, []) + right.get(field, []))
        evidence[field] = claims
        applicable = [claim for claim in claims if _eligible(field, claim, game_version)]
        candidates = applicable
        # A build-specific claim can disagree with a Classic or known older
        # claim without making the current edition unresolved.
        if field not in _SENSITIVE and applicable:
            authoritative = [claim for claim in applicable if claim["confidence"] in {VERIFIED_GAME, FOREVER_SPECIFIC, STRUCTURED}
                             and build_matches(game_version, claim.get("gameVersion")) is not False]
            candidates = authoritative or applicable
        disagree = any(_incompatible(field, first["value"], second["value"])
                       for index, first in enumerate(claims) for second in claims[index + 1:])
        unresolved = any(_incompatible(field, first["value"], second["value"])
                         for index, first in enumerate(candidates) for second in candidates[index + 1:])
        if disagree and field not in {"chain", "startOptions", "endOptions", "objectives"}:
            conflicts.append({"field": field, "claims": claims, "unresolved": unresolved or not candidates})
        if candidates and (not unresolved or field in {"chain", "startOptions", "endOptions", "objectives"}):
            best = candidates[0]
            chosen[field] = best
    fixed, groups = chosen.get("requirements"), chosen.get("requirementGroups")
    if fixed and groups and (fixed["provider"], fixed["method"], build_number(fixed.get("gameVersion"))) != (
        groups["provider"], groups["method"], build_number(groups.get("gameVersion"))
    ):
        # AND-ing independently sourced fragments invents a prerequisite
        # formula. Preserve the claims for review until one source supplies it.
        for field in ("requirements", "requirementGroups"):
            chosen.pop(field)
            if not any(row["field"] == field for row in conflicts):
                conflicts.append({"field": field, "claims": evidence[field], "unresolved": True})
    record = {field: claim["value"] for field, claim in chosen.items()}
    if isinstance(record.get("startItem"), dict):
        record["startItem"] = {**record["startItem"], "verifiedForever": True}
    record["classification"] = record.get("classification", "unknown")
    record["fieldEvidence"] = evidence
    record["conflicts"] = conflicts
    names = {claim["provider"] for claims in evidence.values() for claim in claims}
    if not names:
        fallback = clean_text(incoming.get("source")) or clean_text(existing.get("source"))
        if fallback:
            names.add(fallback)
    record["source"] = ", ".join(sorted(names))[:400] or "Local metadata"
    representative = chosen.get("name") or next(iter(chosen.values()), None)
    if representative:
        record["confidence"] = representative["confidence"]
        if representative.get("sourceUrl"):
            record["sourceUrl"] = representative["sourceUrl"]
    else:
        record["confidence"] = UNVERIFIED
    dates = [row.get("retrievedAt") for claims in evidence.values() for row in claims if row.get("retrievedAt")]
    if dates:
        record["observedAt"] = max(dates)
    return record
