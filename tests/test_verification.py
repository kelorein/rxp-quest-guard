from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from questguard.engine import Audit, Finding, Route
from questguard.ingest import Guide, GuideLocation, QuestAction, Snapshot, Step
from questguard.installation import generate_addon_data
from questguard.providers import ProviderError, ProviderResult
from questguard.sync import import_database, load_cache, timestamp
from questguard.verification import (CacheRecord, LookupSettings, VerificationCache, guide_discrepancies,
                                    load_verification_cache, plan_verification, verification_state,
                                    verified_database, verify_quests)


BUILD = "1.60.1.70938"
RELEASE = "v4.11.36"


def named_claim(value, provider="ForeverDB.net", *, build=BUILD):
    return {"value": value, "provider": provider, "sourceUrl": "https://foreverdb.net/quest/654",
            "method": "Synthetic application fixture", "confidence": "forever-specific", "observedForever": True,
            "gameVersion": build, "retrievedAt": timestamp()}


def start_metadata(item=8524, provider="ForeverDB.net", *, duration=7200):
    return {"source": provider, "name": "Tanaris Field Sampling", "fieldEvidence": {
        "startItem": [named_claim({"itemId": item}, provider)], "timerSeconds": [named_claim(duration, provider)],
    }}


def linked_route():
    # Shape of the level-43 through 60 path, with invented unrelated objective IDs.
    levels = (43, 44, 45, 46, 48, 49, 50, 51, 52, 53, 54, 55, 56, 58, 59)
    chapters = []
    for index, level in enumerate(levels):
        if index == 0:
            steps = (Step(1, (QuestAction("complete", 95882), QuestAction("complete", 95261))),
                     Step(2, (QuestAction("accept", 10001, condition="Mage"),), condition="Mage"))
        else:
            steps = (Step(1, (QuestAction("complete", 654 if index == 1 else 20000 + index),)),)
        chapters.append(Guide(f"guide-{level}", f"Level {level}", "Horde route", steps, "Synthetic",
                              faction="Horde", level_min=level, level_max=levels[index + 1] if index + 1 < len(levels) else 60))
    return Route(tuple(chapters), faction="Horde", character_class="Hunter", active_id=chapters[0].id)


def character():
    return Snapshot("Horde Hunter", character="Test Hunter", realm="Test Realm", current_guide="Level 43",
                    current_group="Horde route", current_step=1, exported_at=datetime.now(UTC),
                    active=frozenset({95261}), completed=frozenset({95260}))


class FakeProvider:
    id = "foreverdb"
    private_key = "DO-NOT-PERSIST-THIS-APPROVED-KEY"

    def __init__(self, failure=None):
        self.called = []
        self.failure = failure

    def lookup(self, quest_id, *, cancelled=None):
        self.called.append(quest_id)
        if self.failure:
            raise ProviderError(self.failure, self.private_key)
        return ProviderResult(quest_id, start_metadata(), BUILD)


class VerificationTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {"QG_DATA_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_smart_triage_prioritizes_vargas_and_prefetches_two_linked_chapters(self):
        route = linked_route()
        finding = Finding("warning", "missing_pickup", "Vargas", "Earlier pickup expected", 95882,
                          "guide-43", 1, pickup_guide="Lantern Marsh I")
        queue = plan_verification(route, character(), Audit((), (finding,)))
        self.assertEqual([candidate.quest_id for candidate in queue], [95882, 654, 20002])
        self.assertIn("suspected missing pickup", queue[0].reason)
        future = plan_verification(route, character(), mode="prefetch")
        self.assertEqual({candidate.quest_id for candidate in future}, {654, 20002})
        full = plan_verification(route, character(), mode="full")
        self.assertEqual(len({candidate.chapter for candidate in full}), 15)
        self.assertNotIn(10001, {candidate.quest_id for candidate in full})

    def test_dedup_and_guide_change_revalidates_dependencies_only(self):
        now = timestamp()
        cache = VerificationCache({"foreverdb": {quest: CacheRecord({"name": "Cached"}, BUILD, now, BUILD, RELEASE)
                                                for quest in (95882, 654, 20002)}})
        route = linked_route()
        self.assertEqual(plan_verification(route, character(), cache=cache, game_version=BUILD, guide_version=RELEASE), ())
        changed_guide = plan_verification(route, character(), cache=cache, game_version=BUILD, guide_version="4.11.37")
        self.assertEqual([candidate.quest_id for candidate in changed_guide], [95882])
        changed_game = plan_verification(route, character(), cache=cache, game_version="1.60.1.71001", guide_version=RELEASE)
        self.assertEqual({candidate.quest_id for candidate in changed_game}, {95882, 654, 20002})
        explicit = plan_verification(route, character(), mode="selected", quest_id=654, cache=cache)
        self.assertEqual(explicit[0].quest_id, 654)

    def test_verified_cache_is_separate_from_legacy_and_failure_preserves_both(self):
        local = self.root / "local.json"
        local.write_text(json.dumps({"schemaVersion": 1, "source": "Local notes", "quests": {"95882": {
            "name": "Vargas", "requirements": [{"questId": 95260, "state": "completed"}]}}}), encoding="utf-8")
        previous = import_database(local)
        original = (self.root / "quests-v1.json").read_bytes()
        request = plan_verification(None, None, mode="selected", quest_id=654)
        fake = FakeProvider()
        result = verify_quests((*request, *request), [fake], game_version=BUILD, guide_version=RELEASE)
        self.assertEqual(result.fetched, 1)
        self.assertEqual(fake.called, [654])
        fresh = load_verification_cache()
        database = verified_database(load_cache(), cache=fresh, game_version=BUILD)
        self.assertEqual(database.quests[654]["timerSeconds"], 7200)
        self.assertEqual(database.quests[654]["startItem"]["itemId"], 8524)
        self.assertEqual(database.quests[95882], previous.quests[95882])
        failure = verify_quests(request, [FakeProvider("unavailable")], game_version=BUILD)
        self.assertEqual(failure.fetched, 0)
        self.assertIn(654, failure.errors)
        self.assertEqual(failure.cache.records, fresh.records)
        self.assertEqual((self.root / "quests-v1.json").read_bytes(), original)
        self.assertNotIn(fake.private_key, (self.root / "verification-v2.json").read_text(encoding="utf-8"))
        lua = generate_addon_data(database)
        self.assertNotIn(fake.private_key.encode(), lua)
        self.assertIn(b"https://foreverdb.net", lua)

    def test_invalid_key_cooldown_ends_batch_and_cancellation_avoids_calls(self):
        queue = plan_verification(linked_route(), character())
        fake = FakeProvider("invalid_key")
        result = verify_quests(queue, [fake])
        self.assertEqual(len(fake.called), 1)
        self.assertEqual(len(result.errors), len(queue))
        self.assertNotIn(fake.private_key, str(result.errors))
        verify_quests(queue, [fake])
        self.assertEqual(len(fake.called), 1)
        result = verify_quests(queue, [fake], cancelled=lambda: True)
        self.assertTrue(result.cancelled)
        self.assertEqual(len(fake.called), 1)

    def test_stale_dependencies_are_advisory_and_conflicts_preserve_both_claims(self):
        past = (datetime.now(UTC) - timedelta(days=16)).isoformat()
        old = CacheRecord(start_metadata(), BUILD, past, BUILD)
        old_database = verified_database(cache=VerificationCache({"foreverdb": {654: old}}), game_version=BUILD)
        self.assertNotIn("startItem", old_database.quests[654])
        self.assertIn("startItem", old_database.quests[654]["fieldEvidence"])
        first = CacheRecord(start_metadata(), BUILD, timestamp(), BUILD)
        second = CacheRecord(start_metadata(8888, "Other source"), BUILD, timestamp(), BUILD)
        cache = VerificationCache({"foreverdb": {654: first}, "other": {654: second}})
        changed = verified_database(cache=cache, game_version="1.60.1.71001")
        self.assertNotIn("timerSeconds", changed.quests[654])
        current = verified_database(cache=cache, game_version=BUILD)
        self.assertNotIn("startItem", current.quests[654])
        conflict, = [c for c in current.quests[654]["conflicts"] if c["field"] == "startItem"]
        self.assertEqual({c["value"]["itemId"] for c in conflict["claims"]}, {8524, 8888})
        self.assertEqual(verification_state(654, character(), current.quests[654])[0], "Conflicting sources")
        self.assertEqual(verification_state(95261, character(), current.quests[654])[0], "Verified active")

    def test_guide_location_difference_requires_explicit_compatible_map_and_source(self):
        guide = Guide("one", "One", "Route", (Step(1, (QuestAction("accept", 654,
                      location=GuideLocation(map_id=900, x=20.0, y=20.0)),)),), "Fixture")
        location = {"mapId": 900, "x": 35.0, "y": 20.0}
        row = {"startLocation": location, "fieldEvidence": {"startLocation": [named_claim(location)]}}
        self.assertEqual(guide_discrepancies(Route((guide,)), {654: row})[654][0], "Possible guide inconsistency")
        self.assertFalse(guide_discrepancies(Route((guide,)), {654: {"startLocation": {**location, "mapId": 901}}}))
        self.assertEqual(verification_state(654, character(), {}, planned=True)[0], "Future planned quest")
