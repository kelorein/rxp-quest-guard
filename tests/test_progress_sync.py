from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
from unittest import TestCase

from questguard.ingest import load_snapshots
from questguard.installation import _lua, addon_source


class ProgressSyncTests(TestCase):
    def test_only_a_flushed_addon_snapshot_changes_the_desktop_view(self):
        try:
            from lupa.lua51 import LuaRuntime, lua_type
        except ImportError:
            self.skipTest("Install requirements-test.txt to simulate the WoW save boundary")
        runtime = LuaRuntime()
        private = runtime.table()
        for module in ("Engine.lua", "QuestLog.lua", "RestedXP.lua", "Snapshot.lua"):
            runtime.execute((addon_source() / module).read_text(encoding="utf-8"), "RXPQuestGuard", private)

        now = datetime.now(UTC)
        runtime.execute("""
            local qg, start = ...
            qg.settings = {authorizedGuides = true}
            savedTime = start
            shownStep, stableStep = 42, 98120042
            function GetServerTime() return savedTime end
            function GetBuildInfo() return '1.60.1', '70938', '', 16001 end
            function GetAddOnMetadata(_, key) if key == 'Version' then return '0.2.0' end end
            function UnitName() return 'Sample', 'Hunter' end
            function UnitLevel() return 43 end
            function UnitFactionGroup() return 'Horde' end
            function UnitClass() return 'Hunter', 'HUNTER' end
            function UnitRace() return 'Orc', 'Orc' end
            function GetRealmName() return 'Example Realm' end
            function GetRealmID() return 91 end
            function InCombatLockdown() return false end
            qg.restedXP.context = function()
                return {guide={subgroup='Sample Route 40-50', key='Fixture-Key'},
                        name='43-44 Lantern Marsh II', group='Fixture', index=shownStep,
                        stepId=stableStep, skipped={[2]=true}, version='v4.11.36'}
            end
            function changeLog(activeCount, completedCount)
                local active, completed = {95261}, {95260}
                for i=2,activeCount do active[i] = 10000 + i end
                for i=2,completedCount do completed[i] = 20000 + i end
                C_QuestLog = {
                    GetNumQuestLogEntries = function() return #active, #active end,
                    GetInfo = function(index) return {questID=active[index]} end,
                    GetAllCompletedQuestIDs = function() return completed end,
                    ReadyForTurnIn = function() return false end,
                }
            end
            changeLog(30, 381)
            qg.snapshot.event('PLAYER_ENTERING_WORLD')
        """, private, int((now - timedelta(hours=30)).timestamp()))

        def literal(table):
            if lua_type(table) == "table":
                return {key: literal(value) for key, value in table.items()}
            return table

        def save(file):
            private.snapshot.event("PLAYER_LOGOUT")
            snapshot = literal(runtime.globals().RXPQuestGuardSnapshot)
            file.write_text("RXPQuestGuardSnapshot = " + _lua(snapshot), encoding="ascii")

        with tempfile.TemporaryDirectory() as temp:
            wtf = Path(temp) / "WTF"
            saved = wtf / "Account" / "SYNTHETIC" / "91" / "Sample-Hunter" / "SavedVariables"
            saved.mkdir(parents=True)
            file = saved / "RXPQuestGuard.lua"
            save(file)
            before, = load_snapshots(wtf)
            self.assertEqual((before.current_step, before.current_step_id), (42, 98120042))
            self.assertEqual((len(before.completed), len(before.active)), (381, 30))
            self.assertIn(95261, before.active)
            self.assertIn(95260, before.completed)
            self.assertEqual(before.freshness, "stale")

            runtime.execute("""
                local qg, updated = ...
                savedTime, shownStep, stableStep = updated, 62, 98120064
                changeLog(23, 402)
                qg.snapshot.event('PLAYER_ENTERING_WORLD')
                qg.snapshot.event('QUEST_LOG_UPDATE')
            """, private, int(now.timestamp()))
            self.assertEqual(runtime.globals().RXPQuestGuardSnapshot.currentStep, 62)
            still_saved, = load_snapshots(wtf)
            self.assertEqual((still_saved.current_step, still_saved.exported_at), (42, before.exported_at))

            file.with_suffix(".lua.bak").write_bytes(file.read_bytes())
            save(file)
            after, = load_snapshots(wtf)
            self.assertEqual(after.identity_key, before.identity_key)
            self.assertEqual(after.state_source, "Quest Guard")
            self.assertEqual((after.current_step, after.current_step_id), (62, 98120064))
            self.assertEqual((len(after.completed), len(after.active)), (402, 23))
            self.assertEqual(after.freshness, "fresh")
            self.assertGreater(after.exported_at, before.exported_at)
