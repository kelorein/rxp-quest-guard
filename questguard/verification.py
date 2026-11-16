"""Select, cache and apply independent quest evidence without sending character data."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from threading import RLock

from .engine import _condition, match_current_guide, resolve_current_step
from .evidence import FOREVER_SPECIFIC, UNVERIFIED, VERIFIED_GAME, build_matches
from .sync import Database, MAX_QUEST_ID, _read_json, atomic_write, cache_directory, timestamp


CACHE_SCHEMA = 2
MAX_CACHE_RECORDS = 25_000
MAX_EVIDENCE_ROW = 96 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
_cache_lock = RLock()
_objectives = {"complete", "turnin", "collect"}
_missing_categories = {"missing_pickup", "unaccepted_objective", "optional_missing"}
_unknown_categories = {"prerequisites_unknown", "prerequisites_review", "pickup_unknown", "earlier_pickup"}
_version_sensitive = {"requirements", "requirementGroups", "minLevel", "faction", "classes", "classification",
                      "startItem", "timerSeconds", "startLocation", "endLocation", "chain", "objectives"}


@dataclass(frozen=True)
class LookupSettings:
    verify_unknown: bool = True
    verify_missing: bool = True
    prefetch: bool = True
    chapters: int = 2
    prefer_cache: bool = True
    refresh_outdated: bool = True
    facts_days: int = 30
    dependencies_days: int = 14


@dataclass(frozen=True)
class LookupCandidate:
    quest_id: int
    reason: str
    chapter: str
    priority: int


@dataclass(frozen=True)
class CacheRecord:
    quest: dict
    source_version: str
    retrieved_at: str
    client_version: str | None = None
    guide_version: str | None = None


@dataclass(frozen=True)
class VerificationCache:
    records: dict[str, dict[int, CacheRecord]] = field(default_factory=dict)
    failures: dict[str, dict[int, dict]] = field(default_factory=dict)

    @property
    def quest_ids(self) -> set[int]:
        return {quest_id for rows in self.records.values() for quest_id in rows}

    @property
    def updated_at(self) -> str | None:
        return max((row.retrieved_at for rows in self.records.values() for row in rows.values()), default=None)


@dataclass(frozen=True)
class VerificationRun:
    cache: VerificationCache
    fetched: int
    errors: dict[int, str]
    cancelled: bool
    message: str


def _valid_id(value) -> bool:
    return type(value) is int and 1 <= value <= MAX_QUEST_ID


def quest_reference(quest_id: int) -> str:
    from .providers import wowhead_reference

    return wowhead_reference(quest_id)


def _date(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _cache_path():
    return cache_directory() / "verification-v2.json"


def _identifier(raw, *, allow_global=False) -> int:
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise ValueError("Invalid quest ID in the verification cache")
    value = int(raw)
    if str(value) != raw or not (_valid_id(value) or allow_global and value == 0):
        raise ValueError("Invalid quest ID in the verification cache")
    return value


def _provider_key(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 40 and value.isascii() and value.replace("-", "").replace("_", "").isalnum()


def load_verification_cache() -> VerificationCache:
    """Leave any schema-1 manual/legacy quest cache in place as the offline base."""
    try:
        with _cache_path().open("rb") as file:
            data = _read_json(file)
    except FileNotFoundError:
        return VerificationCache()
    if not isinstance(data, dict) or data.get("schemaVersion") != CACHE_SCHEMA:
        raise ValueError("Unsupported quest verification cache format")
    records, failures = {}, {}
    for section in ("records", "failures"):
        providers = data.get(section, {})
        if not isinstance(providers, dict) or len(providers) > 32:
            raise ValueError("Invalid verification provider list")
        for provider, rows in providers.items():
            if not _provider_key(provider) or not isinstance(rows, dict) or len(rows) > MAX_CACHE_RECORDS:
                raise ValueError("Invalid verification cache records")
            target = records if section == "records" else failures
            target[provider] = {}
            for raw_id, row in rows.items():
                quest_id = _identifier(raw_id, allow_global=section == "failures")
                if not isinstance(row, dict):
                    raise ValueError("Invalid verification cache row")
                if section == "records":
                    if (not isinstance(row.get("quest"), dict) or not isinstance(row.get("sourceVersion"), str)
                            or not _date(row.get("retrievedAt"))):
                        raise ValueError("Invalid verified quest row")
                    target[provider][quest_id] = CacheRecord(row["quest"], row["sourceVersion"], row["retrievedAt"],
                                                            row.get("clientVersion"), row.get("guideVersion"))
                elif _date(row.get("until")) and isinstance(row.get("message"), str) and isinstance(row.get("kind"), str):
                    target[provider][quest_id] = row
    return VerificationCache(records, failures)


def _save(cache: VerificationCache) -> None:
    records = {provider: {quest_id: {"quest": row.quest, "sourceVersion": row.source_version,
                                    "retrievedAt": row.retrieved_at, "clientVersion": row.client_version,
                                    "guideVersion": row.guide_version} for quest_id, row in rows.items()}
               for provider, rows in cache.records.items()}
    payload = json.dumps({"schemaVersion": CACHE_SCHEMA, "records": records, "failures": cache.failures},
                         ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_CACHE_BYTES:
        raise ValueError("The verified quest cache is too large to save")
    atomic_write(_cache_path(), payload)


def _alert_cooldown(kind: str, retry_after) -> timedelta:
    if isinstance(retry_after, (int, float)) and 0 <= retry_after <= 86_400:
        return timedelta(seconds=max(1, retry_after))
    return {
        "invalid_key": timedelta(minutes=15), "unauthorized": timedelta(minutes=15),
        "quota": timedelta(hours=8), "rate_limited": timedelta(minutes=1),
        "not_found": timedelta(days=1), "malformed": timedelta(hours=2),
    }.get(kind, timedelta(minutes=5))


def _blocked(cache: VerificationCache, provider: str, quest_id: int, now: datetime, *, manual: bool) -> dict | None:
    rows = cache.failures.get(provider, {})
    for key in (0, quest_id):
        row = rows.get(key)
        if (row and (until := _date(row.get("until"))) and until > now
                and (not manual or row["kind"] in {"rate_limited", "quota"})):
            return row
    return None


def _normalized_version(value) -> str | None:
    if not isinstance(value, str) or not value.strip() or value.strip().casefold() == "unknown":
        return None
    return value.strip().lower().removeprefix("v").replace("_", ".")


def _changed(known, observed) -> bool:
    left, right = _normalized_version(known), _normalized_version(observed)
    if not left or not right:
        return False
    if left.isdecimal() and right.endswith("." + left) or right.isdecimal() and left.endswith("." + right):
        return False
    return left != right


def _fresh(row: CacheRecord, now: datetime, settings: LookupSettings, dependency: bool,
           game_version: str | None, guide_version: str | None) -> bool:
    retrieved = _date(row.retrieved_at)
    if not retrieved or retrieved > now + timedelta(minutes=5):
        return False
    if settings.refresh_outdated and (_changed(row.client_version, game_version)
                                      or dependency and _changed(row.guide_version, guide_version)):
        return False
    days = settings.dependencies_days if dependency else settings.facts_days
    # Even when the user prefers online data, saves arriving together do not re-request an unchanged quest.
    ttl = timedelta(days=max(1, days)) if settings.prefer_cache else timedelta(hours=1)
    return now - retrieved < ttl


def _guide_position(route, snapshot, chapter_id):
    planned = tuple(route.planned_guides) if route is not None else ()
    if not planned:
        return planned, 0, None
    current = match_current_guide(planned, snapshot) if snapshot else None
    scope = next((i for i, guide in enumerate(planned) if guide.id == chapter_id), None)
    if scope is None and current:
        scope = planned.index(current)
    return planned, scope or 0, current


def _usable_positions(guide, snapshot, current):
    first = 1
    if snapshot is not None and guide is current:
        first = resolve_current_step(guide, snapshot).offline_step or 1
    return (step for step in guide.steps if step.number >= first)


def _location_discrepancy(guide, action, row):
    point, online = action.location, row.get("startLocation")
    if action.kind != "accept" or action.optional or action.uncertain or not point or not isinstance(online, Mapping):
        return None
    if not point.map_id or point.map_id != online.get("mapId") or point.x is None or point.y is None:
        return None
    x, y = online.get("x"), online.get("y")
    if type(x) not in (int, float) or type(y) not in (int, float):
        return None
    # Map percentages under two points apart can be the same moving NPC or a marker offset.
    if max(abs(x - point.x), abs(y - point.y)) < 2:
        return None
    claims = row.get("fieldEvidence", {}).get("startLocation", ())
    if not isinstance(claims, (tuple, list)):
        return None
    for claim in claims:
        confidence = claim.get("confidence") if isinstance(claim, Mapping) and claim.get("value") == online else None
        if confidence in {VERIFIED_GAME, FOREVER_SPECIFIC}:
            exact = confidence == VERIFIED_GAME and claim.get("observedForever") is True and not action.condition
            state = "Confirmed guide inconsistency" if exact else "Possible guide inconsistency"
            return state, (f"{guide.name} gives {point.x:g}, {point.y:g}; the same map's source gives {x:g}, {y:g}. "
                           "Check the pickup in-game before traveling.")
    return None


def guide_discrepancies(route, quests: Mapping[int, dict]) -> dict[int, tuple[str, str]]:
    """Compare only explicit guide and source coordinates on the same UI map."""
    result = {}
    for guide in route.planned_guides if route else ():
        for step in guide.steps:
            if step.optional or step.uncertain or step.condition:
                continue
            for action in step.actions:
                if action.quest_id not in result and (difference := _location_discrepancy(
                    guide, action, quests.get(action.quest_id, {}),
                )):
                    result[action.quest_id] = difference
    return result


def plan_verification(route, snapshot, audit=None, *, mode: str = "smart", quest_id: int | None = None,
                      chapter_id: str | None = None, settings: LookupSettings | None = None,
                      cache: VerificationCache | None = None, game_version: str | None = None,
                      guide_version: str | None = None) -> tuple[LookupCandidate, ...]:
    """Build a deduplicated queue only from locally selected guide and finding data."""
    settings = settings or LookupSettings()
    cache = cache if cache is not None else load_verification_cache()
    if mode == "selected":
        if not _valid_id(quest_id):
            raise ValueError("Select a valid quest before requesting verification")
        return (LookupCandidate(quest_id, "Explicit quest verification", "Selected quest", 0),)
    if mode not in {"smart", "chapter", "prefetch", "full"}:
        raise ValueError("Unknown verification scope")
    planned, position, current = _guide_position(route, snapshot, chapter_id)
    if not planned:
        return ()
    upcoming = max(0, min(10, settings.chapters))
    if mode == "full":
        # Use the linked path, including chapters already traversed at or after 43.
        chapters = [guide for guide in route.guides if guide.level_min is not None and 43 <= guide.level_min < 60]
    elif mode == "prefetch":
        chapters = planned[position + 1:position + upcoming + 1]
    elif mode == "chapter" or not settings.prefetch:
        chapters = planned[position:position + 1]
    else:
        chapters = planned[position:position + upcoming + 1]

    now = datetime.now(UTC)
    auto = mode == "smart"
    active = snapshot.active if snapshot and snapshot.active is not None else ()
    completed = snapshot.completed if snapshot and snapshot.completed is not None else ()
    candidates = {}
    selected_ids = {guide.id for guide in chapters}
    titles = {guide.id: guide.name for guide in chapters}
    pickups = {}
    for guide in route.guides:
        for step in guide.steps:
            if _condition(step.condition, route) is False:
                continue
            for action in step.actions:
                if (action.kind == "accept" and not step.optional and not action.optional
                        and _condition(action.condition, route) is not False):
                    pickups.setdefault(action.quest_id, guide.id)

    def add(id_, reason, chapter, priority, dependency=True):
        if not _valid_id(id_) or auto and (id_ in active or id_ in completed):
            return
        existing = candidates.get(id_)
        if existing and existing.priority <= priority:
            return
        if mode != "full" and any(_fresh(row, now, settings, dependency, game_version, guide_version)
                                  for rows in cache.records.values() if (row := rows.get(id_)) is not None):
            return
        candidates[id_] = LookupCandidate(id_, reason, chapter, priority)

    if audit is not None and mode in {"smart", "chapter"}:
        for finding in audit.findings:
            if finding.chapter_id not in selected_ids:
                continue
            chapter = titles[finding.chapter_id]
            if finding.category in _missing_categories and settings.verify_missing:
                add(finding.quest_id, "Investigate a suspected missing pickup or starting item", chapter, 0)
            elif (finding.category in _unknown_categories or finding.requirements) and settings.verify_unknown:
                add(finding.quest_id, "Investigate an unresolved prerequisite", chapter, 1)

    for guide in chapters:
        steps = guide.steps if mode in {"full", "chapter"} else _usable_positions(guide, snapshot, current)
        for step in steps:
            if _condition(step.condition, route) is False:
                continue
            for action in step.actions:
                if _condition(action.condition, route) is False:
                    continue
                if action.kind not in _objectives and action.kind != "accept":
                    continue
                cross = pickups.get(action.quest_id) not in (None, guide.id)
                current_chapter = guide.id == planned[position].id
                deliberate = mode != "smart"
                if deliberate or (not current_chapter and settings.prefetch) or (current_chapter and settings.verify_unknown):
                    if action.kind in _objectives:
                        add(action.quest_id, "Cross-chapter dependency" if cross else "Quest objective in the selected route",
                            guide.name, 2 if cross else 3, dependency=cross or current_chapter)
                    elif deliberate or current_chapter:
                        add(action.quest_id, "Check pickup prerequisites", guide.name, 4)
                if action.kind == "accept" and settings.verify_unknown and not step.optional and not step.uncertain:
                    for rows in cache.records.values():
                        row = rows.get(action.quest_id)
                        if row and _location_discrepancy(guide, action, row.quest):
                            add(action.quest_id, "Investigate a guide and provider pickup discrepancy", guide.name, 1)
                            break
    return tuple(sorted(candidates.values(), key=lambda candidate: (candidate.priority, candidate.quest_id)))


def _provider_error(kind: str) -> str:
    return {
        "invalid_key": "ForeverDB rejected the API key. Reauthorize with an approved key.",
        "unauthorized": "The provider requires explicit authorization.",
        "rate_limited": "The provider asked Quest Guard to wait before retrying.",
        "quota": "The provider's account quota is unavailable or exhausted.",
        "not_found": "The provider has no record for this quest.",
        "malformed": "The provider returned unusable quest metadata.",
        "cancelled": "Verification was cancelled.",
    }.get(kind, "The online source is temporarily unavailable. Cached evidence is retained.")


def verify_quests(candidates: Iterable[LookupCandidate], providers, *, game_version: str | None = None,
                  guide_version: str | None = None, progress: Callable[[str], None] | None = None,
                  cancelled: Callable[[], bool] | None = None) -> VerificationRun:
    from .providers import ProviderError

    existing = load_verification_cache()
    unique = {item.quest_id: item for item in reversed(tuple(candidates)) if _valid_id(item.quest_id)}
    queue = sorted(unique.values(), key=lambda item: (item.priority, item.quest_id))
    providers = tuple(providers)
    if not providers or not queue:
        return VerificationRun(existing, 0, {}, False,
                               "No authorized provider is configured." if not providers else "No lookups needed for this scope.")
    successful, failed, errors, stopped = {}, {}, {}, {}
    was_cancelled = False
    for index, candidate in enumerate(queue, 1):
        if cancelled and cancelled():
            was_cancelled = True
            break
        quest_id = candidate.quest_id
        if progress:
            progress(f"Verifying quest {index} of {len(queue)} (ID {quest_id})")
        manual = candidate.priority == 0 and candidate.reason == "Explicit quest verification"
        for provider in providers:
            if not _provider_key(provider.id):
                raise ValueError("Invalid quest provider identifier")
            now = datetime.now(UTC)
            if provider.id in stopped:
                errors.setdefault(quest_id, stopped[provider.id])
                continue
            if hold := _blocked(existing, provider.id, quest_id, now, manual=manual):
                errors.setdefault(quest_id, hold["message"])
                continue
            try:
                result = provider.lookup(quest_id, cancelled=cancelled)
                if (result.quest_id != quest_id or not isinstance(result.quest, dict) or not result.quest
                        or len(json.dumps(result.quest, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_EVIDENCE_ROW):
                    raise ProviderError("malformed", "Invalid normalized quest record")
            except (TypeError, ValueError, OverflowError, ProviderError) as exc:
                kind = getattr(exc, "kind", "malformed")
                if kind == "cancelled":
                    was_cancelled = True
                    break
                message = _provider_error(kind)
                errors[quest_id] = message
                global_failure = kind in {"invalid_key", "unauthorized", "quota", "rate_limited", "unavailable"}
                if global_failure:
                    stopped[provider.id] = message
                until = now + _alert_cooldown(kind, getattr(exc, "retry_after", None))
                failed[provider.id, 0 if global_failure else quest_id] = {
                    "kind": kind, "message": message, "until": until.isoformat(timespec="seconds"),
                }
                continue
            successful[provider.id, quest_id] = CacheRecord(result.quest, result.source_version or "Unknown", timestamp(),
                                                           game_version, guide_version)
        if was_cancelled:
            break
    if successful or failed:
        with _cache_lock:
            cache = load_verification_cache()
            records = {provider: dict(rows) for provider, rows in cache.records.items()}
            failures = {provider: dict(rows) for provider, rows in cache.failures.items()}
            for (provider, id_), row in failed.items():
                failures.setdefault(provider, {})[id_] = row
            for (provider, id_), row in successful.items():
                records.setdefault(provider, {})[id_] = row
                failures.setdefault(provider, {}).pop(id_, None)
                if (provider, 0) not in failed:
                    failures[provider].pop(0, None)
            cache = VerificationCache(records, failures)
            _save(cache)
    else:
        cache = existing
    verified_ids = {id_ for _, id_ in successful}
    errors = {id_: message for id_, message in errors.items() if id_ not in verified_ids}
    message = f"{len(verified_ids)} quests refreshed · {len(errors)} unavailable or deferred."
    if was_cancelled:
        message = "Verification cancelled. " + message
    return VerificationRun(cache, len(verified_ids), errors, was_cancelled, message)


def verified_database(base: Database | None = None, *, cache: VerificationCache | None = None,
                      game_version: str | None = None, guide_version: str | None = None,
                      settings: LookupSettings | None = None) -> Database | None:
    from .providers import merge_quest_evidence

    cache = cache if cache is not None else load_verification_cache()
    settings = settings or LookupSettings()
    now = datetime.now(UTC)
    quests = {quest_id: merge_quest_evidence(quest_id, row, None, game_version=game_version)
              if row.get("source") == "ForeverDB.net" or row.get("fieldEvidence") else row
              for quest_id, row in base.quests.items()} if base else {}
    if not cache.quest_ids:
        if base and quests != base.quests:
            return Database(quests, base.source, base.source_version, base.updated_at)
        return base
    versions = set()
    sources = set()
    for rows in cache.records.values():
        for quest_id, row in rows.items():
            incoming = row.quest
            build_status = build_matches(game_version, row.source_version)
            stale_build = build_status is False or build_status is None and _changed(row.client_version, game_version)
            retrieved = _date(row.retrieved_at)
            stale_dependencies = retrieved is None or now - retrieved > timedelta(days=max(1, settings.dependencies_days))
            if stale_build or stale_dependencies:
                incoming = {key: value for key, value in row.quest.items() if key not in _version_sensitive}
                evidence = row.quest.get("fieldEvidence", {})
                if isinstance(evidence, dict):
                    incoming["fieldEvidence"] = {
                        field: [{**claim, "confidence": UNVERIFIED, "observedForever": False,
                                 "sourceLabel": "Cached for another build" if stale_build else "Cached evidence needs refresh"}
                                if field in _version_sensitive and isinstance(claim, dict) else claim for claim in claims]
                        for field, claims in evidence.items() if isinstance(claims, (tuple, list))
                    }
            current = quests.get(quest_id, {})
            quests[quest_id] = merge_quest_evidence(quest_id, current, incoming, game_version=game_version)
            source = row.quest.get("source")
            if isinstance(source, str) and source:
                sources.add(source)
            if row.source_version != "Unknown":
                versions.add(row.source_version)
    source = ", ".join(sorted(sources)) or "Verified source cache"
    if base:
        source = "Local data + " + source
    version = next(iter(versions)) if len(versions) == 1 else "Multiple builds" if versions else "Unknown"
    return Database(quests, source, version, cache.updated_at or (base.updated_at if base else "Unknown"))


def verification_state(quest_id: int, snapshot, meta: Mapping, finding=None, *,
                       error: str | None = None, pending: bool = False, planned: bool = False,
                       discrepancy: tuple[str, str] | None = None) -> tuple[str, str]:
    if snapshot:
        if snapshot.active is not None and quest_id in snapshot.active:
            if snapshot.ready is not None and quest_id in snapshot.ready:
                return "Ready to turn in", "Check the live quest log before turning in."
            return ("Verified active" if snapshot.freshness == "fresh" else "Active in last save",
                    "Follow the live quest objective.")
        if snapshot.completed is not None and quest_id in snapshot.completed:
            return ("Verified completed" if snapshot.freshness == "fresh" else "Completed in last save",
                    "The saved quest state satisfies this objective.")
    if any(isinstance(conflict, Mapping) and conflict.get("unresolved") is not False for conflict in meta.get("conflicts", ())):
        return "Conflicting sources", "Review the competing claims before traveling or starting a quest."
    category = getattr(finding, "category", "")
    if category in {"missing_pickup", "unaccepted_objective"}:
        return ("Missing earlier pickup" if finding.pickup_guide else "Unaccepted objective", finding.detail)
    if category == "prerequisites_missing":
        return "Missing prerequisite", finding.detail
    if category in {"ineligible", "not_eligible", "not_yet_eligible"}:
        return "Not yet eligible", finding.detail
    if category.startswith("optional") or meta.get("classification") in {"optional", "breadcrumb"}:
        return "Optional quest", "Handle this only if you choose to follow that branch."
    if discrepancy:
        return discrepancy
    if planned:
        return "Future planned quest", "Follow the scheduled pickup shown by the guide."
    if pending:
        return "Awaiting online verification", "Keep using the currently published data in WoW."
    if error:
        return "Online source unavailable", error
    if meta.get("startItem"):
        return "Check starting item in WoW", "Quest Guard checks your carried bags in-game before recommending activation."
    if category in _unknown_categories or category in {"conditional_pickup", "quest_state_stale", "quest_state_unknown"}:
        return "Insufficient information", finding.detail
    if meta.get("fieldEvidence"):
        return "Evidence available", "Inspect the source claims and compare them with your live quest log."
    return "Insufficient information", "Verify this quest or open its reference page."
