from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

try:
    import tkinter as tk
except ImportError as error:
    raise unittest.SkipTest("Native Tk is unavailable") from error

from questguard.gui import Dashboard, load_associations, load_monitor_preference, save_association, selected_ids, source_age
from questguard.evidence import FOREVER_SPECIFIC
from questguard.ingest import Guide, GuideCollection, QuestAction, Snapshot, Step, Transition, load_snapshots
from questguard.installation import Installation
from questguard.verification import CacheRecord, VerificationCache, VerificationRun
from questguard.verification_view import VerificationPanel, trusted_reference


HISTORY = Guide("first", "40-42 Earlier", "Horde", (Step(1, (QuestAction("accept", 95882),)),),
                "synthetic", next=(Transition("43-44 Later"),), level_min=40, level_max=42)
CURRENT = Guide("last", "43-44 Later", "Horde", tuple(
    Step(number, (QuestAction("complete", 95882),) if number == 43 else (), step_id=10_000 + number)
    for number in range(1, 70)
), "synthetic", next=(Transition("44-45 Next"),), level_min=43, level_max=44)
NEXT = Guide("next", "44-45 Next", "Horde", (Step(1, ()),), "synthetic", level_min=44, level_max=45)
GUIDES = GuideCollection((HISTORY, CURRENT, NEXT))


def lua_ids(values):
    return "{" + ",".join(map(str, values)) + "}"


def write_capture(path, *, runtime_step, expanded_step, active, completed, captured_at, character="Guard"):
    path.write_text(
        f'RXPQuestGuardSnapshot = {{schemaVersion=1, character="{character}", realm="Test Realm", '
        'faction="Horde", class="HUNTER", currentGuide="43-44 Later", currentGuideGroup="Horde", '
        f'currentStep={runtime_step}, currentStepId={10_000 + expanded_step}, '
        f'active={lua_ids(active)}, completed={lua_ids(completed)}, capturedAt={int(captured_at.timestamp())}}}',
        encoding="utf-8",
    )


class GUIHelpersTests(unittest.TestCase):
    def test_provider_id_field(self):
        self.assertEqual(selected_ids("95882, 95261\n95882"), [95882, 95261])
        for raw in ("", "0", "95882x", "9999999999"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                selected_ids(raw)

    def test_freshness(self):
        self.assertEqual(source_age(None, 30), "Unknown")
        self.assertEqual(source_age(1, 30), "1 day old")
        self.assertIn("Stale", source_age(30, 30))

    def test_source_details_explain_conflicts_without_untrusted_links(self):
        self.assertTrue(trusted_reference("https://www.wowhead.com/forever/quest=654"))
        self.assertFalse(trusted_reference("https://foreverdb.net.example.com/quests/654"))
        details, links = VerificationPanel.describe_evidence({
            "fieldEvidence": {"timerSeconds": [
                {"value": 7200, "provider": "ForeverDB", "confidence": "Forever-specific evidence",
                 "sourceUrl": "https://foreverdb.net/quest/654", "method": "Fixture", "gameVersion": "16001"},
                {"value": 3600, "provider": "Classic fixture", "confidence": "Inherited Classic information",
                 "sourceUrl": "https://foreverdb.net.example.com/quest/654", "inheritedClassic": True},
            ]}, "conflicts": [{"field": "timerSeconds", "unresolved": True}],
        })
        combined = "\n".join(details)
        self.assertIn("2 hr", combined)
        self.assertIn("1 hr", combined)
        self.assertIn("Conflicting sources", combined)
        self.assertIn("Build 16001", combined)
        self.assertEqual(links, (("ForeverDB", "https://foreverdb.net/quest/654"),))

    def test_association_remembered_in_local_settings(self):
        with TemporaryDirectory() as name:
            root = Path(name)
            account = root / "WTF" / "Account" / "fixture"
            account_file = account / "SavedVariables" / "RXPGuides.lua"
            char_file = account / "Test Realm" / "Guard" / "SavedVariables" / "RXPGuides.lua"
            for path in (account_file, char_file):
                path.parent.mkdir(parents=True)
            account_file.write_text('RXPData = {ChatGPTActive={95261}, ChatGPTQuestExport={95260}}', encoding="utf-8")
            char_file.write_text('RXPCData = {currentGuideName="43-44 Later", currentStep=42}', encoding="utf-8")
            source = root / "WTF"
            exported, character = load_snapshots(source)
            self.assertIsNone(character.active)
            with patch("questguard.gui.cache_directory", return_value=root / "cache"):
                save_association(exported, character)
                combined, = load_snapshots(source, associations=load_associations())
            self.assertEqual(combined.active, frozenset({95261}))
            self.assertEqual(combined.current_step, 42)
            stored = (root / "cache" / "character-associations.json").read_text(encoding="utf-8")
            self.assertNotIn("Guard", stored)
            self.assertNotIn("95261", stored)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        preference = patch("questguard.gui.load_monitor_preference", return_value=False)
        self.addCleanup(preference.stop)
        preference.start()
        cache = patch("questguard.gui.load_verification_cache", return_value=VerificationCache())
        self.addCleanup(cache.stop)
        cache.start()

    def open_dashboard(self):
        try:
            window = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Desktop display unavailable: {error}")
        window.withdraw()
        app = Dashboard(window)
        self.addCleanup(app.close)
        self.run_job(app)
        return app

    def run_job(self, app):
        self.wait_for(app, lambda: app.future is None)

    def wait_for(self, app, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            app.window.update()
            time.sleep(0.02)
        self.assertTrue(condition())

    @patch("questguard.gui.discover_installations", return_value=[])
    @patch("questguard.gui.load_cache", return_value=None)
    @patch("questguard.gui.load_associations", return_value={})
    @patch("questguard.gui.messagebox.showerror")
    def test_dashboard_scans_and_displays_snapshot_and_chapter(self, showerror, _associations, _cache, _installs):
        app = self.open_dashboard()
        app.set_guides(GUIDES, Path("guides.zip"))
        snapshot = Snapshot("Guard (Test Realm)", character="Guard", realm="Test Realm", active=frozenset(),
                            completed=frozenset(), current_guide=CURRENT.name, current_group=CURRENT.group, current_step=1,
                            exported_at=datetime.now(UTC) - timedelta(days=2))
        app.set_snapshots((snapshot,), Path("RXPQuestGuard.lua"))
        self.assertEqual(app.metrics["Chapter"].get(), CURRENT.name)
        self.assertEqual(app.metrics["Active quests"].get(), "0")
        self.assertIn("Stale", app.progress_notice.get())
        self.assertIn("Stale", app.diagnostic_view.get("1.0", "end"))
        app.scan()
        self.run_job(app)
        self.assertIsNotNone(app.audit)
        self.assertEqual(app.visible_chapters, [CURRENT, NEXT])
        self.assertIn(HISTORY, app.route.history_guides)
        self.assertIn("Confirmed blockers", app.counts_var.get())
        showerror.assert_not_called()

    @patch("questguard.gui.load_cache", return_value=None)
    @patch("questguard.gui.load_associations", return_value={})
    @patch("questguard.gui.load_guides", return_value=GUIDES)
    @patch("questguard.gui.messagebox.showerror")
    def test_refresh_loads_the_saved_step_and_preserves_manual_override(self, showerror, _guides, _associations, _cache):
        with TemporaryDirectory() as name:
            client = Path(name) / "_forever_"
            save = client / "WTF" / "Account" / "fixture" / "Test Realm" / "Guard" / "SavedVariables" / "RXPQuestGuard.lua"
            save.parent.mkdir(parents=True)
            install = Installation(client / "Interface" / "AddOns", 16001, "v4.11.36", "Fixture")
            when = datetime.now(UTC) - timedelta(minutes=1)
            write_capture(save, runtime_step=42, expanded_step=43, active=[95261, *range(100, 129)],
                          completed=range(1000, 1381), captured_at=when)
            with patch("questguard.gui.discover_installations", return_value=[install]):
                app = self.open_dashboard()
            self.assertFalse(app.authorized_guides.get())
            app.refresh_progress()
            self.run_job(app)
            _guides.assert_not_called()
            self.assertIsNone(app.collection)
            self.assertEqual(app.snapshot().active, frozenset({95261, *range(100, 129)}))
            app.authorized_guides.set(True)
            app.refresh_progress()
            self.run_job(app)
            self.assertEqual(app.metrics["Active quests"].get(), "30")
            self.assertEqual(app.metrics["Completed quests"].get(), "381")
            self.assertIn("Saved step 42", app.character_details.get())
            self.assertEqual(app.step_var.get(), "43")
            app.refresh_progress()
            self.run_job(app)
            self.assertIn("No newer save found", app.status_var.get())
            self.assertEqual(app.step_var.get(), "43")
            app.scan()
            self.run_job(app)
            self.assertTrue(any(item.quest_id == 95882 and item.category == "missing_pickup" for item in app.audit.findings))

            write_capture(save, runtime_step=62, expanded_step=64, active=[95261, 3], completed=[95260],
                          captured_at=when + timedelta(seconds=10))
            app.refresh_progress()
            self.run_job(app)
            self.assertIn("Saved step 62", app.character_details.get())
            self.assertEqual(app.step_var.get(), "64")
            self.assertEqual(app.metrics["Active quests"].get(), "2")
            self.assertEqual(app.metrics["Completed quests"].get(), "1")
            self.assertEqual(app.audit.chapters[0].first_step, 64)
            self.assertFalse(any(item.quest_id == 95882 for item in app.audit.findings))

            app.step_override.set(True)
            app.toggle_step_override()
            app.step_var.set("63")
            app.refresh_progress()
            self.run_job(app)
            self.assertEqual(app.step_var.get(), "63")
            self.assertIn("Manual", app.step_display.get())
            self.assertIn("Saved step 62", app.character_details.get())
            showerror.assert_not_called()

    @patch("questguard.gui.discover_installations", return_value=[])
    @patch("questguard.gui.load_cache", return_value=None)
    @patch("questguard.gui.load_associations", return_value={})
    @patch("questguard.gui.messagebox.showwarning")
    @patch("questguard.gui.messagebox.showerror")
    def test_progress_without_quest_state_offers_a_deliberate_structural_scan(self, showerror, warning, *_mocks):
        app = self.open_dashboard()
        app.set_guides(GUIDES, Path("guides.zip"))
        snapshot = Snapshot("Guard (Test Realm)", character="Guard", realm="Test Realm", current_guide=CURRENT.name,
                            current_group=CURRENT.group, current_step=42, current_step_id=10_043)
        app.set_snapshots((snapshot,), Path("progress.lua"))
        app.scan()
        warning.assert_called_once()
        self.assertIsNone(app.future)
        self.assertIsNone(app.audit)
        app.scan(structural_only=True)
        self.run_job(app)
        self.assertIsNotNone(app.audit)
        self.assertTrue(app.audit_structural)
        self.assertEqual(app.audit.counts.missing_pickups, 0)
        showerror.assert_not_called()

    @patch("questguard.gui.MONITOR_INTERVAL_MS", 10)
    @patch("questguard.gui.load_cache", return_value=None)
    @patch("questguard.gui.load_associations", return_value={})
    @patch("questguard.gui.load_guides", return_value=GUIDES)
    @patch("questguard.gui.messagebox.showerror")
    def test_watch_bootstraps_auto_preflight_and_preserves_the_selected_character(self, errors, guide_load, *_mocks):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = root / "_forever_"
            realm = client / "WTF" / "Account" / "fixture" / "Test Realm"
            save = realm / "Guard" / "SavedVariables" / "RXPQuestGuard.lua"
            foreign = realm / "Other" / "SavedVariables" / "RXPQuestGuard.lua"
            for path in (save, foreign):
                path.parent.mkdir(parents=True)
            install = Installation(client / "Interface" / "AddOns", 16001, "v4.11.36", "Fixture")
            when = datetime.now(UTC) - timedelta(minutes=1)
            with patch("questguard.gui.discover_installations", return_value=[install]):
                app = self.open_dashboard()
            self.assertIsNone(app.monitor)
            self.assertIsNone(app.audit)
            app.authorized_guides.set(True)
            with patch("questguard.gui.cache_directory", return_value=root / "profile"):
                app.monitor_enabled.set(True)
                app.toggle_monitor()
                self.assertTrue(load_monitor_preference())
                write_capture(save, runtime_step=42, expanded_step=43, active=[95261], completed=[95260], captured_at=when)
                self.wait_for(app, lambda: app.audit is not None)
                first_scan = app.last_scan_at
                self.assertIn("Automatic preflight", app.status_var.get())
                self.assertIn("Snapshot used", app.last_scan_var.get())
                self.assertEqual(app.audit.chapters[0].first_step, 43)
                self.assertEqual(app.metrics["Active quests"].get(), "1")
                self.assertEqual(app.last_scan_snapshot_at, app.snapshot().exported_at)
                chosen_identity = app.snapshot().identity_key

                write_capture(foreign, runtime_step=55, expanded_step=55, active=[], completed=[],
                              captured_at=when + timedelta(seconds=20), character="Other")
                self.wait_for(app, lambda: any(row.character == "Other" for row in app.snapshots))
                self.assertEqual(app.snapshot().identity_key, chosen_identity)
                self.assertEqual(app.last_scan_at, first_scan)

                old_inode_modified = save.stat().st_mtime_ns
                os.utime(save, ns=(old_inode_modified, old_inode_modified + 1_000_000_000))
                self.wait_for(app, lambda: app.monitor.handled[save].modified_ns != old_inode_modified)
                self.assertEqual(app.last_scan_at, first_scan)

                save.write_text("RXPQuestGuardSnapshot = {", encoding="utf-8")
                self.wait_for(app, lambda: "incomplete" in app.monitor_status.get())
                self.assertEqual(app.snapshot().current_step, 42)
                self.assertEqual(app.last_scan_at, first_scan)

                app.step_override.set(True)
                app.toggle_step_override()
                app.step_var.set("63")
                write_capture(save, runtime_step=62, expanded_step=64, active=[95261, 11], completed=[95260],
                              captured_at=when + timedelta(seconds=25))
                self.wait_for(app, lambda: app.audit is not None and app.last_scan_at > first_scan)
                self.assertEqual(app.snapshot().current_step, 62)
                self.assertEqual(app.step_var.get(), "63")
                self.assertEqual(app.audit.chapters[0].first_step, 63)
                self.assertEqual(app.metrics["Active quests"].get(), "2")
                self.assertIn("manual override", app.status_var.get())

                app.chapter_box.current(1)
                app.scope_var.set("Selected chapter")
                app.change_scan_chapter()
                app.scan(structural_only=True)
                self.run_job(app)
                structural_scan = app.last_scan_at
                write_capture(save, runtime_step=63, expanded_step=65, active=[95261, 11], completed=[95260],
                              captured_at=when + timedelta(seconds=30))
                self.wait_for(app, lambda: app.audit is not None and app.last_scan_at > structural_scan)
                self.assertTrue(app.audit_structural)
                self.assertEqual(app.visible_chapters, [NEXT])
                self.assertIsNone(app.last_scan_snapshot_at)
                guide_load.assert_called_once_with(client)
                app.authorized_guides.set(False)
                app.change_guide_permission()
                self.assertIsNone(app.collection)
                self.assertIsNone(app.route)
                self.assertIsNone(app.audit)
                app.monitor_enabled.set(False)
                app.toggle_monitor()
                self.assertIsNone(app.monitor)
                self.assertFalse(load_monitor_preference())
            errors.assert_not_called()

    @patch.dict(os.environ, {"QG_FOREVERDB_API_KEY": "approved-fixture-api-key"})
    @patch("questguard.gui.discover_installations", return_value=[])
    @patch("questguard.gui.load_cache", return_value=None)
    @patch("questguard.gui.load_associations", return_value={})
    @patch("questguard.verification_view.verify_quests")
    def test_verification_requires_consent_and_reaudits_in_the_background(self, verify, *_mocks):
        app = self.open_dashboard()
        app.set_guides(GUIDES, Path("guides.zip"))
        now = datetime.now(UTC).replace(microsecond=0)
        snapshot = Snapshot("Guard (Test Realm)", character="Guard", realm="Test Realm",
                            current_guide=CURRENT.name, current_group=CURRENT.group, current_step=42,
                            current_step_id=10_043, active=frozenset({95261}), completed=frozenset(), exported_at=now)
        app.set_snapshots((snapshot,), Path("progress.lua"))
        app.scan()
        self.run_job(app)
        first = app.audit
        self.assertTrue(any(finding.category == "missing_pickup" for finding in first.findings))
        panel = app.verification
        self.assertFalse(panel.intelligent.get())
        self.assertFalse(panel.auto_publish.get())
        self.assertEqual(panel.chapter_count.get(), "2")
        panel.query.set("95882")
        panel.request("selected")
        verify.assert_not_called()
        self.assertIn("Authorize ForeverDB", panel.status.get())

        ready, finish = Event(), Event()
        self.addCleanup(finish.set)
        claim = {"provider": "ForeverDB", "confidence": FOREVER_SPECIFIC,
                 "method": "Fixture", "retrievedAt": now.isoformat(), "sourceUrl": "https://foreverdb.net/quest/95882"}
        record = CacheRecord({
            "name": "Scoutmaster Vargas", "source": "ForeverDB", "fieldEvidence": {
                "name": [{**claim, "value": "Scoutmaster Vargas"}],
                "classification": [{**claim, "value": "optional"}],
            },
        }, "Unknown", now.isoformat())

        def worker(candidates, _providers, *, progress, cancelled, **_versions):
            self.assertIn(95882, {item.quest_id for item in candidates})
            progress("Checking 95882")
            ready.set()
            finish.wait(4)
            self.assertFalse(cancelled())
            return VerificationRun(VerificationCache({"foreverdb": {95882: record}}), 1, {}, False, "1 quest refreshed.")

        verify.side_effect = worker
        panel.authorize("")
        self.wait_for(app, ready.is_set)
        self.assertTrue(panel.intelligent.get())
        self.assertIsNone(app.future)
        self.assertIsNotNone(panel.future)
        self.assertIn(95882, panel.pending)
        finish.set()
        self.wait_for(app, lambda: panel.future is None and app.audit is not first and app.audit is not None)
        self.assertIsNone(app.base_database)
        self.assertEqual(app.database.quests[95882]["name"], "Scoutmaster Vargas")
        self.assertIn("Removed", panel.differences)
        self.assertEqual(panel.rows[95882].state, "Optional quest")
        self.assertEqual(panel.selected_id(), 95882)
        self.assertNotIn("approved-fixture-api-key", panel.status.get())
        verify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
