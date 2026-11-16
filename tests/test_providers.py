"""Fixture responses exercise the provider contract without live network access."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from unittest import TestCase
from urllib.error import HTTPError

from questguard.evidence import CLASSIC, FOREVER_SPECIFIC, build_matches, validated_evidence
from questguard.providers import (
    FOREVERDB_DOCS,
    ForeverDBProvider,
    ProviderError,
    merge_quest_evidence,
    normalize_foreverdb,
    wowhead_reference,
)

GAME = "1.60.1.70938"
CAPTURE = "2026-11-16T12:00:00+00:00"


def sample_quest(quest_id=654, *, source="Seen in WoW Forever", build=GAME):
    return {
        "data": {
            "questId": quest_id,
            "name": "Synthetic sampling quest",
            "guide": f"/quest/{quest_id}-synthetic-sampling-quest",
            "level": 46,
            "minLevel": 38,
            "side": "horde",
            "source": source,
            "timerSeconds": 7200,
            "timerSource": source,
            "starts": {"type": "item", "id": 8524, "name": "Synthetic power source", "source": source},
            "ends": {"type": "npc", "id": 1234, "name": "Synthetic engineer", "source": source,
                     "location": {"uiMapId": 1446, "zone": "Synthetic zone", "x": 52.5, "y": 28.5}},
            "chain": {
                "previous": [
                    {"questId": 200, "relation": "Complete first", "source": source},
                    {"questId": 201, "relation": "Must be active", "source": source},
                ],
                "next": [{"questId": 202, "relation": "Next quest", "source": source}],
            },
        },
        "meta": {"gameBuild": build, "client": "private-developer-reference"},
    }


def http_error(status, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://foreverdb.net/api/v1/quests/654", status, "server debug string", headers, None)


class Clock:
    def __init__(self):
        self.seconds = 0

    def now(self):
        return self.seconds

    def sleep(self, duration):
        self.seconds += duration

    def calendar(self):
        return datetime.fromisoformat(CAPTURE) + timedelta(seconds=self.seconds)


class OfflineProviderTests(TestCase):
    def provider(self, replies, *, authorized=True):
        clock, requests = Clock(), []
        responses = iter(replies)

        def transport(request, timeout):
            requests.append((clock.now(), request, timeout))
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result

        return ForeverDBProvider(
            "fdb_fixture_only", authorized=authorized, transport=transport,
            clock=clock.now, sleep=clock.sleep, wall_clock=clock.calendar,
        ), requests

    def test_authorized_id_lookup_normalizes_attribution_and_never_persists_key(self):
        provider, requests = self.provider([sample_quest()])
        result = provider.lookup(654)
        self.assertEqual(result.quest_id, 654)
        self.assertEqual(result.source_version, GAME)
        self.assertEqual(result.quest["source"], "ForeverDB.net")
        self.assertEqual(result.quest["observedAt"], CAPTURE)
        self.assertEqual(result.quest["startItem"], {
            "itemId": 8524, "name": "Synthetic power source", "verifiedForever": True,
        })
        self.assertEqual(result.quest["timerSeconds"], 7200)
        self.assertEqual(result.quest["endLocation"]["mapId"], 1446)
        self.assertNotIn("requirements", result.quest)
        self.assertNotIn("requirementGroups", result.quest)
        self.assertNotIn("minLevel", result.quest)  # no independent Forever field label
        self.assertEqual(result.quest["fieldEvidence"]["minLevel"][0]["value"], 38)
        self.assertEqual([edge["direction"] for edge in result.quest["chain"]], ["previous", "previous", "next"])
        evidence = result.quest["fieldEvidence"]["startItem"][0]
        self.assertEqual((evidence["confidence"], evidence["gameVersion"]), (FOREVER_SPECIFIC, GAME))
        request = requests[0][1]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://foreverdb.net/api/v1/quests/654")
        self.assertEqual(request.get_header("X-api-key"), "fdb_fixture_only")
        self.assertIsNone(request.data)
        exported = json.dumps(result.quest)
        self.assertNotIn("fdb_fixture_only", exported)
        self.assertNotIn("private-developer-reference", exported)

    def test_forever_classic_timer_and_start_item_stay_advisory(self):
        result = normalize_foreverdb(sample_quest(source="Classic data (CMaNGOS)"), 654, CAPTURE).quest
        self.assertNotIn("timerSeconds", result)
        self.assertNotIn("startItem", result)
        self.assertNotIn("requirements", result)
        for field in ("timerSeconds", "startItem", "endLocation"):
            claim = result["fieldEvidence"][field][0]
            self.assertEqual(claim["confidence"], CLASSIC)
            self.assertTrue(claim["inheritedClassic"])
            self.assertFalse(claim["observedForever"])

    def test_unlabelled_timer_not_promoted_by_whole_quest_seen_flag(self):
        payload = sample_quest()
        del payload["data"]["timerSource"]
        result = normalize_foreverdb(payload, 654, CAPTURE).quest
        self.assertNotIn("timerSeconds", result)
        self.assertNotIn("timerSeconds", result["fieldEvidence"])

    def test_forever_npc_does_not_promote_classic_coordinates(self):
        payload = sample_quest()
        payload["data"]["ends"]["location"]["source"] = "Classic data"
        result = normalize_foreverdb(payload, 654, CAPTURE).quest
        self.assertEqual(result["endLocation"]["id"], 1234)
        self.assertNotIn("x", result["endLocation"])
        claims = result["fieldEvidence"]["endLocation"]
        self.assertEqual(claims[1]["confidence"], CLASSIC)
        self.assertEqual(claims[1]["value"]["x"], 52.5)

    def test_unauthorized_uses_no_connection(self):
        provider, requests = self.provider([], authorized=False)
        with self.assertRaisesRegex(ProviderError, "authorize") as failure:
            provider.lookup(654)
        self.assertEqual(failure.exception.kind, "unauthorized")
        self.assertEqual(requests, [])

    def test_invalid_key_and_quota_do_not_retry_or_expose_error_body(self):
        for code, expected in ((401, "invalid_key"), (402, "quota"), (403, "unauthorized"), (404, "not_found")):
            with self.subTest(code=code):
                provider, requests = self.provider([http_error(code)])
                with self.assertRaises(ProviderError) as failure:
                    provider.lookup(654)
                self.assertEqual(failure.exception.kind, expected)
                self.assertEqual(len(requests), 1)
                self.assertNotIn("server debug", str(failure.exception))
                self.assertNotIn("fdb_fixture_only", str(failure.exception))

    def test_long_rate_limit_blocks_later_lookups_and_uses_no_extra_request(self):
        provider, requests = self.provider([http_error(429, "90")])
        with self.assertRaises(ProviderError) as failure:
            provider.lookup(654)
        self.assertEqual(failure.exception.kind, "rate_limited")
        self.assertEqual(failure.exception.retry_after, 90)
        with self.assertRaises(ProviderError):
            provider.lookup(95882)
        self.assertEqual(len(requests), 1)

    def test_short_rate_limit_uses_retry_after_and_real_request_spacing(self):
        date = format_datetime(datetime.fromisoformat(CAPTURE) + timedelta(seconds=4), usegmt=True)
        provider, requests = self.provider([http_error(429, date), sample_quest(), sample_quest(95882)])
        provider.lookup(654)
        provider.lookup(95882)
        self.assertEqual(len(requests), 3)
        self.assertGreaterEqual(requests[1][0] - requests[0][0], 4)
        self.assertGreaterEqual(requests[2][0] - requests[1][0], 2.09)

    def test_timeout_retries_bounded_and_reports_unavailable(self):
        provider, requests = self.provider([TimeoutError("private")] * 3)
        with self.assertRaises(ProviderError) as failure:
            provider.lookup(654)
        self.assertEqual(failure.exception.kind, "unavailable")
        self.assertEqual(len(requests), 3)
        self.assertAlmostEqual(requests[1][0], 2.1)
        self.assertAlmostEqual(requests[2][0], 6.3)
        self.assertNotIn("private", str(failure.exception))

    def test_cancellation_during_backoff_prevents_more_requests(self):
        provider, requests = self.provider([TimeoutError()])
        with self.assertRaises(ProviderError) as failure:
            provider.lookup(654, cancelled=lambda: bool(requests))
        self.assertEqual(failure.exception.kind, "cancelled")
        self.assertEqual(len(requests), 1)

    def test_unexpected_payload_and_echoed_credential_are_never_stored(self):
        echo = sample_quest()
        echo["data"]["name"] = "fdb_fixture_only"
        for response in (None, sample_quest(999), echo, {"data": {"questId": 654}}):
            with self.subTest(response=type(response).__name__):
                provider, requests = self.provider([response])
                with self.assertRaises(ProviderError) as failure:
                    provider.lookup(654)
                self.assertEqual(failure.exception.kind, "malformed")
                self.assertEqual(len(requests), 1)

    def test_reference_links_are_safe_and_never_make_programmatic_wowhead_requests(self):
        self.assertEqual(wowhead_reference(654), "https://www.wowhead.com/forever/quest=654")
        with self.assertRaises(ValueError):
            wowhead_reference("654?token=secret")
        bad = sample_quest()
        bad["data"]["guide"] = "https://unrelated.example/quest/654-pretender?token=secret"
        self.assertEqual(normalize_foreverdb(bad, 654, CAPTURE).quest["sourceUrl"], FOREVERDB_DOCS)


class EvidenceMergerTests(TestCase):
    def claimed(self, field, value, provider, **metadata):
        return {"fieldEvidence": {field: [{"value": value, "provider": provider,
                                           "confidence": FOREVER_SPECIFIC, "method": "Fixture observation",
                                           "gameVersion": GAME, "retrievedAt": CAPTURE,
                                           "observedForever": True, **metadata}]}}

    def test_comparable_disagreements_are_visible_and_neither_location_is_selected(self):
        first = self.claimed("startLocation", {"mapId": 1446, "x": 40.5, "y": 30}, "Source A")
        other = self.claimed("startLocation", {"mapId": 1446, "x": 60, "y": 45}, "Source B")
        result = merge_quest_evidence(95882, first, other, game_version=GAME)
        self.assertNotIn("startLocation", result)
        conflict, = result["conflicts"]
        self.assertEqual(conflict["field"], "startLocation")
        self.assertTrue(conflict["unresolved"])
        self.assertEqual({claim["provider"] for claim in conflict["claims"]}, {"Source A", "Source B"})

    def test_different_claims_retain_classic_context_without_downgrading_forever(self):
        first = self.claimed("startItem", {"itemId": 8524}, "Source A")
        old = self.claimed("startItem", {"itemId": 8525}, "Classic", inheritedClassic=True)
        result = merge_quest_evidence(654, first, old, game_version=GAME)
        self.assertEqual(result["startItem"], {"itemId": 8524, "verifiedForever": True})
        self.assertFalse(result["conflicts"][0]["unresolved"])
        self.assertEqual(result["conflicts"][0]["claims"][1]["confidence"], CLASSIC)

    def test_build_change_never_promotes_classic_or_publishes_old_sensitive_fields(self):
        first = self.claimed("startItem", {"itemId": 8524}, "Source A", gameVersion=70938)
        result = merge_quest_evidence(654, None, first, game_version="1.60.1.70939")
        self.assertNotIn("startItem", result)
        self.assertEqual(result["fieldEvidence"]["startItem"][0]["gameVersion"], "70938")
        result = merge_quest_evidence(654, None, first, game_version="1.60.1_70938")
        self.assertEqual(result["startItem"]["itemId"], 8524)
        self.assertTrue(build_matches(GAME, "WOW-70938patch1.60.1_Retail"))
        self.assertIsNone(build_matches(GAME, "1.60.1"))
        self.assertFalse(build_matches(GAME, "1.60.0"))

    def test_can_read_explicit_local_requirements_without_promoting_unverified_old_provider_fields(self):
        local = {"source": "Local fixture", "requirements": [{"questId": 95260, "state": "completed"}]}
        stale = {"source": "ForeverDB.net", "startItem": {"itemId": 8524, "verifiedForever": True},
                 "requirements": [{"questId": 100, "state": "completed"}]}
        merged = merge_quest_evidence(95261, local, stale, game_version=GAME)
        self.assertEqual(merged["requirements"], local["requirements"])
        self.assertNotIn("startItem", merged)
        self.assertFalse(merged["conflicts"][0]["unresolved"])

    def test_do_not_assemble_hard_prerequisites_from_unrelated_fragments(self):
        first = self.claimed("requirements", [{"questId": 110, "state": "completed"}], "Source A")
        other = self.claimed("requirementGroups", [[{"questId": 120, "state": "active"}]], "Source B")
        merged = merge_quest_evidence(654, first, other)
        self.assertNotIn("requirements", merged)
        self.assertNotIn("requirementGroups", merged)
        self.assertEqual({conflict["field"] for conflict in merged["conflicts"]}, {"requirements", "requirementGroups"})

    def test_live_player_status_is_not_provider_quest_metadata(self):
        self.assertEqual(validated_evidence(654, {"active": [
            {"value": True, "provider": "Source", "method": "Fixture", "confidence": FOREVER_SPECIFIC},
        ]}), {})
