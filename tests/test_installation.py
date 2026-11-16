from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from questguard import __version__
from questguard.ingest import Guide, GuideLocation, QuestAction, Snapshot, Step
from questguard.installation import (_read_published_database, generate_addon_data, inspect_installation,
                                     install_addon, publish_route, write_addon_data)
from questguard.sync import Database


class InstallationTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.path = Path(work.name) / "World of Warcraft"
        self.addons = self.path / "_forever_" / "Interface" / "AddOns"
        self.rxp = self.addons / "RXPGuides" / "RXPGuides.toc"
        self.rxp.parent.mkdir(parents=True)
        self.original = b"## Version: v4.11.36\n## Interface: 11509, 16001\n"
        self.rxp.write_bytes(self.original)

    def test_local_build_has_priority_and_install_never_writes_restedxp(self):
        (self.path / ".build.info").write_text(
            "Active!DEC:1|Version!STRING:0|Product!STRING:0\n1|1.60.2.999|wowf\n1|9.99.1.1|wow\n",
            encoding="utf-8",
        )
        installation = inspect_installation(self.path)
        self.assertEqual(installation.interface, 16002)
        self.assertEqual(installation.restedxp_version, "v4.11.36")
        target = install_addon(installation)
        self.assertIn(b"## Interface: 16002", (target / "RXPQuestGuard.toc").read_bytes())
        data = Database({95882: {"name": "Example", "requirements": []}}, "Test", "1", "Unknown")
        installed_data = write_addon_data(installation, data)
        self.assertEqual(installed_data.read_bytes(), generate_addon_data(data))
        self.assertEqual(self.rxp.read_bytes(), self.original)
        self.assertEqual(install_addon(installation), target)
        self.assertEqual(installed_data.read_bytes(), generate_addon_data(data))

    def test_install_failure_does_not_replace_existing_addon(self):
        installation = inspect_installation(self.addons)
        target = install_addon(installation)
        old = (target / "Data.lua").read_bytes()
        with patch("questguard.installation.shutil.copyfile", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                install_addon(installation, interface=16002)
        self.assertEqual((target / "Data.lua").read_bytes(), old)
        self.assertEqual(self.rxp.read_bytes(), self.original)

    def test_upgrade_and_route_publish_keep_legacy_quest_data_and_saved_variables(self):
        installation = inspect_installation(self.addons)
        target = self.addons / "RXPQuestGuard"
        target.mkdir()
        manifest = target / "RXPQuestGuard.toc"
        manifest.write_text("## Interface: 16001\n## Title: RXP Quest Guard\n## Version: 0.1.0\n", encoding="utf-8")
        database = Database({95882: {"name": "Wanted – Vargas", "source": "Local test", "confidence": "verified",
                                     "classification": "unknown", "classes": ["HUNTER"],
                                     "requirementGroups": [[{"questId": 95260, "state": "completed"}]]}},
                            "Local test", "test", "Unknown")
        old_data = generate_addon_data(database)
        output = target / "Data.lua"
        output.write_bytes(old_data)
        settings = self.addons.parent.parent / "WTF" / "SavedVariables" / "RXPQuestGuard.lua"
        settings.parent.mkdir(parents=True)
        settings.write_bytes(b"RXPQuestGuardSettings={ scope='30' }\n")

        install_addon(installation)
        self.assertIn(f"## Version: {__version__}", manifest.read_text(encoding="utf-8"))
        self.assertEqual(output.read_bytes(), old_data)
        self.assertEqual(settings.read_bytes(), b"RXPQuestGuardSettings={ scope='30' }\n")
        self.assertEqual(self.rxp.read_bytes(), self.original)

        guide = Guide("route|chapter", "Chapter", "Route", (Step(1, ()),), "Fixture", faction="Horde")
        snapshot = Snapshot("Fixture", character="Test Hunter", realm="Test Realm", faction="Horde",
                            character_class="Hunter", active=frozenset({90001}), client_interface=16001)
        self.assertEqual(publish_route(installation, None, (guide,), snapshot=snapshot, start_chapter_id=guide.id), output)
        content = output.read_bytes()
        self.assertIn(b'["routeContext"]', content)
        self.assertIn(b'["character"]="Test Hunter"', content)
        self.assertIn(b'["realm"]="Test Realm"', content)
        self.assertIn(b'["startChapterId"]="route|chapter"', content)
        self.assertIn(b'["requirementGroups"]=', content)
        self.assertIn(b'Wanted \\226\\128\\147 Vargas', content)
        self.assertNotIn(b'90001', content)
        with patch("questguard.sync.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                publish_route(installation, None, (guide,), snapshot=snapshot, start_chapter_id=guide.id)
        self.assertEqual(output.read_bytes(), content)

        # The maintenance upgrade preserves an existing character-bound route and its saved snapshot.
        manifest.write_bytes(manifest.read_bytes().replace(f"## Version: {__version__}".encode(), b"## Version: 0.2.0"))
        v020_data = content.replace(f'["publisherVersion"]="{__version__}"'.encode(), b'["publisherVersion"]="0.2.0"')
        output.write_bytes(v020_data)
        saved = settings.read_bytes() + b"RXPQuestGuardSnapshot={ schemaVersion=1, active={95261} }\n"
        settings.write_bytes(saved)
        install_addon(installation)
        self.assertIn(f"## Version: {__version__}", manifest.read_text(encoding="utf-8"))
        self.assertEqual(output.read_bytes(), v020_data)
        self.assertEqual(settings.read_bytes(), saved)

    def test_publish_refuses_unidentified_or_mismatched_context(self):
        installation = inspect_installation(self.addons)
        target = install_addon(installation)
        initial = (target / "Data.lua").read_bytes()
        guide = Guide("route|chapter", "Chapter", "Route", (Step(1, ()),), "Fixture", faction="Horde")
        identified = Snapshot("Fixture", character="Hunter Player", realm="A Realm", faction="Alliance")
        for snapshot, chapter_id, interface in (
            (Snapshot("Unknown"), guide.id, 16001),
            (identified, guide.id, 16001),
            (identified, guide.id, 16002),
            (identified, "different route", 16001),
        ):
            with self.subTest(snapshot=snapshot, chapter_id=chapter_id, interface=interface):
                with self.assertRaises(ValueError):
                    publish_route(installation, None, (guide,), snapshot=snapshot, start_chapter_id=chapter_id,
                                  interface=interface)
        self.assertEqual((target / "Data.lua").read_bytes(), initial)

    def test_route_publish_preserves_prior_evidence_and_adds_new_quests(self):
        installation = inspect_installation(self.addons)
        install_addon(installation)
        claim = {"value": [{"questId": 95260, "state": "completed"}], "provider": "ForeverDB.net",
                 "confidence": "forever-specific", "sourceUrl": "https://foreverdb.net/quest/95261",
                 "method": "Fixture", "observedForever": True}
        other = {**claim, "provider": "Local fixture", "value": [{"questId": 95882, "state": "completed"}]}
        evidence = {"requirements": [claim, other]}
        previous = Database({95261: {"name": "It Takes Guts", "fieldEvidence": evidence,
                                    "conflicts": [{"field": "requirements", "claims": [claim, other],
                                                   "unresolved": True}]}}, "Local fixtures", "1", "Unknown")
        output = write_addon_data(installation, previous)
        chapter = Guide("start", "Start", "Route", (Step(1, ()),), "Fixture")
        snapshot = Snapshot("Fixture", character="Hunter", realm="Realm")
        new = Database({654: {"name": "Tanaris Field Sampling", "startLocation": {"type": "item", "id": 8524}}},
                       "New fixture", "2", "Unknown")
        publish_route(installation, new, (chapter,), snapshot=snapshot, start_chapter_id=chapter.id)
        publish_route(installation, None, (chapter,), snapshot=snapshot, start_chapter_id=chapter.id)
        rows = _read_published_database(output).quests
        self.assertEqual(rows[654]["startLocation"], {"type": "item", "id": 8524})
        self.assertEqual({entry["provider"] for entry in rows[95261]["fieldEvidence"]["requirements"]},
                         {"ForeverDB.net", "Local fixture"})
        self.assertEqual(len(rows[95261]["conflicts"][0]["claims"]), 2)
        self.assertNotIn("requirements", rows[95261])

    def test_publish_records_level_60_coverage_and_keeps_pickup_history(self):
        from questguard.ingest import Transition

        (self.path / ".build.info").write_text(
            "Active!DEC:1|Version!STRING:0|Product!STRING:0\n1|1.60.1.70938|wowf\n", encoding="utf-8",
        )
        installation = inspect_installation(self.addons)
        install_addon(installation)
        chapters = (
            Guide("history", "40-42 Earlier", "Route", (Step(1, (QuestAction("accept", 95882),)),), "Fixture",
                  next=(Transition("43-44 Start"),), faction="Horde", level_max=42),
            Guide("start", "43-44 Start", "Route", (Step(1, (QuestAction("complete", 95882),)),), "Fixture",
                  next=(Transition("59-60 Finish"),), faction="Horde", level_min=43, level_max=44, version=7),
            Guide("finish", "59-60 Finish", "Route", (Step(1, ()),), "Fixture",
                  next=(Transition("60-60 Extra"),), faction="Horde", level_min=59, level_max=60),
            Guide("extra", "60-60 Extra", "Route", (Step(1, ()),), "Fixture", faction="Horde", level_max=60),
        )
        character = Snapshot("Fixture", character="Sample Hunter", realm="Sample Realm", faction="Horde")
        output = publish_route(installation, None, chapters, snapshot=character, start_chapter_id="start",
                               guide_release="v4.11.36")
        content = output.read_bytes()
        self.assertIn(b'["startChapterId"]="start"', content)
        self.assertIn(b'["id"]="history"', content)
        self.assertIn(b'["terminalChapterId"]="finish"', content)
        self.assertIn(b'["targetLevel"]=60', content)
        self.assertIn(b'["complete"]=true', content)
        self.assertIn(b'["guideVersion"]=7', content)
        self.assertIn(b'["guideRelease"]="v4.11.36"', content)
        self.assertIn(b'["clientVersion"]="1.60.1.70938"', content)
        self.assertNotIn(b'["id"]="extra"', content)

        publish_route(installation, None, chapters[:2], snapshot=character, start_chapter_id="start")
        self.assertIn(b'["complete"]=false', output.read_bytes())

    def test_unrecognized_folder_is_preserved(self):
        installation = inspect_installation(self.addons)
        (self.addons / "RXPQuestGuard").mkdir()
        with self.assertRaisesRegex(ValueError, "could not be identified"):
            install_addon(installation)

    def test_generated_lua_treats_quotes_code_and_unicode_as_data(self):
        label = 'Rog\'mar "é"\n\\'
        payload = label + '\nQG.compromised = true'
        content = generate_addon_data(Database({95882: {"name": payload}}, "Test", "1", "Unknown"))
        self.assertNotIn(b'\nQG.compromised = true', content)
        self.assertIn(rb"\195\169", content)
        try:
            from lupa.lua51 import LuaRuntime
        except ImportError:
            return
        runtime = LuaRuntime()
        qg = runtime.execute(content.decode("ascii") + "\nreturn QG", "RXPQuestGuard", runtime.table())
        self.assertEqual(qg.data.quests[95882].name, payload)
        self.assertIsNone(qg.compromised)

    def test_generated_route_drives_lua_preflight(self):
        try:
            from lupa.lua51 import LuaRuntime
        except ImportError:
            self.skipTest("Install requirements-test.txt to run Lua serialization integration")
        point = GuideLocation(name="Test wanted poster", world=(2548, 0, -10.0, 20.0))
        route = (Guide("earlier", "Earlier", "Route", (Step(9, (
            QuestAction("accept", 95882, name="Vargas", location=point, condition="Horde"),), condition="Horde"),), "Fixture",
            condition="Horde", faction="Horde"),
                 Guide("later", "Later", "Route", (Step(42, (
                     QuestAction("complete", 95882, objective=1, name="Vargas"),)),), "Fixture"))
        runtime = LuaRuntime()
        private = runtime.table()
        runtime.execute(generate_addon_data(None, route).decode("ascii"), "RXPQuestGuard", private)
        engine = Path(__file__).resolve().parents[1] / "addon" / "RXPQuestGuard" / "Engine.lua"
        runtime.execute(engine.read_text(encoding="utf-8"), "RXPQuestGuard", private)
        row = runtime.execute("""
            local qg = ...
            local first, last = qg.data.route[1], qg.data.route[2]
            assert(first.faction == 'Horde' and first.conditional == false)
            local pickup, objective = first.steps[1], last.steps[1]
            pickup.chapter, objective.chapter = first.name, last.name
            local prior = {}
            qg.engine.recordPickup(prior, 95882, pickup, pickup.actions[1])
            local result = qg.engine.scan({steps={objective}, prior=prior,
                stateFor=function() return {active=false, completed=false} end})
            return result.issues[1]
        """, private)
        self.assertEqual((row.code, row.severity, row.pickup.chapter), ("unaccepted", "warning", "Earlier"))
        self.assertEqual(row.location.worldMapId, 2548)
        self.assertIsNone(row.location.mapId)
