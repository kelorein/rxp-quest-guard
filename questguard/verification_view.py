"""Desktop interface for inspecting and authorizing quest verification."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import os
from queue import Empty, SimpleQueue
import re
from threading import Event
import time
import tkinter as tk
from tkinter import ttk
from typing import Any
from urllib.parse import urlsplit
import webbrowser

from .engine import Route
from .evidence import (CLASSIC, FOREVER_SPECIFIC, FOREVERDB_CREDIT, FOREVERDB_URL, GUIDE,
                       STRUCTURED, UNVERIFIED, VERIFIED_GAME, from_foreverdb)
from .providers import ForeverDBProvider
from .verification import (
    LookupSettings,
    VerificationCache,
    guide_discrepancies,
    plan_verification,
    quest_reference,
    verification_state,
    verified_database,
    verify_quests,
)


UNKNOWN = "Unknown"
FOREVERDB_DEVELOPERS = "https://foreverdb.net/developers"
ALLOWED_SOURCE_HOSTS = {"foreverdb.net", "www.foreverdb.net", "wowhead.com", "www.wowhead.com"}
AUTO_RETRY_SECONDS = 300
MODES = {"selected": "selected", "current": "chapter", "upcoming": "prefetch", "route": "full", "auto": "smart"}
FIELD_NAMES = {
    "startItem": "Quest-starting item",
    "timerSeconds": "Quest timer",
    "startLocation": "Pickup location",
    "endLocation": "Turn-in location",
    "startOptions": "Possible pickups",
    "endOptions": "Possible turn-ins",
    "requirements": "Prerequisites",
    "requirementGroups": "Prerequisite alternatives",
    "questLevel": "Quest level",
    "minLevel": "Minimum player level",
    "questLine": "Quest chain",
}
CONFIDENCE_NAMES = {
    VERIFIED_GAME: "Verified game state",
    FOREVER_SPECIFIC: "Forever-specific evidence",
    STRUCTURED: "Authoritative structured metadata",
    CLASSIC: "Inherited Classic information",
    GUIDE: "Guide-derived inference",
    UNVERIFIED: "Unverified",
}


def display_text(value: Any, default: str = UNKNOWN) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def display_timestamp(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        return UNKNOWN
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, OSError, OverflowError):
        return UNKNOWN


def trusted_reference(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return (parts.scheme == "https" and parts.hostname in ALLOWED_SOURCE_HOSTS and not parts.query and not parts.fragment
                and parts.username is None and parts.password is None and parts.port in {None, 443})
    except (ValueError, TypeError):
        return False


def describe_value(field: str, value: Any) -> str:
    if field == "timerSeconds" and type(value) is int:
        hours, remaining = divmod(value, 3600)
        minutes, seconds = divmod(remaining, 60)
        return " ".join(f"{amount} {unit}" for amount, unit in ((hours, "hr"), (minutes, "min"), (seconds, "sec")) if amount)
    if isinstance(value, Mapping):
        names = {"itemId": "Item ID", "questId": "Quest ID", "mapId": "Map ID", "verifiedForever": "Forever verified"}
        return " · ".join(f"{names.get(key, key.capitalize())} {item}" for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return "\n".join(describe_value(field, item) for item in value) or "None reported"
    return str(value)


@dataclass(frozen=True)
class QuestView:
    quest_id: int
    name: str
    state: str
    character: str
    source: str
    confidence: str
    timestamp: str
    action: str
    detail: str = ""
    links: tuple[tuple[str, str], ...] = ()


def audit_changes(previous: Any, current: Any) -> str:
    if previous is None:
        return "There was no earlier preflight for this route and snapshot."

    def indexed(audit: Any) -> dict:
        return {(row.quest_id, row.chapter_id, row.step, row.category): row
                for row in (*audit.findings, *audit.historical_findings)}

    old, new = indexed(previous), indexed(current)
    added = [row for key, row in new.items() if key not in old]
    resolved = [row for key, row in old.items() if key not in new]
    revised = [(old[key], row) for key, row in new.items() if key in old and old[key] != row]
    if not (added or resolved or revised):
        return "No preflight findings changed. Newly cached evidence is available in the quest details."
    lines = [f"{len(added)} added · {len(resolved)} removed · {len(revised)} revised"]
    for row in added:
        lines.append(f"\nAdded · {row.title} ({row.quest_id}), step {row.step}\n{row.detail}")
    for row in resolved:
        lines.append(f"\nRemoved · {row.title} ({row.quest_id}), step {row.step}\n{row.detail}")
    for before, after in revised:
        changes = [f"\nRevised · {after.title} ({after.quest_id}), step {after.step}"]
        for field in ("severity", "classification", "confidence", "source"):
            old_value, new_value = getattr(before, field), getattr(after, field)
            if old_value != new_value:
                changes.append(f"{field.title()} · {old_value} → {new_value}")
        if before.detail != after.detail:
            changes.extend((f"Earlier: {before.detail}", f"Current: {after.detail}"))
        lines.append("\n".join(changes))
    return "\n".join(lines)


class VerificationPanel:
    def __init__(self, dashboard: Any, page: ttk.Frame) -> None:
        self.dashboard = dashboard
        self.page = page
        self.window = dashboard.window
        self.authorized_key: str | None = None
        self.intelligent = tk.BooleanVar(value=False)
        self.verify_unknown = tk.BooleanVar(value=True)
        self.verify_missing = tk.BooleanVar(value=True)
        self.prefetch = tk.BooleanVar(value=True)
        self.chapter_count = tk.StringVar(value="2")
        self.prefer_cache = tk.BooleanVar(value=True)
        self.refresh_outdated = tk.BooleanVar(value=True)
        self.facts_days = tk.StringVar(value="30")
        self.dependencies_days = tk.StringVar(value="14")
        self.auto_publish = tk.BooleanVar(value=False)
        self.notify_reload = tk.BooleanVar(value=True)
        self.auto_publish_target = None
        self.query = tk.StringVar()
        self.heading = tk.StringVar(value="Current guide · Unknown")
        self.summary = tk.StringVar()
        self.status = tk.StringVar(value="Authorize ForeverDB in Provider settings to enable online requests.")
        self.pending: set[int] = set()
        self.unavailable: dict[int, str] = {}
        self.provider_errors = 0
        self.cache = VerificationCache()
        self.projected = None
        self.projection_key = None
        self.view_key = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quest-verification")
        self.future: Future | None = None
        self.after: str | None = None
        self.context_after: str | None = None
        self.messages: SimpleQueue[str] = SimpleQueue()
        self.cancelled: Event | None = None
        self.retry_after: dict[tuple, float] = {}
        self.waiting_auto = False
        self.origin: tuple | None = None
        self.authorization_revision = 0
        self.origin_revision = 0
        self.batch_automatic = False
        self.comparison = None
        self.publish_after_scan: tuple | None = None
        self.rows: dict[int, QuestView] = {}
        self.differences = "Run a verification to compare its results with the previous preflight."
        self.differences_has_foreverdb = False
        self.settings_dialog: tk.Toplevel | None = None
        self.compose()

    def compose(self) -> None:
        header = ttk.Frame(self.page)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, textvariable=self.heading, style="Section.TLabel").pack(side="left")
        self.dashboard.button(header, "Provider settings…", self.show_settings, busy_sensitive=False).pack(side="right")
        self.attribution = ttk.Button(header, text=FOREVERDB_CREDIT, style="Link.TButton",
                                      command=lambda: webbrowser.open(FOREVERDB_URL))
        ttk.Label(self.page, textvariable=self.summary, style="Hint.TLabel", wraplength=900).pack(anchor="w", pady=(0, 6))
        working = ttk.Frame(self.page)
        working.pack(fill="x")
        ttk.Label(working, textvariable=self.status, style="Caution.TLabel", wraplength=800).pack(side="left")
        self.progress = ttk.Progressbar(working, mode="indeterminate", length=110)

        controls = ttk.Frame(self.page)
        controls.pack(fill="x", pady=(8, 5))
        ttk.Label(controls, text="Quest name or ID", style="Hint.TLabel").pack(side="left", padx=(0, 5))
        ttk.Entry(controls, textvariable=self.query, width=20).pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.dashboard.button(controls, "Verify selected quest", lambda: self.request("selected"),
                              busy_sensitive=False).pack(side="left", padx=(0, 5))
        self.dashboard.button(controls, "Verify current chapter", lambda: self.request("current"),
                              busy_sensitive=False).pack(side="left", padx=(0, 5))
        self.dashboard.button(controls, "Prefetch upcoming", lambda: self.request("upcoming"),
                              busy_sensitive=False).pack(side="left")

        actions = ttk.Frame(self.page)
        actions.pack(fill="x", pady=(0, 7))
        for name, callback in (
            ("Refresh route to level 60", lambda: self.request("route")),
            ("View source", self.open_source),
            ("Wowhead reference", self.open_wowhead),
            ("View preflight differences", self.show_differences),
            ("Publish verified data", self.publish),
        ):
            self.dashboard.button(actions, name, callback, busy_sensitive=False).pack(side="left", padx=(0, 5))
        self.cancel_button = self.dashboard.button(actions, "Cancel lookups", self.cancel, busy_sensitive=False)
        self.cancel_button.pack(side="right")
        self.cancel_button.state(["disabled"])

        details = ttk.Frame(self.page)
        details.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Label(details, text="Evidence & recommended action", style="Hint.TLabel").pack(anchor="w", pady=(0, 2))
        self.detail = self.dashboard.text_view(details, height=9)
        self.table = self.dashboard.tree(self.page, [("Quest", 193, True), ("ID", 60, False),
                                                       ("Verification state", 160, False), ("Saved character state", 136, False),
                                                       ("Source", 112, False), ("Confidence", 142, False),
                                                       ("Last verified", 136, False), ("Recommended action", 200, True)], height=9)
        self.table.bind("<<TreeviewSelect>>", self.show_details)
        self.query.trace_add("write", lambda *_args: self.fill_table())
        self.fill_table()

    def fill_table(self) -> None:
        selected = self.table.selection()
        self.table.delete(*self.table.get_children())
        query = self.query.get().strip().casefold()
        for row in self.rows.values():
            if query and query not in row.name.casefold() and query not in str(row.quest_id):
                continue
            self.table.insert("", "end", iid=str(row.quest_id), values=(
                row.name, row.quest_id, row.state, row.character, row.source, row.confidence, row.timestamp, row.action,
            ))
        children = self.table.get_children()
        if children:
            self.table.selection_set(selected[0] if selected and selected[0] in children else children[0])
        self.show_details()

    def selected(self) -> QuestView | None:
        keys = self.table.selection()
        return self.rows.get(int(keys[0])) if keys else None

    def selected_id(self) -> int | None:
        row = self.selected()
        if row:
            return row.quest_id
        query = self.query.get().strip()
        if re.fullmatch(r"\d{1,10}", query) and 0 < int(query) <= 2_147_483_647:
            return int(query)
        return None

    def show_details(self, _event: Any = None) -> None:
        row = self.selected()
        if row is None:
            text = "No rows match the filter." if self.query.get().strip() else (
                "Import a linked RestedXP route or search for a quest ID to verify."
            )
            self.dashboard.put_text(self.detail, text)
            return
        references = "\n".join(f"{label} · {url}" for label, url in row.links)
        self.dashboard.put_text(self.detail, "\n\n".join(filter(None, (
            f"{row.name} ({row.quest_id}) · {row.state}", row.detail, row.action, references,
        ))))

    def open_source(self) -> None:
        row = self.selected()
        link = next((url for _, url in row.links if trusted_reference(url)), None) if row else None
        if link:
            webbrowser.open(link)
        else:
            self.status.set("This quest has no external reference. Use Wowhead reference for manual research.")

    def open_wowhead(self) -> None:
        quest_id = self.selected_id()
        if quest_id is None:
            self.status.set("Select a quest or enter a quest ID before opening its reference.")
        else:
            webbrowser.open(quest_reference(quest_id))

    def show_settings(self) -> None:
        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.settings_dialog.lift()
            return
        dialog = self.settings_dialog = tk.Toplevel(self.window)
        dialog.title("Quest verification settings")
        dialog.transient(self.window)
        panel = ttk.Frame(dialog, padding=18)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="ForeverDB provider", style="Section.TLabel").pack(anchor="w")
        ttk.Label(panel, text="Authorize ForeverDB to receive quest IDs selected for verification, including automatic "
                              "lookups for your route. Your API key is kept for this session. Character data stays on this device.",
                  wraplength=520, style="Hint.TLabel").pack(anchor="w", pady=(4, 8))
        key_var = tk.StringVar()
        access = ttk.Frame(panel)
        access.pack(fill="x", pady=(0, 5))
        ttk.Label(access, text="API key").pack(side="left", padx=(0, 7))
        ttk.Entry(access, textvariable=key_var, show="•", width=24).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(access, text="Authorize ForeverDB", style="Primary.TButton",
                   command=lambda: (self.authorize(key_var.get()), key_var.set(""))).pack(side="left", padx=(0, 6))
        ttk.Button(access, text="Disconnect", command=self.disconnect).pack(side="left")
        ttk.Button(panel, text="ForeverDB developer documentation ↗", style="Link.TButton",
                   command=lambda: webbrowser.open(FOREVERDB_DEVELOPERS)).pack(anchor="w")
        ttk.Label(panel, textvariable=self.status, wraplength=520, style="Caution.TLabel").pack(anchor="w", pady=(3, 8))
        for label, variable, callback in (
            ("Intelligent online verification", self.intelligent, self.change_intelligent),
            ("Verify unknown prerequisites", self.verify_unknown, None),
            ("Verify suspected missing pickups", self.verify_missing, None),
            ("Prefetch upcoming chapters", self.prefetch, None),
            ("Prefer cached data", self.prefer_cache, None),
            ("Refresh outdated metadata automatically", self.refresh_outdated, None),
            ("Automatically publish verified data for this character and installation", self.auto_publish, self.bind_auto_publish),
            ("Notify when updated addon data needs /reload", self.notify_reload, None),
        ):
            ttk.Checkbutton(panel, text=label, variable=variable, command=callback).pack(anchor="w", pady=1)
        prefetch = ttk.Frame(panel)
        prefetch.pack(fill="x", pady=(6, 0))
        ttk.Label(prefetch, text="Upcoming chapters to prefetch").pack(side="left", padx=(0, 6))
        ttk.Spinbox(prefetch, from_=0, to=10, width=4, textvariable=self.chapter_count).pack(side="left")
        ages = ttk.Frame(panel)
        ages.pack(fill="x", pady=(6, 0))
        ttk.Label(ages, text="Quest details (days)").pack(side="left", padx=(0, 5))
        ttk.Spinbox(ages, from_=1, to=365, width=5, textvariable=self.facts_days).pack(side="left", padx=(0, 12))
        ttk.Label(ages, text="Dependencies (days)").pack(side="left", padx=(0, 5))
        ttk.Spinbox(ages, from_=1, to=365, width=5, textvariable=self.dependencies_days).pack(side="left")
        ttk.Button(panel, text="Done", command=dialog.destroy).pack(anchor="e", pady=(8, 0))

    def authorize(self, raw_key: str) -> None:
        key = raw_key.strip() or os.environ.get("QG_FOREVERDB_API_KEY", "").strip()
        if not key or len(key) > 256 or any(not 33 <= ord(character) <= 126 for character in key):
            self.status.set("Enter a valid ForeverDB developer API key, or configure QG_FOREVERDB_API_KEY.")
            return
        if key != self.authorized_key:
            self.cancel()
            self.retry_after.clear()
            self.authorization_revision += 1
        self.authorized_key = key
        self.intelligent.set(True)
        self.status.set("ForeverDB lookups authorized for this session.")
        self.authorization_changed()

    def disconnect(self) -> None:
        self.authorized_key = None
        self.intelligent.set(False)
        self.authorization_revision += 1
        self.cancel()
        self.status.set("ForeverDB disconnected. Cached quest data remains available offline.")
        self.authorization_changed()

    def change_intelligent(self) -> None:
        if self.intelligent.get() and self.authorized_key is None:
            self.intelligent.set(False)
            self.status.set("Authorize ForeverDB first to enable intelligent online verification.")
        if not self.intelligent.get() and self.batch_automatic:
            self.cancel()
        self.authorization_changed()

    def publishing_target(self) -> tuple | None:
        snapshot, installation = self.dashboard.snapshot(), self.dashboard.installation
        if snapshot is None or not snapshot.identity_key or installation is None:
            return None
        return installation.addons, snapshot.identity_key

    def bind_auto_publish(self) -> None:
        self.auto_publish_target = self.publishing_target() if self.auto_publish.get() else None
        if self.auto_publish.get() and self.auto_publish_target is None:
            self.auto_publish.set(False)
            self.status.set("Select an installation and identified character before enabling automatic publishing.")

    def publish(self) -> None:
        self.dashboard.publish(notify_reload=self.notify_reload.get())

    def show_differences(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("Changes since preflight")
        dialog.transient(self.window)
        dialog.geometry("680x430")
        if self.differences_has_foreverdb:
            ttk.Button(dialog, text=FOREVERDB_CREDIT, style="Link.TButton",
                       command=lambda: webbrowser.open(FOREVERDB_URL)).pack(anchor="w")
        view = self.dashboard.text_view(dialog, height=20)
        self.dashboard.put_text(view, self.differences)

    def chapter_limit(self) -> int:
        raw = self.chapter_count.get().strip()
        return min(10, int(raw)) if re.fullmatch(r"\d{1,2}", raw) else 2

    def versions(self) -> tuple[str | None, str | None]:
        installation, snapshot = self.dashboard.installation, self.dashboard.snapshot()
        collection = self.dashboard.collection
        game = getattr(installation, "client_version", None) or getattr(snapshot, "client_version", None)
        guide = (getattr(collection, "version", None) or getattr(snapshot, "restedxp_version", None)
                 or getattr(installation, "restedxp_version", None))
        return game, guide

    def effective_database(self, base: Any, *, fresh_projection: bool = False) -> Any:
        game, guide = self.versions()
        settings = self.settings()
        key = base, self.cache, game, guide, settings, datetime.now().date()
        if fresh_projection or key != self.projection_key:
            self.projected = verified_database(base, cache=self.cache, game_version=game, guide_version=guide,
                                               settings=settings)
            self.projection_key = key
        return self.projected

    def settings(self) -> LookupSettings:
        def age(text: str, default: int) -> int:
            raw = text.strip()
            return min(365, max(1, int(raw))) if re.fullmatch(r"[0-9]{1,3}", raw) else default

        return LookupSettings(
            verify_unknown=self.verify_unknown.get(), verify_missing=self.verify_missing.get(),
            prefetch=self.prefetch.get(), chapters=self.chapter_limit(), prefer_cache=self.prefer_cache.get(),
            refresh_outdated=self.refresh_outdated.get(),
            facts_days=age(self.facts_days.get(), 30), dependencies_days=age(self.dependencies_days.get(), 14),
        )

    def scope_key(self) -> tuple:
        route, chapter = self.dashboard.route, self.dashboard.selected_chapter()
        snapshot = self.dashboard.snapshot()
        return (self.publishing_target(), id(route), getattr(chapter, "id", None), id(snapshot),
                self.dashboard.scope_var.get(), self.dashboard.step_var.get())

    def eligible_automatic(self) -> bool:
        return bool(self.authorized_key and self.intelligent.get() and self.dashboard.quest_state_ready()
                    and self.dashboard.route and self.dashboard.selected_chapter())

    def authorization_changed(self) -> None:
        self.refresh(force=True)
        if self.eligible_automatic():
            if self.dashboard.audit is not None and not self.dashboard.audit_structural:
                self.request("auto")
            elif self.dashboard.future is None and self.dashboard.audit is None:
                self.dashboard.scan(automatic=True)

    def context_changed(self) -> None:
        if not self.eligible_automatic():
            return
        if self.context_after is not None:
            self.window.after_cancel(self.context_after)
        self.context_after = self.window.after(200, self.scan_when_ready)

    def scan_when_ready(self) -> None:
        self.context_after = None
        if not self.eligible_automatic() or self.dashboard.audit is not None:
            return
        if self.dashboard.future is not None:
            self.context_after = self.window.after(200, self.scan_when_ready)
            return
        self.dashboard.scan(automatic=True)

    def request_ids(self, ids: list[int]) -> None:
        self.request("selected", ids=ids)

    def request(self, mode: str, *, ids: list[int] | None = None) -> None:
        automatic = mode == "auto"
        key = self.authorized_key
        if automatic and not self.eligible_automatic():
            return
        if not key:
            self.status.set("Authorize ForeverDB in Provider settings before sending quest IDs.")
            return
        if self.future is not None:
            if automatic:
                self.waiting_auto = True
            else:
                self.status.set("Verification is running. Cancel it or wait before starting another lookup.")
            return

        chapter = self.dashboard.selected_chapter()
        route, snapshot = self.dashboard.route, self.dashboard.snapshot()
        game, guide = self.versions()
        now = time.monotonic()
        arguments = dict(mode=MODES[mode], chapter_id=getattr(chapter, "id", None), settings=self.settings(),
                         cache=self.cache, game_version=game, guide_version=guide)
        if mode == "selected":
            chosen = self.selected_id()
            targets = ids or ([chosen] if chosen else [])
            if not targets:
                self.status.set("Select a quest or enter a quest ID before verifying it.")
                return
            candidates = tuple(candidate for quest_id in targets for candidate in plan_verification(
                route or Route(()), snapshot, self.dashboard.audit, quest_id=quest_id, **arguments,
            ))
        elif route and chapter:
            candidates = plan_verification(route, snapshot, self.dashboard.audit, **arguments)
        else:
            self.status.set("Import and select a linked guide chapter to verify its quests.")
            return

        if automatic:
            candidates = tuple(candidate for candidate in candidates if
                               self.retry_after.get((game, guide, candidate.quest_id), 0) <= now)
        if not candidates:
            if not automatic:
                self.status.set("No new lookups are needed for this selection. Inspect the cached evidence below.")
            return
        self.retry_after = {token: expiry for token, expiry in self.retry_after.items() if expiry > now}
        for candidate in candidates:
            self.retry_after[(game, guide, candidate.quest_id)] = now + AUTO_RETRY_SECONDS

        self.origin = self.scope_key()
        self.origin_revision = self.authorization_revision
        self.batch_automatic = automatic
        self.pending = {candidate.quest_id for candidate in candidates}
        self.cancelled = cancelled = Event()
        for quest_id in self.pending:
            self.unavailable.pop(quest_id, None)
        self.cancel_button.state(["!disabled"])
        self.progress.pack(side="right")
        self.progress.start(60)
        self.status.set(f"Queued {len(self.pending)} authorized quest lookups.")
        self.refresh(force=True)

        def worker():
            provider = ForeverDBProvider(key, authorized=True)
            return verify_quests(candidates, (provider,), game_version=game, guide_version=guide,
                                 progress=lambda message: self.messages.put(message.replace(key, "[redacted]")),
                                 cancelled=cancelled.is_set)

        self.future = self.executor.submit(worker)
        self.after = self.window.after(100, self.poll)

    def poll(self) -> None:
        self.after = None
        while True:
            try:
                message = self.messages.get_nowait()
            except Empty:
                break
            if self.cancelled is not None and not self.cancelled.is_set():
                self.status.set(message)
        if self.future is None:
            return
        if not self.future.done() or self.dashboard.future is not None:
            self.after = self.window.after(100, self.poll)
            return

        future, self.future = self.future, None
        self.batch_automatic = False
        self.cancelled = None
        self.cancel_button.state(["disabled"])
        self.progress.stop()
        self.progress.pack_forget()
        self.pending.clear()
        try:
            result = future.result()
        except (OSError, ValueError):
            self.provider_errors += 1
            self.status.set("Verification could not finish. Existing offline data remains available.")
        else:
            self.apply_result(result)
        self.refresh(force=True)
        if self.waiting_auto and self.dashboard.future is None and self.dashboard.audit is not None:
            self.waiting_auto = False
            self.request("auto")

    def cancel(self) -> None:
        if self.cancelled is not None:
            self.cancelled.set()
            self.waiting_auto = False
            self.status.set("Stopping quest verification…")
            self.cancel_button.state(["disabled"])

    def close(self) -> None:
        self.cancel()
        if self.after is not None:
            self.window.after_cancel(self.after)
        if self.context_after is not None:
            self.window.after_cancel(self.context_after)
        self.executor.shutdown(wait=False, cancel_futures=True)

    def apply_result(self, result: Any) -> None:
        self.cache = result.cache
        self.unavailable.update(result.errors)
        self.provider_errors += len(result.errors)
        same_context = self.origin == self.scope_key() and self.origin_revision == self.authorization_revision
        if self.origin_revision == self.authorization_revision:
            self.status.set(result.message)
        previous = self.dashboard.audit
        previous_database = self.dashboard.database
        structural = self.dashboard.audit_structural
        self.dashboard.update_effective_database()
        changed = previous_database != self.dashboard.database
        if changed and self.dashboard.route and self.dashboard.selected_chapter() and (previous is not None or self.dashboard.quest_state_ready()):
            self.comparison = previous
            self.differences_has_foreverdb = bool((previous_database and previous_database.has_foreverdb)
                                                 or (self.dashboard.database and self.dashboard.database.has_foreverdb))
            requested = self.publishing_target()
            if (result.fetched and not result.cancelled and same_context and not structural and
                    self.auto_publish.get() and requested and self.auto_publish_target == requested):
                self.publish_after_scan = self.scope_key()
            self.dashboard.scan(structural_only=structural, automatic=True, from_verification=True)
        elif previous is not None:
            self.differences = audit_changes(previous, previous)
            self.differences_has_foreverdb = bool(previous_database and previous_database.has_foreverdb)

    def on_preflight(self, audit: Any, *, from_verification: bool) -> None:
        self.refresh(force=True)
        if from_verification:
            self.differences = audit_changes(self.comparison, audit)
            expected, self.publish_after_scan = self.publish_after_scan, None
            if expected == self.scope_key() and self.auto_publish.get() and self.auto_publish_target == self.publishing_target():
                self.dashboard.publish(notify_reload=self.notify_reload.get(), quiet=True)
            if self.waiting_auto and self.dashboard.future is None and not self.dashboard.audit_structural:
                self.waiting_auto = False
                self.request("auto")
        elif not self.dashboard.audit_structural:
            self.request("auto")

    def refresh(self, *, force: bool = False) -> None:
        route, snapshot = self.dashboard.route, self.dashboard.snapshot()
        chapter, audit = self.dashboard.selected_chapter(), self.dashboard.audit
        database = self.dashboard.database
        key = (id(snapshot), id(route), id(chapter), id(audit), id(database), id(self.cache),
               self.authorized_key is not None, self.chapter_limit())
        if not force and key == self.view_key:
            return
        self.view_key = key
        if self.auto_publish.get() and self.auto_publish_target != self.publishing_target():
            self.auto_publish.set(False)
            self.auto_publish_target = None
            self.status.set("Automatic publishing paused because the character or installation changed.")
        self.heading.set(f"Current guide · {getattr(chapter, 'name', UNKNOWN)}")
        self.rows, cached = self.make_rows(route, snapshot, chapter, audit, database)
        if database and database.has_foreverdb:
            self.attribution.pack(side="right", padx=(0, 8))
        else:
            self.attribution.pack_forget()
        provider = "ForeverDB authorized" if self.authorized_key else "ForeverDB not authorized"
        cached_chapters = ", ".join(cached) if cached else "None fully cached"
        self.summary.set(f"{provider}   |   {len(self.cache.quest_ids)} quests with online source records"
                         f"   |   {len(self.pending)} awaiting verification   |   {self.provider_errors} lookup errors\n"
                         f"Last quest database update · {display_timestamp(getattr(database, 'updated_at', None))}"
                         f"   |   Upcoming chapters cached · {cached_chapters}")
        self.fill_table()

    @staticmethod
    def character_state(snapshot: Any, quest_id: int) -> str:
        if snapshot is None:
            return UNKNOWN
        if quest_id in (snapshot.active or ()):
            return "Ready to turn in" if quest_id in (snapshot.ready or ()) else "Active"
        if quest_id in (snapshot.completed or ()):
            return "Completed"
        if snapshot.active is not None and snapshot.completed is not None:
            return "Absent in saved log"
        return UNKNOWN

    def make_rows(self, route: Any, snapshot: Any, chapter: Any, audit: Any, database: Any) -> tuple[dict[int, QuestView], list[str]]:
        quest_data = database.quests if database else {}
        discrepancies = guide_discrepancies(route, quest_data) if quest_data else {}
        findings = {}
        if audit is not None:
            findings.update({row.quest_id: row for row in audit.historical_findings})
            findings.update({row.quest_id: row for row in reversed(audit.findings)})
        ids = dict.fromkeys((*findings, *sorted(self.pending), *sorted(self.unavailable)))
        ids.update(dict.fromkeys(discrepancies))
        names: dict[int, str] = {}
        cached = []
        planned: set[int] = set()
        optional: set[int] = set()
        if audit is not None:
            for summary in audit.chapters:
                planned.update(summary.planned_quests)
                optional.update(summary.optional_quests)
                names.update(summary.quest_names)
        if route and chapter and chapter in route.guides:
            index = route.guides.index(chapter)
            for offset, guide in enumerate(route.guides[index:index + self.chapter_limit() + 1]):
                chapter_ids = set()
                for step in guide.steps:
                    for action in step.actions:
                        ids[action.quest_id] = None
                        chapter_ids.add(action.quest_id)
                        if action.name:
                            names.setdefault(action.quest_id, action.name)
                if offset and chapter_ids and all(
                    quest_data.get(quest_id, {}).get("fieldEvidence") for quest_id in chapter_ids
                ):
                    cached.append(guide.name)
        ids.update(dict.fromkeys(sorted(quest_data)))

        rows = {}
        for quest_id in ids:
            metadata, finding = quest_data.get(quest_id, {}), findings.get(quest_id)
            name = display_text(metadata.get("name"), getattr(finding, "title", None) or names.get(quest_id) or f"Quest {quest_id}")
            character = self.character_state(snapshot, quest_id)
            problem = self.unavailable.get(quest_id)
            state, action = verification_state(quest_id, snapshot, metadata, finding,
                                               error=problem, pending=quest_id in self.pending,
                                               planned=quest_id in planned, discrepancy=discrepancies.get(quest_id))
            if state == "Insufficient information" and quest_id in optional:
                state, action = "Optional quest", "Follow the guide conditions if you choose to do this quest."
            gaps, links = self.describe_evidence(metadata)
            detail = []
            if finding:
                detail.append(finding.detail)
                if finding.pickup_guide:
                    detail.append(f"Guide pickup · {finding.pickup_guide} · Step {finding.pickup_step or UNKNOWN}")
            if problem:
                detail.append(problem)
            if not gaps:
                detail.append("No independent facts are recorded for this quest. Any local guide findings use the saved route and character data.")
            detail.extend(gaps)
            claim_dates = [claim.get("verifiedAt") for claims in metadata.get("fieldEvidence", {}).values() for claim in claims
                           if claim.get("verifiedAt")]
            last_verified = display_timestamp(max(claim_dates, default=None))
            retrieved = display_timestamp(metadata.get("observedAt"))
            rows[quest_id] = QuestView(
                quest_id, name, state, character,
                display_text(metadata.get("source"), getattr(finding, "source", UNKNOWN)),
                display_text(CONFIDENCE_NAMES.get(metadata.get("confidence"), metadata.get("confidence")),
                             getattr(finding, "confidence", UNKNOWN)),
                last_verified if last_verified != UNKNOWN else (f"Retrieved {retrieved}" if retrieved != UNKNOWN else UNKNOWN),
                action, "\n\n".join(detail), links,
            )
        return rows, cached

    @staticmethod
    def describe_evidence(metadata: Mapping) -> tuple[list[str], tuple[tuple[str, str], ...]]:
        lines = []
        links = []
        if metadata.get("sourceUrl"):
            links.append((display_text(metadata.get("source"), "Source"), metadata["sourceUrl"]))
        for title, field in (("Pickup", "startLocation"), ("Turn-in", "endLocation")):
            location = metadata.get(field)
            if not isinstance(location, Mapping):
                continue
            description = [str(location[key]) for key in ("name", "zone") if location.get(key)]
            x, y = location.get("x"), location.get("y")
            if isinstance(x, (float, int)) and isinstance(y, (float, int)):
                description.append(f"{x:g}%, {y:g}%")
            if description:
                lines.append(f"{title} · " + " · ".join(description))

        evidence = metadata.get("fieldEvidence")
        if isinstance(evidence, Mapping):
            for field, claims in evidence.items():
                if not isinstance(claims, (list, tuple)):
                    continue
                for claim in claims:
                    if not isinstance(claim, Mapping):
                        continue
                    origin = display_text(claim.get("source") or claim.get("provider"), "Source unknown")
                    code = claim.get("confidence") or claim.get("classification")
                    confidence = display_text(CONFIDENCE_NAMES.get(code, code), "Confidence unknown")
                    value = claim.get("value")
                    label = FIELD_NAMES.get(field, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", field).capitalize())
                    title = f"{label} · {origin} · {confidence}"
                    facts = [
                        display_text(claim.get("method"), ""),
                        f"Build {claim['gameVersion']}" if claim.get("gameVersion") else "",
                        f"Verified {display_timestamp(claim.get('verifiedAt'))}" if claim.get("verifiedAt") else "",
                        f"Retrieved {display_timestamp(claim.get('retrievedAt'))}" if claim.get("retrievedAt") else "",
                        "Observed in Forever" if claim.get("observedForever") else "",
                        "Inherited from Classic" if claim.get("inheritedClassic") else "",
                        "Guide-derived" if claim.get("guideDerived") else "",
                    ]
                    lines.append("\n".join(filter(None, (title, " · ".join(filter(None, facts)),
                                                         describe_value(field, value) if value is not None else ""))))
                    link = claim.get("sourceUrl") or claim.get("url")
                    if isinstance(link, str) and trusted_reference(link):
                        links.append((origin, link))
        for field in metadata.get("conflicts", ()):
            conflict_field = field.get("field") if isinstance(field, Mapping) else field
            if isinstance(conflict_field, str):
                label = FIELD_NAMES.get(conflict_field, conflict_field)
                detail = "Inspect the claims before using this field." if not isinstance(field, Mapping) or field.get("unresolved") else (
                    "A source applicable to this build takes precedence."
                )
                lines.append(f"Conflicting sources · {label}. {detail}")
        if from_foreverdb(metadata) and not any(link == FOREVERDB_URL for _, link in links):
            links.append(("Data from ForeverDB.net", FOREVERDB_URL))
        return lines, tuple(dict.fromkeys((label, link) for label, link in links if trusted_reference(link)))
