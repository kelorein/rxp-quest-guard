"""Read-only imports for RestedXP guide text and literal Lua SavedVariables."""

from __future__ import annotations

import hashlib
import os
import re
import zipfile
import zlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from collections.abc import Iterable, Iterator, Mapping


MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 192 * 1024 * 1024
MAX_GUIDE = 2 * 1024 * 1024
MAX_ENTRIES = 12_000
MAX_FIELDS = 1_000_000
QUEST_FRESHNESS_LIMIT = timedelta(hours=24)
_CAPTURE_TOLERANCE = timedelta(minutes=5)


class ImportError(ValueError):
    """The selected local data is unavailable or outside the supported format."""


@dataclass(frozen=True)
class GuideLocation:
    zone: str | None = None
    map_id: int | None = None
    x: float | None = None
    y: float | None = None
    name: str | None = None
    world: tuple[int, int, float, float] | None = None


@dataclass(frozen=True)
class Guard:
    kind: str
    quest_ids: tuple[int, ...]
    condition: str = ""


@dataclass(frozen=True)
class QuestAction:
    kind: str
    quest_id: int
    objective: int | None = None
    condition: str = ""
    name: str | None = None
    location: GuideLocation | None = None
    optional: bool = False
    uncertain: bool = False


@dataclass(frozen=True)
class Step:
    number: int
    actions: tuple[QuestAction, ...]
    optional: bool = False
    condition: str = ""
    guards: tuple[Guard, ...] = ()
    uncertain: bool = False
    location: GuideLocation | None = None
    label: str | None = None
    step_id: int | None = None
    branch: bool = False
    includes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Transition:
    target: str
    condition: str = ""
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class Guide:
    id: str
    name: str
    group: str
    steps: tuple[Step, ...]
    source: str
    subgroup: str = ""
    next: tuple[Transition, ...] = ()
    faction: str | None = None
    condition: str = ""
    level_min: int | None = None
    level_max: int | None = None
    version: int | None = None
    variants: tuple[str, ...] = ()
    uncertain: bool = False
    includes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuideCollection:
    guides: tuple[Guide, ...]
    diagnostics: tuple[str, ...] = ()
    version: str | None = None
    interfaces: tuple[int, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    label: str
    character: str | None = None
    realm: str | None = None
    imported_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    current_guide: str | None = None
    current_group: str | None = None
    current_step: int | None = None
    skipped_steps: frozenset[int] = frozenset()
    active: frozenset[int] | None = None
    completed: frozenset[int] | None = None
    source_modified_at: datetime | None = None
    exported_at: datetime | None = None
    faction: str | None = None
    character_class: str | None = None
    level: int | None = None
    diagnostics: tuple[str, ...] = ()
    source: str = ""
    current_step_id: int | None = None
    ready: frozenset[int] | None = None
    race: str | None = None
    client_interface: int | None = None
    current_subgroup: str | None = None
    current_guide_key: str | None = None
    client_version: str | None = None
    client_build: str | None = None
    restedxp_version: str | None = None
    addon_version: str | None = None
    progress_modified_at: datetime | None = None
    quest_modified_at: datetime | None = None
    identity_key: str | None = None
    association_key: str | None = None
    account_key: str | None = None
    state_source: str | None = None
    ownership_conflict: bool = False
    state_guide_name: str | None = None
    state_guide_group: str | None = None
    state_step_id: int | None = None
    source_slot: str | None = None
    backup: bool = False
    realm_id: int | None = None
    progress_source: str | None = None

    @property
    def freshness(self) -> str:
        if self.active is None or self.completed is None:
            return "unavailable"
        if self.exported_at is None or self.exported_at.tzinfo is None:
            return "unknown"
        age = datetime.now(UTC) - self.exported_at
        if age < -_CAPTURE_TOLERANCE:
            return "unknown"
        if self.quest_modified_at and self.progress_modified_at:
            if self.progress_modified_at - self.quest_modified_at > _CAPTURE_TOLERANCE:
                return "stale"
        return "stale" if age > QUEST_FRESHNESS_LIMIT else "fresh"


_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>--)|(?P<long>\[(?P<equals>=*)\[)"
    r"|(?P<quote>['\"])|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<number>[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?))"
    r"|(?P<symbol>.)",
    re.ASCII | re.DOTALL,
)
_LONG = re.compile(r"\[(=*)\[")
_ESCAPE = re.compile(r"\\(?:[0-9]{1,3}|x[0-9a-fA-F]{2}|\r\n|z\s*|.)", re.DOTALL)
_CONTROL = dict(zip("abfnrtv", "\a\b\f\n\r\t\v"))
_CONTROL.update({"\r": "\n", "\n": "\n", "\r\n": "\n"})
_END = ("end", "")


def _escape(match: re.Match[str]) -> str:
    token = match[0][1:]
    if token[0].isdigit():
        if int(token) > 255:
            raise ImportError("A Lua string contains a decimal byte escape over 255.")
        return chr(int(token))
    if len(token) == 3 and token.startswith("x"):
        return chr(int(token[1:], 16))
    if token.startswith("z"):
        return ""
    return _CONTROL.get(token, token)


def _long_string(text: str, start: int, equals: str) -> tuple[str, int]:
    close = "]" + equals + "]"
    end = text.find(close, start)
    if end < 0:
        raise ImportError("An imported Lua long string or comment is unterminated.")
    value = text[start:end]
    return re.sub(r"^(?:\r\n|\n\r|\n|\r)", "", value, count=1), end + len(close)


def _tokens(text: str, *, code: bool = False) -> Iterator[tuple[str, str]]:
    quoted = {"'": re.compile(r"[^'\\]*(?:\\[\s\S][^'\\]*)*'"),
              '"': re.compile(r'[^"\\]*(?:\\[\s\S][^"\\]*)*"')}
    index = 0
    while index < len(text):
        match = _TOKEN.match(text, index)
        index = match.end()
        kind = match.lastgroup
        value = match[0]
        if kind == "space":
            continue
        if kind == "comment":
            long_comment = _LONG.match(text, index)
            if long_comment:
                _, index = _long_string(text, long_comment.end(), long_comment[1])
            else:
                next_line = text.find("\n", index)
                index = len(text) if next_line < 0 else next_line + 1
            continue
        if kind == "long":
            value, index = _long_string(text, index, match["equals"])
            yield "string", value
        elif kind == "quote":
            close = quoted[value].match(text, index)
            if not close:
                if code:
                    return
                raise ImportError("An imported Lua string is unterminated.")
            yield "string", _ESCAPE.sub(_escape, text[index:close.end() - 1])
            index = close.end()
        else:
            yield kind, value


class _LiteralReader:
    def __init__(self, text: str):
        self.tokens = _tokens(text)
        self.token = next(self.tokens, _END)
        self.fields = 0

    def pop(self) -> tuple[str, str]:
        token = self.token
        self.token = next(self.tokens, _END)
        return token

    def expect(self, kind: str, value: str | None = None) -> str:
        if self.token[0] != kind or (value is not None and self.token[1] != value):
            raise ImportError("Expected a Lua literal assignment or table. Executable expressions are unsupported.")
        return self.pop()[1]

    def value(self, depth: int, first: tuple[str, str] | None = None):
        kind, value = self.pop() if first is None else first
        if kind == "string":
            return value
        if kind == "number":
            if len(value) > 100:
                raise ImportError("A Lua number exceeds the import limit.")
            if value.lower().lstrip("+-").startswith("0x"):
                return int(value, 16)
            return float(value) if any(mark in value for mark in ".eE") else int(value)
        if kind == "name" and value in ("true", "false", "nil"):
            return {"true": True, "false": False, "nil": None}[value]
        if (kind, value) == ("symbol", "{") and depth < 64:
            return self.table(depth + 1)
        raise ImportError("Expected a Lua literal value. Executable expressions or excess nesting are unsupported.")

    def table(self, depth: int) -> dict:
        fields = {}
        array_index = 1
        while self.token != ("symbol", "}"):
            self.fields += 1
            if self.fields > MAX_FIELDS:
                raise ImportError("The SavedVariables table exceeds the field limit.")
            first = self.pop()
            if first == ("symbol", "["):
                key = self.value(depth)
                if isinstance(key, dict) or key is None:
                    raise ImportError("An imported Lua table key is unsupported.")
                self.expect("symbol", "]")
                self.expect("symbol", "=")
                value = self.value(depth)
            elif first[0] == "name" and self.token == ("symbol", "="):
                key = first[1]
                self.pop()
                value = self.value(depth)
            else:
                key = array_index
                array_index += 1
                value = self.value(depth, first)
            if value is not None:
                fields[key] = value
            else:
                fields.pop(key, None)
            if self.token in {("symbol", ","), ("symbol", ";")}:
                self.pop()
            elif self.token != ("symbol", "}"):
                raise ImportError("A Lua table field separator or closing brace is missing.")
        self.pop()
        return fields


def parse_saved_variables(text: str) -> dict[str, object]:
    """Parse data assignments, never invoke Lua or a Python evaluator."""
    if len(text) > MAX_FILE:
        raise ImportError("The SavedVariables input exceeds the file size limit.")
    reader = _LiteralReader(text.removeprefix("\ufeff").removeprefix("\xef\xbb\xbf"))
    assignments = {}
    while reader.token != _END:
        if len(assignments) >= MAX_FIELDS:
            raise ImportError("The SavedVariables input has too many assignments.")
        key = reader.expect("name")
        reader.expect("symbol", "=")
        assignments[key] = reader.value(0)
        if reader.token == ("symbol", ";"):
            reader.pop()
    return assignments


def _utf8(value: str) -> str:
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def _string(value) -> str | None:
    return _utf8(value).strip() if isinstance(value, str) and value.strip() else None


def _positive(value, limit: int = 2_147_483_647) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,11}", value.strip()):
        value = int(value)
    if type(value) is int and 0 < value <= limit:
        return value
    return None


def _stable_id(value) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,11}", value.strip()):
        value = int(value)
    return value if type(value) is int and -(2**31) <= value <= 2**32 + MAX_GUIDE else None


def _table(value) -> dict:
    return value if isinstance(value, dict) else {}


def _ids(value) -> frozenset[int] | None:
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9,;\s]*", value, re.ASCII):
            return None
        values = re.split(r"[,;\s]+", value.strip())
        result = {_positive(item) for item in values if item}
        return None if None in result else frozenset(result)
    if not isinstance(value, dict):
        return None
    result = set()
    next_index = 1
    for key, item in value.items():
        if type(item) is bool:
            quest = _positive(key)
            if quest is None:
                return None
            if item:
                result.add(quest)
        else:
            if key != next_index:
                return None
            next_index += 1
            quest = _positive(item)
            if quest is None:
                return None
            result.add(quest)
    return frozenset(result)


def _skips(value) -> frozenset[int]:
    return frozenset(
        index for key, skipped in _table(value).items() if skipped is True
        if (index := _positive(key)) is not None
    )


def _timestamp(value) -> datetime | None:
    try:
        if type(value) in {int, float} and value > 0:
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str):
            date = datetime.fromisoformat(value)
            if date.tzinfo is not None:
                return date.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        pass
    return None


_SAVED_NAME = re.compile(r"(?:rxpguides|rxpquestguard).*?(?:\.lua(?:\(\d+\))?(?:\.bak)?|\.bak)$", re.I)


def _is_saved_name(name: str) -> bool:
    return _SAVED_NAME.fullmatch(name) is not None


def _path_parts(name: str) -> tuple[str, ...]:
    return PurePosixPath(name.replace("\\", "/")).parts


def _member_kind(name: str, purpose: str, direct: bool) -> str | None:
    parts = _path_parts(name)
    lower = tuple(part.casefold() for part in parts)
    basename = lower[-1] if lower else ""
    saved = "savedvariables" in lower and _is_saved_name(basename)
    addon = any(part.startswith("rxpguides") for part in lower[:-1])
    if purpose == "quest_guard_snapshot" and not (basename.startswith("rxpquestguard") and _is_saved_name(basename)):
        return None
    if purpose in {"snapshot", "quest_guard_snapshot"}:
        return "saved" if direct or saved or _is_saved_name(basename) and not addon else None
    if saved or direct and _is_saved_name(basename):
        return "saved"
    if basename.startswith("rxpguides") and basename.endswith(".toc"):
        return "toc"
    if direct or basename.endswith((".lua", ".txt")) and (addon or "guides" in lower) or basename.endswith(".txt"):
        return "guide"
    return None


def _selected_directories(source: Path, purpose: str) -> tuple[Path, ...]:
    candidates = []
    for parent in (source, source / "_forever_"):
        wtf = parent / "WTF"
        addons = parent / "Interface" / "AddOns"
        if wtf.is_dir():
            candidates.append(wtf)
        if purpose == "guide" and addons.is_dir():
            candidates.extend(path for path in sorted(addons.iterdir()) if path.is_dir() and path.name.casefold().startswith("rxpguides"))
    return tuple(candidates) if candidates else (source,)


def _members(source: Path, purpose: str) -> Iterator[tuple[str, str, str, datetime | None, str]]:
    """Yield a bounded stream without extracting archives or traversing symlinks."""
    total = 0
    if source.is_file() and source.suffix.casefold() == ".zip":
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ImportError("The archive has too many entries.")
            for entry in entries:
                name = entry.filename
                kind = _member_kind(name, purpose, False)
                member_path = PurePosixPath(name.replace("\\", "/"))
                if entry.is_dir() or not kind or member_path.is_absolute() or ".." in member_path.parts:
                    continue
                if entry.file_size > MAX_FILE or total + entry.file_size > MAX_TOTAL:
                    raise ImportError("The archive exceeds the local import size limit.")
                with archive.open(entry) as stream:
                    value = stream.read(MAX_FILE + 1)
                total += len(value)
                if len(value) > MAX_FILE or total > MAX_TOTAL:
                    raise ImportError("The archive exceeds the local import size limit.")
                yield name, kind, value.decode("latin-1"), datetime(*entry.date_time).astimezone(UTC), f"{source.resolve()}!/{name}"
        return

    if source.is_file():
        parts = source.parts
        parent = next((index for index, part in enumerate(parts) if part.casefold() == "account"), len(parts) - 1)
        name = PurePosixPath(*parts[parent:]).as_posix()
        paths = ((source, name, _member_kind(name, purpose, True)),)
    elif source.is_dir():
        def directory_entries():
            visited = 0
            for base in _selected_directories(source, purpose):
                for root, dirs, files in os.walk(base):
                    folder = Path(root)
                    dirs[:] = sorted(item for item in dirs if not (folder / item).is_symlink() and not (folder / item).is_junction())
                    if purpose == "guide" and folder.name.casefold() == "rxpguides":
                        excluded = {"db", "libs", "locale", "lang", "textures", "ui"}
                        dirs[:] = [item for item in dirs if item.casefold() not in excluded]
                    if purpose == "guide" and folder.name.casefold() == "guides" and "forever" in {item.casefold() for item in dirs}:
                        dirs[:] = [item for item in dirs if item.casefold() in {"forever", "survivalguide"}]
                    for filename in sorted(files):
                        visited += 1
                        if visited > MAX_ENTRIES:
                            raise ImportError("The directory has too many entries. Select the RestedXP or WTF folder directly.")
                        path = folder / filename
                        if path.is_symlink():
                            continue
                        name = path.relative_to(source.parent).as_posix()
                        kind = _member_kind(name, purpose, False)
                        if kind:
                            yield path, name, kind
        paths = directory_entries()
    else:
        raise ImportError("The selected local path does not exist.")
    for path, name, kind in paths:
        before = path.stat()
        size = before.st_size
        if size > MAX_FILE or total + size > MAX_TOTAL:
            raise ImportError("The selected files exceed the local import size limit.")
        with path.open("rb") as stream:
            value = stream.read(MAX_FILE + 1)
        total += len(value)
        if len(value) > MAX_FILE or total > MAX_TOTAL:
            raise ImportError("The selected files exceed the local import size limit.")
        after = path.stat()
        if purpose in {"snapshot", "quest_guard_snapshot"} and (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ImportError("A SavedVariables file changed during the import. Refresh again after WoW finishes saving.")
        modified = _timestamp(after.st_mtime)
        yield name, kind, value.decode("latin-1"), modified, str(path.resolve())


def _digest(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode("utf-8", errors="surrogatepass")).hexdigest()


def _character_token(value: str) -> str:
    # Forever may capture a first and last name separated by a space but
    # write the same character directory with a hyphen.
    return re.sub(r"[-\s]+", " ", value.casefold()).strip()


def _path_owner(origin: str) -> tuple[str | None, str | None, str | None]:
    parts = _path_parts(origin)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].casefold() != "account":
            continue
        tail = parts[index + 1:]
        if len(tail) in {3, 5} and tail[-2].casefold() == "savedvariables":
            key = _digest("account", "/".join(parts[:index + 2]).casefold())
            return (key, tail[2], tail[1]) if len(tail) == 5 else (key, None, None)
    return None, None, None


def _quest_notes(active, completed) -> tuple[str, ...]:
    notes = []
    if active is None:
        notes.append("Active quest export is unavailable.")
    if completed is None:
        notes.append("Completed quest export is unavailable.")
    if active and completed and active & completed:
        notes.append("Some quest IDs appear in both lists. Active state takes precedence for those IDs.")
    return tuple(notes)


_UNASSIGNED = "The quest export has no verified character ownership. Associate it only with the character that created it."
_AMBIGUOUS = "The saved character identity conflicts with the file's character directory."
_EMPTY_DATE = datetime.min.replace(tzinfo=UTC)


def _read_snapshot(entries, name: str, origin: str, modified: datetime | None) -> Snapshot | None:
    payload = _table(entries.get("RXPQuestGuardSnapshot"))
    if payload and (type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1):
        raise ImportError("The Quest Guard snapshot version is unsupported.")
    character = _table(entries.get("RXPCData"))
    account = _table(entries.get("RXPData"))
    export = payload or account or character
    progress = payload or character
    active = _ids(export.get("active", export.get("ChatGPTActive")))
    completed = _ids(export.get("completed", export.get("ChatGPTQuestExport")))
    guide_name = _string(progress.get("currentGuide") or progress.get("currentGuideName"))
    if active is None and completed is None and guide_name is None and not payload:
        return None
    metadata = _table(export.get("exportMetadata"))
    account_key, path_character, path_realm = _path_owner(origin)
    declared_character = _string(metadata.get("character") or export.get("character"))
    declared_realm = _string(metadata.get("realm") or export.get("realm"))
    name_conflict = bool(path_character and declared_character and _character_token(path_character) != _character_token(declared_character))
    realm_conflict = bool(path_realm and declared_realm and not path_realm.isdecimal() and path_realm.casefold() != declared_realm.casefold())
    conflict = name_conflict or realm_conflict
    char_name = declared_character or path_character
    realm = declared_realm or path_realm
    export_time = _timestamp(payload.get("capturedAt") or metadata.get("exportedAt") or export.get("exportedAt"))
    identity = None
    if account_key and char_name and realm and not conflict:
        folder_character = path_character.casefold() if path_character else _character_token(char_name).replace(" ", "-")
        identity = _digest("character", account_key, folder_character, (path_realm or realm).casefold())
    basename = _path_parts(name)[-1]
    backup = basename.casefold().endswith(".bak")
    logical_origin = re.sub(r"\.bak$", "", origin.casefold()).replace("\\", "/")
    slot = _digest("file", logical_origin)
    association = None
    diagnostics = list(_quest_notes(active, completed))
    has_quest_state = active is not None or completed is not None
    if conflict:
        diagnostics.append(_AMBIGUOUS)
    elif has_quest_state and not identity:
        diagnostics.append(_UNASSIGNED)
        association = _digest(
            "export", slot, str(sorted(active) if active is not None else None),
            str(sorted(completed) if completed is not None else None),
            str(export_time), char_name or "", realm or "",
        )
    label = f"{char_name} ({realm})" if char_name and realm else char_name or f"{basename} · unattributed"
    if backup:
        label += " · backup"
    if conflict:
        label += " · identity conflict"
    current_group = _string(progress.get("currentGuideGroup"))
    step_id = _stable_id(progress.get("currentStepId"))
    ready = _ids(export.get("ready"))
    client_build = _string(export.get("clientBuild"))
    if client_build is None and (numeric_build := _positive(export.get("clientBuild"))) is not None:
        client_build = str(numeric_build)
    return Snapshot(
        label=label, character=char_name, realm=realm, current_guide=guide_name,
        current_group=current_group, current_step=_positive(progress.get("currentStep")),
        current_subgroup=_string(progress.get("currentGuideSubgroup")),
        current_guide_key=_string(progress.get("currentGuideKey")),
        current_step_id=step_id,
        skipped_steps=(_ids(progress.get("skippedSteps")) or frozenset()) if payload else _skips(progress.get("stepSkip")),
        active=active, completed=completed, ready=(ready & active) if ready is not None and active is not None else None,
        source_modified_at=modified, exported_at=export_time,
        progress_modified_at=modified if progress else None,
        quest_modified_at=modified if has_quest_state or payload else None,
        faction=_string(metadata.get("faction") or export.get("faction")),
        character_class=_string(metadata.get("class") or export.get("class") or export.get("characterClass")),
        level=_positive(metadata.get("level") or export.get("level")),
        race=_string(metadata.get("race") or export.get("race")),
        client_interface=_positive(export.get("clientInterface") or export.get("gameVersion")),
        client_version=_string(export.get("clientVersion")), client_build=client_build,
        restedxp_version=_string(export.get("restedXPVersion")), addon_version=_string(export.get("addonVersion")),
        diagnostics=tuple(diagnostics), source=basename, identity_key=identity,
        association_key=association, account_key=account_key, source_slot=slot, backup=backup,
        state_source="Quest Guard" if payload else "RestedXP export" if has_quest_state else None,
        state_guide_name=guide_name if payload else None, state_guide_group=current_group if payload else None,
        state_step_id=step_id if payload else None, ownership_conflict=conflict,
        realm_id=_positive(export.get("realmId")),
        progress_source="Quest Guard" if payload else "RestedXP" if character else None,
    )


def _association_target(quest_export: Snapshot, progression: Snapshot) -> None:
    if quest_export.ownership_conflict or progression.ownership_conflict:
        raise ImportError("Resolve the character identity conflict before applying this export.")
    if quest_export.account_key and progression.account_key and quest_export.account_key != progression.account_key:
        raise ImportError("The export and character progress belong to different WoW accounts or installations.")
    if quest_export.identity_key and progression.identity_key and quest_export.identity_key != progression.identity_key:
        raise ImportError("The export and character progress identify different characters.")
    if quest_export.character and progression.character and _character_token(quest_export.character) != _character_token(progression.character):
        raise ImportError("The export and character progress name different characters.")
    if quest_export.realm and progression.realm and quest_export.realm.casefold() != progression.realm.casefold():
        # A matching path-derived identity already accounts for Forever's
        # numeric realm folder and display name difference.
        if not quest_export.identity_key or quest_export.identity_key != progression.identity_key:
            raise ImportError("The export and character progress name different realms.")


def _fuse(quest_export: Snapshot, progression: Snapshot) -> Snapshot:
    notes = tuple(
        note for note in progression.diagnostics
        if note not in {*_quest_notes(progression.active, progression.completed), _UNASSIGNED}
    )
    same_step = (
        progression.current_guide and quest_export.current_guide == progression.current_guide
        and quest_export.current_group == progression.current_group
        and progression.current_step_id is not None and quest_export.current_step_id == progression.current_step_id
    )
    return replace(
        progression,
        active=quest_export.active, completed=quest_export.completed, ready=quest_export.ready,
        character=quest_export.character or progression.character, realm=quest_export.realm or progression.realm,
        realm_id=quest_export.realm_id or progression.realm_id,
        faction=quest_export.faction or progression.faction,
        character_class=quest_export.character_class or progression.character_class,
        race=quest_export.race or progression.race, level=quest_export.level or progression.level,
        exported_at=quest_export.exported_at, quest_modified_at=quest_export.quest_modified_at or quest_export.source_modified_at,
        current_subgroup=progression.current_subgroup if progression.current_subgroup is not None else (
            quest_export.current_subgroup if same_step else None
        ),
        current_guide_key=progression.current_guide_key or (quest_export.current_guide_key if same_step else None),
        skipped_steps=progression.skipped_steps or (quest_export.skipped_steps if same_step else frozenset()),
        association_key=quest_export.association_key, state_source=quest_export.state_source,
        state_guide_name=quest_export.state_guide_name, state_guide_group=quest_export.state_guide_group,
        state_step_id=quest_export.state_step_id, addon_version=quest_export.addon_version,
        restedxp_version=quest_export.restedxp_version or progression.restedxp_version,
        client_version=quest_export.client_version or progression.client_version,
        client_build=quest_export.client_build or progression.client_build,
        client_interface=quest_export.client_interface or progression.client_interface,
        diagnostics=tuple(dict.fromkeys((*notes, *(note for note in quest_export.diagnostics if note != _UNASSIGNED)))),
    )


def attach_progress(quest_export: Snapshot, progression: Snapshot) -> Snapshot:
    """Associate an export with progress after the user confirms the character."""
    _association_target(quest_export, progression)
    result = _fuse(quest_export, progression)
    return replace(result, diagnostics=(*result.diagnostics, "Quest export explicitly associated with this character."))


def remember_association(associations: Mapping[str, str], quest_export: Snapshot, progression: Snapshot) -> dict[str, str]:
    _association_target(quest_export, progression)
    if not quest_export.association_key or not progression.identity_key:
        raise ImportError("The selected local files do not provide enough provenance to remember this association.")
    return {**associations, quest_export.association_key: progression.identity_key}


def merge_snapshots(
    snapshots: Iterable[Snapshot], *, associations: Mapping[str, str] | None = None,
) -> tuple[Snapshot, ...]:
    """Fuse exact file ownership and previously confirmed, unchanged exports."""
    records = tuple(snapshots)
    by_identity = {}
    unassigned = {}
    primary_slots = {row.source_slot for row in records if row.source_slot and not row.backup}
    for row in records:
        if row.identity_key:
            by_identity.setdefault(row.identity_key, []).append(row)
        else:
            selector = row.association_key or row.source_slot or row.label
            old = unassigned.get(selector)
            if old is None or old.backup and not row.backup:
                unassigned[selector] = row

    linked = set()
    result = []
    for identity, rows in by_identity.items():
        rows = [row for row in rows if not row.backup or row.source_slot not in primary_slots]
        if not rows:
            continue
        progress_rows = [row for row in rows if row.current_guide]
        base = max(progress_rows or rows, key=lambda row: (
            (row.exported_at if row.progress_source == "Quest Guard" else None)
            or row.progress_modified_at or row.source_modified_at or _EMPTY_DATE,
            not row.backup, row.progress_source == "RestedXP",
        ))
        states = [row for row in rows if row.state_source or row.active is not None or row.completed is not None]
        confirmed = []
        for key, export in unassigned.items():
            if not export.association_key or not associations or associations.get(key) != identity:
                continue
            if export.backup and export.source_slot in primary_slots:
                continue
            try:
                _association_target(export, base)
            except ImportError:
                continue
            confirmed.append(export)
            linked.add(key)
        state_rows = states + confirmed
        if state_rows:
            state = max(state_rows, key=lambda row: (
                row.exported_at is not None, row.exported_at or row.quest_modified_at or _EMPTY_DATE,
                not row.backup, row.state_source == "Quest Guard",
            ))
            base = attach_progress(state, base) if state in confirmed else _fuse(state, base)
        result.append(base)
    return (*[row for key, row in unassigned.items() if key not in linked], *result)


def load_snapshots(
    source: Path, *, associations: Mapping[str, str] | None = None, quest_guard_only: bool = False,
) -> tuple[Snapshot, ...]:
    source = Path(source)
    result = []
    failures = 0
    purpose = "quest_guard_snapshot" if quest_guard_only else "snapshot"
    for name, _, value, modified, origin in _members(source, purpose):
        try:
            snapshot = _read_snapshot(parse_saved_variables(value), name, origin, modified)
        except ImportError:
            if source.is_file() and source.suffix.casefold() != ".zip":
                raise
            failures += 1
            continue
        if snapshot:
            result.append(snapshot)
    if not result:
        detail = f" ({failures} unreadable local files)" if failures else ""
        label = "Quest Guard" if quest_guard_only else "RestedXP or Quest Guard"
        raise ImportError(f"No supported {label} progress was found" + detail + ".")
    if failures:
        message = f"Skipped {failures} SavedVariables files outside the supported literal format."
        result = [replace(row, diagnostics=(*row.diagnostics, message)) for row in result]
    return merge_snapshots(result, associations=associations)


_HEADER = re.compile(r"^#(?P<tag>[a-zA-Z][\w-]*)(?:\s+(?P<value>.*))?$")
_STEP = re.compile(r"^step(?:\s|$)", re.I)
_DIRECTIVE = re.compile(r"^\.([a-zA-Z][\w]*)(?:\s+(.*))?$")
_GUIDE_NAME = re.compile(r"(?m)^\s*#name\s+[^\r\n]")
_GUIDE_STEP = re.compile(r"(?mi)^\s*step(?:\s|$)")
_LEVEL = re.compile(r"^(\d{1,2})\s*[-–—]\s*(\d{1,2})(?:\s|$)")
_ACTION_TAGS = {
    "accept": "accept", "turnin": "turnin", "complete": "complete",
    "abandon": "abandon", "collect": "precollect", "daily": "accept", "dailyturnin": "turnin",
}
_GUARD_TAGS = {
    "isonquest": "active", "isnotonquest": "not_active",
    "isquestturnedin": "completed", "isquestavailable": "not_completed",
    "isquestcomplete": "ready", "isquestnotcomplete": "not_ready",
}
_BRANCH_TAGS = {
    "include", "requires", "skip", "skipif", "skips", "skipto",
    "season", "softcore", "hardcore", "xprate", "level", "levelrange", "era",
}
_BRANCH_DIRECTIVES = {
    "abandon", "achievementcomplete", "achievementincomplete", "buff", "dmf", "dungeon", "enabledungeon",
    "enablegroupquests", "group", "holiday", "isquestobjectivecomplete", "isquestobjectivenotcomplete",
    "itemcount", "multibox", "next", "nodmf", "nospell", "profession", "questcount",
    "reputation", "singlebox", "skill", "skipto", "spellknown", "spellmissing",
}
_GUIDE_VARIANTS = {"forever", "classic", "tbc", "wotlk", "cata", "mists", "sod", "retail", "df", "hardcore", "softcore"}
_ID_REMAP = {"convertquest", "mirror", "questmirror", "mirrorquest"}


def _arguments(value: str) -> tuple[str, str, str]:
    code, _, comment = value.partition("--")
    code, _, display = code.partition(">>")
    return code.strip(), display.strip(), comment.strip()


def _condition(value: str) -> tuple[str, str]:
    code, _, condition = value.partition("<<")
    return code.strip(), condition.strip()


def _combine_conditions(*conditions: str) -> str:
    nonempty = [condition for condition in conditions if condition]
    if len(nonempty) <= 1:
        return nonempty[0] if nonempty else ""
    return " ".join(f"({condition})" for condition in nonempty)


def _simple_text(value: str) -> str | None:
    value = re.sub(r"\|T[^|]*\|t|\|c(?:[0-9a-fA-F]{8}|RXP_[A-Z_]+_)|\|r", "", value)
    value = value.replace("*", "").strip()
    return value if value and len(value) <= 160 else None


def _action_name(kind: str, display: str, comment: str) -> str | None:
    if kind not in ("accept", "turnin"):
        return None
    text = _simple_text(display) or _simple_text(comment)
    if not text:
        return None
    return re.sub(r"^(?:accept(?: the quest)?\s+|turn in\s+)", "", text, flags=re.I) or None


def _guide_location(arguments: str) -> GuideLocation | None:
    fields = [field.strip() for field in arguments.split(",")]
    if len(fields) < 3:
        return None
    try:
        x, y = map(float, fields[1:3])
    except ValueError:
        return None
    world = re.fullmatch(r"(\d{1,7})/(\d{1,4})", fields[0])
    if world and abs(x) <= 1e7 and abs(y) <= 1e7:
        return GuideLocation(world=(int(world[1]), int(world[2]), x, y))
    if not (0 <= x <= 100 and 0 <= y <= 100 and fields[0]):
        return None
    map_id = _positive(fields[0])
    return GuideLocation(None if map_id else fields[0], map_id, x, y)


def _parse_step(raw: list[str], number: int, step_id: int | None) -> Step:
    _, condition = _condition(raw[0])
    optional = False
    uncertain = False
    branch = False
    label = None
    location = None
    unconditional_location = None
    target = None
    actions = []
    guards = []
    includes = []
    for line in raw[1:]:
        # Comments and user-facing guide prose never drive analysis.
        if line.startswith(">>"):
            marker = re.search(r"\|cRXP_PICK_([^|]+)\|r", line)
            if marker:
                target = _simple_text(marker[1])
        if not line or line.startswith(("--", ">>", "+", "*")):
            continue
        raw_code, filter_text = _condition(line)
        code, display, comment = _arguments(raw_code)
        header = _HEADER.fullmatch(code)
        if header:
            tag = header["tag"].lower()
            if tag == "optional":
                optional = True
                uncertain |= bool(filter_text)
            elif tag == "label":
                label = header["value"]
            elif tag == "include" and header["value"] and not filter_text:
                includes.append(header["value"])
            elif tag in _BRANCH_TAGS:
                uncertain = True
                branch |= tag in {"include", "skip", "skipif", "skips", "skipto"}
            continue
        directive = _DIRECTIVE.fullmatch(code)
        if not directive:
            continue
        tag = directive[1].lower()
        arguments = (directive[2] or "").strip()
        fields = [field.strip() for field in arguments.split(",")]
        if tag == "goto":
            location = _guide_location(arguments)
            unconditional_location = location if not filter_text else None
        elif tag == "target" and not filter_text:
            target = _simple_text(re.sub(r"::\d+\b", "", arguments))
        elif tag in _GUARD_TAGS:
            ids = tuple(identifier for field in fields if (identifier := _positive(field)))
            if ids and len(ids) == len(fields):
                guards.append(Guard(_GUARD_TAGS[tag], ids, filter_text))
            else:
                uncertain = True
        elif tag in _ACTION_TAGS:
            kind = _ACTION_TAGS[tag]
            if tag == "collect":
                quest_id = _positive(fields[2]) if len(fields) > 2 else None
            else:
                quest_id = _positive(fields[0])
            if quest_id:
                index = _positive(fields[1]) if kind == "complete" and len(fields) > 1 else None
                # A turn-in can be flagged to skip when missing; advanced objective forms can
                # also encode alternatives. Preserve the reference without claiming certainty.
                advanced = (kind in ("accept", "turnin") and len(fields) > 1 and any(fields[1:])) or tag.startswith("daily")
                actions.append(QuestAction(
                    kind, quest_id, index, filter_text, _action_name(kind, display, comment),
                    unconditional_location if kind == "accept" else None, uncertain=advanced,
                ))
            elif tag != "collect":
                uncertain = True
        elif tag in _BRANCH_DIRECTIVES:
            uncertain = True
            branch |= tag in {"next", "skipto"}
    accept_count = sum(action.kind == "accept" for action in actions)
    if target and accept_count == 1:
        actions = [
            replace(action, location=replace(action.location or GuideLocation(), name=target))
            if action.kind == "accept" else action for action in actions
        ]
    return Step(number, tuple(actions), optional, condition, tuple(guards), uncertain, location, label, step_id, branch, tuple(includes))


def parse_guide(text: str, source: str = "Guide text", *, metadata: dict | None = None) -> Guide | None:
    """Extract structural quest and route metadata from one plain guide block."""
    if not _GUIDE_NAME.search(text) or not _GUIDE_STEP.search(text):
        return None
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        return None
    meta = metadata or {}
    headers = {}
    variants = set()
    guide_conditions = []
    transitions = []
    includes = []
    steps = []
    current = []
    first_step_line = None
    guide_id = _stable_id(meta.get("guideId"))
    remapped = False
    for lineno, original_line in enumerate(re.findall(r"[^\r\n]+", text), 1):
        line = original_line.strip()
        if _STEP.match(line):
            if current:
                steps.append(_parse_step(current, len(steps) + 1, guide_id + first_step_line if guide_id is not None else None))
            current = [line]
            first_step_line = lineno
            continue
        if current:
            current.append(line)
            directive = _DIRECTIVE.match(line)
            remapped |= bool(directive and directive[1].lower() in _ID_REMAP)
            continue
        code, condition = _condition(line)
        code, _, _ = _arguments(code)
        header = _HEADER.fullmatch(code)
        if not code and condition:
            guide_conditions.append(condition)
        if not header:
            continue
        tag = header["tag"].lower()
        value = header["value"]
        if tag in _GUIDE_VARIANTS:
            variants.add(tag)
        if tag == "next" and value:
            choices = tuple(part.strip() for part in value.split(";") if part.strip())
            if choices:
                transitions.append(Transition(choices[0], condition, choices[1:]))
        elif tag == "include" and value and not condition:
            includes.append(value)
        elif tag == "include":
            remapped = True
        elif value and tag in {"name", "group", "subgroup", "version", "enabledfor"}:
            headers[tag] = value
            if condition:
                guide_conditions.append(condition)
    if current:
        steps.append(_parse_step(current, len(steps) + 1, guide_id + first_step_line if guide_id is not None else None))
    name = _string(meta.get("name")) or headers.get("name")
    group = _string(meta.get("group")) or headers.get("group")
    subgroup = _string(meta.get("subgroup")) or headers.get("subgroup", "")
    if not (name and group and steps):
        return None
    explicit_condition = _string(meta.get("enabledFor")) or headers.get("enabledfor")
    if explicit_condition and explicit_condition not in guide_conditions:
        guide_conditions.append(explicit_condition)
    condition = _combine_conditions(*guide_conditions)
    suffix = re.search(r"\(([HA])\)$", group, re.I)
    faction = {"h": "Horde", "a": "Alliance"}[suffix[1].lower()] if suffix else None
    if faction is None and condition.casefold() in {"alliance", "horde"}:
        faction = condition.capitalize()
    level = _LEVEL.match(name)
    version = _positive(meta.get("version")) or _positive(headers.get("version"))
    if not transitions:
        next_value = _string(meta.get("next"))
        if next_value:
            choices = tuple(part.strip() for part in next_value.split(";") if part.strip())
            if choices:
                transitions.append(Transition(choices[0], alternatives=choices[1:]))
    return Guide(
        id=f"{group}|{subgroup}|{name}", name=name, group=group, subgroup=subgroup,
        steps=tuple(steps), source=source, next=tuple(transitions), faction=faction,
        condition=condition, level_min=int(level[1]) if level else None, level_max=int(level[2]) if level else None,
        version=version, variants=tuple(sorted(variants)), uncertain=remapped, includes=tuple(includes),
    )


def _cache_text(value: str) -> str | None:
    if _GUIDE_NAME.search(value) and _GUIDE_STEP.search(value):
        return _utf8(value)
    try:
        payload = value.encode("latin-1")
        decoder = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
        unpacked = decoder.decompress(payload, MAX_GUIDE + 1)
    except (zlib.error, UnicodeEncodeError):
        return None
    if not decoder.eof or len(unpacked) > MAX_GUIDE:
        return None
    # Read ordinary local Deflate guide caches. Require guide syntax before
    # processing and leave all other (including protected) formats opaque.
    text = _utf8(unpacked.decode("latin-1"))
    return text if _GUIDE_NAME.search(text) and _GUIDE_STEP.search(text) else None


def _cached_guides(data: dict, label: str) -> tuple[list[Guide], int]:
    profiles = _table(_table(data.get("RXPDB")).get("profiles"))
    guides = []
    opaque = 0
    expanded = 0
    for profile in profiles.values():
        for record in _table(_table(profile).get("guides")).values():
            entry = _table(record)
            payload = entry.get("groupOrContent")
            if not isinstance(payload, str):
                opaque += 1
                continue
            text = _cache_text(payload)
            if text is None:
                opaque += 1
                continue
            expanded += len(text)
            if expanded > MAX_TOTAL:
                raise ImportError("The expanded guide cache exceeds the local import size limit.")
            meta = _table(entry.get("metadata")).copy()
            for key in ("version", "enabledFor"):
                if key in entry:
                    meta.setdefault(key, entry[key])
            guide = parse_guide(text, label + " (local guide cache)", metadata=meta)
            if guide:
                guides.append(guide)
            else:
                opaque += 1
    return guides, opaque


def _guide_blocks(text: str) -> Iterator[str]:
    text = _utf8(text).removeprefix("\ufeff")
    first = next((line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("--")), "")
    if first.startswith(("#", "<<")):
        yield text
        return
    for kind, value in _tokens(text, code=True):
        if kind == "string" and _GUIDE_NAME.search(value) and _GUIDE_STEP.search(value):
            yield value


def _expand_guides(guides: dict[str, Guide], diagnostics: list[str]) -> tuple[Guide, ...]:
    lookup = {}
    for guide in guides.values():
        key = (guide.group.casefold(), guide.name.casefold())
        lookup.setdefault(key, []).append(guide)
    unresolved = set()
    failures = 0

    def include(parent: Guide, value: str, parents: frozenset[str]) -> list[Step]:
        nonlocal failures
        path, _, span = value.partition("@")
        group, separator, name = path.rpartition("\\")
        key = (group.casefold() if separator else parent.group.casefold(), name.strip().casefold())
        targets = lookup.get(key, ())
        if len(targets) == 1 and len(parents) < 20:
            target = targets[0]
            if target.id not in parents:
                start, _, end = span.partition("-")
                eligible = target.steps
                if start or end:
                    start_indices = [i for i, step in enumerate(eligible) if step.label == start or str(step.step_id) == start]
                    end_indices = [i for i, step in enumerate(eligible) if step.label == end or str(step.step_id) == end]
                    if (not start or len(start_indices) == 1) and (not end or len(end_indices) == 1):
                        first = start_indices[0] if start else 0
                        last = end_indices[0] if end else len(eligible) - 1
                        if first <= last:
                            entries = expand(target, eligible[first:last + 1], parents, False)
                            return [replace(item, condition=_combine_conditions(target.condition, item.condition)) for item in entries]
                else:
                    entries = expand(target, eligible, parents, True)
                    return [replace(item, condition=_combine_conditions(target.condition, item.condition)) for item in entries]
        failures += 1
        unresolved.add(parent.id)
        return []

    def expand(guide: Guide, steps: tuple[Step, ...], parents: frozenset[str], header: bool) -> list[Step]:
        parents = parents | {guide.id}
        expanded = []
        if header:
            for value in guide.includes:
                expanded.extend(include(guide, value, parents))
        for step in steps:
            if not step.includes:
                expanded.append(step)
                continue
            if step.actions or step.label or step.guards:
                expanded.append(step)
            for value in step.includes:
                included = include(guide, value, parents)
                if len(expanded) + len(included) > 25_000:
                    raise ImportError("Expanded guide includes exceed the step limit.")
                expanded.extend(
                    replace(
                        entry, condition=_combine_conditions(step.condition, entry.condition),
                        guards=step.guards + entry.guards,
                        uncertain=entry.uncertain or step.uncertain,
                    )
                    for entry in included
                )
                if not included:
                    expanded.append(replace(step, uncertain=True, branch=True))
        return expanded

    result = []
    for guide in guides.values():
        previous_failures = failures
        steps = expand(guide, guide.steps, frozenset(), True)
        result.append(replace(
            guide, steps=tuple(replace(step, number=index) for index, step in enumerate(steps, 1)),
            uncertain=guide.uncertain or previous_failures != failures,
        ))
    if unresolved:
        diagnostics.append(f"{len(unresolved)} guides refer to unavailable or ambiguous include ranges.")
    return tuple(result)


def load_guides(source: Path) -> GuideCollection:
    source = Path(source)
    selected: dict[str, Guide] = {}
    versions = set()
    interfaces = set()
    conflicts = set()
    unreadable = 0
    opaque = 0
    diagnostics = []
    for name, kind, value, _, _ in _members(source, "guide"):
        basename = PurePosixPath(name).name
        try:
            if kind == "toc":
                for header in re.finditer(r"(?im)^\s*##\s*(version|interface)\s*:\s*([^\r\n]+)", value):
                    if header[1].casefold() == "version":
                        versions.add(header[2].strip())
                    else:
                        interfaces.update(int(item) for item in re.findall(r"\b\d{4,7}\b", header[2]))
                continue
            if kind == "saved":
                if "RXPDB" not in value:
                    continue
                parsed, unknown = _cached_guides(parse_saved_variables(value), basename)
                opaque += unknown
            else:
                if not _GUIDE_NAME.search(value):
                    continue
                parsed = [guide for block in _guide_blocks(value) if (guide := parse_guide(block, name)) is not None]
        except ImportError:
            if source.is_file() and source.suffix.casefold() != ".zip":
                raise
            unreadable += 1
            continue
        for guide in parsed:
            previous = selected.get(guide.id)
            if previous is None or (guide.version or 0) > (previous.version or 0):
                selected[guide.id] = guide
                conflicts.discard(guide.id)
            elif (guide.version or 0) == (previous.version or 0):
                comparable = replace(guide, source=previous.source)
                if comparable != previous:
                    # Prefer saved usage if the only difference is a stable step ID.
                    if replace(comparable, steps=tuple(replace(step, step_id=None) for step in comparable.steps)) == replace(
                        previous, steps=tuple(replace(step, step_id=None) for step in previous.steps),
                    ):
                        if kind == "saved":
                            selected[guide.id] = guide
                    else:
                        conflicts.add(guide.id)
    if not selected:
        diagnostics.append("No supported plain guide text or readable local guide cache was found.")
    if opaque:
        diagnostics.append(f"{opaque} local cache records could not be read as ordinary guide text.")
    if unreadable:
        diagnostics.append(f"{unreadable} files were outside the supported input format.")
    if conflicts:
        diagnostics.append(f"{len(conflicts)} guide identities have conflicting same-version copies and require review.")
    if len(versions) > 1:
        diagnostics.append("Multiple RestedXP versions were found in the selected input.")
    selected = {id_: replace(guide, uncertain=True) if id_ in conflicts else guide for id_, guide in selected.items()}
    guides = _expand_guides(selected, diagnostics)
    return GuideCollection(
        guides,
        tuple(diagnostics), next(iter(versions)) if len(versions) == 1 else None, tuple(sorted(interfaces)),
    )
