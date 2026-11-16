"""Detect the Forever install and publish this addon's files."""

from __future__ import annotations

import math
import os
import re
import shutil
import string
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import __version__

if TYPE_CHECKING:
    from .ingest import Guide, Snapshot
    from .sync import Database

ADDON_NAME = "RXPQuestGuard"
ADDON_TITLE = "RXP Quest Guard"
CLIENT_DIRECTORIES = ("_forever_", "_camelot_")
MAX_LUA_BYTES = 24 * 1024 * 1024


@dataclass(frozen=True)
class Installation:
    addons: Path
    interface: int | None
    restedxp_version: str | None
    status: str
    client_version: str | None = None


def _toc(path: Path) -> dict[str, str]:
    try:
        with path.open("rb") as file:
            content = file.read(65536).decode("utf-8-sig", errors="replace")
    except OSError:
        return {}
    return {key.lower(): value.strip() for key, value in re.findall(r"^##\s*([\w-]+):\s*([^\r\n]*)", content, re.M)}


def _forever_build(client: Path) -> tuple[int | None, str | None]:
    for directory in (client, client.parent):
        try:
            with (directory / ".build.info").open(encoding="utf-8-sig") as file:
                lines = file.read(65536).splitlines()
        except (OSError, UnicodeError):
            continue
        if not lines:
            continue
        header = [column.split("!", 1)[0] for column in lines[0].split("|")]
        for line in lines[1:]:
            fields = dict(zip(header, line.split("|")))
            product = fields.get("Product", "").lower()
            version = fields.get("Version", "")
            if product not in {"wowf", "wow_camelot"} or fields.get("Active") != "1":
                continue
            match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{1,2})(?:\.\d+)?", version)
            if match:
                major, minor, patch = map(int, match.groups())
                return major * 10000 + minor * 100 + patch, version
    return None, None


def inspect_installation(path: Path | str) -> Installation:
    selected = Path(path).expanduser().resolve()
    if selected.name.lower() == "addons" and selected.parent.name.lower() == "interface":
        candidates = (selected,)
    elif selected.name.lower() == "interface":
        candidates = (selected / "AddOns",)
    else:
        candidates = tuple(selected / client / "Interface" / "AddOns" for client in CLIENT_DIRECTORIES)
        candidates += (selected / "Interface" / "AddOns",)
    addons = next((candidate.resolve() for candidate in candidates if candidate.is_dir()), None)
    if addons is None:
        raise ValueError("Select the WoW Forever folder or its Interface/AddOns folder.")
    client = addons.parent.parent
    if client.name.casefold() in {"_retail_", "_classic_", "_classic_era_", "_ptr_", "_beta_"}:
        raise ValueError("The selected AddOns folder belongs to a different WoW client.")
    interface, client_version = _forever_build(client)
    restedxp = addons / "RXPGuides"
    toc_names = ("RXPGuides_Forever.toc", "RXPGuides_Camelot.toc", "RXPGuides.toc")
    tocs = [_toc(restedxp / name) for name in toc_names]
    version = next((toc["version"] for toc in tocs if toc.get("version")), None)
    status = "Interface from local client build"
    if interface is None:
        candidates = {int(number) for toc in tocs for number in re.findall(r"\b16\d{3}\b", toc.get("interface", ""))}
        if len(candidates) == 1:
            interface = candidates.pop()
            status = "Interface from installed RestedXP TOC. Confirm the number in WoW if the addon is outdated."
        else:
            status = "Client interface unknown. Enter the fourth GetBuildInfo() value."
    if not any(tocs):
        status += " RestedXP was not found."
    return Installation(addons, interface, version, status, client_version)


def _install_candidates():
    override = os.environ.get("QG_WOW_PATH")
    if override:
        yield Path(override)
    for key in ("ProgramFiles(x86)", "ProgramFiles"):
        if folder := os.environ.get(key):
            yield Path(folder) / "World of Warcraft"
    if os.name != "nt":
        return
    import ctypes
    import winreg

    keys = (r"SOFTWARE\Blizzard Entertainment\World of Warcraft",
            r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft")
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in keys:
            try:
                with winreg.OpenKey(hive, key) as handle:
                    for name in ("InstallPath", "GamePath"):
                        try:
                            location, _ = winreg.QueryValueEx(handle, name)
                            yield Path(location)
                        except (OSError, TypeError):
                            pass
            except OSError:
                pass
    for drive in string.ascii_uppercase:
        root = f"{drive}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
            for publisher in ("", "Blizzard", "Games", "Battle.net"):
                yield Path(root) / publisher / "World of Warcraft"


def discover_installations() -> list[Installation]:
    results = {}
    for candidate in _install_candidates():
        try:
            installation = inspect_installation(candidate)
        except (OSError, ValueError):
            continue
        results[installation.addons] = installation
    return list(results.values())


def addon_source() -> Path:
    return Path(__file__).resolve().parent.parent / "addon" / ADDON_NAME


def _lua(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return repr(value)
    if isinstance(value, str):
        escaped = []
        for byte in value.encode("utf-8"):
            if 32 <= byte <= 126 and byte not in (34, 92):
                escaped.append(chr(byte))
            else:
                escaped.append(f"\\{byte:03d}")
        return '"' + "".join(escaped) + '"'
    if isinstance(value, (tuple, list)):
        return "{" + ",".join(map(_lua, value)) + "}"
    if isinstance(value, dict):
        return "{" + ",".join(f"[{_lua(key)}]={_lua(item)}" for key, item in value.items()) + "}"
    raise TypeError(f"Unsupported data type for Lua generation: {type(value).__name__}")


def _guide_metadata(route: Sequence) -> list[dict]:
    records = []
    for guide in route:
        steps = []
        branched = False
        resolved = {"", "forever", (guide.faction or "").casefold()}
        chapter_conditional = bool(guide.uncertain or guide.condition.strip().casefold() not in resolved)
        for step in guide.steps:
            actions = []
            for action in step.actions:
                if action.kind not in {"accept", "complete", "turnin", "abandon"}:
                    continue
                guarded = any(action.quest_id in guard.quest_ids for guard in step.guards)
                record = {"kind": action.kind, "questId": action.quest_id,
                          "optional": step.optional or action.optional, "guarded": guarded,
                          "conditional": bool(action.uncertain or action.condition.strip().casefold() not in resolved)}
                if action.objective is not None:
                    record["objectiveIndex"] = action.objective
                if action.name:
                    record["name"] = action.name
                if action.kind == "accept" and (location := action.location or step.location):
                    record["location"] = {name: value for name, value in (
                        ("name", location.name), ("mapId", location.map_id), ("x", location.x),
                        ("y", location.y), ("zone", location.zone),
                    ) if value is not None}
                    if location.world:
                        record["location"].update(zip(("worldMapId", "worldFloor", "worldX", "worldY"), location.world))
                actions.append(record)
            guards = [{"kind": guard.kind, "questIds": guard.quest_ids,
                       "conditional": guard.condition.strip().casefold() not in resolved} for guard in step.guards]
            result = {"index": step.number, "optional": step.optional,
                      "conditional": bool(step.uncertain or step.branch or branched or
                                          step.condition.strip().casefold() not in resolved),
                      "actions": actions, "guards": guards}
            steps.append(result)
            branched = branched or step.branch
        chapter = {"id": guide.id, "name": guide.name, "group": guide.group or "", "subgroup": guide.subgroup or "",
                   "conditional": chapter_conditional, "steps": steps}
        if guide.faction:
            chapter["faction"] = guide.faction
        for field, value in (("guideVersion", guide.version), ("levelMin", guide.level_min), ("levelMax", guide.level_max)):
            if value is not None:
                chapter[field] = value
        records.append(chapter)
    return records


def generate_addon_data(database: Database | None, route: Sequence = (), *, route_context: dict | None = None,
                        route_info: dict | None = None) -> bytes:
    data = {"schemaVersion": 1, "source": database.source if database else "Local guide / game quest log",
            "sourceVersion": database.source_version if database else "Unknown",
            "updatedAt": database.updated_at if database else "Unknown"}
    records = _guide_metadata(route)
    quests = database.quests if database else {}
    if database and database.has_foreverdb:
        from .evidence import FOREVERDB_URL

        data["sourceUrl"] = FOREVERDB_URL
    if route_context:
        from .sync import timestamp

        data.update(routeContext=route_context, publisherVersion=__version__, publishedAt=timestamp())
    if route_info:
        data["routeInfo"] = route_info
    # Separate records keep Lua constructor and source-line sizes modest.
    lines = ["-- RXP Quest Guard. Generated quest metadata.\nlocal _, QG = ...\n", f"QG.data = {_lua(data)}\n",
             "QG.data.quests = {\n"]
    lines.extend(f"[{quest_id}]={_lua(quest)},\n" for quest_id, quest in sorted(quests.items()))
    lines.append("}\nQG.data.route = {}\n")
    for index, guide in enumerate(records, 1):
        steps = guide.pop("steps")
        lines.append(f"QG.data.route[{index}] = {_lua(guide)}\nQG.data.route[{index}].steps = {{\n")
        lines.extend(f"{_lua(step)},\n" for step in steps)
        lines.append("}\n")
    encoded = "".join(lines).encode("ascii")
    if len(encoded) > MAX_LUA_BYTES:
        raise ValueError("Generated addon data exceeds its size limit. Choose a smaller route or dataset.")
    return encoded


def _installed_directory(installation: Installation) -> Path:
    addons = installation.addons
    if not addons.is_dir() or addons.resolve() != addons:
        raise ValueError("The selected AddOns directory moved or points to another location.")
    target = addons / ADDON_NAME
    if target.resolve() != target:
        raise ValueError("The Quest Guard directory points to another location.")
    if target.exists() and _toc(target / f"{ADDON_NAME}.toc").get("title") != ADDON_TITLE:
        raise ValueError("The existing RXPQuestGuard folder could not be identified as this addon.")
    return target


def write_addon_data(installation: Installation, database: Database | None, route: Sequence = ()) -> Path:
    from .sync import atomic_write

    target = _installed_directory(installation)
    if not target.is_dir():
        raise ValueError("Install RXP Quest Guard before sending quest data.")
    output = target / "Data.lua"
    atomic_write(output, generate_addon_data(database, route), create_parent=False)
    return output


_GENERATED_HEADER = b"-- RXP Quest Guard. Generated quest metadata."
_LEGACY_PLACEHOLDER = b"QG.data = {schemaVersion = 1, quests = {}, route = {}}"


def _read_published_database(path: Path) -> Database | None:
    """Recover the publisher's literal metadata only, including version 0.1.0 files."""
    from .ingest import parse_saved_variables
    from .sync import _decode_database

    if path.resolve().parent != path.parent:
        raise ValueError("The current addon data points outside its own directory.")
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_LUA_BYTES + 1)
    except FileNotFoundError:
        return None
    content = raw.replace(b"\r\n", b"\n")
    if len(content) < 300 and content.rstrip().endswith(_LEGACY_PLACEHOLDER):
        return None

    def decode(value):
        if isinstance(value, str):
            return value.encode("latin-1").decode("utf-8")
        if isinstance(value, dict):
            return {decode(key): decode(item) for key, item in value.items()}
        return value

    def list_values(value):
        if not isinstance(value, dict) or set(value) != set(range(1, len(value) + 1)):
            raise ValueError("Expected a Lua list")
        return [value[index] for index in range(1, len(value) + 1)]

    list_fields = {"classes", "requirements", "requirementGroups", "chain", "objectives", "startOptions", "endOptions"}

    def fact_list(field, value):
        if field not in list_fields:
            return value
        rows = list_values(value)
        return [list_values(group) for group in rows] if field == "requirementGroups" else rows

    def claim_list(field, value):
        rows = list_values(value)
        for claim in rows:
            if not isinstance(claim, dict) or "value" not in claim:
                raise ValueError("Expected a literal source claim")
            claim["value"] = fact_list(field, claim["value"])
        return rows

    try:
        if len(raw) > MAX_LUA_BYTES or not content.startswith(_GENERATED_HEADER + b"\n"):
            raise ValueError("Unrecognized file")
        _, _, literals = content.partition(b"\nQG.data = ")
        metadata, separator, remainder = literals.partition(b"\nQG.data.quests = ")
        quests, following, _ = remainder.partition(b"\nQG.data.route = {}\n")
        if not (separator and following):
            raise ValueError("Missing Quest Guard data sections")
        values = decode(parse_saved_variables((b"metadata=" + metadata + b"\nquests=" + quests).decode("latin-1")))
        metadata, quests = values["metadata"], values["quests"]
        if not isinstance(metadata, dict) or not isinstance(quests, dict):
            raise ValueError("Expected Lua tables")
        for quest in quests.values():
            if not isinstance(quest, dict):
                raise ValueError("Expected a quest record")
            for field in list_fields:
                if field in quest:
                    quest[field] = fact_list(field, quest[field])
            if isinstance(quest.get("fieldEvidence"), dict):
                quest["fieldEvidence"] = {key: claim_list(key, rows) for key, rows in quest["fieldEvidence"].items()}
            if "conflicts" in quest:
                quest["conflicts"] = list_values(quest["conflicts"])
                for conflict in quest["conflicts"]:
                    if not isinstance(conflict, dict) or not isinstance(conflict.get("field"), str):
                        raise ValueError("Expected a literal source conflict")
                    conflict["claims"] = claim_list(conflict["field"], conflict.get("claims"))
        normalized = _decode_database({**metadata, "quests": {str(key): value for key, value in quests.items()}})
        return normalized if normalized.quests else None
    except (ValueError, UnicodeError, KeyError) as exc:
        raise ValueError("Existing quest data could not be preserved. Import a quest database before replacing it.") from exc


def publish_route(installation: Installation, database: Database | None, route: Sequence[Guide], *,
                  snapshot: Snapshot, start_chapter_id: str, interface: int | None = None,
                  guide_release: str | None = None) -> Path:
    """Bind the selected preflight route to its character and publish one atomic data update."""
    from .sync import Database, atomic_write

    installed = inspect_installation(installation.addons)
    selected_interface = installed.interface if interface is None else interface
    if installed.interface is not None and installed.interface != selected_interface:
        raise ValueError("The selected interface differs from this Forever installation.")
    if selected_interface is None:
        raise ValueError("The WoW Forever interface could not be verified. Enter the value from GetBuildInfo().")
    # Validate the type and range even if the interface came from a user override.
    installed_toc(b"## Interface: 0", selected_interface)
    client_folder = installed.addons.parent.parent.name.casefold()
    if installed.client_version is None and (client_folder not in CLIENT_DIRECTORIES or not 16000 <= selected_interface < 20000):
        raise ValueError("Select the WoW Forever installation before publishing a route.")
    if snapshot.client_interface is not None and snapshot.client_interface != selected_interface:
        raise ValueError("The selected character snapshot belongs to another client interface. Refresh progress.")
    if not (snapshot.character and snapshot.realm):
        raise ValueError("Select a preflight for an identified character before publishing.")
    chapters = {guide.id: guide for guide in route}
    start = chapters.get(start_chapter_id)
    if not start or not start.steps or len(chapters) != len(route):
        raise ValueError("The route must contain steps in the selected chapter and have unique guide identities.")
    if snapshot.faction and any(guide.faction and guide.faction.casefold() != snapshot.faction.casefold() for guide in route):
        raise ValueError("The route contains a chapter for another faction.")

    # The selected guide path is already resolved by the route planner. Level
    # metadata marks its endpoint but never selects or joins chapter variants.
    target_level = 60
    start_index = next(index for index, guide in enumerate(route) if guide.id == start_chapter_id)
    terminal = next((index for index in range(start_index, len(route))
                     if route[index].level_max is not None and route[index].level_max >= target_level), None)
    if terminal is not None:
        route = route[:terminal + 1]
    info = {"targetLevel": target_level, "terminalChapterId": route[-1].id,
            "complete": terminal is not None and all(guide.steps and not guide.uncertain for guide in route[start_index:])}
    if guide_release:
        info["guideRelease"] = guide_release
    if installed.client_version:
        info["clientVersion"] = installed.client_version

    target = _installed_directory(installed)
    toc_path = target / f"{ADDON_NAME}.toc"
    toc = _toc(toc_path)
    if toc.get("version") != __version__ or str(selected_interface) not in toc.get("interface", "").replace(",", " ").split():
        raise ValueError(f"Install Quest Guard {__version__} for this Forever interface before publishing.")
    required = (target / module.name for module in addon_source().glob("*.lua") if module.name != "Data.lua")
    if any(not path.is_file() or path.resolve().parent != target for path in required):
        raise ValueError("The Quest Guard installation is incomplete. Reinstall the addon before publishing.")
    output = target / "Data.lua"
    previous = _read_published_database(output)
    if database is None:
        database = previous
    elif previous is not None:
        database = Database({**previous.quests, **database.quests}, database.source,
                            database.source_version, database.updated_at)
    context = {"character": snapshot.character, "realm": snapshot.realm,
               "clientInterface": selected_interface, "startChapterId": start_chapter_id}
    if snapshot.faction:
        context["faction"] = snapshot.faction
    if snapshot.character_class:
        context["class"] = snapshot.character_class.upper()
    atomic_write(output, generate_addon_data(database, route, route_context=context, route_info=info), create_parent=False)
    return output


def installed_toc(source: bytes, interface: int) -> bytes:
    if type(interface) is not int or not 10000 <= interface <= 999999:
        raise ValueError("Enter the numeric WoW interface from GetBuildInfo().")
    toc, changes = re.subn(rb"(?m)^## Interface:[^\r\n]*", f"## Interface: {interface}".encode(), source, count=1)
    if changes != 1:
        raise ValueError("RXP Quest Guard TOC has no Interface field.")
    return toc


def _rename(source: Path, target: Path) -> None:
    for pause in (0.02, 0.05, 0.15, None):
        try:
            source.rename(target)
            return
        except PermissionError:
            if pause is None:
                raise
            time.sleep(pause)


def install_addon(installation: Installation, database: Database | None = None, route: Sequence = (), *,
                  interface: int | None = None) -> Path:
    from .sync import atomic_write

    interface = installation.interface if interface is None else interface
    source = addon_source()
    toc_name = f"{ADDON_NAME}.toc"
    toc = installed_toc((source / toc_name).read_bytes(), interface)
    target = _installed_directory(installation)
    data = None
    if database is not None or route:
        if database is None:
            database = _read_published_database(target / "Data.lua")
        data = generate_addon_data(database, route)
    code = tuple(sorted(source.glob("*.lua")))
    manifest = target / toc_name
    if manifest.is_file() and manifest.read_bytes() == toc and all(
        (target / path.name).is_file() and (target / path.name).read_bytes() == path.read_bytes()
        for path in code if path.name != "Data.lua"
    ):
        output = target / "Data.lua"
        if data is not None or not output.is_file():
            atomic_write(output, data if data is not None else (source / "Data.lua").read_bytes(), create_parent=False)
        return target
    stage = Path(tempfile.mkdtemp(prefix=".qg-", dir=installation.addons))
    previous = stage / "previous"
    staged_addon = stage / "new"
    try:
        staged_addon.mkdir()
        for path in code:
            shutil.copyfile(path, staged_addon / path.name)
        (staged_addon / toc_name).write_bytes(toc)
        if data is None and (target / "Data.lua").is_file():
            old_data = target / "Data.lua"
            if old_data.resolve().parent != target:
                raise ValueError("The current addon data points outside its own directory.")
            shutil.copyfile(old_data, staged_addon / "Data.lua")
        if data is not None:
            (staged_addon / "Data.lua").write_bytes(data)
        if target.exists():
            _rename(target, previous)
        try:
            _rename(staged_addon, target)
        except OSError:
            if previous.exists() and not target.exists():
                _rename(previous, target)
            raise
    finally:
        if target.exists() or not previous.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return target
