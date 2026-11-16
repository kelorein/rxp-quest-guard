"""Read stable, character-owned Quest Guard saves from a WoW installation."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .ingest import MAX_ENTRIES, Snapshot, load_snapshots, merge_snapshots


@dataclass(frozen=True)
class SaveVersion:
    device: int
    file_id: int
    modified_ns: int
    created_ns: int
    size: int


@dataclass(frozen=True)
class MonitorUpdate:
    saves: tuple[tuple[Path, Snapshot], ...]
    problems: tuple[str, ...]
    file_count: int


def file_version(path: Path) -> SaveVersion | None:
    try:
        entry = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(entry.st_mode):
        return None
    return SaveVersion(entry.st_dev, entry.st_ino, entry.st_mtime_ns, entry.st_ctime_ns, entry.st_size)


def saved_paths(client: Path) -> tuple[Path, ...]:
    """Find the exact per-character SavedVariables path without a recursive scan."""
    account_root = client / "WTF" / "Account"
    if account_root.parent.is_symlink() or account_root.parent.is_junction():
        return ()
    visited = 0

    def directories(parent: Path):
        nonlocal visited
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        raise ValueError("The installed SavedVariables directory exceeds the inspection limit.")
                    folder = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False) and not folder.is_junction():
                        yield folder
        except FileNotFoundError:
            return

    if account_root.is_symlink() or account_root.is_junction():
        return ()
    paths = []
    for account in directories(account_root):
        for realm in directories(account):
            if realm.name.casefold() == "savedvariables":
                continue
            for character in directories(realm):
                saved = character / "SavedVariables"
                if saved.is_symlink() or saved.is_junction():
                    continue
                path = saved / "RXPQuestGuard.lua"
                if file_version(path) is not None:
                    paths.append(path)
    return tuple(sorted(paths))


class SavedProgressMonitor:
    """Require two matching observations, then verify the file again after parsing."""

    def __init__(self, client: Path) -> None:
        self.client = client
        self.observed: dict[Path, SaveVersion] = {}
        self.handled: dict[Path, SaveVersion] = {}
        self.accepted: dict[Path, Snapshot] = {}
        self.errors: dict[Path, str] = {}

    def poll(self) -> MonitorUpdate:
        try:
            versions = {path: version for path in saved_paths(self.client)
                        if (version := file_version(path)) is not None}
        except (OSError, ValueError) as exc:
            return MonitorUpdate((), (f"Could not check WoW's saved progress: {exc}",), 0)

        saves = []
        self.handled = {path: version for path, version in self.handled.items() if path in versions}
        self.errors = {path: problem for path, problem in self.errors.items() if path in versions}
        for path, version in versions.items():
            if self.handled.get(path) == version or self.observed.get(path) != version:
                continue
            try:
                snapshot = self.read_save(path, version)
            except OSError:
                # A filesystem lock can clear without a new file timestamp.
                self.errors[path] = "Quest Guard's save is temporarily unreadable. Retrying locally."
                continue
            except ValueError as exc:
                self.errors[path] = str(exc)
                self.handled[path] = version
                continue
            if snapshot is None:
                continue
            self.errors.pop(path, None)
            self.handled[path] = version
            self.accepted[path] = snapshot
            saves.append((path, snapshot))

        self.observed = versions
        problems = tuple(dict.fromkeys(self.errors.values()))
        if set(self.accepted) - versions.keys():
            problems = (*problems, "An accepted save is temporarily missing. Its last good snapshot was kept.")
        return MonitorUpdate(tuple(saves), problems, len(versions))

    def read_save(self, path: Path, version: SaveVersion) -> Snapshot | None:
        try:
            rows = load_snapshots(path)
        except ValueError as exc:
            if file_version(path) != version:
                return None
            raise ValueError("A Quest Guard save is incomplete or invalid. Waiting for the next completed WoW save.") from exc
        if file_version(path) != version:
            return None
        snapshot = rows[0] if len(rows) == 1 else None
        if (not snapshot or not snapshot.identity_key or snapshot.ownership_conflict
                or snapshot.state_source != "Quest Guard" or not snapshot.exported_at
                or snapshot.active is None or snapshot.completed is None):
            raise ValueError("A Quest Guard save lacks verified character ownership or complete quest state.")

        previous = self.accepted.get(path)
        if previous and snapshot.exported_at < previous.exported_at:
            raise ValueError("An older Quest Guard save was left out. The latest good snapshot was kept.")

        # RestedXP can save a newer position separately during the same reload.
        progress_path = path.with_name("RXPGuides.lua")
        try:
            progress_version = file_version(progress_path)
            if progress_version is not None:
                progress = load_snapshots(progress_path)
                if file_version(progress_path) == progress_version:
                    eligible = (row for row in progress if row.identity_key == snapshot.identity_key
                                and not row.ownership_conflict and row.current_guide)
                    snapshot, = merge_snapshots((snapshot, *eligible))
        except (OSError, ValueError):
            # The Quest Guard snapshot itself remains usable during a companion-file rotation.
            pass
        return snapshot if file_version(path) == version else None
