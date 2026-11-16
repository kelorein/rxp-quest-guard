"""Quest metadata, authorized provider requests, and atomic local cache updates."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .evidence import FIELDS, FOREVERDB_SOURCE, FOREVERDB_URL, fact_value, from_foreverdb, safe_url, validated_evidence

SCHEMA_VERSION = 1
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_QUEST_ID = 2_147_483_647


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Database:
    quests: dict[int, dict]
    source: str
    source_version: str
    updated_at: str

    @cached_property
    def has_foreverdb(self) -> bool:
        return any(from_foreverdb(quest) for quest in self.quests.values())

    @property
    def age_days(self) -> float | None:
        try:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(self.updated_at)).total_seconds() / 86400
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True)
class SyncResult:
    database: Database | None
    message: str
    used_cache: bool


def cache_directory() -> Path:
    override = os.environ.get("QG_DATA_DIR")
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share"
    return Path(local) / "RXPQuestGuard"


def atomic_write(path: Path, content: bytes, *, create_parent: bool = True) -> None:
    """Flush a sibling temporary file, then publish it with one replace."""
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _read_json(file):
    data = file.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("Quest data exceeds the size limit")
    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Quest data must be a UTF-8 JSON object") from exc


def _text(value, limit=400) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > limit or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in cleaned):
        return None
    return cleaned


def _integer(value, lower=1, upper=MAX_QUEST_ID) -> int | None:
    if type(value) is int and lower <= value <= upper:
        return value
    return None


def _coord(value) -> float | None:
    if type(value) in (int, float) and 0 <= value <= 100 and math.isfinite(value):
        return round(float(value), 4)
    return None


def _location(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {}
    for field in ("name", "zone", "source"):
        if label := _text(value.get(field)):
            result[field] = label
    x, y = _coord(value.get("x")), _coord(value.get("y"))
    if x is not None and y is not None:
        result.update(x=x, y=y)
        if map_id := _integer(value.get("mapId")):
            result["mapId"] = map_id
    return result or None


def _requirements(value, quest_id: int) -> list[dict]:
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError(f"Invalid requirement list for quest {quest_id}")
    result = []
    for requirement in value:
        if isinstance(requirement, dict):
            related = _integer(requirement.get("questId"))
            state = requirement.get("state")
            if related and related != quest_id and state in ("completed", "active"):
                result.append({"questId": related, "state": state})
                continue
        raise ValueError(f"Invalid requirement for quest {quest_id}")
    return result


def _normalize_quest(quest_id: int, row: dict, source: str) -> dict:
    classification = row.get("classification", "unknown")
    if classification not in ("mandatory", "optional", "breadcrumb", "unknown"):
        raise ValueError(f"Invalid classification for quest {quest_id}")
    item = {"classification": classification, "source": _text(row.get("source")) or source,
            "confidence": _text(row.get("confidence"), 40) or "unknown"}
    if name := _text(row.get("name")):
        item["name"] = name
    if level := _integer(row.get("minLevel"), upper=255):
        item["minLevel"] = level
    if (side := str(row.get("faction", "")).lower()) in ("alliance", "horde"):
        item["faction"] = side.capitalize()
    classes = row.get("classes")
    if isinstance(classes, list):
        filtered = [_text(value, 30) for value in classes[:20]]
        item["classes"] = [value.upper() for value in filtered if value]
        if not item["classes"]:
            item.pop("classes")
    for key in ("startLocation", "endLocation"):
        if location := fact_value(key, row.get(key), quest_id):
            item[key] = location
    if "requirements" in row:
        item["requirements"] = _requirements(row["requirements"], quest_id)
    if "requirementGroups" in row:
        groups = row["requirementGroups"]
        if not isinstance(groups, list) or len(groups) > 128 or any(not group for group in groups):
            raise ValueError(f"Invalid requirement groups for quest {quest_id}")
        item["requirementGroups"] = [_requirements(group, quest_id) for group in groups]
    if isinstance(row.get("chain"), list):
        item["chain"] = fact_value("chain", row["chain"], quest_id) or []
        for edge in item["chain"]:
            edge.setdefault("relation", "Unknown")
            edge.setdefault("source", source)
    if observed := _text(row.get("observedAt"), 64):
        item["observedAt"] = observed
    if url := safe_url(row.get("sourceUrl")):
        item["sourceUrl"] = url
    if version := _text(row.get("gameVersion"), 100):
        item["gameVersion"] = version
    for field in ("zone", "startItem", "timerSeconds", "questLevel", "objectives", "startOptions", "endOptions", "questLine"):
        if (value := fact_value(field, row.get(field), quest_id)) is not None:
            item[field] = value
    if "startItem" in item and row["startItem"].get("verifiedForever") is True:
        item["startItem"]["verifiedForever"] = True
    if evidence := validated_evidence(quest_id, row.get("fieldEvidence")):
        item["fieldEvidence"] = evidence
    conflicts = []
    for conflict in row["conflicts"][:30] if isinstance(row.get("conflicts"), list) else ():
        if not isinstance(conflict, dict) or not (field := _text(conflict.get("field"), 48)) or field not in FIELDS:
            continue
        if claims := validated_evidence(quest_id, {field: conflict.get("claims")}).get(field):
            unresolved = conflict.get("unresolved") is not False
            conflicts.append({"field": field, "claims": claims, "unresolved": unresolved})
            if unresolved and field != "name":
                item.pop(field, None)
                if field == "classification":
                    item[field] = "unknown"
    if conflicts:
        item["conflicts"] = conflicts
    return item


def _decode_database(raw) -> Database:
    if not isinstance(raw, dict) or type(raw.get("schemaVersion")) is not int or raw["schemaVersion"] != SCHEMA_VERSION:
        raise ValueError("Quest data requires schemaVersion 1")
    rows = raw.get("quests")
    if not isinstance(rows, dict) or len(rows) > 25_000:
        raise ValueError("Quest data requires an object keyed by quest ID (maximum 25,000)")
    source = _text(raw.get("source")) or "Local JSON"
    quests = {}
    for key, row in rows.items():
        if not key.isascii() or not key.isdecimal() or len(key) > 10:
            raise ValueError("Quest IDs must be positive decimal integers")
        quest_id = _integer(int(key))
        if quest_id is None or quest_id in quests or not isinstance(row, dict):
            raise ValueError(f"Invalid or duplicate quest record: {key}")
        quests[quest_id] = _normalize_quest(quest_id, row, source)
    return Database(quests, source, _text(raw.get("sourceVersion")) or "Unknown",
                    _text(raw.get("updatedAt"), 64) or "Unknown")


def _cache_path() -> Path:
    return cache_directory() / "quests-v1.json"


def load_cache() -> Database | None:
    try:
        with _cache_path().open("rb") as file:
            return _decode_database(_read_json(file))
    except FileNotFoundError:
        return None


def _save_cache(database: Database) -> None:
    encoded = json.dumps({"schemaVersion": SCHEMA_VERSION, "source": database.source,
                          "sourceVersion": database.source_version, "updatedAt": database.updated_at,
                          "quests": database.quests}, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ValueError("Quest cache exceeds the size limit")
    atomic_write(_cache_path(), encoded)


def import_database(path: Path | str) -> Database:
    with Path(path).open("rb") as file:
        database = _decode_database(_read_json(file))
    _save_cache(database)
    return database


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A provider redirect must never forward this user's key.
        return None


def _provider_request(quest_id: int, key: str) -> dict:
    request = Request(f"{FOREVERDB_URL}/api/v1/quests/{quest_id}", headers={
        "X-API-Key": key, "User-Agent": f"RXP Quest Guard Desktop/{__version__}",
        "Accept": "application/json",
    })
    with build_opener(_NoRedirect()).open(request, timeout=12) as response:
        payload = _read_json(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Unexpected ForeverDB response format")
    return payload


def _provider_location(raw) -> dict | None:
    if isinstance(raw, list):
        if len(raw) != 1:
            return None
        raw = raw[0]
    if not isinstance(raw, dict):
        return None
    location = raw.get("location")
    location = location if isinstance(location, dict) else {}
    return _location({"name": raw.get("name"), "source": raw.get("source"),
                      "mapId": location.get("uiMapId"), "zone": location.get("zone"),
                      "x": location.get("x"), "y": location.get("y")})


def _normalize_provider(payload: dict, quest_id: int, observed_at: str) -> tuple[dict, str]:
    row = payload["data"]
    if _integer(row.get("questId", row.get("id"))) != quest_id or _text(row.get("name")) is None:
        raise ValueError(f"ForeverDB response did not identify quest {quest_id}")
    meta = payload.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    version = next((_text(str(meta[key]), 100) for key in ("gameBuild", "build", "version")
                    if type(meta.get(key)) in (str, int)), None) or "Unknown"
    chain = []
    raw_chain = row.get("chain")
    if isinstance(raw_chain, dict):
        for direction in ("previous", "next"):
            edges = raw_chain.get(direction)
            if isinstance(edges, list):
                chain.extend({**edge, "direction": direction} for edge in edges[:64] if isinstance(edge, dict))
    return _normalize_quest(quest_id, {
        "name": row["name"], "faction": row.get("side"), "minLevel": row.get("minLevel"),
        "startLocation": _provider_location(row.get("starts")),
        "endLocation": _provider_location(row.get("ends")), "chain": chain,
        "confidence": "provider-reported", "observedAt": observed_at,
    }, FOREVERDB_SOURCE), version


def sync_foreverdb(quest_ids: Iterable[int], api_key: str, *,
                   progress: Callable[[str], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> SyncResult:
    """Fetch only requested IDs. Preserve the prior cache on failed batches."""
    try:
        existing = load_cache()
    except (ValueError, OSError):
        existing = None
    ids = sorted({value for value in quest_ids if _integer(value)})
    key = api_key.strip()
    if not ids or len(ids) > 1000 or not key or len(key) > 256 or any(not 33 <= ord(c) <= 126 for c in key):
        return SyncResult(existing, "Select 1–1,000 guide quest IDs and enter a valid ForeverDB API key.", existing is not None)

    updated_at = timestamp()
    observed = {}
    versions = set()
    unavailable = 0
    try:
        for index, quest_id in enumerate(ids, 1):
            if index > 1:
                while (remaining := earliest - time.monotonic()) > 0:
                    if cancelled and cancelled():
                        return SyncResult(existing, "ForeverDB sync cancelled.", existing is not None)
                    time.sleep(min(remaining, 0.15))
            if cancelled and cancelled():
                return SyncResult(existing, "ForeverDB sync cancelled.", existing is not None)
            earliest = time.monotonic() + 2.1
            if progress:
                progress(f"ForeverDB quest {index} of {len(ids)} (ID {quest_id})")
            try:
                payload = _provider_request(quest_id, key)
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                unavailable += 1
                continue
            observed[quest_id], version = _normalize_provider(payload, quest_id, updated_at)
            versions.add(version)
        if cancelled and cancelled():
            return SyncResult(existing, "ForeverDB sync cancelled.", existing is not None)
        if not observed:
            return SyncResult(existing, "ForeverDB had no records for the selected quest IDs.", existing is not None)
        # Local verified records take precedence over provider metadata. Keep their source and provenance.
        try:
            latest = load_cache()
        except (ValueError, OSError):
            latest = None
        merged = dict((latest or existing).quests) if latest or existing else {}
        for quest_id, quest in observed.items():
            previous = merged.get(quest_id)
            if previous and previous["source"] != FOREVERDB_SOURCE:
                continue
            merged[quest_id] = quest
        sources = {record["source"] for record in merged.values()}
        source = FOREVERDB_SOURCE if sources == {FOREVERDB_SOURCE} else "Local cache with ForeverDB.net sync"
        previous_version = latest.source_version if latest else None
        retained_old_provider_rows = any(record["source"] == FOREVERDB_SOURCE and quest_id not in observed
                                         for quest_id, record in merged.items())
        if retained_old_provider_rows and previous_version:
            versions.add(previous_version)
        version = versions.pop() if len(versions) == 1 else "Multiple or unknown builds"
        database = Database(merged, source, version, updated_at)
        _save_cache(database)
    except HTTPError as exc:
        reasons = {401: "The API key was rejected.", 402: "The source reports a subscription or quota requirement.",
                   403: "The source refused the request.", 429: "The source rate limit was reached. Try again later."}
        return SyncResult(existing, reasons.get(exc.code, f"ForeverDB returned HTTP {exc.code}."), existing is not None)
    except (URLError, TimeoutError):
        return SyncResult(existing, "ForeverDB is unreachable. Check your connection and retry.", existing is not None)
    except (OSError, ValueError) as exc:
        return SyncResult(existing, f"Quest data could not be updated: {exc}", existing is not None)
    return SyncResult(database, f"ForeverDB returned {len(observed)} quest records. {unavailable} unavailable.", False)


# Public desktop integration entry points also live here for callers using the UI.
from .installation import (  # noqa: E402
    Installation, discover_installations, inspect_installation, install_addon, publish_route, write_addon_data,
)
