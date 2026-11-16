"""Launch the desktop UI or run a reproducible local preflight."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys


def _require_authorized_guides(args) -> None:
    if args.guides and not args.authorized_guides:
        raise ValueError("Add --authorized-guides to confirm that every selected guide is yours or permitted for processing with Quest Guard.")


def _one(records, term, fields, label):
    term = term.casefold()
    exact = [row for row in records if any(term == str(getattr(row, field, "")).casefold() for field in fields)]
    matches = exact or [row for row in records if any(term in str(getattr(row, field, "")).casefold() for field in fields)]
    if len(matches) != 1:
        raise ValueError(f"{label} must match exactly one record. Matched {len(matches)}.")
    return matches[0]


def _audit(args) -> int:
    from .engine import audit_route, build_route, match_current_guide
    from .evidence import FOREVERDB_CREDIT
    from .ingest import attach_progress, load_guides, load_snapshots
    from .sync import atomic_write, load_cache

    _require_authorized_guides(args)
    collection = load_guides(args.guides)
    snapshots = load_snapshots(args.snapshot) if args.snapshot else ()
    if args.character:
        snapshot = _one(snapshots, args.character, ("label", "character"), "Character")
    elif len(snapshots) > 1:
        raise ValueError("Multiple snapshots are available. Use --character to select one.")
    else:
        snapshot = next(iter(snapshots), None)
    if args.progress_character:
        if snapshot is None:
            raise ValueError("Select an export snapshot with --character before associating its guide progress.")
        progress = _one(snapshots, args.progress_character, ("label", "character"), "Progress character")
        snapshot = attach_progress(snapshot, progress)
    if snapshot:
        snapshot = replace(snapshot, faction=args.faction or snapshot.faction,
                           character_class=args.character_class or snapshot.character_class)
    if not args.structural_only and (
        snapshot is None or not snapshot.character or not snapshot.realm or snapshot.freshness == "unavailable"
    ):
        raise ValueError("Select identified character progress with both quest lists, or use --structural-only.")
    faction = args.faction or (snapshot and snapshot.faction)
    character_class = args.character_class or (snapshot and snapshot.character_class)
    current = match_current_guide(collection.guides, snapshot) if snapshot else None
    anchor = _one(collection.guides, args.route_start, ("id", "name"), "Route start") if args.route_start else None
    route = build_route(collection, anchor.id if anchor else None, faction=faction,
                        character_class=character_class, race=snapshot and snapshot.race,
                        current_id=current.id if current else None)
    if not route.planned_guides:
        raise ValueError("No route matched. Review the guide inputs and choose a route start.")
    if args.chapter:
        chapter = _one(route.planned_guides, args.chapter, ("id", "name"), "Chapter")
    else:
        chapter = current if current in route.planned_guides else route.planned_guides[0]
    database = load_cache() if args.use_cache else None
    result = audit_route(route, snapshot, database.quests if database else None,
                         chapter_id=chapter.id, start_step=args.step, end_level=args.through_level,
                         structural_only=args.structural_only)
    sources = []
    if database and database.has_foreverdb:
        sources.append(FOREVERDB_CREDIT)
    report = "\n\n".join([result.render().rstrip(), *sources]) + "\n"
    if args.output:
        atomic_write(args.output, report.encode("utf-8"))
    else:
        print(report)
    return 0


def _inputs(args) -> int:
    from .ingest import load_guides, load_snapshots

    _require_authorized_guides(args)
    if args.guides:
        collection = load_guides(args.guides)
        for guide in collection.guides:
            print(guide.id)
        for line in collection.diagnostics:
            print(line, file=sys.stderr)
    if args.snapshot:
        for snapshot in load_snapshots(args.snapshot, quest_guard_only=not args.authorized_guides):
            print(snapshot.label)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m questguard", description=__doc__)
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("desktop", help="Open the desktop window")
    inputs = subcommands.add_parser("inputs", help="List readable guide or snapshot selections")
    inputs.add_argument("--guides", type=Path)
    inputs.add_argument("--snapshot", type=Path)
    audit = subcommands.add_parser("audit", help="Scan the planned route from one chapter and step")
    audit.add_argument("--guides", type=Path, required=True)
    audit.add_argument("--snapshot", type=Path)
    audit.add_argument("--character", help="Exact snapshot label or character")
    audit.add_argument("--progress-character", help="Explicitly associate a separately saved progression snapshot")
    audit.add_argument("--faction", choices=("Horde", "Alliance"))
    audit.add_argument("--class", dest="character_class")
    audit.add_argument("--route-start", help="Guide name or full identity for the route start")
    audit.add_argument("--chapter", help="Guide name or full identity for the first scanned chapter")
    audit.add_argument("--step", type=int)
    audit.add_argument("--through-level", type=int, help="Stop when the next chapter begins at this level")
    audit.add_argument("--structural-only", action="store_true", help="Assess guide structure without character quest state")
    audit.add_argument("--use-cache", action="store_true", help="Include the currently cached quest metadata")
    audit.add_argument("--output", type=Path)
    for command in (inputs, audit):
        command.add_argument("--authorized-guides", action="store_true",
                             help="Confirm all selected guide sources permit processing with Quest Guard")
    args = parser.parse_args()
    try:
        if args.command == "audit":
            return _audit(args)
        if args.command == "inputs":
            return _inputs(args)
        from .gui import main as desktop_main

        return desktop_main()
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
