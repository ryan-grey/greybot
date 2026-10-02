"""Offline tests for the raid meta achievement: what counts as earned, who is credited,
the order cards go out in, and that nothing is posted until the setup is live."""
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import glory
import handler
import keys

META = {"id": 900, "name": "Glory of the Test Raider", "reward_description": "Mount: Test Snake",
        "criteria": {"child_criteria": [
            {"id": 1, "achievement": {"id": 101, "name": "First Thing"}},
            {"id": 2, "achievement": {"id": 102, "name": "Second Thing"}},
            {"id": 3, "description": "a criterion with no achievement"}]}}
ORDER = [101, 102, 900]
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
SCOPE = keys.Scope.build("us", "proudmoore", "Scrambled", "1", team="prog-raid")
CFG = {"guild_region": "us", "guild_realm": "proudmoore", "guild_name": "Scrambled",
       "discord_guild_id": "1", "bot_token": "t", "channel_id": "55", "team_name": "Prog Raid",
       "blizzard_client_id": "id", "blizzard_client_secret": "secret"}


def profile(points, *done):
    return {"achievement_points": points, "last_login_timestamp": points,
            "achievements": [{"id": aid, "completed_timestamp": at} for aid, at in done]
                            + [{"id": 900, "criteria": {"is_completed": False}}]}


def getter(characters, calls=None):
    """A Blizzard `_get` over {name: profile}: the summary and the achievements document."""
    def get(_token, path, **_params):
        name = path.split("/")[5]
        if calls is not None:
            calls.append(path)
        if name not in characters:
            raise RuntimeError("404")
        return characters[name]
    return get


class PureTests(unittest.TestCase):
    def test_definition_skips_criteria_without_an_achievement(self):
        meta = glory.meta_definition(META)
        self.assertEqual([s["id"] for s in meta["subs"]], [101, 102])
        self.assertEqual(meta["reward"], "Mount: Test Snake")
        with self.assertRaises(ValueError):
            glory.meta_definition({"id": 5, "name": "Not a meta"})

    def test_progress_is_not_earned(self):
        got = glory.earned(profile(1, (101, 5000)), ORDER)
        self.assertEqual(got, {101: 5000})

    def test_one_entry_per_person_and_the_earliest_wins(self):
        holders = {}
        glory.merge(holders, "a", "Main", {101: 9000})
        glory.merge(holders, "a", "Alt", {101: 8000})
        glory.merge(holders, "b", "Other", {101: 8500})
        self.assertEqual(holders[101]["a"], {"at": 8000, "name": "Alt"})
        self.assertEqual(glory.names_text(holders[101]), "Alt and Other")

    def test_long_name_lists_are_cut(self):
        entry = {str(i): {"at": i, "name": f"N{i}"} for i in range(6)}
        self.assertEqual(glory.names_text(entry), "N0, N1, N2, N3 +2 more")

    def test_oldest_first_and_the_meta_last(self):
        holders = {900: {"a": {"at": 1, "name": "A"}}, 102: {"a": {"at": 5, "name": "A"}},
                   101: {"a": {"at": 9, "name": "A"}}}
        self.assertEqual(glory.fresh(holders, set(), ORDER), [102, 101, 900])
        self.assertEqual(glory.fresh(holders, {102}, ORDER), [101, 900])

    def test_boss_from_a_description(self):
        bosses = ["Nek'zali the Soulcoiler", "The Lost Explorers", "Vashnik the Malignant",
                  "The Twin Fangs"]
        self.assertEqual(glory.boss_in("Defeat Nek'zali, the Soulcoiler after a frog.", bosses),
                         "Nek'zali the Soulcoiler")
        self.assertEqual(glory.boss_in("Defeat Vashnik after killing the venom.", bosses),
                         "Vashnik the Malignant")
        self.assertEqual(glory.boss_in("Defeat the Twin Fangs after feeding.", bosses),
                         "The Twin Fangs")
        self.assertIsNone(glory.boss_in("Defeat the raid.", bosses))

    def test_the_message_pings_nobody_and_never_says_it_twice(self):
        copy = glory.sub_copy("Prog Raid", "First Thing", {"a": {"at": 1, "name": "A"}}, 1, 2,
                              "Glory of the Test Raider")
        self.assertEqual(copy["lines"], ["First in Prog Raid: A",
                                         "1 of 2 toward Glory of the Test Raider"])
        plain = glory.payload(copy)
        self.assertEqual(plain["allowed_mentions"], {"parse": []})
        self.assertIn("description", plain["embeds"][0])
        self.assertNotIn("description", glory.payload(copy, "https://x/card.png")["embeds"][0])


class ScanTests(unittest.TestCase):
    WATCHED = [{"id": "a", "characters": [("Main", "proudmoore"), ("Alt", "proudmoore")]},
               {"id": "b", "characters": [("Gone", "proudmoore")]}]

    def test_reads_every_character_and_reports_the_missing(self):
        get = getter({"main": profile(10), "alt": profile(20, (101, 7000))})
        seen = glory.scan(self.WATCHED, "t", get, {}, ORDER)
        self.assertEqual(seen["holders"], {101: {"a": {"at": 7000, "name": "Alt"}}})
        self.assertEqual((seen["read"], seen["unchanged"], seen["missing"]), (2, 0, ["Gone"]))

    def test_an_unchanged_signature_skips_the_download(self):
        calls = []
        get = getter({"main": profile(10), "alt": profile(20, (101, 7000))}, calls)
        sigs = glory.scan(self.WATCHED, "t", get, {}, ORDER)["sigs"]
        del calls[:]
        seen = glory.scan(self.WATCHED, "t", get, sigs, ORDER)
        self.assertEqual((seen["read"], seen["unchanged"]), (0, 2))
        self.assertFalse([c for c in calls if c.endswith("/achievements")])
        self.assertEqual(seen["sigs"], sigs)


class Store:
    """The state row, in memory, with the same claim semantics."""

    def __init__(self, state=None):
        self.state, self.sigs_saved = state, 0

    def load(self, _scope, _meta):
        return None if self.state is None else {"announced": set(self.state["announced"]),
                                                "sigs": dict(self.state["sigs"])}

    def seed(self, _scope, _meta, already, _now):
        self.state = {"announced": set(already), "sigs": {}}
        return True

    def claim(self, _scope, _meta, aid):
        if aid in self.state["announced"]:
            return False
        self.state["announced"].add(aid)
        return True

    def release(self, _scope, _meta, aid):
        self.state["announced"].discard(aid)

    def put_sigs(self, _scope, _meta, sigs, _now):
        self.state["sigs"], self.sigs_saved = sigs, self.sigs_saved + 1


class FlowTests(unittest.TestCase):
    def run_check(self, characters, state, event=None, live=True, post=None):
        self.store, self.posts = Store(state), []

        def post_to(where, payload, **_kw):
            self.posts.append((where, payload["embeds"][0]["title"]))
            return None
        setup = {"achievement": 900, "live": live, "role": "", "channel": ""}
        rollcall = {"members": {"a": ["Main"], "b": ["Other"]}}
        s = handler.store
        with patch.object(handler, "tenant_configs", return_value=[(SCOPE, CFG)]), \
                patch.object(s, "get_glory_setup", return_value=setup), \
                patch.object(s, "get_rollcall_setup", return_value=rollcall), \
                patch.object(s, "get_vault_characters", return_value={}), \
                patch.object(s, "load_glory", self.store.load), \
                patch.object(s, "seed_glory", self.store.seed), \
                patch.object(s, "claim_glory", self.store.claim), \
                patch.object(s, "release_glory", self.store.release), \
                patch.object(s, "put_glory_sigs", self.store.put_sigs), \
                patch.object(s, "record_post"), \
                patch.object(handler.blizzard, "get_token", return_value="t") as token, \
                patch.object(handler.blizzard, "achievement", return_value=META), \
                patch.object(handler.blizzard, "raid_bosses", return_value=[]), \
                patch.object(handler.blizzard, "_get", getter(characters)), \
                patch.object(handler.raiderio, "_get", return_value={"members": []}), \
                patch.object(handler, "kill_card_url", return_value=None), \
                patch.object(handler.discord, "post_to", post or post_to):
            self.token = token
            return handler.glory_check(event or {"mode": "glory"}, CFG, NOW)["results"][0]

    def titles(self):
        return [title for _where, title in self.posts]

    def test_nothing_runs_until_the_setup_is_live(self):
        got = self.run_check({"main": profile(1, (101, 5))}, None, live=False)
        self.assertEqual(got, {"ok": True, "team": "prog-raid", "skipped": "glory_not_live"})
        self.token.assert_not_called()
        self.assertEqual(self.posts, [])
        self.assertIsNone(self.store.state)

    def test_a_dry_run_writes_nothing(self):
        got = self.run_check({"main": profile(1, (101, 5))}, None, {"mode": "glory", "dry": True},
                             live=False)
        self.assertEqual(got["earned"], {"First Thing": "Main"})
        self.assertEqual(got["wouldAnnounce"], ["First Thing"])
        self.assertEqual(got["missingCharacters"], ["Other"])
        self.assertIsNone(self.store.state)
        self.assertEqual(self.posts, [])

    def test_the_first_live_run_seeds_and_says_nothing(self):
        got = self.run_check({"main": profile(1, (101, 5))}, None)
        self.assertEqual(got["posted"], [])
        self.assertEqual(self.store.state["announced"], {101})
        self.assertEqual(self.store.sigs_saved, 1)

    def test_backfill_announces_what_was_already_earned(self):
        got = self.run_check({"main": profile(1, (101, 5))}, None,
                             {"mode": "glory", "backfill": True})
        self.assertEqual(got["posted"], ["First Thing"])

    def test_each_achievement_is_announced_once(self):
        state = {"announced": {101}, "sigs": {}}
        chars = {"main": profile(1, (101, 5), (102, 9)), "other": profile(2, (102, 9))}
        got = self.run_check(chars, state)
        self.assertEqual(self.titles(), ["Raid achievement earned: Second Thing"])
        self.assertEqual(self.posts[0][0], {"bot_token": "t", "channel": "55"})
        self.assertEqual(got["posted"], ["Second Thing"])
        again = self.run_check(chars, self.store.state)
        self.assertEqual((again["posted"], again["read"], self.posts), ([], 0, []))

    def test_the_meta_follows_its_parts(self):
        chars = {"main": profile(1, (900, 9), (102, 9), (101, 5))}
        self.run_check(chars, {"announced": set(), "sigs": {}})
        self.assertEqual(self.titles(), ["Raid achievement earned: First Thing",
                                         "Raid achievement earned: Second Thing",
                                         "Prog Raid completed Glory of the Test Raider"])

    def test_a_failed_post_is_handed_back_and_retried(self):
        def refuse(*_a, **_kw):
            raise handler.discord.DiscordError("no")
        chars = {"main": profile(1, (101, 5), (102, 9))}
        got = self.run_check(chars, {"announced": set(), "sigs": {}}, post=refuse)
        self.assertEqual((got["ok"], got["failed"]), (False, ["First Thing"]))
        self.assertEqual(self.store.state["announced"], set())
        self.assertEqual(self.store.sigs_saved, 0)
        self.run_check(chars, self.store.state)
        self.assertEqual(len(self.posts), 2)


if __name__ == "__main__":
    unittest.main()
