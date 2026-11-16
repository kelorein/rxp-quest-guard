from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from questguard import sync


class CacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {"QG_DATA_DIR": str(self.root / "cache")})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = self.root / "source.json"

    def load_local(self):
        self.data.write_text(json.dumps({
            "schemaVersion": 1, "source": "Example verified data", "updatedAt": "2026-10-01T00:00:00+00:00",
            "quests": {"95882": {"name": "Vargas", "classification": "optional",
                                "startLocation": {"zone": "Example Zone", "x": 59.6, "y": 47, "mapId": 8000}}},
        }), encoding="utf-8")
        return sync.import_database(self.data)

    def test_offline_cache_retains_last_successful_data(self):
        self.assertIsNone(sync.load_cache())
        old = self.load_local()
        with patch.object(sync, "_provider_request", side_effect=URLError("offline")):
            result = sync.sync_foreverdb([95882], "test-key")
        self.assertTrue(result.used_cache)
        self.assertEqual(sync.load_cache(), old)
        self.assertEqual(result.database, old)
        self.assertIsNotNone(result.database.age_days)
        self.assertIn("unreachable", result.message)

    def test_normalizes_provider_without_asserting_ambiguous_requirements(self):
        reply = {"meta": {"gameBuild": "1.60.1.1234"}, "data": {
            "id": 95261, "name": "It Takes Guts", "side": "horde", "level": 43,
            "starts": {"type": "object", "name": "Test Start", "location": {
                "uiMapId": 550, "zone": "Example", "x": 21, "y": 54}},
            "chain": {"previous": [{"questId": 95260, "relation": "Complete first"}]},
        }}
        with patch.object(sync, "_provider_request", return_value=reply) as request:
            update = sync.sync_foreverdb([95261, 95261], "test-key")
        request.assert_called_once_with(95261, "test-key")
        self.assertEqual(update.database.source_version, "1.60.1.1234")
        quest = sync.load_cache().quests[95261]
        self.assertEqual(quest["startLocation"]["mapId"], 550)
        self.assertEqual(quest["faction"], "Horde")
        self.assertEqual(quest["chain"][0]["questId"], 95260)
        self.assertNotIn("requirements", quest)
        self.assertNotIn("minLevel", quest)

    def test_rate_limit_and_cancel_preserve_cache(self):
        old = self.load_local()
        with patch.object(sync, "_provider_request", side_effect=HTTPError("url", 429, "rate", None, None)) as request:
            self.assertTrue(sync.sync_foreverdb([95882], "test-key").used_cache)
            self.assertTrue(sync.sync_foreverdb([95882], "test-key", cancelled=lambda: True).used_cache)
        request.assert_called_once()
        self.assertEqual(sync.load_cache(), old)

    def test_invalid_import_preserves_cache(self):
        old = self.load_local()
        for content in (
            '{"schemaVersion":1,"quests":{"001":{},"1":{}}}',
            '{"schemaVersion":1,"quests":{"1":{"requirements":[{"questId":1,"state":"completed"}]}}}',
            '{"schemaVersion":1,"quests":{},"quests":{}}',
            '{"schemaVersion":2,"quests":{}}',
        ):
            with self.subTest(content=content):
                self.data.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    sync.import_database(self.data)
                self.assertEqual(sync.load_cache(), old)
