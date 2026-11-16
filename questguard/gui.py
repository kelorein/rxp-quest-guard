"""Windows desktop dashboard for local guide, quest and character data."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from itertools import chain
import json
from pathlib import Path
import re
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
from typing import Any, Callable, Iterable
import webbrowser

from . import __version__
from .engine import audit_route, build_route, format_location, match_current_guide, resolve_current_step
from .evidence import FOREVERDB_CREDIT, FOREVERDB_URL
from .ingest import Snapshot, load_guides, load_snapshots, merge_snapshots, remember_association
from .monitoring import MonitorUpdate, SavedProgressMonitor
from .verification import VerificationCache, load_verification_cache
from .verification_view import VerificationPanel
from .sync import (
    atomic_write,
    cache_directory,
    discover_installations,
    import_database,
    inspect_installation,
    install_addon,
    load_cache,
    publish_route,
)


UNKNOWN = "Unknown"
BG = "#151510"
PANEL = "#1f211a"
INPUT = "#24251e"
TEXT = "#f5f0df"
MUTED = "#bbb5a4"
ACCENT = "#e0bd77"
HIGHLIGHT = "#433621"
EDGE = "#393b2e"
ERROR = "#ffad92"
MONITOR_INTERVAL_MS = 2000
CLASSES = ("Druid", "Hunter", "Mage", "Paladin", "Priest", "Rogue", "Shaman", "Warlock", "Warrior")

SCOPES = {
    "Current step": {"lookahead": 0},
    "Next 10 steps": {"lookahead": 10},
    "Next 30 steps": {"lookahead": 30},
    "Selected chapter": {"chapters": 1},
    "Next chapter": {"chapters": 1},
    "Route through level 60": {"end_level": 60},
    "Full planned route": {},
}

COUNT_LABELS = {
    "confirmed_blockers": "Confirmed blockers",
    "missing_pickups": "Missing pickups",
    "unaccepted_objectives": "Unaccepted objectives",
    "optional": "Optional",
    "reviews": "Needs review",
    "future_planned": "Planned",
    "satisfied": "Satisfied",
    "historical": "Historical",
}


def load_associations() -> dict[str, str]:
    path = cache_directory() / "character-associations.json"
    try:
        with path.open("rb") as file:
            raw = file.read(256 * 1024 + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > 256 * 1024:
        raise ValueError("Character association settings are too large.")
    result = json.loads(raw)
    if not isinstance(result, dict) or any(
        not isinstance(identity, str) for identity in result.values()
    ):
        raise ValueError("Character association settings are invalid.")
    return result


def save_association(export: Snapshot, character: Snapshot, *, snapshots: Iterable[Snapshot] = ()) -> dict[str, str]:
    updated = remember_association(load_associations(), export, character)
    records = tuple(snapshots)
    if records and not any(
        row.identity_key == character.identity_key and row.association_key == export.association_key
        for row in merge_snapshots(records, associations=updated)
    ):
        raise ValueError("A newer or verified save takes precedence for this character. Refresh to load its quest history.")
    path = cache_directory() / "character-associations.json"
    atomic_write(path, (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return updated


def load_monitor_preference() -> bool:
    path = cache_directory() / "monitoring.json"
    try:
        with path.open("rb") as file:
            raw = file.read(129)
    except FileNotFoundError:
        return False
    if len(raw) > 128:
        raise ValueError("The saved monitoring preference is invalid.")
    value = json.loads(raw)
    if type(value) is not bool:
        raise ValueError("The saved monitoring preference is invalid.")
    return value


def guide_label(guide: Any) -> str:
    context = " / ".join(part for part in (guide.group, guide.subgroup, guide.faction, guide.condition) if part)
    return f"{guide.name}  [{context}]" if context else guide.name


def source_age(age: float | None, stale_after: float) -> str:
    if age is None:
        return UNKNOWN
    if age < 0:
        return "Unknown (source date is in the future)"
    days = int(age)
    description = "Less than a day old" if days == 0 else f"{days} {'day' if days == 1 else 'days'} old"
    return f"{description} · Stale" if age >= stale_after else description


def local_date(value: datetime | None, *, seconds: bool = False) -> str:
    if value is None:
        return UNKNOWN
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z" if seconds else "%Y-%m-%d %H:%M %Z")


def selected_ids(raw: str) -> list[int]:
    values = re.split(r"[\s,]+", raw.strip())
    if not raw.strip() or any(not re.fullmatch(r"[0-9]{1,10}", val) for val in values):
        raise ValueError("Enter numeric quest IDs separated by spaces or commas.")
    ids = list(dict.fromkeys(map(int, values)))
    if any(not 1 <= item <= 2_147_483_647 for item in ids):
        raise ValueError("Each quest ID must be between 1 and 2,147,483,647.")
    return ids


class Dashboard:
    def __init__(self, window: tk.Tk) -> None:
        self.window = window
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quest-guard")
        self.future: Future | None = None
        self.on_result: Callable[[Any], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self.close_after_job = False
        self.diagnostics: list[str] = []
        self.action_buttons: list[ttk.Widget] = []

        self.installations = []
        self.installation = None
        self.collection = None
        self.guide_path = None
        self.installed_guide_client = None
        self.source_snapshots: dict[Path, tuple[Snapshot, ...]] = {}
        self.watched_snapshots: dict[Path, Snapshot] = {}
        self.monitor: SavedProgressMonitor | None = None
        self.monitor_after: str | None = None
        self.monitor_future: Future | None = None
        self.monitor_file_count = -1
        self.monitor_problems: tuple[str, ...] = ()
        self.monitor_catalog_attempted = False
        self.associations: dict[str, str] = {}
        self.snapshots = []
        self.base_database = None
        self.database = None
        self.route = None
        self.audit = None
        self.audit_context = None
        self.audit_structural = False
        self.last_scan_at: datetime | None = None
        self.last_scan_snapshot_at: datetime | None = None
        self.chapter_options = []
        self.visible_chapters = []
        self.report_rows = {}
        self.step_overrides: dict[tuple[str, str], str] = {}
        self.setting_step = False

        self.install_var = tk.StringVar()
        self.interface_var = tk.StringVar()
        self.snapshot_var = tk.StringVar()
        self.anchor_var = tk.StringVar()
        self.chapter_var = tk.StringVar()
        self.scope_var = tk.StringVar(value="Route through level 60")
        self.step_var = tk.StringVar()
        self.step_override = tk.BooleanVar()
        self.step_display = tk.StringVar()
        self.progress_notice = tk.StringVar()
        self.character_details = tk.StringVar()
        self.readiness_var = tk.StringVar()
        self.monitor_enabled = tk.BooleanVar(value=False)
        self.authorized_guides = tk.BooleanVar(value=False)
        self.monitor_status = tk.StringVar(value="Monitoring off")
        self.last_scan_var = tk.StringVar(value="Last completed preflight · None yet")
        self.counts_var = tk.StringVar()
        self.faction_var = tk.StringVar(value="From snapshot")
        self.class_var = tk.StringVar(value="From snapshot")
        self.api_ids = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.metrics = {name: tk.StringVar(value=UNKNOWN) for name in (
            "Chapter", "Active quests", "Completed quests", "Findings",
        )}

        self.window.title("RXP Quest Guard Desktop")
        self.window.geometry("1150x790")
        self.window.minsize(850, 680)
        self.window.configure(background=BG)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.configure_style()
        self.compose()
        self.update_status_details()
        self.begin("Checking for WoW installations and cached quest data…",
                   self.load_startup, self.startup_ready)

    def configure_style(self) -> None:
        style = ttk.Style(self.window)
        style.theme_use("clam")
        families = set(font.families(self.window))
        family = next((name for name in ("Inter", "Source Sans 3", "Segoe UI") if name in families),
                      font.nametofont("TkDefaultFont").actual("family"))
        base = (family, 10)
        self.window.option_add("*Font", base)
        self.window.option_add("*Listbox.background", INPUT)
        self.window.option_add("*Listbox.foreground", TEXT)
        self.window.option_add("*Listbox.selectBackground", HIGHLIGHT)
        self.window.option_add("*Listbox.selectForeground", TEXT)
        style.configure(".", font=base, background=BG, foreground=TEXT, bordercolor=EDGE,
                        troughcolor=PANEL, fieldbackground=INPUT, selectbackground=HIGHLIGHT,
                        selectforeground=TEXT, lightcolor=EDGE, darkcolor=EDGE)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Title.TLabel", font=(family, 24, "bold"), foreground=TEXT)
        style.configure("Section.TLabel", font=(family, 12, "bold"), foreground=ACCENT)
        style.configure("Hint.TLabel", foreground=MUTED)
        style.configure("Caution.TLabel", foreground=ACCENT)
        style.configure("CardHint.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Value.TLabel", background=PANEL, font=(family, 13, "bold"))
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(18, 9), background=PANEL, foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", HIGHLIGHT)],
                  foreground=[("selected", TEXT)])
        style.configure("TButton", padding=(10, 5), background=PANEL, foreground=TEXT)
        style.configure("Primary.TButton", background=ACCENT, foreground="#201b12")
        style.configure("Link.TButton", padding=(3, 2), foreground=ACCENT, background=BG)
        style.map("TButton", foreground=[("disabled", MUTED)],
                  background=[("active", HIGHLIGHT), ("pressed", HIGHLIGHT)])
        style.map("Primary.TButton", background=[("disabled", EDGE), ("active", "#f3d79d")],
                  foreground=[("disabled", MUTED)])
        style.configure("TEntry", padding=4, foreground=TEXT, insertcolor=TEXT)
        style.configure("TCombobox", padding=4, foreground=TEXT, arrowcolor=ACCENT)
        style.map("TCombobox", fieldbackground=[("readonly", INPUT)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", HIGHLIGHT)])
        style.configure("Treeview", rowheight=27, background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, borderwidth=1)
        style.map("Treeview", background=[("selected", HIGHLIGHT)],
                  foreground=[("selected", TEXT)])
        style.configure("Treeview.Heading", background=INPUT, foreground=ACCENT,
                        font=(family, 10, "bold"), padding=(5, 4))
        style.configure("TPanedwindow", sashwidth=7)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL)

    def compose(self) -> None:
        page = ttk.Frame(self.window, padding=(22, 14, 22, 12))
        page.pack(fill="both", expand=True)
        ttk.Label(page, text="RXP Quest Guard", style="Title.TLabel").pack(anchor="w", pady=(0, 10))
        notebook = ttk.Notebook(page)
        notebook.pack(fill="both", expand=True)
        self.notebook = notebook
        notebook.enable_traversal()

        self.plan_page = ttk.Frame(notebook, padding=(0, 13, 0, 0))
        self.verification_page = ttk.Frame(notebook, padding=(1, 13, 1, 2))
        self.sources_page = ttk.Frame(notebook, padding=(1, 18, 1, 2))
        self.diagnostic_page = ttk.Frame(notebook, padding=(1, 18, 1, 2))
        notebook.add(self.plan_page, text="Route preflight")
        notebook.add(self.verification_page, text="Quest Verification")
        notebook.add(self.sources_page, text="Setup & updates")
        notebook.add(self.diagnostic_page, text="Diagnostics")
        self.build_preflight(self.plan_page)
        self.verification = VerificationPanel(self, self.verification_page)
        self.build_scrollable_setup()
        self.build_diagnostics(self.diagnostic_page)

        ttk.Separator(page).pack(fill="x", pady=(10, 6))
        footer = ttk.Frame(page)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status_var, style="Hint.TLabel").pack(side="left")
        self.foreverdb_credit = ttk.Button(footer, text=FOREVERDB_CREDIT, style="Link.TButton",
                                           command=lambda: webbrowser.open(FOREVERDB_URL))
        self.progress_bar = ttk.Progressbar(footer, mode="indeterminate", length=120)

    def button(self, parent: ttk.Frame, label: str, action: Callable, *, primary: bool = False,
               busy_sensitive: bool = True) -> ttk.Button:
        button = ttk.Button(parent, text=label, command=action,
                            style="Primary.TButton" if primary else "TButton")
        if busy_sensitive:
            self.action_buttons.append(button)
        return button

    def combobox(self, parent: ttk.Frame, label: str, variable: tk.StringVar, *values: str,
                 width: int = 27) -> ttk.Combobox:
        field = ttk.Frame(parent)
        ttk.Label(field, text=label, style="Hint.TLabel").pack(anchor="w", pady=(0, 2))
        widget = ttk.Combobox(field, textvariable=variable, values=values, width=width, state="readonly")
        widget.pack(fill="x")
        field.pack(side="left", fill="x", expand=True, padx=(0, 9))
        return widget

    def tree(self, parent: ttk.Frame, columns: list[tuple[str, int, bool]], *, height: int) -> ttk.Treeview:
        box = ttk.Frame(parent)
        tree = ttk.Treeview(box, columns=[title for title, *_ in columns], show="headings",
                            height=height, selectmode="browse")
        for title, width, stretch in columns:
            tree.heading(title, text=title)
            tree.column(title, width=width, minwidth=38, stretch=stretch)
        vscroll = ttk.Scrollbar(box, command=tree.yview)
        tree.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        hscroll = ttk.Scrollbar(box, orient="horizontal", command=tree.xview)
        tree.configure(xscrollcommand=hscroll.set)
        hscroll.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        box.pack(fill="both", expand=True)
        return tree

    def text_view(self, parent: ttk.Frame, *, height: int) -> tk.Text:
        box = ttk.Frame(parent)
        view = tk.Text(box, height=height, font="TkDefaultFont", wrap="word", relief="flat",
                       background=PANEL, foreground=TEXT, selectbackground=HIGHLIGHT,
                       selectforeground=TEXT, padx=10, pady=8, state="disabled")
        scrollbar = ttk.Scrollbar(box, command=view.yview)
        view.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        view.pack(fill="both", expand=True)
        box.pack(fill="both", expand=True)
        return view

    @staticmethod
    def put_text(view: tk.Text, text: str) -> None:
        view.configure(state="normal")
        view.delete("1.0", "end")
        view.insert("1.0", text)
        view.configure(state="disabled")

    def build_preflight(self, page: ttk.Frame) -> None:
        progress = ttk.Frame(page)
        progress.pack(fill="x", pady=(0, 5))
        self.button(progress, "Refresh character progress", self.refresh_progress, primary=True).pack(side="right")
        ttk.Label(progress, textvariable=self.character_details, style="Section.TLabel", wraplength=640).pack(side="left")
        tracking = ttk.Frame(page)
        tracking.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(tracking, text="Monitor saved progress", variable=self.monitor_enabled,
                        command=self.toggle_monitor).pack(side="left", padx=(0, 10))
        ttk.Label(tracking, textvariable=self.monitor_status, style="Hint.TLabel", wraplength=585).pack(side="left")
        ttk.Label(page, textvariable=self.last_scan_var, style="Hint.TLabel", wraplength=800).pack(anchor="w", pady=(0, 3))
        ttk.Label(page, textvariable=self.readiness_var, style="Hint.TLabel", wraplength=800).pack(anchor="w")
        notice = ttk.Frame(page)
        notice.pack(fill="x", pady=(4, 8))
        self.associate_button = self.button(notice, "Confirm quest export…", self.associate)
        self.associate_button.pack(side="right")
        ttk.Label(notice, textvariable=self.progress_notice, style="Caution.TLabel", wraplength=620).pack(side="left")

        metrics = ttk.Frame(page)
        metrics.pack(fill="x", pady=(0, 10))
        for index, (name, value) in enumerate(self.metrics.items()):
            metrics.columnconfigure(index, weight=2 if index == 0 else 1, uniform="metric")
            card = ttk.Frame(metrics, style="Card.TFrame", padding=(11, 6))
            card.grid(row=0, column=index, sticky="nsew", padx=(0, 6 if index < 3 else 0))
            ttk.Label(card, text=name, style="CardHint.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=value, style="Value.TLabel").pack(anchor="w")

        selection = ttk.Frame(page)
        selection.pack(fill="x", pady=(0, 8))
        self.snapshot_box = self.combobox(selection, "Character snapshot", self.snapshot_var,
                                          "No character selected", width=32)
        self.snapshot_box.current(0)
        self.snapshot_box.bind("<<ComboboxSelected>>", self.select_character)
        self.chapter_box = self.combobox(selection, "Preflight chapter", self.chapter_var, width=27)
        self.chapter_box.bind("<<ComboboxSelected>>", self.change_scan_chapter)

        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=(0, 10))
        self.scope_box = self.combobox(controls, "Scan range", self.scope_var, *SCOPES, width=27)
        self.scope_box.bind("<<ComboboxSelected>>", self.change_scan_scope)
        step_field = ttk.Frame(controls)
        step_field.pack(side="left", fill="y", padx=(0, 9))
        ttk.Label(step_field, textvariable=self.step_display, style="Hint.TLabel").pack(anchor="w", pady=(0, 2))
        self.step_entry = ttk.Entry(step_field, textvariable=self.step_var, width=10)
        self.step_entry.pack()
        self.step_entry.bind("<Return>", lambda _event: self.scan())
        self.button(controls, "Run preflight", self.scan, primary=True).pack(side="left", anchor="s", padx=(0, 8))
        self.button(controls, "Structural-only scan", lambda: self.scan(structural_only=True)).pack(
            side="left", anchor="s", padx=(0, 8))
        self.button(controls, "Save report…", self.save_report).pack(side="left", anchor="s", padx=(0, 8))
        ttk.Checkbutton(controls, text="Manual step override", variable=self.step_override,
                        command=self.toggle_step_override).pack(side="left", anchor="s", pady=(0, 5))
        self.step_var.trace_add("write", self.store_step_override)
        self.set_step_display("", False, "Expanded step")

        panes = ttk.Panedwindow(page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        chapters = ttk.Frame(panes, padding=(0, 0, 4, 0))
        findings = ttk.Frame(panes, padding=(4, 0, 0, 0))
        panes.add(chapters, weight=3)
        panes.add(findings, weight=7)
        ttk.Label(chapters, text="Planned chapters", style="Section.TLabel").pack(anchor="w", pady=(0, 5))
        chapter_detail = ttk.Frame(chapters)
        chapter_detail.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Label(chapter_detail, text="Chapter summary", style="Hint.TLabel").pack(anchor="w", pady=(0, 2))
        self.chapter_view = self.text_view(chapter_detail, height=6)
        self.put_text(self.chapter_view, "Chapter summaries appear after a scan.")
        self.chapter_tree = self.tree(chapters, [("Chapter", 218, True), ("Findings", 70, False)], height=8)
        self.chapter_tree.bind("<Double-1>", self.scan_selected_tree_chapter)
        self.chapter_tree.bind("<Return>", self.scan_selected_tree_chapter)
        self.chapter_tree.bind("<<TreeviewSelect>>", self.show_chapter)
        ttk.Label(findings, text="Findings", style="Section.TLabel").pack(anchor="w", pady=(0, 5))
        ttk.Label(findings, textvariable=self.counts_var, style="Hint.TLabel", wraplength=525).pack(anchor="w")
        detail = ttk.Frame(findings)
        detail.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Label(detail, text="Finding details", style="Hint.TLabel").pack(anchor="w", pady=(0, 2))
        self.detail_view = self.text_view(detail, height=6)
        self.finding_tree = self.tree(findings, [("Status", 104, False), ("Quest", 65, False),
                                                ("Finding", 266, True), ("Step", 51, False)], height=8)
        self.finding_tree.tag_configure("blocker", foreground=ERROR)
        self.finding_tree.tag_configure("warning", foreground=ACCENT)
        self.finding_tree.bind("<<TreeviewSelect>>", self.show_finding)
        self.put_text(self.detail_view, "Import installed RestedXP guides and a character snapshot under Setup & updates.")

    def build_scrollable_setup(self) -> None:
        canvas = tk.Canvas(self.sources_page, background=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(self.sources_page, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        contents = ttk.Frame(canvas, padding=(0, 0, 10, 7))
        slot = canvas.create_window(0, 0, window=contents, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(slot, width=event.width))
        contents.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))

        def scroll_setup(event: Any) -> None:
            if str(event.widget).startswith(str(canvas)) and not isinstance(event.widget, ttk.Combobox):
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        self.window.bind("<MouseWheel>", scroll_setup, add="+")
        self.build_sources(contents)

    def build_sources(self, page: ttk.Frame) -> None:
        ttk.Label(page, text="WoW installation", style="Section.TLabel").pack(anchor="w")
        location = ttk.Frame(page)
        location.pack(fill="x", pady=(5, 5))
        self.install_box = ttk.Combobox(location, textvariable=self.install_var, state="readonly")
        self.install_box.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.install_box.bind("<<ComboboxSelected>>", self.update_install_selection)
        self.button(location, "Find installations", self.find_installations).pack(side="left", padx=(0, 5))
        self.button(location, "Browse…", self.browse_installation).pack(side="left")
        self.install_details = ttk.Label(page, style="Hint.TLabel", wraplength=790)
        self.install_details.pack(anchor="w", pady=(0, 5))
        folder_actions = ttk.Frame(page)
        folder_actions.pack(fill="x", pady=(0, 9))
        self.button(folder_actions, "Import installed data", self.import_installed).pack(side="left", padx=(0, 8))
        self.button(folder_actions, "Install or update Quest Guard", self.install, primary=True).pack(side="left", padx=(0, 9))
        ttk.Label(folder_actions, text="Client interface override", style="Hint.TLabel").pack(side="left", padx=(0, 5))
        ttk.Entry(folder_actions, textvariable=self.interface_var, width=9).pack(side="left")
        ttk.Label(page, text="After installing or publishing data, start WoW or use /reload in the running client.",
                  style="Hint.TLabel", wraplength=800).pack(anchor="w")

        ttk.Separator(page).pack(fill="x", pady=13)
        ttk.Label(page, text="Local imports", style="Section.TLabel").pack(anchor="w")
        permission = ttk.Checkbutton(page, text="Analyze guides I wrote or have permission to use",
                                     variable=self.authorized_guides, command=self.change_guide_permission)
        permission.pack(anchor="w", pady=(4, 2))
        self.action_buttons.append(permission)
        ttk.Label(page, text="Only select sources that permit automated analysis and local route publishing. "
                  "Buying a paid guide alone does not authorize this use.", style="Hint.TLabel",
                  wraplength=790).pack(anchor="w")
        files = ttk.Frame(page)
        files.pack(fill="x", pady=(6, 6))
        self.button(files, "Guide folder…", lambda: self.browse_guides(folder=True)).pack(side="left", padx=(0, 5))
        self.button(files, "Guide ZIP…", self.browse_guides).pack(side="left", padx=(0, 12))
        self.button(files, "Snapshot file…", self.browse_snapshot).pack(side="left", padx=(0, 5))
        self.button(files, "WTF folder…", lambda: self.browse_snapshot(folder=True)).pack(side="left", padx=(0, 12))
        self.button(files, "Associate export…", self.associate).pack(side="left")
        self.import_details = ttk.Label(page, style="Hint.TLabel", wraplength=790)
        self.import_details.pack(anchor="w")
        context = ttk.Frame(page)
        context.pack(fill="x", pady=(6, 0))
        faction = self.combobox(context, "Route faction", self.faction_var, "From snapshot", "Horde", "Alliance")
        faction.bind("<<ComboboxSelected>>", self.update_route)
        character_class = self.combobox(context, "Route class", self.class_var, "From snapshot", *CLASSES)
        character_class.bind("<<ComboboxSelected>>", self.update_route)
        history = ttk.Frame(page)
        history.pack(fill="x", pady=(6, 0))
        self.anchor_box = self.combobox(history, "Guide browser / route start", self.anchor_var,
                                        "Current linked guide", width=43)
        self.anchor_box.current(0)
        self.anchor_box.bind("<<ComboboxSelected>>", self.update_route)

        ttk.Separator(page).pack(fill="x", pady=13)
        ttk.Label(page, text="Quest database", style="Section.TLabel").pack(anchor="w")
        store = ttk.Frame(page)
        store.pack(fill="x", pady=(6, 6))
        self.button(store, "Import quest JSON…", self.browse_database).pack(side="left", padx=(0, 5))
        self.button(store, "Reload local cache", self.reload_database).pack(side="left", padx=(0, 8))
        self.button(store, "Publish preflight route to addon", self.publish).pack(side="left")
        self.database_details = ttk.Label(page, style="Hint.TLabel", wraplength=790)
        self.database_details.pack(anchor="w")

        ttk.Separator(page).pack(fill="x", pady=13)
        ttk.Label(page, text="Verify specific quest IDs", style="Section.TLabel").pack(anchor="w")
        ttk.Label(page, text="Authorize ForeverDB in the Quest Verification tab to look up quest IDs.",
                  style="Hint.TLabel", wraplength=790).pack(anchor="w", pady=(3, 5))
        access = ttk.Frame(page)
        access.pack(fill="x")
        ttk.Label(access, text="Quest IDs", style="Hint.TLabel").pack(side="left", padx=(0, 5))
        ttk.Entry(access, textvariable=self.api_ids).pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.button(access, "Use reported IDs", self.use_reported_ids).pack(side="left", padx=(0, 5))
        self.button(access, "Verify quest IDs", self.sync).pack(side="left")

    def build_diagnostics(self, page: ttk.Frame) -> None:
        ttk.Label(page, text="Sources and compatibility", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        self.diagnostic_view = self.text_view(page, height=22)

    @staticmethod
    def load_startup() -> tuple[list, Any, VerificationCache, dict[str, str], bool, list[str]]:
        problems = []
        installations = []
        database = None
        verified = VerificationCache()
        associations = {}
        monitoring = False
        try:
            installations = discover_installations()
        except (OSError, ValueError) as exc:
            problems.append(f"Installation discovery: {exc}")
        try:
            database = load_cache()
        except (OSError, ValueError) as exc:
            problems.append(f"Quest cache: {exc}")
        try:
            verified = load_verification_cache()
        except (OSError, ValueError) as exc:
            problems.append(f"Verification cache: {exc}")
        try:
            associations = load_associations()
        except (OSError, ValueError) as exc:
            problems.append(f"Character associations: {exc}")
        try:
            monitoring = load_monitor_preference()
        except (OSError, ValueError) as exc:
            problems.append(f"Saved progress monitoring: {exc}")
        return installations, database, verified, associations, monitoring, problems

    def startup_ready(self, result: tuple[list, Any, VerificationCache, dict[str, str], bool, list[str]]) -> None:
        installations, self.base_database, self.verification.cache, self.associations, monitoring, problems = result
        self.diagnostics.extend(problems)
        self.monitor_enabled.set(monitoring)
        self.set_installations(installations)
        self.status_var.set("Use Refresh character progress to read the selected installation."
                            if self.installation else "Select your WoW installation or local guides under Setup & updates.")

    def begin(self, message: str, job: Callable, callback: Callable, *, quiet: bool = False,
              on_error: Callable[[str], None] | None = None) -> None:
        if self.future is not None:
            return
        self.status_var.set(message)
        for button in self.action_buttons:
            button.state(["disabled"])
        self.progress_bar.pack(side="right")
        self.progress_bar.start(20)
        self.on_result = callback
        self.on_error = on_error or (self.note_monitor_problem if quiet else self.fail)
        self.future = self.executor.submit(job)
        self.window.after(70, self.poll)

    def poll(self) -> None:
        if not self.future.done():
            self.window.after(70, self.poll)
            return
        future, self.future = self.future, None
        callback, self.on_result = self.on_result, None
        on_error, self.on_error = self.on_error, None
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        if self.close_after_job:
            self.close()
            return
        for button in self.action_buttons:
            button.state(["!disabled"])
        try:
            callback(future.result())
        except Exception as exc:
            on_error(str(exc) or type(exc).__name__)
        self.update_status_details()

    def fail(self, message: str) -> None:
        self.diagnostics.append(message)
        self.status_var.set("Action needs attention. See Diagnostics for details.")
        self.update_status_details()
        messagebox.showerror("RXP Quest Guard", message, parent=self.window)

    def close(self) -> None:
        self.stop_monitor()
        self.verification.cancel()
        if self.future is not None:
            self.close_after_job = True
            self.status_var.set("Closing after the current action finishes…")
            return
        self.verification.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.window.destroy()

    def set_installations(self, installations: list, selected: Any = None) -> None:
        self.installations = installations
        self.install_box.configure(values=[str(item.addons) for item in installations])
        if selected in installations or len(installations) == 1:
            index = installations.index(selected) if selected in installations else 0
            self.install_box.current(index)
        else:
            self.install_var.set("")
        self.update_install_selection()

    def find_installations(self) -> None:
        def done(found: list) -> None:
            candidates = {item.addons: item for item in self.installations}
            candidates.update({item.addons: item for item in found})
            selected = candidates.get(self.installation.addons) if self.installation else None
            self.set_installations(list(candidates.values()), selected)
            self.status_var.set(f"Found {len(found)} WoW installations. Browse to choose another folder.")

        self.begin("Looking for WoW installations…", discover_installations, done)

    def browse_installation(self) -> None:
        value = filedialog.askdirectory(title="Select the WoW installation, client or AddOns folder", parent=self.window)
        if not value:
            return

        def done(installation: Any) -> None:
            candidates = {item.addons: item for item in self.installations}
            candidates[installation.addons] = installation
            self.set_installations(list(candidates.values()), installation)
            self.status_var.set("WoW installation selected.")

        self.begin("Inspecting the selected installation…", lambda: inspect_installation(Path(value)), done)

    def update_install_selection(self, _event: Any = None) -> None:
        previous = self.installation
        index = self.install_box.current()
        self.installation = self.installations[index] if index >= 0 else None
        self.interface_var.set("")
        if self.installation != previous:
            self.watched_snapshots.clear()
            self.clear_audit()
            if previous is not None and self.authorized_guides.get():
                self.authorized_guides.set(False)
                self.change_guide_permission()
        self.restart_monitor()
        self.update_status_details()

    def toggle_monitor(self) -> None:
        try:
            path = cache_directory() / "monitoring.json"
            atomic_write(path, b"true\n" if self.monitor_enabled.get() else b"false\n")
        except OSError as exc:
            self.diagnostics.append(f"Monitoring preference is set for this session only: {exc}")
        self.restart_monitor()
        self.update_status_details()

    def stop_monitor(self) -> None:
        if self.monitor_after is not None:
            self.window.after_cancel(self.monitor_after)
        self.monitor_after = self.monitor_future = self.monitor = None
        self.monitor_file_count = -1
        self.monitor_problems = ()

    def restart_monitor(self) -> None:
        client = self.installation.addons.parent.parent if self.installation else None
        enabled = self.monitor_enabled.get()
        if enabled and self.monitor and self.monitor.client == client:
            return
        self.stop_monitor()
        self.monitor_catalog_attempted = False
        if not enabled:
            self.monitor_status.set("Monitoring off")
        elif client is None:
            self.monitor_status.set("Select a WoW installation to monitor saved files.")
        else:
            self.monitor = SavedProgressMonitor(client)
            self.monitor_status.set("Waiting for WoW to write a Quest Guard save.")
            self.schedule_monitor(100)

    def schedule_monitor(self, interval: int | None = None) -> None:
        if self.monitor is not None:
            self.monitor_after = self.window.after(MONITOR_INTERVAL_MS if interval is None else interval, self.monitor_tick)

    def monitor_tick(self) -> None:
        self.monitor_after = None
        monitor = self.monitor
        if monitor is None:
            return
        if self.future is not None or self.window.grab_current() is not None:
            self.schedule_monitor()
            return
        if self.monitor_future is None:
            self.monitor_future = self.executor.submit(monitor.poll)
        if not self.monitor_future.done():
            self.schedule_monitor(100)
            return
        completed, self.monitor_future = self.monitor_future, None
        try:
            self.apply_monitor_update(completed.result())
        except (OSError, ValueError) as exc:
            self.note_monitor_problem(str(exc))
        finally:
            self.schedule_monitor()

    def note_monitor_problem(self, message: str) -> None:
        self.monitor_status.set(message)
        self.monitor_problems = (message,)
        if message not in self.diagnostics:
            self.diagnostics.append(message)
        self.update_status_details()

    def apply_monitor_update(self, update: MonitorUpdate) -> None:
        monitor_changed = self.monitor_problems != update.problems or self.monitor_file_count != update.file_count
        self.monitor_problems = update.problems
        self.monitor_file_count = update.file_count
        if monitor_changed or update.saves:
            if update.problems:
                self.monitor_status.set(update.problems[0])
            elif update.file_count:
                self.monitor_status.set("Monitoring WoW's Quest Guard saves.")
            else:
                self.monitor_status.set("Waiting for WoW to write a Quest Guard save.")
            self.update_status_details()
        if not update.saves:
            return

        before = self.snapshot()
        old_rows = {row.identity_key: row for row in self.snapshots if row.identity_key}
        for path, row in update.saves:
            old = old_rows.get(row.identity_key)
            if old and old.exported_at and row.exported_at and old.exported_at > row.exported_at:
                continue
            self.watched_snapshots[path] = row

        had_rows = bool(self.snapshots)
        rows = merge_snapshots(self.all_source_snapshots(), associations=self.associations)
        if before and before.identity_key:
            selected = next((row for row in rows if row.identity_key == before.identity_key), before)
        elif before:
            selected = before
        elif not had_rows and not self.audit_structural:
            owners = {row.identity_key for row in self.watched_snapshots.values()}
            auto_candidates = [row for row in rows if row.identity_key in owners and row.current_guide]
            selected = auto_candidates[0] if len(auto_candidates) == 1 else None
        else:
            selected = None

        changed = bool(selected and (before is None or self.progress_signature(selected) != self.progress_signature(before)))
        if not changed:
            # Older or unrelated saves may add choices. Keep the displayed report and route.
            self.snapshots = [before if before and row.identity_key and row.identity_key == before.identity_key else row
                              for row in rows]
            if before and before not in self.snapshots:
                self.snapshots.append(before)
            self.populate_snapshots(before, update_route=False)
            if selected is None and len(rows) > 1 and not update.problems:
                self.monitor_status.set("New saves found. Select the character whose route you want to scan.")
            elif before and self.last_scan_at is None and self.watched_character(before):
                self.preflight_monitored_save(before, structural=False)
            return

        chapter = self.selected_chapter()
        previous_guide = match_current_guide(self.chapter_options, before)
        keep_chapter = chapter.id if chapter and chapter != previous_guide else None
        structural = self.audit_structural
        self.snapshots = list(rows)
        self.populate_snapshots(selected, chapter_id=keep_chapter)
        self.status_var.set("New Quest Guard save imported.")
        if keep_chapter and not any(guide.id == keep_chapter for guide in self.chapter_options):
            self.chapter_var.set("")
            self.change_scan_chapter()
            self.monitor_status.set("The selected chapter is outside the new saved route. Choose a chapter to continue.")
            return
        self.preflight_monitored_save(selected, structural=structural)

    def watched_character(self, snapshot: Snapshot) -> bool:
        return bool(self.monitor and snapshot.identity_key and any(
            row.identity_key == snapshot.identity_key and row.exported_at == snapshot.exported_at
            for row in self.watched_snapshots.values()
        ))

    def preflight_monitored_save(self, snapshot: Snapshot, *, structural: bool) -> None:
        monitor = self.monitor
        if monitor is None:
            return
        if not self.authorized_guides.get():
            self.monitor_status.set("Save imported. Enable authorized guide analysis in Setup & updates to run preflight.")
            return
        if self.collection is None or self.installed_guide_client not in {None, monitor.client}:
            if self.monitor_catalog_attempted:
                return
            self.monitor_catalog_attempted = True

            def done(collection: Any) -> None:
                if self.monitor is monitor and self.authorized_guides.get():
                    self.set_guides(collection, monitor.client)
                    self.installed_guide_client = monitor.client
                    if snapshot is self.snapshot():
                        self.preflight_monitored_save(snapshot, structural=structural)

            self.begin("Loading installed guides for automatic preflight…", lambda: load_guides(monitor.client),
                       done, quiet=True)
            return
        if (self.anchor_box.current() <= 0
                and not match_current_guide(self.collection.guides, snapshot)):
            self.monitor_status.set("Save imported. Choose a guide to run preflight.")
            return
        self.scan(structural_only=structural, automatic=True)

    def import_installed(self) -> None:
        installation = self.installation
        if installation is None:
            self.fail("Select a WoW installation first.")
            return
        client_path = installation.addons.parent.parent
        wtf_path = client_path / "WTF"
        authorized = self.authorized_guides.get()

        def read() -> tuple[Any, tuple, list[str]]:
            problems = []
            guides = None
            snapshots = ()
            if authorized:
                try:
                    guides = load_guides(client_path)
                except (OSError, ValueError) as exc:
                    problems.append(f"Installed RestedXP guides: {exc}")
            if wtf_path.is_dir():
                try:
                    snapshots = load_snapshots(wtf_path, quest_guard_only=not authorized)
                except (OSError, ValueError) as exc:
                    problems.append(f"Installed character snapshots: {exc}")
            else:
                problems.append(f"WTF folder was not found at {wtf_path}")
            return guides, snapshots, problems

        def done(result: tuple[Any, tuple, list[str]]) -> None:
            guides, snapshots, problems = result
            if self.installation != installation or (authorized and not self.authorized_guides.get()):
                return
            self.diagnostics.extend(problems)
            if guides is not None:
                if self.installed_guide_client and self.installed_guide_client != client_path:
                    self.source_snapshots.pop(self.installed_guide_client / "WTF", None)
                self.set_guides(guides, client_path)
                self.installed_guide_client = client_path
            if snapshots or wtf_path not in self.source_snapshots:
                self.set_snapshots(snapshots, wtf_path)
            self.status_var.set("Installed data import complete. Open Diagnostics for details." if problems else
                                "Installed guide and snapshot import complete." if authorized else
                                "Quest Guard progress imported. You can enable authorized guide analysis in Setup & updates.")
            if not problems and self.verification.eligible_automatic():
                self.scan(automatic=True)

        self.begin("Reading installed guides and exported character data…" if authorized else
                   "Reading saved Quest Guard progress…", read, done)

    def refresh_progress(self) -> None:
        if self.future is not None:
            return
        installation = self.installation
        if installation is not None:
            client = installation.addons.parent.parent
            if self.authorized_guides.get() and (self.collection is None or self.installed_guide_client not in {None, client}):
                self.import_installed()
                return
        sources = dict(self.source_snapshots)
        if installation is not None:
            sources.setdefault(installation.addons.parent.parent / "WTF", ())
        if not sources:
            self.notebook.select(self.sources_page)
            self.status_var.set("Select your WoW installation or import a SavedVariables file to refresh progress.")
            return

        before = self.snapshot()
        selected_chapter = self.selected_chapter()
        current = match_current_guide(self.chapter_options, before)
        keep_chapter = selected_chapter.id if selected_chapter and selected_chapter != current else None
        rescan = self.audit_structural if self.audit is not None else None
        quest_guard_only = not self.authorized_guides.get()

        def read() -> tuple[dict[Path, tuple[Snapshot, ...]], dict[str, str], list[str]]:
            associations = dict(self.associations)
            try:
                associations.update(load_associations())
            except (OSError, ValueError) as exc:
                problems = [f"Character association settings: {exc}"]
            else:
                problems = []
            for path in sources:
                try:
                    sources[path] = load_snapshots(path, quest_guard_only=quest_guard_only)
                except (OSError, ValueError) as exc:
                    problems.append(f"Unable to refresh {path.name}: {exc}")
            return sources, associations, problems

        def done(result: tuple[dict[Path, tuple[Snapshot, ...]], dict[str, str], list[str]]) -> None:
            if not quest_guard_only and not self.authorized_guides.get():
                return
            self.source_snapshots, self.associations, problems = result
            self.diagnostics.extend(problems)
            self.merge_character_sources(self.snapshot(), chapter_id=keep_chapter)
            after = self.snapshot()
            if problems:
                self.status_var.set("Some saved files could not be refreshed. See Diagnostics for details.")
            elif before and after and self.progress_signature(before) == self.progress_signature(after):
                self.status_var.set("No newer save found. Use /reload or log out of WoW, then refresh.")
            else:
                self.status_var.set("Saved character progress refreshed.")
            verify = self.verification.eligible_automatic()
            if not problems and (rescan is not None and (rescan or self.quest_state_ready()) or verify):
                self.scan(structural_only=bool(rescan), automatic=verify)

        self.begin("Reading the latest saved character progress…", read, done)

    @staticmethod
    def progress_signature(snapshot: Snapshot) -> tuple:
        return (snapshot.current_guide, snapshot.current_group, snapshot.current_subgroup, snapshot.current_guide_key,
                snapshot.current_step, snapshot.current_step_id, snapshot.skipped_steps, snapshot.active,
                snapshot.completed, snapshot.ready, snapshot.faction, snapshot.character_class, snapshot.race,
                snapshot.level, snapshot.exported_at, snapshot.freshness, snapshot.state_source,
                snapshot.state_guide_name, snapshot.state_guide_group, snapshot.state_step_id)

    def browse_guides(self, *, folder: bool = False) -> None:
        if not self.authorized_guides.get():
            self.fail("Confirm permission to analyze the selected guide source first.")
            return
        if folder:
            value = filedialog.askdirectory(title="Select a WoW client, RXPGuides or WTF folder", parent=self.window)
        else:
            value = filedialog.askopenfilename(title="Select your RestedXP or WTF ZIP", parent=self.window,
                                               filetypes=[("ZIP archive", "*.zip"), ("All files", "*.*")])
        if value:
            path = Path(value)
            self.begin("Reading locally installed guide metadata…", lambda: load_guides(path),
                       lambda guides: self.set_guides(guides, path) if self.authorized_guides.get() else None)

    def set_guides(self, collection: Any, path: Path) -> None:
        self.collection, self.guide_path = collection, path
        self.installed_guide_client = None
        self.anchor_box.configure(values=["Current linked guide"] + [guide_label(item) for item in collection.guides])
        self.anchor_box.current(0)
        self.update_route()
        self.status_var.set(f"Read {len(collection.guides)} guides. Select a chapter and run preflight.")

    def change_guide_permission(self) -> None:
        self.monitor_catalog_attempted = False
        if self.authorized_guides.get():
            self.status_var.set("Import an authorized guide source or refresh the selected installation.")
            return
        if self.verification.batch_automatic:
            self.verification.cancel()
        self.collection = self.route = self.guide_path = self.installed_guide_client = None
        self.chapter_options.clear()
        self.anchor_box.configure(values=["Current linked guide"])
        self.anchor_box.current(0)
        self.chapter_box.configure(values=[])
        self.chapter_var.set("")
        self.source_snapshots.clear()
        self.merge_character_sources(self.snapshot())
        self.clear_audit()
        self.populate_chapters()
        self.put_text(self.detail_view, "Enable authorized guide analysis to run a route preflight.")
        self.verification.differences = "Run a verification to compare its results with the previous preflight."
        self.verification.refresh(force=True)
        self.status_var.set("Guide analysis disabled. Refresh to read Quest Guard progress.")

    def browse_snapshot(self, *, folder: bool = False) -> None:
        if folder:
            value = filedialog.askdirectory(title="Select the WTF folder", parent=self.window)
        else:
            value = filedialog.askopenfilename(title="Select a SavedVariables snapshot or WTF ZIP", parent=self.window,
                                               filetypes=[("SavedVariables or ZIP", "*.lua *.bak *.zip"),
                                                          ("All files", "*.*")])
        if value:
            path = Path(value)
            quest_guard_only = not self.authorized_guides.get()
            self.begin("Reading exported quest state locally…", lambda: load_snapshots(path, quest_guard_only=quest_guard_only),
                       lambda snapshots: self.set_snapshots(snapshots, path)
                       if quest_guard_only or self.authorized_guides.get() else None)

    def set_snapshots(self, snapshots: tuple, path: Path) -> None:
        self.source_snapshots[path] = snapshots
        self.merge_character_sources(self.snapshot())
        self.status_var.set(f"Read {len(snapshots)} character snapshots from the selected source.")

    @staticmethod
    def character_key(snapshot: Snapshot | None) -> str:
        if snapshot is None:
            return "structural"
        if snapshot.identity_key:
            return snapshot.identity_key
        if snapshot.character and snapshot.realm:
            return f"{snapshot.realm.casefold()}/{snapshot.character.casefold()}"
        return snapshot.association_key or snapshot.label

    def merge_character_sources(self, previous: Snapshot | None, *, chapter_id: str | None = None) -> None:
        self.snapshots = list(merge_snapshots(self.all_source_snapshots(), associations=self.associations))
        selected = next((item for item in self.snapshots if previous and
                         self.character_key(item) == self.character_key(previous)), None)
        if selected is None and self.snapshots:
            selected = max(self.snapshots, key=lambda item: (
                bool(item.character and item.realm and item.current_guide),
                bool(item.character and item.realm and item.active is not None and item.completed is not None),
                item.exported_at or item.source_modified_at or datetime.min.replace(tzinfo=UTC),
            ))
        self.populate_snapshots(selected, chapter_id=chapter_id)

    def all_source_snapshots(self) -> Iterable[Snapshot]:
        return chain(chain.from_iterable(self.source_snapshots.values()), self.watched_snapshots.values())

    def populate_snapshots(self, selected: Snapshot | None, *, chapter_id: str | None = None,
                           update_route: bool = True) -> None:
        base_labels = [f"{item.label} · {item.current_guide} · Step {item.current_step or UNKNOWN}"
                       if item.current_guide else f"{item.label} · Quest export" for item in self.snapshots]
        counts = Counter(base_labels)
        labels = [f"{label} ({index + 1})" if counts[label] > 1 else label
                  for index, label in enumerate(base_labels)]
        self.snapshot_box.configure(values=["No character selected", *labels])
        self.snapshot_box.current(self.snapshots.index(selected) + 1 if selected else 0)
        if update_route:
            self.update_snapshot_selection(chapter_id=chapter_id)

    def association_choices(self) -> tuple[list[Snapshot], list[Snapshot]]:
        primary_slots = {item.source_slot for item in chain.from_iterable(self.source_snapshots.values())
                         if item.source_slot and not item.backup}
        exports = [item for item in self.snapshots if item.association_key and not item.identity_key
                   and not (item.backup and item.source_slot in primary_slots)]
        characters = [item for item in self.snapshots if item.current_guide and item.identity_key]
        return exports, characters

    def associate(self) -> None:
        exports, characters = self.association_choices()
        if not exports or not characters:
            self.fail("Import a quest export and character guide progress before associating them.")
            return

        def description(item: Any) -> str:
            label = f"{item.label} · Step {item.current_step}" if item.current_step else item.label
            if item.exported_at or item.source_modified_at:
                label += f" · {local_date(item.exported_at or item.source_modified_at)}"
            return label

        dialog = tk.Toplevel(self.window, background=BG)
        dialog.title("Associate quest export")
        dialog.transient(self.window)
        dialog.resizable(False, False)
        panel = ttk.Frame(dialog, padding=18)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="Select the character who created this quest export. The confirmation is saved for this exact export.",
                  wraplength=520).pack(anchor="w", pady=(0, 10))
        ttk.Label(panel, text="Quest export", style="Hint.TLabel").pack(anchor="w")
        export_box = ttk.Combobox(panel, state="readonly", values=[description(item) for item in exports], width=58)
        export_box.pack(fill="x", pady=(2, 8))
        current = self.snapshot()
        if current in exports:
            export_box.current(exports.index(current))
        elif len(exports) == 1:
            export_box.current(0)
        ttk.Label(panel, text="Guide progress", style="Hint.TLabel").pack(anchor="w")
        character_box = ttk.Combobox(panel, state="readonly", width=58,
                                    values=[description(item) for item in characters])
        character_box.pack(fill="x", pady=(2, 12))
        if current in characters:
            character_box.current(characters.index(current))

        def match() -> None:
            export_index, character_index = export_box.current(), character_box.current()
            if min(export_index, character_index) < 0:
                messagebox.showerror("Associate quest export", "Select an export and a character.", parent=dialog)
                return
            try:
                exported, character = exports[export_index], characters[character_index]
                self.associations = save_association(
                    exported, character, snapshots=chain.from_iterable(self.source_snapshots.values()),
                )
            except OSError as exc:
                messagebox.showerror("Associate quest export", f"Could not save the confirmation. {exc}", parent=dialog)
                return
            except ValueError as exc:
                messagebox.showerror("Associate quest export", str(exc), parent=dialog)
                return
            self.merge_character_sources(character)
            self.status_var.set("Quest export associated with the selected character. Confirmation saved.")
            dialog.destroy()

        actions = ttk.Frame(panel)
        actions.pack(fill="x")
        ttk.Button(actions, text="Associate export", style="Primary.TButton", command=match).pack(side="right")
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right", padx=(0, 5))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        export_box.focus_set()

    def snapshot(self) -> Any:
        index = self.snapshot_box.current()
        return self.snapshots[index - 1] if index > 0 else None

    def select_character(self, _event: Any = None) -> None:
        structural = self.audit_structural
        self.update_snapshot_selection()
        snapshot = self.snapshot()
        if snapshot and self.future is None and self.watched_character(snapshot):
            self.preflight_monitored_save(snapshot, structural=structural)

    def update_snapshot_selection(self, _event: Any = None, *, chapter_id: str | None = None) -> None:
        snapshot = self.snapshot()
        for label, field in (("Active quests", "active"), ("Completed quests", "completed")):
            state = getattr(snapshot, field, None)
            self.metrics[label].set(len(state) if state is not None else UNKNOWN)
        self.update_route(chapter_id=chapter_id)
        self.update_status_details()

    def route_context(self) -> tuple[str | None, str | None, str | None]:
        snapshot = self.snapshot()
        faction = self.faction_var.get()
        character_class = self.class_var.get()
        return (
            getattr(snapshot, "faction", None) if faction == "From snapshot" else faction,
            getattr(snapshot, "character_class", None) if character_class == "From snapshot" else character_class,
            getattr(snapshot, "race", None),
        )

    def clear_audit(self) -> None:
        self.audit = None
        self.audit_context = None
        self.audit_structural = False
        self.metrics["Findings"].set(UNKNOWN)
        self.counts_var.set("")
        self.finding_tree.delete(*self.finding_tree.get_children())
        self.report_rows.clear()

    def update_route(self, _event: Any = None, *, chapter_id: str | None = None) -> None:
        self.clear_audit()
        if self.collection is None:
            return
        snapshot = self.snapshot()
        index = self.anchor_box.current()
        anchor = self.collection.guides[index - 1].id if index > 0 else None
        faction, character_class, race = self.route_context()
        matched = match_current_guide(self.collection.guides, snapshot) if snapshot else None
        self.route = build_route(self.collection.guides, anchor, faction=faction, character_class=character_class,
                                 race=race, current_id=matched.id if matched else None)
        self.chapter_options = list(self.route.planned_guides)
        self.chapter_box.configure(values=[guide_label(item) for item in self.chapter_options])
        self.chapter_var.set("")
        if self.chapter_options:
            selected = next((idx for idx, guide in enumerate(self.chapter_options) if guide.id == chapter_id), 0)
            self.chapter_box.current(selected)
        self.update_step_default()
        self.populate_chapters()
        message = "Choose a chapter and scan range. Double-click a chapter to scan it."
        if not self.chapter_options:
            message = "No route could be selected. Choose a route start or review Diagnostics."
        elif snapshot and snapshot.current_guide and not matched:
            message = "The saved guide could not be matched to this route. Check the route start and chapter variant."
        self.put_text(self.detail_view, message)
        self.update_status_details()
        self.verification.context_changed()

    def selected_chapter(self) -> Any:
        index = self.chapter_box.current()
        return self.chapter_options[index] if 0 <= index < len(self.chapter_options) else None

    def step_key(self) -> tuple[str, str] | None:
        chapter = self.selected_chapter()
        return (self.character_key(self.snapshot()), chapter.id) if chapter else None

    def set_step_display(self, value: str, override: bool, label: str) -> None:
        self.setting_step = True
        self.step_var.set(value)
        self.step_override.set(override)
        self.step_display.set(label)
        self.step_entry.state(["!readonly"] if override else ["readonly"])
        self.setting_step = False

    def update_step_default(self) -> None:
        chapter = self.selected_chapter()
        if chapter is None:
            self.set_step_display("", False, "Expanded step")
            self.metrics["Chapter"].set(UNKNOWN)
            return
        self.metrics["Chapter"].set(chapter.name)
        key = self.step_key()
        if key in self.step_overrides:
            self.set_step_display(self.step_overrides[key], True, "Manual expanded step")
            return
        snapshot = self.snapshot()
        current = match_current_guide(self.chapter_options, snapshot)
        resolution = resolve_current_step(chapter, snapshot) if snapshot and chapter == current else None
        if resolution:
            value = str(resolution.offline_step) if resolution.offline_step is not None else UNKNOWN
            self.set_step_display(value, False, "Mapped saved step" if resolution.exact else "Estimated saved step")
        else:
            self.set_step_display("1", False, "Chapter first step")

    def toggle_step_override(self) -> None:
        key = self.step_key()
        if key is not None:
            if self.step_override.get():
                current = self.step_var.get()
                self.step_overrides[key] = current if current.isdecimal() else "1"
            else:
                self.step_overrides.pop(key, None)
        self.change_scan_chapter()
        if self.step_override.get():
            self.step_entry.focus_set()
            self.step_entry.select_range(0, "end")

    def store_step_override(self, *_args: str) -> None:
        if self.setting_step or not self.step_override.get():
            return
        key = self.step_key()
        if key is not None:
            self.step_overrides[key] = self.step_var.get()
        self.change_scan_scope()

    def change_scan_chapter(self, _event: Any = None) -> None:
        self.update_step_default()
        self.change_scan_scope()
        self.verification.context_changed()

    def change_scan_scope(self, _event: Any = None) -> None:
        self.clear_audit()
        self.populate_chapters()
        self.put_text(self.detail_view, "Run preflight to evaluate the selected range.")
        self.update_status_details()

    def scan_selected_tree_chapter(self, _event: Any) -> None:
        selected = self.chapter_tree.selection()
        if not selected or self.future:
            return
        chapter = self.visible_chapters[int(selected[0])]
        self.chapter_box.current(self.chapter_options.index(chapter))
        self.scope_var.set("Selected chapter")
        self.change_scan_chapter()
        self.scan()

    def quest_state_ready(self) -> bool:
        snapshot = self.snapshot()
        return bool(snapshot and snapshot.character and snapshot.realm and not snapshot.ownership_conflict
                    and snapshot.freshness != "unavailable")

    def scan(self, *, structural_only: bool = False, automatic: bool = False, from_verification: bool = False) -> None:
        if self.future is not None:
            return
        reject = self.monitor_status.set if automatic else self.fail
        index = self.chapter_box.current()
        if self.route is None or index < 0:
            reject("Import local RestedXP guides under Setup & updates and select a route chapter.")
            return
        if not structural_only and not self.quest_state_ready():
            warning = ("A character needs both active and completed quests for preflight. Use /reload or log out in WoW, "
                       "then refresh. Confirm a legacy export if you know who made it, or choose Structural-only scan.")
            if automatic:
                reject("Save imported. Both quest lists are needed for automatic preflight.")
            else:
                self.status_var.set("Quest history is incomplete. A structural-only scan is available.")
                messagebox.showwarning("Quest history unavailable", warning, parent=self.window)
            return
        scope = self.scope_var.get()
        if scope == "Next chapter":
            index += 1
            if index >= len(self.chapter_options):
                reject("The selected route has no linked chapter after this one.")
                return
        chapter = self.chapter_options[index]
        start, start_override = 1, False
        if scope != "Next chapter":
            raw_step = self.step_var.get().strip()
            start_override = self.step_override.get()
            if structural_only and raw_step == UNKNOWN:
                raw_step = "1"
            if not re.fullmatch(r"[0-9]{1,7}", raw_step) or not 1 <= int(raw_step) <= len(chapter.steps):
                reject(f"Choose a manual expanded step from 1 through {len(chapter.steps)} in {chapter.name}.")
                return
            start = int(raw_step)
        self.update_effective_database(fresh_projection=True)
        route, snapshot, database = self.route, self.snapshot(), self.database
        installation = self.installation
        selection = (self.chapter_var.get(), self.step_var.get(), self.step_override.get(), scope)
        current = match_current_guide(route.guides, snapshot)
        requested_start = None if chapter == current and not start_override and scope != "Next chapter" else start
        faction, character_class, _race = self.route_context()
        context = snapshot
        if snapshot:
            context = replace(snapshot, faction=faction, character_class=character_class)
        elif faction or character_class:
            context = Snapshot("Route context", faction=faction, character_class=character_class)

        def done(audit: Any) -> None:
            current_selection = (self.chapter_var.get(), self.step_var.get(), self.step_override.get(), self.scope_var.get())
            if route is self.route and snapshot is self.snapshot() and selection == current_selection:
                self.audit_structural = structural_only
                self.audit_context = (installation, database, route, context)
                self.last_scan_at = datetime.now(UTC)
                self.last_scan_snapshot_at = snapshot.exported_at if snapshot and not structural_only else None
                self.update_last_scan()
                method = "Structural-only" if structural_only else "Automatic preflight" if automatic else "Preflight"
                marker = " (manual override)" if start_override else ""
                self.show_audit(audit, f"{method} · {chapter.name} · Step {start}{marker} · {scope}")
                self.verification.on_preflight(audit, from_verification=from_verification)
            else:
                self.status_var.set("Selection changed. Run preflight for the new route or character.")

        self.clear_audit()
        self.begin(f"Scanning {chapter.name} from step {start}…",
                   lambda: audit_route(route, context, database.quests if database else None,
                                       chapter_id=chapter.id, start_step=requested_start, structural_only=structural_only,
                                       **SCOPES[scope]), done, quiet=automatic,
                   on_error=self.verification.status.set if from_verification else None)

    def update_last_scan(self) -> None:
        scanned = local_date(self.last_scan_at, seconds=True)
        saved = local_date(self.last_scan_snapshot_at, seconds=True)
        self.last_scan_var.set(f"Last completed preflight · {scanned}  |  Snapshot used · {saved}")

    def preview_chapters(self) -> list:
        index = self.chapter_box.current()
        if index < 0:
            return []
        scope = self.scope_var.get()
        options = SCOPES[scope]
        guides = self.chapter_options[index + (scope == "Next chapter"):]
        if "chapters" in options:
            return guides[:options["chapters"]]
        visible = []
        raw = self.step_var.get()
        preceding = max(0, int(raw) - 1) if re.fullmatch(r"[0-9]{1,7}", raw) else 0
        for guide in guides:
            if visible and "end_level" in options and guide.level_min is not None and guide.level_min >= options["end_level"]:
                break
            visible.append(guide)
            preceding -= len(guide.steps)
            if "lookahead" in options and preceding + options["lookahead"] < 0:
                break
        return visible

    def populate_chapters(self) -> None:
        counts = {item.guide.id: len(item.findings) for item in self.audit.chapters} if self.audit else {}
        selected = self.chapter_tree.selection()
        selected_guide = self.visible_chapters[int(selected[0])] if selected else self.selected_chapter()
        self.visible_chapters = [item.guide for item in self.audit.chapters] if self.audit else self.preview_chapters()
        self.chapter_tree.delete(*self.chapter_tree.get_children())
        for index, chapter in enumerate(self.visible_chapters):
            count = counts.get(chapter.id, "—")
            self.chapter_tree.insert("", "end", iid=str(index), values=(chapter.name, count))
        if self.visible_chapters:
            index = self.visible_chapters.index(selected_guide) if selected_guide in self.visible_chapters else 0
            self.chapter_tree.selection_set(str(index))
            self.chapter_tree.see(str(index))
        self.show_chapter()

    def show_chapter(self, _event: Any = None) -> None:
        selected = self.chapter_tree.selection()
        if not selected:
            self.put_text(self.chapter_view, "Chapter summaries appear after a scan.")
            return
        guide = self.visible_chapters[int(selected[0])]
        summary = next((item for item in self.audit.chapters if item.guide.id == guide.id), None) if self.audit else None
        if summary is None:
            coverage = "Outside the report scope." if self.audit else "Ready to scan."
            self.put_text(self.chapter_view, f"{guide.name}\n{len(guide.steps)} steps · {coverage}\n"
                          "Double-click the chapter to scan it.")
            return
        lines = [f"Steps {summary.first_step}–{summary.last_step}",
                 f"{len(summary.completed_quests)} known completed · {len(summary.planned_quests)} planned",
                 f"{len(summary.unknown_quests)} without quest metadata · {len(summary.unguarded_steps)} without a recognized guard"]
        if summary.active_quests:
            names = {action.quest_id: action.name for step in guide.steps for action in step.actions if action.name}
            if self.database:
                names.update({quest_id: self.database.quests[quest_id].get("name") or names.get(quest_id)
                              for quest_id in summary.active_quests if quest_id in self.database.quests})
            lines.extend(("", f"{len(summary.active_quests)} already active"))
            lines.extend(f"{names[quest_id]} ({quest_id})" if names.get(quest_id) else str(quest_id)
                         for quest_id in summary.active_quests)
        self.put_text(self.chapter_view, "\n".join(lines))

    def show_audit(self, audit: Any, scope: str) -> None:
        self.audit = audit
        self.populate_chapters()
        self.finding_tree.delete(*self.finding_tree.get_children())
        self.report_rows.clear()
        findings = (*audit.findings, *audit.historical_findings)
        for index, item in enumerate(findings):
            key = str(index)
            self.report_rows[key] = item
            historical = index >= len(audit.findings)
            status = "Historical" if historical else item.severity.replace("_", " ").title()
            self.finding_tree.insert("", "end", iid=key, tags=(item.severity,), values=(
                status, item.quest_id or UNKNOWN, item.title, item.step or "—",
            ))
        self.metrics["Findings"].set(len(findings))
        self.counts_var.set("  ·  ".join(f"{label}: {getattr(audit.counts, key)}" for key, label in COUNT_LABELS.items()))
        self.status_var.set(scope)
        if self.report_rows:
            self.finding_tree.selection_set("0")
            self.show_finding()
        else:
            self.put_text(self.detail_view, "\n".join(audit.diagnostics) or "No findings in the scanned range.")

    def show_finding(self, _event: Any = None) -> None:
        selected = self.finding_tree.selection()
        item = self.report_rows.get(selected[0]) if selected else None
        if item is None:
            return
        lines = [item.title, item.detail]
        if item.pickup_guide:
            pickup = f"Pickup guide  {item.pickup_guide}"
            if item.pickup_step:
                pickup += f" · Step {item.pickup_step}"
            lines.append(pickup)
        location = format_location(item.pickup_location)
        if location:
            lines.append(f"Pickup location  {location}")
        if item.requirements:
            lines.append("Prerequisite quest IDs  " + ", ".join(map(str, item.requirements)))
        lines.append(f"{item.classification.title()} · {item.confidence.title()} confidence · {item.source}")
        self.put_text(self.detail_view, "\n\n".join(lines))

    def save_report(self) -> None:
        if self.audit is None:
            self.fail("Run preflight before saving a report.")
            return
        name = filedialog.asksaveasfilename(title="Save leveling preflight report", parent=self.window,
                                            initialfile="quest-guard-preflight.txt", defaultextension=".txt",
                                            filetypes=[("Text report", "*.txt"), ("All files", "*.*")])
        if name:
            try:
                report = self.audit.render()
                database = self.audit_context[1] if self.audit_context else None
                if database and database.has_foreverdb:
                    report = f"{report.rstrip()}\n\n{FOREVERDB_CREDIT}\n"
                Path(name).write_text(report, encoding="utf-8")
            except OSError as exc:
                self.fail(str(exc))
            else:
                self.status_var.set("Preflight report saved.")

    def update_effective_database(self, *, fresh_projection: bool = False) -> bool:
        database = self.verification.effective_database(self.base_database, fresh_projection=fresh_projection)
        changed = self.database != database
        self.database = database
        return changed

    def set_database(self, database: Any, message: str) -> None:
        self.base_database = database
        changed = self.update_effective_database()
        if changed and self.audit is not None:
            self.clear_audit()
            self.populate_chapters()
            self.put_text(self.detail_view, "Quest data changed. Run preflight to update the report.")
            message += " Run preflight again to use this data."
        self.status_var.set(message)

    def browse_database(self) -> None:
        name = filedialog.askopenfilename(title="Select an authorized quest database JSON", parent=self.window,
                                          filetypes=[("Quest JSON", "*.json"), ("All files", "*.*")])
        if name:
            self.begin("Validating and caching quest data…", lambda: import_database(Path(name)),
                       lambda data: self.set_database(data, "Quest data imported into the local cache."))

    def reload_database(self) -> None:
        def done(result: tuple) -> None:
            data, self.verification.cache = result
            available = data is not None or self.verification.cache.quest_ids
            self.set_database(data, "Quest cache loaded." if available else "No quest cache is available.")

        self.begin("Loading the local quest cache…", lambda: (load_cache(), load_verification_cache()), done)

    def use_reported_ids(self) -> None:
        if self.audit is None:
            self.fail("Run preflight to select quest IDs from its findings.")
            return
        ids = list(dict.fromkeys(item.quest_id for item in (*self.audit.findings, *self.audit.historical_findings)
                                 if item.quest_id))
        if not ids:
            self.status_var.set("This report contains no quest IDs to sync.")
            return
        self.api_ids.set(", ".join(str(item) for item in ids[:30]))
        suffix = " (first 30)" if len(ids) > 30 else ""
        self.status_var.set(f"Added {min(len(ids), 30)} quest IDs{suffix}. Review them before syncing.")

    def sync(self) -> None:
        try:
            ids = selected_ids(self.api_ids.get())
        except ValueError as exc:
            self.fail(str(exc))
            return

        self.notebook.select(self.verification_page)
        self.verification.request_ids(ids)

    def requested_interface(self) -> int | None:
        raw = self.interface_var.get().strip()
        if raw and (not re.fullmatch(r"[0-9]{1,9}", raw) or int(raw) <= 0):
            raise ValueError("Enter the numeric client interface value or leave the override blank for detection.")
        return int(raw) if raw else None

    def install(self) -> None:
        installation = self.installation
        if installation is None:
            self.fail("Select a WoW installation first.")
            return
        try:
            interface = self.requested_interface()
        except ValueError as exc:
            self.fail(str(exc))
            return
        self.begin("Installing Quest Guard…",
                   lambda: install_addon(installation, interface=interface),
                   lambda path: self.status_var.set(f"Installed at {path}. Start WoW or use /reload."))

    def publish(self, *, notify_reload: bool = True, quiet: bool = False) -> None:
        installation = self.installation
        reject = self.verification.status.set if quiet else self.fail
        if installation is None:
            reject("Select a WoW installation first.")
            return
        if self.audit is None or not self.audit.chapters or self.audit_structural or not self.audit_context:
            reject("Run a character preflight for the intended route before publishing it.")
            return
        audited_installation, database, route, snapshot = self.audit_context
        if audited_installation != installation:
            reject("Run preflight again after selecting the target WoW installation.")
            return
        try:
            interface = self.requested_interface()
        except ValueError as exc:
            reject(str(exc))
            return
        scanned = self.audit.chapters
        guide_release = self.collection.version if self.collection else None

        def done(_path: Path) -> None:
            message = "Route and quest data published to Quest Guard."
            if notify_reload:
                message += " Use /reload to load it in WoW."
            self.status_var.set(message)
            self.verification.status.set(message)

        self.begin("Publishing the preflight route…", lambda: publish_route(
            installation, database, route.guides, snapshot=snapshot,
            start_chapter_id=scanned[0].guide.id, interface=interface, guide_release=guide_release,
        ), done, on_error=reject)

    def update_readiness(self) -> None:
        snapshot = self.snapshot()
        named = bool(snapshot and snapshot.character and snapshot.realm and not snapshot.ownership_conflict)
        ready = self.quest_state_ready()
        guide = self.selected_chapter()
        runtime = snapshot.current_step if snapshot else None
        if named:
            identity = f"{snapshot.character} — {snapshot.realm}"
            roles = " ".join(piece for piece in (snapshot.faction, snapshot.character_class) if piece)
            if roles:
                identity += f" · {roles}"
            progress = f"{snapshot.current_guide or 'Guide unknown'} · Saved step {runtime or UNKNOWN}"
            saved_time = f"Last snapshot {local_date(snapshot.exported_at)}"
            self.character_details.set(f"{identity}\n{progress} · {saved_time}")
        else:
            self.character_details.set("Character progress")
        status = [
            "WoW installation detected" if self.installation else "WoW installation not selected",
            "RestedXP guides imported" if self.collection and self.collection.guides else "Import RestedXP guides",
            "Character identified" if named else "Select a character",
            "Quest history associated" if ready else "Quest history unavailable",
            "Route selected" if guide else "Choose a route",
        ]
        step = self.step_var.get()
        if ready and guide and re.fullmatch(r"[0-9]{1,7}", step) and 1 <= int(step) <= len(guide.steps):
            status.append("Ready for preflight")
        self.readiness_var.set("  ·  ".join(status))
        if ready and snapshot.freshness == "stale":
            notice = "Stale quest snapshot. Save with /reload or log out of WoW, then refresh for current results."
        elif ready and snapshot.freshness == "unknown":
            notice = "Quest snapshot time is unknown. Save with /reload or log out of WoW, then refresh."
        elif not ready:
            notice = "Quest-state validation needs both quest lists. Use /reload or log out in WoW, then refresh."
        else:
            notice = "The desktop reads the latest saved files. Use /reload or log out before refreshing new progress."
        self.progress_notice.set(notice)
        exports, characters = self.association_choices()
        allow_confirmation = exports and characters
        self.associate_button.state(["!disabled"] if allow_confirmation and self.future is None else ["disabled"])

    def update_status_details(self) -> None:
        installation = self.installation
        self.update_effective_database()
        database = self.database
        collection = self.collection
        snapshot = self.snapshot()
        if database and database.has_foreverdb:
            self.foreverdb_credit.pack(side="right", padx=(8, 0))
        else:
            self.foreverdb_credit.pack_forget()
        self.update_readiness()
        client = installation.interface if installation else None
        client_version = installation.client_version if installation else None
        restedxp = ((installation.restedxp_version if installation else None) or (collection.version if collection else None))
        install_summary = (f"WoW Forever {client_version or UNKNOWN} · Interface {client or UNKNOWN} · "
                           f"RestedXP {restedxp or UNKNOWN}"
                           if installation else "Choose the WoW client or AddOns folder to install the companion.")
        self.install_details.configure(text=install_summary)
        guides = len(collection.guides) if collection else 0
        self.import_details.configure(text=f"{guides} guides loaded · {len(self.snapshots)} character snapshots")
        db_summary = "No quest database cached. Local guide preflight is available."
        if database:
            updated = database.updated_at or UNKNOWN
            db_summary = (f"{database.source} · Version {database.source_version or UNKNOWN} · "
                          f"{len(database.quests)} quests · {source_age(database.age_days, 30)}")
        else:
            updated = UNKNOWN
        self.database_details.configure(text=db_summary)
        exported = snapshot.exported_at if snapshot else None
        export_age = (datetime.now(UTC) - exported).total_seconds() / 86400 if exported else None
        export_summary = f"{local_date(exported)} · {source_age(export_age, 1)}" if exported else UNKNOWN
        lines = [
            f"Quest Guard Desktop  {__version__}",
            install_summary,
            f"AddOns folder  {installation.addons if installation else UNKNOWN}",
            f"Quest Guard addon  {self.addon_status(installation)}",
            f"Installation detection  {installation.status if installation else UNKNOWN}",
            f"RestedXP guide source  {self.guide_path or UNKNOWN}",
            f"RestedXP interfaces in source  {', '.join(map(str, collection.interfaces)) if collection and collection.interfaces else UNKNOWN}",
            "",
            f"Character snapshot sources  {', '.join(map(str, self.source_snapshots)) or UNKNOWN}",
            f"Selected character  {snapshot.label if snapshot else UNKNOWN}",
            f"Last imported snapshot  {local_date(snapshot.imported_at if snapshot else None)}",
            f"Snapshot source modified  {local_date(snapshot.source_modified_at if snapshot else None)}",
            f"Quest export time  {export_summary}",
            f"Monitoring  {self.monitor_status.get()}",
            self.last_scan_var.get(),
            f"Saved level  {snapshot.level or UNKNOWN if snapshot else UNKNOWN}",
            f"Export client interface  {snapshot.client_interface or UNKNOWN if snapshot else UNKNOWN}",
            f"Saved guide  {snapshot.current_guide or UNKNOWN if snapshot else UNKNOWN}",
            f"Saved step  {snapshot.current_step or UNKNOWN if snapshot else UNKNOWN}",
            f"Saved stable step ID  {snapshot.current_step_id or UNKNOWN if snapshot else UNKNOWN}",
            f"{self.step_display.get()}  {self.step_var.get() or UNKNOWN}",
            f"Quest state source  {snapshot.state_source or UNKNOWN if snapshot else UNKNOWN}",
            f"Skipped steps in export  {len(snapshot.skipped_steps) if snapshot and snapshot.skipped_steps is not None else UNKNOWN}",
            "",
            db_summary,
            f"Last database update  {updated}",
            f"Cache folder  {cache_directory()}",
            "",
            "Character data is processed locally. ForeverDB sync uses the entered quest IDs and API key only.",
        ]
        notes = self.diagnostics.copy()
        notes.extend(self.monitor_problems)
        if collection:
            notes.extend(collection.diagnostics)
        if self.route:
            notes.extend(self.route.diagnostics)
        if self.audit:
            notes.extend(self.audit.diagnostics)
        if snapshot:
            notes.extend(snapshot.diagnostics)
        if notes:
            lines.extend(["", "Diagnostics", *dict.fromkeys(notes)])
        self.put_text(self.diagnostic_view, "\n".join(lines))
        self.verification.refresh()

    @staticmethod
    def addon_status(installation: Any) -> str:
        if not installation:
            return UNKNOWN
        toc = installation.addons / "RXPQuestGuard" / "RXPQuestGuard.toc"
        if not toc.is_file():
            return "Not installed"
        try:
            content = toc.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return "Installed · Manifest unreadable"
        version = re.search(r"^##\s*Version:\s*(\S[^\r\n]*)", content, re.MULTILINE)
        return f"Installed · Version {version[1]}" if version else "Installed · Version unknown"


def main(*, smoke_test: bool = False) -> int:
    window = tk.Tk()
    if smoke_test:
        window.withdraw()
    dashboard = Dashboard(window)
    if smoke_test:
        def finish_when_ready() -> None:
            if dashboard.future is None:
                dashboard.close()
            else:
                window.after(100, finish_when_ready)

        window.after(100, finish_when_ready)
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
