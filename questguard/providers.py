"""Authorized quest-provider interface with a ForeverDB JSON API adapter."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .evidence import (
    CLASSIC,
    FOREVER_SPECIFIC,
    FOREVERDB_SOURCE,
    FOREVERDB_URL,
    STRUCTURED,
    UNVERIFIED,
    clean_text,
    fact_value,
    integer,
    merge_quest_evidence,
    safe_url,
    utc_stamp,
)

FOREVERDB_DOCS = f"{FOREVERDB_URL}/developers"
_QUEST_ENDPOINT = f"{FOREVERDB_URL}/api/v1/quests"
_MAX_RESPONSE = 512 * 1024
_DEFAULT_INTERVAL = 2.1  # Respect the free tier's 30 requests/minute, across every local key.
_MAX_RETRY_WAIT = 15.0


def _not_cancelled() -> bool:
    return False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProviderResult:
    quest_id: int
    quest: dict
    source_version: str


class ProviderError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


class QuestProvider(Protocol):
    id: str
    name: str
    source_url: str

    def lookup(self, quest_id: int, *, cancelled: Callable[[], bool] | None = None) -> ProviderResult:
        ...


def wowhead_reference(quest_id: int) -> str:
    if integer(quest_id) is None:
        raise ValueError("A quest reference requires a positive integer quest ID.")
    return f"https://www.wowhead.com/forever/quest={quest_id}"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # An API key is only sent to the documented endpoint.
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate provider response field.")
        result[key] = value
    return result


def _request(request: Request, timeout: float):
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        payload = response.read(_MAX_RESPONSE + 1)
    if len(payload) > _MAX_RESPONSE:
        raise ValueError("The provider response exceeds the quest lookup size limit.")
    return json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_unique_object)


def _public_quest_url(row: Mapping, quest_id: int) -> str:
    for key in ("guide", "guideUrl", "url"):
        path = row.get(key)
        if isinstance(path, Mapping):
            path = path.get("href") or path.get("url")
        if not isinstance(path, str):
            continue
        url = safe_url(urljoin(FOREVERDB_URL, path))
        if url:
            parsed = urlsplit(url)
            if parsed.hostname == "foreverdb.net" and re.fullmatch(rf"/quest/{quest_id}(?:-[^/]+)?/?", parsed.path):
                return url
    return FOREVERDB_DOCS


def _source(value) -> tuple[str, str | None]:
    label = None
    if isinstance(value, Mapping):
        label = next((text for field in ("label", "kind", "source", "type") if (text := clean_text(value.get(field), 160))), None)
        if not label and (value.get("seenInForever") is True or value.get("observedInForever") is True):
            label = "Seen in Forever"
    elif isinstance(value, str):
        label = clean_text(value, 160)
    if not label:
        return STRUCTURED, None
    marker = re.sub(r"[_-]", " ", label.casefold())
    if "classic" in marker or "cmangos" in marker:
        return CLASSIC, label
    if any(word in marker for word in ("not confirmed", "unconfirmed", "speculation", "not verified", "unverified")):
        return UNVERIFIED, label
    if marker.startswith(("seen in forever", "seen in wow forever", "observed in forever", "observed in wow forever")):
        return FOREVER_SPECIFIC, label
    if marker in {"observed", "forever observed", "seen in game", "forever client", "forever quest map", "forever map marker"}:
        # Only add explicit adapter mappings backed by response docs or a known payload.
        return (FOREVER_SPECIFIC if marker in {"forever observed", "forever client", "forever quest map", "forever map marker"}
                else STRUCTURED), label
    return STRUCTURED, label


def _endpoint_location(raw: Mapping, *, coordinates: bool = True) -> dict:
    location = raw.get("location")
    location = location if isinstance(location, Mapping) else {}
    coord_source = location.get("source") if coordinates else None
    result = {
        "type": raw.get("type"), "id": raw.get("id"), "name": raw.get("name"),
        "zone": location.get("zone") if coordinates else None,
        "mapId": location.get("uiMapId") if coordinates else None,
        "x": location.get("x") if coordinates else None, "y": location.get("y") if coordinates else None,
        "instance": raw.get("instance"), "source": _source(coord_source or raw.get("source"))[1],
    }
    return result


def _endpoints(raw) -> list[Mapping]:
    if isinstance(raw, list):
        return [item for item in raw[:32] if isinstance(item, Mapping)]
    if not isinstance(raw, Mapping):
        return []
    result = [raw]
    if isinstance(raw.get("others"), list):
        result.extend(item for item in raw["others"][:31] if isinstance(item, Mapping))
    return result


def _observed_date(value) -> str | None:
    if isinstance(value, Mapping):
        return utc_stamp(value.get("verifiedAt")) or utc_stamp(value.get("observedAt"))
    return None


def normalize_foreverdb(payload, quest_id: int, retrieved_at: str) -> ProviderResult:
    """Read documented response parts; omissions remain unknown and chains advisory."""
    row = payload.get("data") if isinstance(payload, Mapping) else None
    if (not isinstance(row, Mapping) or integer(row.get("questId", row.get("id"))) != quest_id
            or not clean_text(row.get("name"), 160)):
        raise ValueError("ForeverDB did not return the requested quest with a valid name.")
    meta = payload.get("meta")
    meta = meta if isinstance(meta, Mapping) else {}
    version = next((value for field in ("gameBuild", "build", "version") if type(meta.get(field)) in (str, int)
                    and (value := clean_text(str(meta[field]), 100))), None)
    source_url = _public_quest_url(row, quest_id)
    field_sources = row.get("fieldSources")
    field_sources = field_sources if isinstance(field_sources, Mapping) else {}
    source = row.get("source")
    statements = {}

    def claim(field: str, value, origin, *, method: str = "ForeverDB quest API") -> None:
        value = fact_value(field, value, quest_id)
        if value is None:
            return
        confidence, label = _source(origin)
        result = {"value": value, "provider": FOREVERDB_SOURCE, "sourceUrl": source_url,
                  "retrievedAt": retrieved_at, "method": method, "confidence": confidence,
                  "observedForever": confidence == FOREVER_SPECIFIC and bool(label and "client" not in label.casefold()),
                  "inheritedClassic": confidence == CLASSIC, "guideDerived": False}
        if verified_at := _observed_date(origin):
            result["verifiedAt"] = verified_at
        if version:
            result["gameVersion"] = version
        if label:
            result["sourceLabel"] = label
        statements.setdefault(field, []).append(result)

    for field, value in (
        ("name", row.get("name")), ("zone", row.get("zone")),
        ("questLevel", row.get("questLevel", row.get("level"))), ("minLevel", row.get("minLevel")),
        ("faction", row.get("side")), ("classes", row.get("classes")), ("questLine", row.get("questLine")),
        ("objectives", row.get("objectives")),
    ):
        # A quest-wide Seen in Forever flag does not independently establish
        # all restrictions, objectives, or other facts about that quest.
        origin = field_sources.get(field)
        if origin is None and (field in {"name", "zone", "questLevel", "questLine"} or _source(source)[0] == CLASSIC):
            origin = source
        claim(field, value, origin)

    primary_starts = _endpoints(row.get("starts"))
    for direction, endpoints in (("start", primary_starts), ("end", _endpoints(row.get("ends")))):
        if not endpoints:
            continue
        label = f"{direction}Options"
        claim(label, [_endpoint_location(option) for option in endpoints], source)
        if len(endpoints) == 1:
            endpoint = endpoints[0]
            endpoint_source = endpoint.get("source")
            location = endpoint.get("location")
            coordinate_source = location.get("source") if isinstance(location, Mapping) else None
            if coordinate_source is not None and _source(coordinate_source)[0] != _source(endpoint_source)[0]:
                claim(f"{direction}Location", _endpoint_location(endpoint, coordinates=False), endpoint_source)
            claim(f"{direction}Location", _endpoint_location(endpoint), coordinate_source or endpoint_source)
    for start in primary_starts:
        if start.get("type") == "item" and (ident := integer(start.get("id"))):
            claim("startItem", {"itemId": ident, "name": start.get("name")}, start.get("source"))

    # Only an explicitly unit-qualified, field-attributed duration can qualify.
    # The endpoint docs do not promise a timer, so do not infer one from text.
    timer_source = field_sources.get("timerSeconds") or row.get("timerSource")
    if timer_source is not None:
        claim("timerSeconds", row.get("timerSeconds"), timer_source)
    elif _source(source)[0] == CLASSIC:
        claim("timerSeconds", row.get("timerSeconds"), source)

    edges = []
    chain = row.get("chain")
    if isinstance(chain, Mapping):
        for direction in ("previous", "next"):
            entries = chain.get(direction)
            if not isinstance(entries, list):
                continue
            for entry in entries[:64]:
                if isinstance(entry, Mapping):
                    confidence, label = _source(entry.get("source"))
                    edges.append({"questId": entry.get("questId"), "direction": direction, "name": entry.get("name"),
                                  "relation": entry.get("relation"), "source": label or FOREVERDB_SOURCE,
                                  "confidence": confidence})
    claim("chain", edges, source, method="ForeverDB advisory quest chain")
    quest = merge_quest_evidence(quest_id, None, {"fieldEvidence": statements, "source": FOREVERDB_SOURCE})
    # The row title remains a useful display name even if optional facts are absent.
    return ProviderResult(quest_id, quest, version or "Unknown")


def _cancel(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise ProviderError("cancelled", "Quest verification cancelled.")


class _RequestGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._next = 0.0

    def reserve(self, clock, interval: float) -> float:
        with self._lock:
            now = clock()
            wait = self._next - now
            if wait <= 0:
                self._next = now + interval
            return max(0.0, wait)

    def defer(self, clock, interval: float) -> None:
        with self._lock:
            self._next = max(self._next, clock() + interval)


_LIVE_GATE = _RequestGate()


def _retry_after(headers, now: datetime) -> float | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if not isinstance(raw, str) or len(raw) > 128:
        return None
    raw = raw.strip()
    if raw.isascii() and raw.isdecimal() and len(raw) <= 10:
        return float(raw)
    try:
        target = parsedate_to_datetime(raw)
        target = target if target.tzinfo is not None else target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - now).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def _http_failure(error: HTTPError, wall_now: datetime) -> tuple[ProviderError, bool]:
    messages = {
        401: ("invalid_key", "ForeverDB rejected the API key."),
        402: ("quota", "ForeverDB reports that this account needs a subscription or has reached its quota."),
        403: ("unauthorized", "ForeverDB refused this request. Check that the key is approved for this desktop app."),
        404: ("not_found", "ForeverDB has no record for this quest ID."),
        429: ("rate_limited", "ForeverDB rate-limited the request. The existing cache remains available."),
    }
    kind, message = messages.get(error.code, ("unavailable", f"ForeverDB returned HTTP {error.code}."))
    return ProviderError(kind, message, _retry_after(error.headers, wall_now)), error.code in {408, 429, 500, 502, 503, 504}


class ForeverDBProvider:
    """One authorized GET per quest ID, with bounded retries and no redirects."""

    id = "foreverdb"
    name = FOREVERDB_SOURCE
    source_url = FOREVERDB_URL

    def __init__(
        self, api_key: str, *, authorized: bool = False,
        transport: Callable[[Request, float], Mapping] | None = None,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], datetime] = _utc_now,
    ):
        self._authorized = authorized is True
        self._key = api_key.strip() if isinstance(api_key, str) else ""
        self._transport = transport or _request
        self._gate = _RequestGate() if transport is not None else _LIVE_GATE
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock

    def _pause(self, duration: float, cancelled: Callable[[], bool]) -> None:
        finish = self._clock() + duration
        while (remaining := finish - self._clock()) > 0:
            _cancel(cancelled)
            self._sleep(min(0.2, remaining))

    def lookup(self, quest_id: int, *, cancelled: Callable[[], bool] | None = None) -> ProviderResult:
        if not self._authorized:
            raise ProviderError("unauthorized", "Configure and authorize an approved ForeverDB key before looking up quests.")
        if not self._key or len(self._key) > 256 or any(not 33 <= ord(character) <= 126 for character in self._key):
            raise ProviderError("invalid_key", "Enter a valid ForeverDB API key.")
        if integer(quest_id) is None:
            raise ValueError("A quest lookup requires a positive integer quest ID.")
        cancelled = cancelled or _not_cancelled
        request = Request(f"{_QUEST_ENDPOINT}/{quest_id}", headers={
            "X-API-Key": self._key,
            "User-Agent": f"RXP Quest Guard Desktop/{__version__}",
            "Accept": "application/json",
        }, method="GET")
        for attempt in range(3):
            _cancel(cancelled)
            while (wait := self._gate.reserve(self._clock, _DEFAULT_INTERVAL)) > 0:
                if wait > _MAX_RETRY_WAIT:
                    raise ProviderError("rate_limited", "ForeverDB requested a longer pause. Try later.", wait)
                self._pause(wait, cancelled)
                _cancel(cancelled)
            try:
                payload = self._transport(request, 12)
                _cancel(cancelled)
                result = normalize_foreverdb(payload, quest_id, self._wall_clock().isoformat(timespec="seconds"))
                if len(self._key) >= 8 and self._key in json.dumps(result.quest, ensure_ascii=False):
                    raise ValueError("Response would disclose the credential.")
                return result
            except HTTPError as error:
                failure, retry = _http_failure(error, self._wall_clock())
                error.close()
            except (URLError, TimeoutError, OSError):
                failure, retry = ProviderError("unavailable", "ForeverDB is unreachable or timed out."), True
            except (ValueError, RecursionError):
                raise ProviderError("malformed", "ForeverDB did not return usable quest data.") from None
            delay = max(_DEFAULT_INTERVAL * 2**attempt, failure.retry_after or 0)
            if retry:
                self._gate.defer(self._clock, delay)
            if not retry or attempt >= 2 or delay > _MAX_RETRY_WAIT:
                raise failure from None
        raise AssertionError("The bounded retry loop must return or raise.")
