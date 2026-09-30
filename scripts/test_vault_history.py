"""History never loses runs or mixes characters/weeks/providers; boards are partial."""
import copy
import json
import sys
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_vault import START, END
import vault
import vault_history as history
import vault_leaderboards as boards
import vault_sources as sources

PROFILE = {"name": "Main", "realm": "Proudmoore"}
SCOPE = SimpleNamespace(tenant="TENANT#test#prog-raid")
AT = int(START.timestamp()*1000)


def run(at=AT, level=10, dungeon="249", **kw):
    return {"at": at, "level": level, "dungeon": dungeon, **kw}


class MemoryDB:
    def __init__(self):
        self.rows = {}
        self.writes = 0
        self.race = None

    def key(self, item):
        return item["pk"]["S"], item["sk"]["S"]

    def get_item(self, **kw):
        item = self.rows.get(self.key(kw["Key"]))
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, **kw):
        key = self.key(kw["Item"])
        if self.race:
            self.rows[key] = self.race
            self.race = None
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.rows[key] = copy.deepcopy(kw["Item"])
        self.writes += 1


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()

    def reconcile(self, observed, **kw):
        return history.reconcile(SCOPE, PROFILE, "us", START, END, observed,
                                 ddb=self.db, table="history", **kw)

    def test_removed_runs_and_provider_outage_do_not_erase_history(self):
        original = [run(AT+i*100_000) for i in range(8)]
        self.reconcile({"Blizzard": original}, persist=True)
        got = self.reconcile({"Blizzard": original[:1]}, persist=True)
        self.assertEqual(len(got["Blizzard"]), 8)
        got = self.reconcile({"Blizzard": None})
        self.assertEqual(history.levels(got)["Blizzard"], [10]*8)
        self.assertIsNone(got["Raider.IO"])

    def test_repeated_collection_and_overlapping_profile_board_runs_count_once(self):
        a = [run(AT+123), run(AT+100_000)]
        self.reconcile({"Blizzard": a}, persist=True)
        got = self.reconcile({"Blizzard": [run(), run(AT+100_500)]}, persist=True)
        self.assertEqual(len(got["Blizzard"]), 2)

    def test_rio_ids_deduplicate_changed_timestamps_and_names(self):
        got = history.merge_runs([run(ids=["1"])], [run(AT+5000, dungeon="Renamed", ids=["1"])],
                                 "Raider.IO", START, END)
        self.assertEqual(len(got), 1)

    def test_sources_are_not_added_together(self):
        got = self.reconcile({"Raider.IO": [run(AT+i*100_000) for i in range(4)],
                              "Blizzard": [run(AT+i*200_000) for i in range(4)]})
        source, levels = sources.best_source(history.levels(got))
        self.assertEqual((source, len(levels), vault.mplus_slots(levels)[0]), ("Raider.IO", 4, 2))

    def test_week_boundaries_and_identity_keys(self):
        got = self.reconcile({"Blizzard": [run(AT-1), run(), run(int(END.timestamp()*1000))]})
        self.assertEqual(len(got["Blizzard"]), 1)
        original = history.history_key(SCOPE, PROFILE, "us", END)
        for profile, region, end in (({**PROFILE, "realm": "Other"}, "us", END),
                                     ({**PROFILE, "name": "Alt"}, "us", END),
                                     (PROFILE, "eu", END), (PROFILE, "us", END+timedelta(days=7))):
            self.assertNotEqual(original, history.history_key(SCOPE, profile, region, end))

    def test_dry_run_is_read_only_and_expired_rows_are_ignored(self):
        self.reconcile({"Blizzard": [run()]}, persist=True)
        writes = self.db.writes
        self.reconcile({"Blizzard": [run(AT+100_000)]})
        self.assertEqual(writes, self.db.writes)
        next(iter(self.db.rows.values()))["expiresAt"] = {"N": "0"}
        self.assertIsNone(self.reconcile({})["Blizzard"])

    def test_concurrent_winner_is_merged_on_retry(self):
        self.reconcile({"Blizzard": [run()]}, persist=True)
        winner = copy.deepcopy(next(iter(self.db.rows.values())))
        evidence = json.loads(winner["evidence"]["S"])
        evidence["Blizzard"].append(run(AT+200_000))
        winner["evidence"]["S"] = json.dumps(evidence)
        winner["version"]["N"] = "2"
        self.db.race = winner
        got = self.reconcile({"Blizzard": [run(AT+100_000)]}, persist=True)
        self.assertEqual(len(got["Blizzard"]), 3)

    def test_wcl_clock_skew_deduplicates_across_collection_passes(self):
        got = history.merge_runs([run()], [run(AT+59_000), run(AT+300_000)], "Warcraft Logs", START, END)
        self.assertEqual(len(got), 2)


class LeaderboardTests(unittest.TestCase):
    def document(self):
        return {"period": 7, "map_challenge_mode_id": 249, "leading_groups": [
            {"completed_timestamp": AT+100_000, "keystone_level": 10, "duration": 9_999_999,
             "members": [{"profile": {"name": "Main", "realm": {"slug": "proudmoore"}}},
                         {"profile": {"name": "Alt", "realm": {"slug": "proudmoore"}}}]}]}

    def test_untimed_completion_identity_filter_and_reset_boundary(self):
        doc = self.document()
        got = boards.extract(doc, {"main": PROFILE, "other": {**PROFILE, "realm": "Other"}}, START, END)
        self.assertEqual(len(got["main"]), 1)
        self.assertEqual(got["other"], [])
        self.assertNotIn("alt", got)
        doc["leading_groups"][0]["completed_timestamp"] = int(END.timestamp()*1000)
        self.assertEqual(boards.extract(doc, {"main": PROFILE}, START, END)["main"], [])

    def test_period_discovery_and_partial_board_failure(self):
        calls = []
        def get(token, path, **kw):
            calls.append(path)
            if path.endswith("period/index"):
                return {"periods": [{"id": n} for n in (6, 7, 8)]}
            if path.endswith("period/8") and "leaderboard" not in path:
                return {"start_timestamp": int(END.timestamp()*1000), "end_timestamp": int((END+timedelta(days=7)).timestamp()*1000)-1000}
            if path.endswith("period/7") and "leaderboard" not in path:
                return {"start_timestamp": AT, "end_timestamp": int(END.timestamp()*1000)-1000}
            if "/realm/" in path:
                return {"connected_realm": {"href": "https://us.api.blizzard.com/data/wow/connected-realm/5"}}
            if path.endswith("leaderboard/index"):
                return {"current_leaderboards": [{"id": 249}, {"id": 250}]}
            if "/249/" in path:
                return self.document()
            raise RuntimeError("temporarily unavailable")
        got = boards.fetch({"main": PROFILE}, "token", get, START, END)
        self.assertEqual(len(got["main"]), 1)
        self.assertFalse(any(p.endswith("period/6") for p in calls))

    def test_complete_outage_is_unknown(self):
        def fail(*a, **kw):
            raise RuntimeError("offline")
        self.assertIsNone(boards.fetch({"main": PROFILE}, "token", fail, START, END)["main"])


class CollectorTests(unittest.TestCase):
    def test_locks_before_fetch_and_checkpoints_without_posting(self):
        import handler
        import vault_collect as collector
        cfg = {"discord_guild_id": "guild", "role_id": "role", "guild_region": "us",
               "guild_realm": "Proudmoore", "guild_name": "Test", "blizzard_client_id": "id",
               "blizzard_client_secret": "secret", "wcl_client_id": "id", "wcl_client_secret": "secret"}
        with ExitStack() as stack:
            def mock(obj, name, **kw):
                return stack.enter_context(patch.object(obj, name, **kw))
            scope = SimpleNamespace(tenant=SCOPE.tenant, team="prog-raid")
            mock(handler, "tenant_configs", return_value=[(scope, cfg)])
            shared = {"1": ["Main", "Alt"]}
            mock(collector.store, "get_rollcall_setup", return_value={"members": shared})
            mock(collector.store, "get_vault_characters", return_value={"1": "Main"})
            mock(vault, "fetch_members", return_value=[{"user": {"id": "1"}}])
            mock(collector.raiderio, "_get", return_value={"members": [{"character": PROFILE}]})
            profiles = mock(vault, "fetch_profiles", return_value={"main": PROFILE})
            mock(collector.blizzard, "get_token", side_effect=RuntimeError("offline"))
            mock(collector.wcl, "get_token", side_effect=RuntimeError("offline"))
            checkpoint = mock(history, "for_profiles", return_value={})
            post = mock(handler.discord, "post_to")
            result = collector.collect(cfg, END+timedelta(hours=1), dry=True)
        self.assertEqual(profiles.call_args.args[0], ["Main"])
        self.assertEqual(shared["1"], ["Main", "Alt"])
        self.assertEqual(result["characters"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertGreaterEqual(checkpoint.call_count, 4)
        self.assertTrue(all(c.kwargs["persist"] is False for c in checkpoint.call_args_list))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
