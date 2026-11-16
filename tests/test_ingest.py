from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zipfile import ZipFile
import zlib

from questguard.ingest import ImportError, load_guides, load_snapshots, parse_saved_variables, remember_association


def cached_record(text: str, name: str, group: str = "Test Route", guide_id: int = 3_000_000_000) -> str:
    encoder = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    payload = encoder.compress(text.encode()) + encoder.flush()
    escaped = "".join(f"\\{item:03d}" for item in payload)
    return (
        "{groupOrContent=\"" + escaped + "\","
        f'metadata={{group="{group}", name="{name}", guideId={guide_id}, version=1}}}}'
    )


class LuaImportTests(TestCase):
    def test_we_read_literal_byte_escapes_and_require_no_execution(self):
        result = parse_saved_variables(r"""
        -- comment
        RXPData = {
            ChatGPTQuestExport = { 95260, 95261 },
            label = [=[A label ]]=],
            binary = "\000\255\n\065",
            ["quoted\034key"] = true,
        }
        """)
        account = result["RXPData"]
        self.assertEqual(account["ChatGPTQuestExport"], {1: 95260, 2: 95261})
        self.assertEqual(account["label"], "A label ]")
        self.assertEqual(account["binary"], "\x00\xff\nA")
        self.assertTrue(account['quoted"key'])
        for text in ("x = os.execute('test')", "x = {n = 1 + 5}", "x = {n = foo}", "x = function() return true end"):
            with self.assertRaises(ImportError):
                parse_saved_variables(text)

    def test_snapshot_folders_keep_account_export_unattributed(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "WTF.zip"
            with ZipFile(path, "w") as archive:
                archive.writestr(
                    "WTF/Account/fixture/SavedVariables/RXPGuides.lua",
                    'RXPData={ChatGPTActive={95261},ChatGPTQuestExport={[95260]=true}}',
                )
                for player, realm in [("One", "TestRealm"), ("Two", "OtherRealm")]:
                    archive.writestr(
                        f"WTF/Account/fixture/{realm}/{player}/SavedVariables/RXPGuides.lua",
                        'RXPCData={currentGuideName="Test", currentStep=3, currentStepId=3000000010, stepSkip={[2]=true}}',
                    )
            account, first, second = load_snapshots(path)
            self.assertIsNone(account.character)
            self.assertEqual(account.active, {95261})
            self.assertEqual(account.completed, {95260})
            self.assertEqual([(first.character, first.realm), (second.character, second.realm)],
                             [("One", "TestRealm"), ("Two", "OtherRealm")])
            self.assertIsNone(first.active)
            self.assertEqual(first.current_step_id, 3_000_000_010)
            self.assertEqual(first.skipped_steps, {2})

    def test_local_cache_expands_labeled_range_and_keeps_stable_id(self):
        wrapper = cached_record(
            "#group Test Route\n#name 42-43 Wrapper\nstep\n#optional\n#include Included\\Fragment@Start-End",
            "42-43 Wrapper",
        )
        fragment = cached_record(
            "#group Included\n#name Fragment\nstep\n#label Start\n.accept 123\n"
            "step\n#label End\n.complete 123,1\nstep\n.turnin 123",
            "Fragment", group="Included",
        )
        with TemporaryDirectory() as folder:
            path = Path(folder) / "RXPGuides(2).lua"
            path.write_text(
                "RXPDB={profiles={test={guides={wrapper=" + wrapper + ", fragment=" + fragment
                + ', opaque={groupOrContent="not a compressed guide"}}}}}',
                encoding="utf-8",
            )
            collection = load_guides(path)
            self.assertTrue(any("could not be read" in item for item in collection.diagnostics))
            guide = next(item for item in collection.guides if item.name == "42-43 Wrapper")
            self.assertEqual([step.number for step in guide.steps], [1, 2])
            self.assertEqual([step.step_id for step in guide.steps], [3_000_000_003, 3_000_000_006])
            self.assertEqual([step.actions[0].kind for step in guide.steps], ["accept", "complete"])
            self.assertFalse(guide.steps[1].optional)

    def test_plain_guide_lookup_and_collect_syntax(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "RXPGuides.zip"
            with ZipFile(path, "w") as archive:
                archive.writestr("RXPGuides/RXPGuides.toc", "## Interface: 16001\n## Version: v4.test")
                archive.writestr("RXPGuides/Guides/example.lua", """local guide = [=[
                    #group Test Route
                    #name 44-45 Example
                    step
                    .collect 90000,4,1337,1
                    step
                    .goto 42,50.5,30
                    .accept 1337 >> Accept Example
                    ]=]
                """)
            collection = load_guides(path)
            self.assertEqual(collection.interfaces, (16001,))
            self.assertEqual(collection.version, "v4.test")
            collect, accept = [step.actions[0] for step in collection.guides[0].steps]
            self.assertEqual((collect.kind, collect.quest_id), ("precollect", 1337))
            self.assertEqual((accept.location.map_id, accept.location.x, accept.location.y), (42, 50.5, 30))

    def test_association_survives_new_progress_but_rejects_changed_unattributed_export(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "WTF"
            account = root / "Account" / "local"
            export = account / "SavedVariables" / "RXPGuides.lua"
            progress = account / "Example Realm" / "One" / "SavedVariables" / "RXPGuides.lua"
            alternate = root / "Account" / "other" / "Example Realm" / "One" / "SavedVariables" / "RXPGuides.lua"
            for path in (export, progress, alternate):
                path.parent.mkdir(parents=True)
            active = ",".join(str(i) for i in range(2000, 2030))
            completed = ",".join(str(i) for i in range(1000, 1518))
            export_text = f"RXPData={{ChatGPTActive={{{active}}},ChatGPTQuestExport={{{completed}}}}}"
            export.write_text(export_text, encoding="utf-8")
            progress_text = 'RXPCData={currentGuideName="Lantern Marsh II", currentStep=%d, currentStepId=%d}'
            progress.write_text(progress_text % (42, 98120042), encoding="utf-8")
            alternate.write_text(progress_text % (19, 2031292300), encoding="utf-8")
            imported = load_snapshots(root)
            legacy = next(item for item in imported if item.association_key)
            target = next(item for item in imported if item.current_step == 42)
            other = next(item for item in imported if item.current_step == 19)
            with self.assertRaises(ImportError):
                remember_association({}, legacy, other)
            consent = remember_association({}, legacy, target)
            progress.write_text(progress_text % (62, 98120064), encoding="utf-8")
            paired = load_snapshots(root, associations=consent)
            now = next(item for item in paired if item.identity_key == target.identity_key)
            self.assertEqual((len(now.completed), len(now.active)), (518, 30))
            self.assertEqual((now.current_step, now.current_step_id), (62, 98120064))
            self.assertEqual(now.freshness, "unknown")
            self.assertIsNone(next(item for item in paired if item.identity_key == other.identity_key).completed)
            export.with_suffix(".lua.bak").write_text(export_text, encoding="utf-8")
            export.write_text("RXPData={ChatGPTActive={95261},ChatGPTQuestExport={95260}}", encoding="utf-8")
            updated = load_snapshots(root, associations=consent)
            now = next(item for item in updated if item.identity_key == target.identity_key)
            self.assertIsNone(now.completed)
            new_export = next(item for item in updated if item.active == {95261})
            self.assertNotEqual(new_export.association_key, legacy.association_key)

    def test_quest_guard_capture_auto_associates_updates_and_preserves_distinct_source_times(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "WTF"
            saved = root / "Account" / "local" / "Example" / "First-Last" / "SavedVariables"
            saved.mkdir(parents=True)
            snapshot = saved / "RXPQuestGuard.lua"
            progress = saved / "RXPGuides.lua"
            current = int(datetime.now(UTC).timestamp())
            payload = (
                'RXPQuestGuardSnapshot={schemaVersion=1,character="First Last",realm="Example",class="HUNTER",'
                'faction="Horde",currentGuide="History",currentGuideGroup="Test",currentGuideSubgroup="Leveling",'
                'currentGuideKey="Test|Leveling|History",currentStep=42,currentStepId=-98120042,'
                'skippedSteps={7,8},ready={95261},clientInterface=16001,addonVersion="0.2.0",'
                'active={95261},completed={%s},capturedAt=%d}'
            )
            snapshot.write_text(payload % ("", current - int(timedelta(days=2).total_seconds())), encoding="utf-8")
            progress.write_text('RXPCData={currentGuideName="History",currentGuideGroup="Test",currentStep=62,currentStepId=98120064}',
                                encoding="utf-8")
            old, = load_snapshots(root)
            self.assertEqual(old.freshness, "stale")
            self.assertEqual((old.state_step_id, old.current_step_id), (-98120042, 98120064))
            snapshot.write_text(payload.replace("currentStep=42,currentStepId=-98120042", "currentStep=62,currentStepId=98120064")
                                % ("95260", current), encoding="utf-8")
            new, = load_snapshots(root)
            self.assertEqual((new.freshness, new.state_source, new.addon_version), ("fresh", "Quest Guard", "0.2.0"))
            self.assertEqual((new.current_step, new.active, new.completed), (62, {95261}, {95260}))
            self.assertEqual((new.skipped_steps, new.ready), ({7, 8}, {95261}))
            self.assertEqual(new.current_guide_key, "Test|Leveling|History")

    def test_conflicting_capture_identity_is_not_fused_or_persistable(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            saved = root / "Account" / "test" / "realm" / "Alice" / "SavedVariables"
            saved.mkdir(parents=True)
            (saved / "RXPGuides.lua").write_text('RXPCData={currentGuideName="Guide",currentStep=1}', encoding="utf-8")
            (saved / "RXPQuestGuard.lua").write_text(
                'RXPQuestGuardSnapshot={schemaVersion=1,character="Unrelated",realm="realm",active={95261},completed={}}',
                encoding="utf-8",
            )
            names = load_snapshots(root)
            conflicted = next(item for item in names if item.ownership_conflict)
            owner = next(item for item in names if item.identity_key)
            self.assertIsNone(owner.active)
            self.assertIsNone(conflicted.association_key)
            with self.assertRaises(ImportError):
                remember_association({}, conflicted, owner)

    def test_named_account_export_is_fused_only_to_same_account_realm_and_character(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            account = root / "Account" / "local"
            export = account / "SavedVariables" / "RXPGuides.lua"
            export.parent.mkdir(parents=True)
            current = int(datetime.now(UTC).timestamp())
            export.write_text(
                f'RXPData={{exportMetadata={{character="One",realm="Target",exportedAt={current}}},'
                'ChatGPTActive={95261},ChatGPTQuestExport={95260}}',
                encoding="utf-8",
            )
            for origin, realm in ((account, "Target"), (account, "Other"), (root / "Account" / "different", "Target")):
                path = origin / realm / "One" / "SavedVariables" / "RXPGuides.lua"
                path.parent.mkdir(parents=True)
                path.write_text('RXPCData={currentGuideName="Guide",currentStep=1}', encoding="utf-8")
            rows = load_snapshots(root)
            matched = [row for row in rows if row.active is not None]
            exact, = matched
            self.assertEqual((exact.realm, exact.active, exact.completed), ("Target", {95261}, {95260}))
            self.assertEqual(exact.freshness, "fresh")
            self.assertEqual(len(rows), 3)
