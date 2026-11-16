from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import TestCase

from questguard.engine import audit_route, build_route, match_current_step, resolve_current_step
from questguard.ingest import Snapshot, parse_guide


GROUP = "Test Horde Route"


def chapter(name: str, steps: str, next_chapter: str = "", guide_id: int = 100):
    text = f"#name {name}\n#group {GROUP}\n<< Horde\n"
    if next_chapter:
        text += f"#next {next_chapter}\n"
    return parse_guide(text + steps, metadata={"guideId": guide_id})


def at(guide, number=1, *, active=(), completed=()):
    return Snapshot(
        "Synthetic character", current_guide=guide.name, current_group=guide.group,
        current_step=number, current_step_id=guide.steps[number - 1].step_id,
        active=frozenset(active), completed=frozenset(completed), exported_at=datetime.now(UTC),
    )


class RouteEngineTests(TestCase):
    def test_scoutmaster_vargas_is_flagged_in_lookahead_with_pickup(self):
        early = chapter("40-42 Lantern Marsh I", """
step
.goto Test Lantern Marsh,65,25
.accept 95882 >> Accept WANTED: Scoutmaster Vargas
.target WANTED poster
""", "43-44 Lantern Marsh II", 1000)
        later = chapter("43-44 Lantern Marsh II", """
step
.goto Test Lantern Marsh,50,50
step
.complete 95882,1
""", guide_id=2000)
        route = build_route((early, later), current_id=later.id, faction="Horde", character_class="Hunter")
        snapshot = at(later)
        self.assertEqual(match_current_step(later, snapshot), 1)
        self.assertFalse(audit_route(route, snapshot, lookahead=0).findings)
        scan = audit_route(route, snapshot, lookahead=1)
        self.assertEqual(len(scan.findings), 1)
        issue = scan.findings[0]
        self.assertEqual((issue.quest_id, issue.category, issue.severity, issue.step), (95882, "missing_pickup", "warning", 2))
        self.assertEqual((issue.pickup_guide, issue.pickup_step, issue.pickup_location.name), (early.name, 1, "WANTED poster"))
        self.assertEqual(issue.classification, "unknown")

    def test_it_takes_guts_active_and_completed_prerequisite(self):
        guide = chapter("43-44 Lantern Marsh II", """
step
.complete 95261,1
step
.turnin 95261
.accept 95267
step
.complete 95267,1
""")
        route = build_route((guide,), guide.id, faction="Horde")
        data = {95261: {"requirements": [{"questId": 95260}]}, 95267: {"requirements": [{"questId": 95261}]}}
        scan = audit_route(route, at(guide, active=(95261,), completed=(95260,)), data)
        self.assertEqual(scan.chapters[0].active_quests, (95261,))
        self.assertEqual(scan.chapters[0].planned_quests, (95267,))
        self.assertFalse(scan.findings)

    def test_feralas_active_carryover_is_not_missing(self):
        guide = chapter("44-45 Emberwood", "step\n.turnin 2981 >> Turn in A Threat in Feralas")
        route = build_route((guide,), guide.id, faction="Horde")
        report = audit_route(route, at(guide, active=(2981,)))
        self.assertEqual(report.chapters[0].active_quests, (2981,))
        self.assertIn("A Threat in Feralas", report.render())
        self.assertFalse(report.findings)

    def test_optional_current_objective_is_informational(self):
        guide = chapter("43-44 Example", "step\n#optional\n.complete 12345,1")
        route = build_route((guide,), guide.id, faction="Horde")
        finding, = audit_route(route, at(guide)).findings
        self.assertEqual((finding.severity, finding.category), ("info", "optional_missing"))

    def test_future_route_pickup_suppresses_missing_warnings(self):
        early = chapter("43-44 Earlier", "step\n.collect 1337,3,42,1", "44-45 Later")
        later = chapter("44-45 Later", "step\n.accept 42\nstep\n.complete 42,1", guide_id=3000)
        route = build_route((early, later), early.id, faction="Horde")
        result = audit_route(route, at(early))
        self.assertFalse(result.findings)
        self.assertEqual(result.chapters[1].planned_quests, (42,))

    def test_inactive_guards_and_class_branches_never_raise_a_missing_warning(self):
        guide = chapter("43-44 Example", "step\n.isOnQuest 42\n.complete 42,1\nstep << Mage\n.complete 77,1")
        route = build_route((guide,), guide.id, faction="Horde", character_class="Hunter")
        self.assertFalse(audit_route(route, at(guide)).findings)

    def test_class_and_race_terms_are_kept_together_across_slash_branches(self):
        guide = chapter("43-44 Example", "step << (Orc Hunter/Troll Mage)\n.complete 42,1")
        excluded = build_route((guide,), guide.id, faction="Horde", character_class="Mage", race="Orc")
        included = build_route((guide,), guide.id, faction="Horde", character_class="Mage", race="Troll")
        self.assertFalse(audit_route(excluded, at(guide)).findings)
        report = audit_route(included, at(guide))
        self.assertEqual(report.findings[0].category, "unaccepted_objective")
        self.assertEqual(report.counts.missing_pickups, 0)
        self.assertEqual(report.counts.unaccepted_objectives, 1)

    def test_partial_snapshot_does_not_use_missing_completion_to_assert_absence(self):
        guide = chapter("43-44 Example", "step\n.complete 42,1")
        route = build_route((guide,), guide.id, faction="Horde")
        snapshot = replace(at(guide), active=None)
        finding, = audit_route(route, snapshot).findings
        self.assertEqual(finding.category, "quest_state_unknown")

    def test_prerequisites_flat_and_or_branches_have_distinct_semantics(self):
        guide = chapter("43-44 Example", "step\n.accept 42")
        route = build_route((guide,), guide.id, faction="Horde")
        db = {42: {
            "classification": "mandatory",
            "requirements": [{"questId": 11}],
            "requirementGroups": [[{"questId": 12}, {"questId": 13}], [{"questId": 14}]],
        }}
        self.assertFalse(audit_route(route, at(guide, completed=(11, 14)), db).findings)
        item, = audit_route(route, at(guide, completed=(11, 12)), db).findings
        self.assertEqual((item.severity, item.category), ("blocker", "prerequisites_missing"))

    def test_runtime_and_expanded_step_numbers_are_distinguished(self):
        guide = chapter("43-44 Example", "\n".join(["step\n.goto Map,10,10"] * 62))
        route = build_route((guide,), guide.id, faction="Horde")
        snapshot = replace(at(guide, 62), current_step=61)
        resolution = resolve_current_step(guide, snapshot)
        self.assertEqual((resolution.runtime_step, resolution.offline_step, resolution.exact), (61, 62, True))
        self.assertEqual(audit_route(route, snapshot, lookahead=0).chapters[0].first_step, 62)
        self.assertEqual(audit_route(route, snapshot, start_step=42, lookahead=0).chapters[0].first_step, 42)

    def test_vargas_after_saved_position_is_history_not_upcoming(self):
        early = chapter("40-42 Lantern Marsh I", "step\n.accept 95882", "43-44 Lantern Marsh II", 1000)
        travel = "\n".join(["step\n.goto Test Map,20,20"] * 41)
        finish = "\n".join(["step\n.goto Test Map,30,30"] * 18)
        later = chapter("43-44 Lantern Marsh II",
                        f"{travel}\nstep\n.complete 95882,1\nstep\n.turnin 95882\n{finish}\nstep\n.complete 95261,1",
                        guide_id=2000)
        route = build_route((early, later), current_id=later.id, faction="Horde")
        self.assertEqual(route.planned_guides, (later,))
        self.assertEqual(route.history_guides, (early,))
        at_objective = at(later, 42, active=(95261,), completed=(95260,))
        self.assertEqual(audit_route(route, at_objective).counts.missing_pickups, 1)
        advanced = replace(at(later, 62, active=(95261,), completed=(95260,)),
                           state_step_id=at_objective.current_step_id,
                           state_guide_name=later.name, state_guide_group=later.group,
                           exported_at=datetime.now(UTC) - timedelta(days=4))
        report = audit_route(route, advanced)
        self.assertEqual(report.counts.missing_pickups, 0)
        self.assertEqual(report.counts.historical, 1)
        self.assertEqual(report.historical_findings[0].quest_id, 95882)
        self.assertEqual(report.historical_findings[0].step, 42)
        self.assertFalse(any(item.quest_id == 95261 for item in report.findings))

    def test_stale_absence_and_structural_analysis_are_not_confirmed(self):
        guide = chapter("43-44 Example", "step\n.complete 42,1")
        route = build_route((guide,), guide.id, faction="Horde")
        old = replace(at(guide), exported_at=datetime.now(UTC) - timedelta(days=3))
        result = audit_route(route, old)
        self.assertEqual(result.counts.missing_pickups, 0)
        self.assertEqual(result.findings[0].category, "quest_state_stale")
        structural = audit_route(route, at(guide), structural_only=True)
        self.assertEqual(structural.counts.missing_pickups, 0)
        self.assertEqual(structural.counts.satisfied, 0)
        self.assertTrue(any("Structural-only scan" in detail for detail in structural.diagnostics))
