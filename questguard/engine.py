"""Conservative quest and route projection over an imported character snapshot."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .ingest import Guide, GuideCollection, GuideLocation, Guard, QuestAction, Snapshot, Step


Truth = bool | None
_CLASSES = frozenset({
    "deathknight", "demonhunter", "druid", "evoker", "hunter", "mage", "monk", "paladin",
    "priest", "rogue", "shaman", "warlock", "warrior",
})
_RACES = frozenset({
    "bloodelf", "darkirondwarf", "draenei", "dwarf", "gnome", "goblin", "human", "kul'tiran",
    "nightelf", "orc", "pandaren", "skyborne", "tauren", "troll", "undead", "worgen",
})
_FACTIONS = frozenset({"horde", "alliance", "neutral"})
_CLIENTS = frozenset({"classic", "forever", "tbc", "wotlk", "cata", "mists", "sod", "retail", "df"})
_OBJECTIVES = frozenset({"complete", "turnin", "collect"})
_SEVERITIES = {"blocker": 0, "warning": 1, "review": 2, "info": 3}
_CONDITION_TOKEN = re.compile(r"[A-Za-z0-9]+|[!()/]|[^\s]", re.ASCII)


@dataclass(frozen=True)
class Route:
    guides: tuple[Guide, ...]
    diagnostics: tuple[str, ...] = ()
    faction: str | None = None
    character_class: str | None = None
    race: str | None = None
    active_id: str | None = None

    @property
    def planned_guides(self) -> tuple[Guide, ...]:
        index = next((i for i, guide in enumerate(self.guides) if guide.id == self.active_id), 0)
        return self.guides[index:]

    @property
    def history_guides(self) -> tuple[Guide, ...]:
        return self.guides[:len(self.guides) - len(self.planned_guides)]


@dataclass(frozen=True)
class StepResolution:
    runtime_step: int | None
    offline_step: int | None
    stable_step_id: int | None
    exact: bool


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    title: str
    detail: str
    quest_id: int
    chapter_id: str
    step: int
    pickup_guide: str | None = None
    pickup_step: int | None = None
    classification: str = "unknown"
    confidence: str = "unknown"
    source: str = "RestedXP guide"
    requirements: tuple[int, ...] = ()
    pickup_location: GuideLocation | None = None


@dataclass(frozen=True)
class ChapterSummary:
    guide: Guide
    findings: tuple[Finding, ...]
    first_step: int
    last_step: int
    active_quests: tuple[int, ...] = ()
    completed_quests: tuple[int, ...] = ()
    planned_quests: tuple[int, ...] = ()
    unknown_quests: tuple[int, ...] = ()
    unguarded_steps: tuple[int, ...] = ()
    quest_names: Mapping[int, str] = field(default_factory=dict)
    optional_quests: tuple[int, ...] = ()


@dataclass(frozen=True)
class AuditCounts:
    confirmed_blockers: int
    missing_pickups: int
    optional: int
    reviews: int
    future_planned: int
    satisfied: int
    historical: int
    unaccepted_objectives: int


@dataclass(frozen=True)
class Audit:
    chapters: tuple[ChapterSummary, ...]
    findings: tuple[Finding, ...]
    diagnostics: tuple[str, ...] = ()
    historical_findings: tuple[Finding, ...] = ()

    @property
    def counts(self) -> AuditCounts:
        satisfied = {
            quest_id for chapter in self.chapters for quest_id in (*chapter.active_quests, *chapter.completed_quests)
        }
        future = {quest_id for chapter in self.chapters for quest_id in chapter.planned_quests} - satisfied
        return AuditCounts(
            sum(item.severity == "blocker" and item.category == "prerequisites_missing" for item in self.findings),
            sum(item.category == "missing_pickup" for item in self.findings),
            sum(item.classification in {"optional", "breadcrumb"} and item.severity == "info" for item in self.findings),
            sum(item.severity == "review" for item in self.findings),
            len(future), len(satisfied), len(self.historical_findings),
            sum(item.category == "unaccepted_objective" for item in self.findings),
        )

    def render(self) -> str:
        lines = ["RXP Quest Guard — Route preflight", ""]
        if self.diagnostics:
            lines.extend(self.diagnostics)
            lines.append("")
        counts = self.counts
        lines.append(
            f"{len(self.chapters)} chapters · {counts.confirmed_blockers} confirmed prerequisite blockers · "
            f"{counts.missing_pickups} confirmed missing pickups · {counts.optional} optional findings · {counts.reviews} to review"
        )
        lines.append(f"{counts.future_planned} future planned · {counts.satisfied} satisfied in the saved state")
        if counts.unaccepted_objectives:
            lines.append(f"{counts.unaccepted_objectives} current objectives with unaccepted quests and no confirmed earlier pickup")
        for chapter in self.chapters:
            lines.extend((
                "",
                f"{chapter.guide.name} — {chapter.guide.subgroup or chapter.guide.group}",
                f"Steps {chapter.first_step}–{chapter.last_step} · {len(chapter.active_quests)} active · "
                f"{len(chapter.completed_quests)} completed · {len(chapter.planned_quests)} planned · "
                f"{len(chapter.optional_quests)} optional or breadcrumb · "
                f"{len(chapter.unknown_quests)} without quest metadata · "
                f"{len(chapter.unguarded_steps)} objective steps with no recognized active guard",
            ))
            if chapter.active_quests:
                lines.append("  Active in snapshot: " + ", ".join(
                    f"{chapter.quest_names[quest_id]} ({quest_id})" if quest_id in chapter.quest_names else str(quest_id)
                    for quest_id in chapter.active_quests
                ) + ".")
            for item in sorted(chapter.findings, key=lambda value: (_SEVERITIES[value.severity], value.step)):
                lines.append(f"  [{item.severity.upper()}] Step {item.step}, {item.title} ({item.quest_id})")
                lines.append(f"    {item.detail}")
                if item.pickup_guide:
                    lines.append(f"    Pickup in {item.pickup_guide}, step {item.pickup_step}.")
                location = format_location(item.pickup_location)
                if location:
                    lines.append(f"    Pickup location: {location}.")
                if item.requirements:
                    lines.append("    Prerequisite IDs: " + ", ".join(map(str, item.requirements)) + ".")
                lines.append(f"    Classification {item.classification} · confidence {item.confidence} · {item.source}")
        if self.historical_findings:
            lines.extend(("", "Historical pickup findings"))
            for item in self.historical_findings:
                lines.append(f"  Step {item.step}, {item.title} ({item.quest_id})")
                lines.append(f"    {item.detail}")
                if item.pickup_guide:
                    lines.append(f"    Pickup in {item.pickup_guide}, step {item.pickup_step}.")
        return "\n".join(lines) + "\n"


def format_location(location: GuideLocation | None) -> str:
    if not location:
        return ""
    place = location.zone or (f"map {location.map_id}" if location.map_id else "")
    if location.world:
        map_id, floor, x, y = location.world
        place = f"map/floor {map_id}/{floor}, world coordinates {x:g}, {y:g}"
    elif location.x is not None and location.y is not None:
        place = f"{place} ({location.x:g}%, {location.y:g}%)".strip()
    return ", ".join(filter(None, (location.name, place)))


def _all(values) -> Truth:
    unknown = False
    for value in values:
        if value is False:
            return False
        unknown |= value is None
    return None if unknown else True


def _any(values) -> Truth:
    unknown = False
    for value in values:
        if value is True:
            return True
        unknown |= value is None
    return None if unknown else False


def _not(value: Truth) -> Truth:
    return None if value is None else not value


def _normalized(value: str | None) -> str | None:
    return re.sub(r"[\s_-]", "", value).casefold() if value else None


def _condition(text: str, route: Route) -> Truth:
    if not text:
        return True
    if len(text) > 512:
        return None
    faction, character_class, race = map(_normalized, (route.faction, route.character_class, route.race))
    tokens = _CONDITION_TOKEN.findall(text.casefold())
    cursor = 0

    def term(subject: str) -> Truth:
        subject = {"dk": "deathknight", "scourge": "undead"}.get(subject, subject)
        if subject in _FACTIONS:
            return faction == subject if faction else None
        if subject in _CLASSES:
            return character_class == subject if character_class else None
        if subject in _RACES:
            return race == subject if race else None
        if subject in _CLIENTS:
            return subject == "forever"
        return None

    def read(depth: int) -> Truth:
        nonlocal cursor
        if depth > 16:
            raise ValueError("Guide condition nesting limit reached.")
        alternatives = []
        terms = []
        while cursor < len(tokens) and tokens[cursor] != ")":
            token = tokens[cursor]
            cursor += 1
            if token == "/":
                if not terms:
                    raise ValueError("Guide condition has an empty branch.")
                alternatives.append(_all(terms))
                terms = []
                continue
            reverse = token == "!"
            if reverse:
                if cursor >= len(tokens):
                    raise ValueError("Guide condition has an incomplete negation.")
                token = tokens[cursor]
                cursor += 1
            if token == "(":
                match = read(depth + 1)
                if cursor >= len(tokens) or tokens[cursor] != ")":
                    raise ValueError("Guide condition has an unclosed group.")
                cursor += 1
            elif token.isalnum():
                match = term(token)
            else:
                raise ValueError("Guide condition token is unsupported.")
            terms.append(_not(match) if reverse else match)
        if not terms:
            raise ValueError("Guide condition has an empty branch.")
        return _any((*alternatives, _all(terms)))

    try:
        match = read(0)
    except ValueError:
        return None
    return match if cursor == len(tokens) else None


def _eligible(guide: Guide, route: Route) -> Truth:
    tagged_clients = set(guide.variants) & _CLIENTS
    if tagged_clients and not tagged_clients.intersection({"classic", "forever"}):
        return False
    if guide.faction and route.faction and guide.faction.casefold() != route.faction.casefold():
        return False
    return _condition(guide.condition, route)


def match_current_guide(guides: Sequence[Guide] | GuideCollection, snapshot: Snapshot | None) -> Guide | None:
    if not snapshot:
        return None
    if isinstance(guides, GuideCollection):
        guides = guides.guides
    if snapshot.current_guide_key:
        key_matches = [guide for guide in guides if guide.id.casefold() == snapshot.current_guide_key.casefold()]
        if len(key_matches) == 1:
            return key_matches[0]
    if not snapshot.current_guide:
        return None
    matches = [
        guide for guide in guides
        if guide.name.casefold() == snapshot.current_guide.casefold()
        and (not snapshot.current_group or guide.group.casefold() == snapshot.current_group.casefold())
        and (snapshot.current_subgroup is None or guide.subgroup.casefold() == snapshot.current_subgroup.casefold())
    ]
    if len(matches) == 1:
        return matches[0]
    if snapshot.current_step_id is not None:
        exact = [guide for guide in matches if any(step.step_id == snapshot.current_step_id for step in guide.steps)]
        if len(exact) == 1:
            return exact[0]
    return None


def match_current_step(guide: Guide, snapshot: Snapshot) -> int | None:
    """Return the expanded local ordinal for an unambiguous saved step ID."""
    if snapshot.current_step_id is None:
        return None
    matches = [step.number for step in guide.steps if step.step_id == snapshot.current_step_id]
    return matches[0] if len(matches) == 1 else None


def resolve_current_step(guide: Guide, snapshot: Snapshot) -> StepResolution:
    exact_step = match_current_step(guide, snapshot)
    approximation = snapshot.current_step if snapshot.current_step and 1 <= snapshot.current_step <= len(guide.steps) else None
    return StepResolution(snapshot.current_step, exact_step or approximation, snapshot.current_step_id, exact_step is not None)


def build_route(
    guides: Sequence[Guide] | GuideCollection, start_id: str | None = None, *,
    faction: str | None = None, character_class: str | None = None, race: str | None = None,
    current_id: str | None = None,
) -> Route:
    diagnostics = list(guides.diagnostics) if isinstance(guides, GuideCollection) else []
    catalog = guides.guides if isinstance(guides, GuideCollection) else guides
    requested = start_id or current_id
    selected = next((guide for guide in catalog if guide.id == requested), None)
    if not faction and selected and selected.faction:
        faction = selected.faction
        diagnostics.append(f"Using {faction} guide filters based on the selected chapter.")
    context = Route((), (), faction, character_class, race)
    by_id = {guide.id: guide for guide in catalog if _eligible(guide, context) is not False}
    names = defaultdict(list)
    for guide in by_id.values():
        names[guide.group.casefold(), guide.name.casefold()].append(guide)

    def resolve(guide: Guide, text: str) -> tuple[Guide, ...]:
        group, sep, name = text.rpartition("\\")
        return tuple(names[(group if sep else guide.group).casefold(), name.strip().casefold()])

    def next_guide(guide: Guide) -> tuple[Guide | None, str | None]:
        applicable = []
        for transition in guide.next:
            condition = _condition(transition.condition, context)
            if condition is False:
                continue
            for name in (transition.target, *transition.alternatives):
                matches = resolve(guide, name)
                if len(matches) == 1:
                    choice = matches[0]
                    applicability = _all((condition, _eligible(choice, context)))
                    applicable.append((choice, applicability))
                    if applicability is True:
                        break
                elif len(matches) > 1:
                    same_subgroup = [choice for choice in matches if choice.subgroup == guide.subgroup]
                    if len(same_subgroup) != 1:
                        return None, f"{guide.name} has multiple installed copies of its next chapter."
                    applicable.append((same_subgroup[0], condition))
                    if condition is True:
                        break
        unique = {choice.id: choice for choice, _ in applicable}
        if len(unique) == 1 and any(condition is True for _, condition in applicable):
            return next(iter(unique.values())), None
        if unique:
            return None, f"{guide.name} has unresolved route branches. Select the next chapter and set faction, class or race when known."
        if guide.next:
            return None, f"No compatible next chapter was found after {guide.name}."
        return None, None

    successors = {}
    problems = {}
    incoming = defaultdict(list)
    for guide in by_id.values():
        following, problem = next_guide(guide)
        successors[guide.id] = following
        if following:
            incoming[following.id].append(guide)
        if problem:
            problems[guide.id] = problem

    selected_id = start_id or current_id
    seed = by_id.get(selected_id) if selected_id else None
    if selected_id and seed is None:
        diagnostics.append("The selected starting chapter is unavailable for the selected route filters.")
    if not selected_id and by_id:
        roots = [guide for guide in by_id.values() if guide.id not in incoming] or list(by_id.values())
        roots.sort(key=lambda guide: (guide.level_min is None, guide.level_min or 0, guide.group, guide.name))
        seed = roots[0]
        if len(roots) > 1:
            diagnostics.append(f"Multiple route entry points were found. Starting with {seed.name}.")
    if not seed:
        return Route((), tuple(diagnostics or ["No compatible route chapters were found."]), faction, character_class, race)

    active_id = seed.id
    if selected_id:
        visited = {seed.id}
        while incoming[seed.id]:
            previous = incoming[seed.id]
            same_group = [guide for guide in previous if guide.group == seed.group]
            candidates = same_group or previous
            if len(candidates) != 1:
                diagnostics.append(f"More than one earlier chapter leads to {seed.name}. Earlier pickup coverage is incomplete.")
                break
            predecessor = candidates[0]
            if predecessor.id in visited:
                break
            visited.add(predecessor.id)
            seed = predecessor

    selected = []
    visited = set()
    chapter = seed
    while chapter and chapter.id not in visited:
        selected.append(chapter)
        visited.add(chapter.id)
        if chapter.id in problems:
            diagnostics.append(problems[chapter.id])
        chapter = successors[chapter.id]
    if chapter:
        diagnostics.append("The declared next-chapter chain loops. The repeated chapter was excluded.")
    return Route(tuple(selected), tuple(diagnostics), faction, character_class, race, active_id)


@dataclass(frozen=True)
class _Pickup:
    chapter: Guide
    step: int
    position: tuple[int, int]
    location: GuideLocation | None
    name: str | None
    certainty: Truth


@dataclass
class _Projected:
    snapshot: Snapshot | None
    active: dict[int, Truth] = field(default_factory=dict)
    completed: dict[int, Truth] = field(default_factory=dict)
    ready: dict[int, Truth] = field(default_factory=dict)

    def observed(self, quest_id: int, field_name: str) -> Truth:
        if not self.snapshot:
            return None
        state = getattr(self.snapshot, field_name)
        return quest_id in state if state is not None else None

    def state(self, quest_id: int, field_name: str) -> Truth:
        if field_name == "ready" and self.state(quest_id, "active") is False:
            return False
        planned = getattr(self, field_name)
        return planned[quest_id] if quest_id in planned else self.observed(quest_id, field_name)

    def guard(self, guard: Guard) -> Truth:
        reverse = guard.kind.startswith("not_")
        field_name = guard.kind.removeprefix("not_")
        values = (self.state(quest_id, field_name) for quest_id in guard.quest_ids)
        if reverse and field_name == "completed":
            # .isQuestAvailable keeps the step when any listed quest remains unturned-in.
            return _any(_not(value) for value in values)
        value = _any(values)
        return _not(value) if reverse else value


def _guard_evidence(step: Step, quest_id: int, route: Route) -> bool:
    return any(
        guard.kind in {"active", "ready"} and guard.quest_ids == (quest_id,)
        and _condition(guard.condition, route) is True
        for guard in step.guards
    )


def _step_applicability(guide: Guide, step: Step, route: Route, states: _Projected) -> Truth:
    guards = []
    for guard in step.guards:
        guard_condition = _condition(guard.condition, route)
        if guard_condition is True:
            guards.append(states.guard(guard))
        elif guard_condition is None and states.guard(guard) is not True:
            guards.append(None)
    return _all((_eligible(guide, route), _condition(step.condition, route), None if guide.uncertain or step.uncertain else True, *guards))


def _requirements(data: Mapping, states: _Projected) -> tuple[Truth, tuple[int, ...]]:
    def requirement(row) -> Truth:
        if not isinstance(row, Mapping):
            return None
        quest_id = row.get("questId")
        state = row.get("state", "completed")
        if type(quest_id) is not int or quest_id <= 0 or state not in {"active", "completed"}:
            return None
        return states.state(quest_id, state)

    flat = data.get("requirements") or ()
    groups = data.get("requirementGroups") or ()
    if not isinstance(flat, (list, tuple)) or not isinstance(groups, (list, tuple)):
        return None, ()
    if any(not isinstance(group, (list, tuple)) or not group for group in groups):
        return None, ()
    flat_state = _all(requirement(row) for row in flat)
    group_state = _any(_all(requirement(row) for row in group) for group in groups) if groups else True
    result = _all((flat_state, group_state))
    if result is True:
        return True, ()
    identifiers = {
        row["questId"] for row in (*flat, *(row for group in groups for row in group if group_state is not True))
        if isinstance(row, Mapping) and type(row.get("questId")) is int and requirement(row) is not True
    }
    return result, tuple(sorted(identifiers))


def _catalog_location(data: Mapping) -> GuideLocation | None:
    source = data.get("startLocation")
    if not isinstance(source, Mapping):
        return None
    return GuideLocation(source.get("zone"), source.get("mapId"), source.get("x"), source.get("y"), source.get("name"))


def _classification(data: Mapping, step: Step, action: QuestAction) -> str:
    value = data.get("classification")
    if value == "breadcrumb":
        return value
    if step.optional or action.optional or value == "optional":
        return "optional"
    return "mandatory" if value == "mandatory" else "unknown"


def _known_ineligible(data: Mapping, route: Route, snapshot: Snapshot | None, current: bool) -> str | None:
    faction = _normalized(data.get("faction"))
    if faction in {"horde", "alliance"} and route.faction and faction != _normalized(route.faction):
        return "The quest is limited to the opposing faction."
    classes = data.get("classes", ())
    if classes and route.character_class and _normalized(route.character_class) not in {_normalized(entry) for entry in classes}:
        return "The quest has a class restriction that excludes the selected class."
    level = data.get("minLevel")
    if current and snapshot and snapshot.level and type(level) is int and snapshot.level < level:
        return f"The exported level is {snapshot.level}. Quest metadata requires level {level}."
    return None


def _scan_start(
    route: Route, snapshot: Snapshot | None, chapter_id: str | None, start_step: int | None, diagnostics: list[str],
) -> tuple[int, int, tuple[int, int] | None, bool]:
    current = match_current_guide(route.guides, snapshot)
    current_index = route.guides.index(current) if current else None
    resolution = resolve_current_step(current, snapshot) if current and snapshot else None
    saved_anchor = (current_index, (resolution and resolution.offline_step) or 1) if current_index is not None else None
    if chapter_id:
        chapter_index = next((index for index, guide in enumerate(route.guides) if guide.id == chapter_id), -1)
    else:
        active_index = len(route.history_guides)
        chapter_index = current_index if current_index is not None and current_index >= active_index else active_index
    if chapter_index == -1:
        return -1, 1, saved_anchor, False
    selected = route.guides[chapter_index]
    if snapshot and snapshot.current_guide and not current:
        diagnostics.append("The saved chapter could not be matched uniquely on this route.")
    if current is selected and resolution and not resolution.exact and snapshot and snapshot.current_step:
        diagnostics.append("Using the saved step number as an approximation. The runtime order may differ from the offline guide.")
    first = start_step or (saved_anchor[1] if saved_anchor and current is selected else 1)
    bounded = min(max(1, first), len(selected.steps))
    if bounded != first:
        diagnostics.append("The selected step is outside the available guide. Starting at the nearest visible step.")
    return chapter_index, bounded, saved_anchor, bool(resolution and resolution.exact)


def _status_finding(
    *, states: _Projected, pickup: _Pickup | None, future_pickup: _Pickup | None,
    current_position: bool, anchor: tuple[int, int] | None, exact: bool,
    quest_id: int, classification: str, requirement_state: Truth, requirement_ids: tuple[int, ...],
    applicable: Truth, route_branched: bool, kind: str, fresh: bool, structural_only: bool,
) -> tuple[str, str, str] | None:
    optional = classification in {"optional", "breadcrumb"}
    if kind == "accept":
        if requirement_state is False and states.observed(quest_id, "active") is not True and states.observed(quest_id, "completed") is not True:
            certain = applicable is True and not route_branched
            if fresh and current_position and certain:
                severity = "info" if optional else "blocker"
                return severity, "prerequisites_missing", "The current quest requires prerequisites that are unsatisfied in the saved export."
            if current_position:
                return "review", "prerequisites_review", "Verify the documented prerequisites against a fresh save before accepting this quest."
        if requirement_state is None and requirement_ids and current_position and not structural_only:
            return "review", "prerequisites_unknown", "The snapshot and route cannot establish an explicit prerequisite state."
        return None
    if kind == "precollect":
        return None
    if states.state(quest_id, "active") is True or states.state(quest_id, "completed") is True:
        return None
    if states.active.get(quest_id, False) is None:
        if current_position:
            return "review", "conditional_pickup", "An earlier pickup is conditional. Verify that it was offered before this objective."
        return None
    if future_pickup and not current_position and pickup is None:
        return None
    observed = _all((_not(states.observed(quest_id, "active")), _not(states.observed(quest_id, "completed"))))
    past_pickup = bool(pickup and anchor and (pickup.position[0] < anchor[0] or exact and pickup.position < anchor))
    reliable_pickup = past_pickup and pickup.certainty is True
    confirmed = fresh and observed is True and (current_position or reliable_pickup) and applicable is True and not route_branched
    if confirmed:
        detail = "The export shows this quest neither active nor completed."
        if past_pickup:
            detail += " The pickup precedes your saved chapter or step. Review the earlier pickup before traveling."
        if optional:
            return "info", "optional_missing", detail + " This step can be skipped."
        if current_position and requirement_state is False and requirement_ids:
            return "blocker", "prerequisites_missing", detail + " A documented prerequisite also remains unsatisfied."
        if reliable_pickup:
            return "warning", "missing_pickup", detail
        return "warning", "unaccepted_objective", detail + " No applicable earlier pickup could be confirmed."
    if observed is True and optional and (current_position or past_pickup) and not structural_only:
        return "info", "optional_missing", "The optional quest is absent from the snapshot. Check the conditions before deciding whether to do it."
    if current_position and not structural_only:
        if observed is not True:
            return "review", "quest_state_unknown", "The required quest state is unknown. Import both active and completed exports to verify."
        if not fresh:
            return "review", "quest_state_stale", "The quest is absent from the selected export. Refresh from a recent in-game save to confirm."
    if past_pickup or (pickup and pickup.certainty is not True):
        if observed is True:
            return "review", "conditional_pickup", "An earlier pickup is documented. The saved quest state or guide conditions prevent confirmation."
    if observed is True and pickup and anchor is None:
        return "review", "earlier_pickup", "This objective has an earlier pickup outside the selected scan. Verify it before entering this chapter."
    if pickup is None and future_pickup is None:
        return "review", "pickup_unknown", "The objective has no matching pickup in this route. It may depend on a quest accepted before this route."
    return None


def _historical_pickups(
    route: Route, snapshot: Snapshot | None, anchor: tuple[int, int] | None, exact: bool,
    indexed: Mapping[int, Sequence[_Pickup]], quest_data: Mapping[int, Mapping],
) -> tuple[Finding, ...]:
    if not (snapshot and snapshot.active is not None and snapshot.completed is not None and anchor and exact):
        return ()
    chapter_index, saved_step = anchor
    guide = route.guides[chapter_index]
    observed = _Projected(snapshot)
    result = []
    seen = set()
    for step in guide.steps[:saved_step - 1]:
        if _step_applicability(guide, step, route, observed) is not True:
            continue
        for action in step.actions:
            quest_id = action.quest_id
            if (action.kind != "complete" or action.uncertain or quest_id in seen
                    or quest_id in snapshot.active or quest_id in snapshot.completed
                    or _condition(action.condition, route) is not True):
                continue
            position = chapter_index, step.number
            pickup = next((entry for entry in reversed(indexed.get(quest_id, ())) if entry.position < position and entry.certainty is True), None)
            if not pickup:
                continue
            data = quest_data.get(quest_id, {})
            classification = _classification(data, step, action)
            if classification in {"optional", "breadcrumb"} or _known_ineligible(data, route, snapshot, True):
                continue
            at_capture = (
                snapshot.exported_at is not None and step.step_id is not None
                and snapshot.state_step_id == step.step_id
                and snapshot.state_guide_name == guide.name and snapshot.state_guide_group == guide.group
            )
            detail = "The objective is behind the saved position. The export shows no active or completed quest."
            if at_capture:
                detail += " This absence was captured at the objective."
            else:
                detail += " Whether the quest was active when you reached that step is unknown."
            seen.add(quest_id)
            result.append(Finding(
                "info" if at_capture else "review", "historical_missing_pickup",
                data.get("name") or pickup.name or f"Quest {quest_id}", detail,
                quest_id, guide.id, step.number, pickup.chapter.name, pickup.step, classification,
                "captured at objective" if at_capture else "historical review",
                "Saved quest export and installed guide", (), _catalog_location(data) or pickup.location,
            ))
    return tuple(result)


def audit_route(
    route: Route, snapshot: Snapshot | None, quest_data: Mapping[int, Mapping] | None = None, *,
    chapter_id: str | None = None, start_step: int | None = None, lookahead: int | None = None,
    end_level: int | None = None, chapters: int | None = None, structural_only: bool = False,
) -> Audit:
    diagnostics = list(route.diagnostics)
    if not route.guides:
        return Audit((), (), tuple(diagnostics))
    freshness = snapshot.freshness if snapshot else "unavailable"
    if structural_only:
        diagnostics.append("Structural-only scan. Character quest state is omitted.")
    if not snapshot and not structural_only:
        diagnostics.append("No character snapshot selected. Quest state is unknown.")
    if snapshot:
        diagnostics.extend(snapshot.diagnostics)
        if not structural_only and freshness != "fresh":
            message = {
                "stale": "Quest snapshot is stale. Reload or log out in WoW, then refresh character progress for confirmed findings.",
                "unknown": "Quest snapshot age is unknown. The file timestamp may reflect a later save. Missing quests require review.",
                "unavailable": "Active and completed quests are not both available. This scan cannot confirm missing quests.",
            }[freshness]
            diagnostics.append(message)
        if snapshot.skipped_steps:
            diagnostics.append(f"The snapshot records {len(snapshot.skipped_steps)} skipped runtime steps in the saved chapter.")
    if not quest_data:
        diagnostics.append("No quest database selected. Prerequisites and classifications may be unknown.")
    quest_data = quest_data or {}
    first_chapter, first_step, anchor, exact = _scan_start(route, snapshot, chapter_id, start_step, diagnostics)
    if first_chapter < 0:
        return Audit((), (), (*diagnostics, "Select a chapter from the current planned route."))

    states = _Projected(None if structural_only else snapshot)
    indexed = defaultdict(list)
    for chapter_index, guide in enumerate(route.guides):
        for step in guide.steps:
            for action in step.actions:
                if action.kind != "accept":
                    continue
                certainty = _all((_eligible(guide, route), _condition(step.condition, route), _condition(action.condition, route)))
                if certainty is not False:
                    if certainty is True and (step.optional or step.uncertain or step.guards or action.uncertain or guide.uncertain):
                        certainty = None
                    indexed[action.quest_id].append(_Pickup(
                        guide, step.number, (chapter_index, step.number), action.location, action.name, certainty,
                    ))

    historical = () if structural_only else _historical_pickups(route, snapshot, anchor, exact, indexed, quest_data)
    historical_ids = {finding.quest_id for finding in historical}
    reports = []
    all_findings = []
    dedup = set()
    remaining = None if lookahead is None else max(0, lookahead) + 1
    project_from = anchor if anchor and anchor < (first_chapter, first_step) else (first_chapter, first_step)
    route_branched = False
    for chapter_index, guide in enumerate(route.guides):
        if chapter_index < project_from[0]:
            continue
        if chapter_index > first_chapter and end_level is not None and guide.level_min is not None and guide.level_min >= end_level:
            break
        if chapters is not None and chapter_index >= first_chapter + max(1, chapters):
            break
        if remaining == 0:
            break
        chapter_findings = []
        active = set()
        completed = set()
        planned = set()
        unknown = set()
        unguarded = set()
        quest_names = {}
        optional_quests = set()
        first_seen = None
        last_seen = None
        skip_numbers_match = (anchor and snapshot and chapter_index == anchor[0] and exact
                              and anchor[1] == snapshot.current_step
                              and not any(step.condition or step.uncertain for step in guide.steps))
        for step in guide.steps:
            position = chapter_index, step.number
            if position < project_from:
                continue
            in_range = position >= (first_chapter, first_step)
            if in_range and remaining == 0:
                break
            if in_range:
                first_seen = first_seen or step.number
                last_seen = step.number
                if remaining is not None:
                    remaining -= 1
            if skip_numbers_match and step.number in snapshot.skipped_steps:
                continue
            applicability = _step_applicability(guide, step, route, states)
            if applicability is False:
                continue
            current_position = position == anchor and exact
            for action in step.actions:
                quest_id = action.quest_id
                applicable = _all((applicability, _condition(action.condition, route), None if action.uncertain else True))
                if applicable is False:
                    continue
                data = quest_data.get(quest_id, {})
                classification = _classification(data, step, action)
                if in_range and classification in {"optional", "breadcrumb"}:
                    optional_quests.add(quest_id)
                restrictions = _known_ineligible(data, route, snapshot, current_position)
                pickups = indexed[quest_id]
                pickup = next((entry for entry in reversed(pickups) if entry.position < position), None)
                later = next((entry for entry in pickups if entry.position > position), None)
                requirement_state, requirement_ids = _requirements(data, states)
                status = None
                title = data.get("name") or (pickup and pickup.name) or action.name
                if in_range and title:
                    quest_names.setdefault(quest_id, title)
                if in_range and not data:
                    unknown.add(quest_id)
                if in_range and states.observed(quest_id, "active") is True:
                    active.add(quest_id)
                    if current_position and action.kind in _OBJECTIVES and states.observed(quest_id, "ready") is True:
                        status = ("info", "ready", "The snapshot reports that this quest is ready to turn in.")
                elif in_range and states.observed(quest_id, "completed") is True:
                    completed.add(quest_id)
                    if current_position and action.kind in _OBJECTIVES:
                        status = ("info", "completed_objective", "The snapshot already marks this quest completed. Recheck the current guide step.")
                elif restrictions and in_range:
                    status = ("info", "not_eligible", restrictions)
                elif in_range and action.kind in {*_OBJECTIVES, "accept"}:
                    status = _status_finding(
                        states=states, pickup=pickup, future_pickup=later, current_position=current_position,
                        anchor=anchor, exact=exact, quest_id=quest_id, classification=classification,
                        requirement_state=requirement_state, requirement_ids=requirement_ids, applicable=applicable,
                        route_branched=route_branched, kind=action.kind,
                        fresh=freshness == "fresh" and not structural_only and bool(
                            anchor and position >= anchor and (exact or position[0] > anchor[0])
                        ),
                        structural_only=structural_only,
                    )
                    if status and quest_id in historical_ids and not current_position and status[1] == "conditional_pickup":
                        status = None
                if in_range and action.kind in _OBJECTIVES and not _guard_evidence(step, quest_id, route):
                    unguarded.add(step.number)
                if in_range and status:
                    severity, category, detail = status
                    key = guide.id, quest_id, category
                    if key not in dedup:
                        dedup.add(key)
                        source = "Installed guide structure and character snapshot" if snapshot else "Installed guide structure"
                        if data.get("source"):
                            source += f", {data['source']}"
                        if action.kind in _OBJECTIVES and not _guard_evidence(step, quest_id, route):
                            detail += " No recognized active guard for this quest."
                        chapter_findings.append(Finding(
                            severity, category, title or f"Quest {quest_id}", detail, quest_id, guide.id, step.number,
                            pickup.chapter.name if pickup else None, pickup.step if pickup else None, classification,
                            "high in snapshot" if severity in {"blocker", "warning"} else "review",
                            source, requirement_ids, _catalog_location(data) or (pickup.location if pickup else None),
                        ))
                if not restrictions and action.kind == "accept" and states.observed(quest_id, "completed") is not True:
                    confidence = _all((applicable, requirement_state, None if route_branched or classification in {"optional", "breadcrumb"} else True))
                    if confidence is not False:
                        states.active[quest_id] = _any((states.state(quest_id, "active"), confidence))
                    if in_range and confidence is not False and quest_id not in active:
                        planned.add(quest_id)
                elif not restrictions and action.kind == "turnin":
                    confidence = _all((applicable, states.state(quest_id, "active"), None if route_branched else True))
                    if confidence is not False:
                        states.completed[quest_id] = _any((states.state(quest_id, "completed"), confidence))
                        states.active[quest_id] = False if confidence is True else None
                elif action.kind == "abandon":
                    states.active[quest_id] = False if applicable is True else None
            route_branched |= step.branch
        if first_seen is not None:
            reports.append(ChapterSummary(
                guide, tuple(chapter_findings), first_seen, last_seen, tuple(sorted(active)), tuple(sorted(completed)),
                tuple(sorted(planned)), tuple(sorted(unknown)), tuple(sorted(unguarded)), quest_names,
                tuple(sorted(optional_quests)),
            ))
            all_findings.extend(chapter_findings)
    return Audit(tuple(reports), tuple(all_findings), tuple(dict.fromkeys(diagnostics)), historical)
