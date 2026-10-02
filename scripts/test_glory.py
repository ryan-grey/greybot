"""Offline tests for the raid meta achievement: what counts as earned by the guild, the
order cards go out in, and that nothing is posted until the setup is live."""
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
SUBS = [101, 102]
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
        self.assertEqual(glory.earned(profile(1, (101, 5000)), SUBS), {101: 5000})

    def test_a_main_and_an_alt_are_one_raider(self):
        held = {}
        glory.merge(held, "a", {101: 9000})
        glory.merge(held, "a", {101: 8000})
        glory.merge(held, "b", {101: 8500})
        self.assertEqual(held, {101: {"a": 8000, "b": 8500}})

    def test_a_guild_group_is_many_raiders_at_one_moment(self):
        together = {str(i): 1_000_000 + i * 1000 for i in range(10)}
        self.assertEqual(glory.group_at(together), 1_000_000)
        # Nine is not a group, and neither are ten who each got it on a different day.
        self.assertIsNone(glory.group_at(dict(list(together.items())[:9])))
        self.assertIsNone(glory.group_at({str(i): i * 86_400_000 for i in range(10)}))
        # A raider who had it from a pug last week is simply not part of the group.
        self.assertEqual(glory.group_at({**together, "pug": 5}), 1_000_000)
        self.assertEqual(glory.group_at({"a": 1, "b": 2}, need=2), 1)

    def test_holdings_accumulate_across_runs(self):
        first = {101: {str(i): 1000 for i in range(6)}}
        later = {101: {str(i): 1000 for i in range(6, 10)}}
        self.assertEqual(glory.guild_earned(first, SUBS), {})
        self.assertEqual(glory.guild_earned(glory.combine(first, later), SUBS), {101: 1000})
        self.assertEqual(first[101], {str(i): 1000 for i in range(6)})

    def test_announced_in_the_order_earned(self):
        self.assertEqual(glory.fresh({101: 9, 102: 5}, set()), [102, 101])
        self.assertEqual(glory.fresh({101: 9, 102: 5}, {102}), [101])

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

    def test_the_message_credits_the_guild_and_pings_nobody(self):
        copy = glory.sub_copy("Scrambled", "First Thing", 1, 2, "Glory of the Test Raider")
        self.assertEqual(copy["title"], "Scrambled earned First Thing")
        self.assertEqual(copy["lines"], ["1 of 2 toward Glory of the Test Raider"])
        plain = glory.payload(copy)
        self.assertEqual(plain["allowed_mentions"], {"parse": []})
        self.assertIn("description", plain["embeds"][0])
        self.assertNotIn("description", glory.payload(copy, "https://x/card.png")["embeds"][0])


class ScanTests(unittest.TestCase):
    WATCHED = [{"id": "a", "characters": [("Main", "proudmoore"), ("Alt", "proudmoore")]},
               {"id": "b", "characters": [("Gone", "proudmoore")]}]

    def test_reads_every_character_and_reports_the_missing(self):
        get = getter({"main": profile(10), "alt": profile(20, (101, 7000))})
        seen = glory.scan(self.WATCHED, "t", get, {}, SUBS)
        self.assertEqual(seen["held"], {101: {"a": 7000}})
        self.assertEqual((seen["read"], seen["unchanged"], seen["missing"]), (2, 0, ["Gone"]))

    def test_an_unchanged_signature_skips_the_download(self):
        calls = []
        get = getter({"main": profile(10), "alt": profile(20, (101, 7000))}, calls)
        sigs = glory.scan(self.WATCHED, "t", get, {}, SUBS)["sigs"]
        del calls[:]
        seen = glory.scan(self.WATCHED, "t", get, sigs, SUBS)
        self.assertEqual((seen["read"], seen["unchanged"]), (0, 2))
        self.assertFalse([c for c in calls if c.endswith("/achievements")])
        self.assertEqual(seen["sigs"], sigs)


class Store:
    """The state row, in memory, with the same claim semantics."""

    def __init__(self, state=None):
        self.state = state

    def load(self, _scope, _meta):
        if self.state is None:
            return None
        return {"announced": set(self.state["announced"]),
                "held": {aid: dict(e) for aid, e in self.state.get("held", {}).items()},
                "sigs": dict(self.state.get("sigs", {}))}

    def seed(self, _scope, _meta, already, _now):
        self.state = {"announced": set(already), "held": {}, "sigs": {}}
        return True

    def claim(self, _scope, _meta, aid):
        if aid in self.state["announced"]:
            return False
        self.state["announced"].add(aid)
        return True

    def release(self, _scope, _meta, aid):
        self.state["announced"].discard(aid)

    def progress(self, _scope, _meta, held, sigs, _now):
        self.state["held"], self.state["sigs"] = held, sigs


def raid(*done, who="abcdefghij"):
    """{character: profile} for a raid group that all earned `done` together."""
    return {name: profile(i + 1, *done) for i, name in enumerate(who)}


class FlowTests(unittest.TestCase):
    def run_check(self, characters, state, event=None, live=True, post=None, cfg=CFG):
        self.store, self.posts = Store(state), []

        def post_to(where, payload, **_kw):
            self.posts.append((where, payload["embeds"][0]["title"]))
            return None
        setup = {"achievement": 900, "live": live, "role": "", "channel": "", "group": 0}
        rollcall = {"members": {name: [name] for name in "abcdefghijkl"}}
        s = handler.store
        with patch.object(handler, "tenant_configs", return_value=[(SCOPE, cfg)]), \
                patch.object(s, "get_glory_setup", return_value=setup), \
                patch.object(s, "get_rollcall_setup", return_value=rollcall), \
                patch.object(s, "get_vault_characters", return_value={}), \
                patch.object(s, "load_glory", self.store.load), \
                patch.object(s, "seed_glory", self.store.seed), \
                patch.object(s, "claim_glory", self.store.claim), \
                patch.object(s, "release_glory", self.store.release), \
                patch.object(s, "put_glory_progress", self.store.progress), \
                patch.object(s, "record_post"), \
                patch.object(handler.blizzard, "get_token", return_value="t") as token, \
                patch.object(handler.blizzard, "achievement", return_value=META), \
                patch.object(handler.blizzard, "raid_bosses", return_value=[]), \
                patch.object(handler.blizzard, "_get", getter(characters)), \
                patch.object(handler.raiderio, "_get", return_value={"members": []}), \
                patch.object(handler, "kill_card_url", return_value=None), \
                patch.object(handler.discord, "post_to", post or post_to), \
                patch("builtins.print"):
            self.token = token
            return handler.glory_check(event or {"mode": "glory"}, cfg, NOW)["results"][0]

    def titles(self):
        return [title for _where, title in self.posts]

    def test_nothing_runs_until_the_setup_is_live(self):
        got = self.run_check(raid((101, 5)), None, live=False)
        self.assertEqual(got, {"ok": True, "team": "prog-raid", "skipped": "glory_not_live"})
        self.token.assert_not_called()
        self.assertEqual(self.posts, [])
        self.assertIsNone(self.store.state)

    def test_a_dry_run_writes_nothing(self):
        got = self.run_check(raid((101, 5)), None, {"mode": "glory", "dry": True}, live=False)
        self.assertEqual(got["raidersHolding"], {"First Thing": 10, "Second Thing": 0})
        self.assertEqual(got["wouldAnnounce"], ["First Thing"])
        self.assertEqual(sorted(got["missingCharacters"]), ["k", "l"])
        self.assertIsNone(self.store.state)
        self.assertEqual(self.posts, [])

    def test_the_first_live_run_seeds_and_says_nothing(self):
        got = self.run_check(raid((101, 5)), None)
        self.assertEqual(got["posted"], [])
        self.assertEqual(self.store.state["announced"], {101})
        self.assertEqual(len(self.store.state["held"][101]), 10)

    def test_a_tier_already_finished_seeds_the_meta_too(self):
        self.run_check(raid((101, 5), (102, 9)), None)
        self.assertEqual((self.store.state["announced"], self.posts), ({101, 102, 900, -900}, []))

    def test_backfill_announces_what_was_already_earned(self):
        got = self.run_check(raid((101, 5)), None, {"mode": "glory", "backfill": True})
        self.assertEqual(got["posted"], ["First Thing"])

    def test_one_raider_alone_is_not_the_guild(self):
        got = self.run_check(raid((101, 5), who="abc"), {"announced": set()})
        self.assertEqual((got["posted"], self.posts), ([], []))
        self.assertEqual(self.store.state["held"], {101: {"a": 5, "b": 5, "c": 5}})

    def test_the_card_waits_for_the_group_to_log_out(self):
        # Six are visible on the first run; the other four log out before the second.
        early = {**raid((101, 5), who="abcdef"),
                 **{n: profile(50 + i) for i, n in enumerate("ghij")}}
        self.run_check(early, {"announced": set()})
        self.assertEqual(self.posts, [])
        late = {**early, **{n: profile(90 + i, (101, 5)) for i, n in enumerate("ghij")}}
        got = self.run_check(late, self.store.state)
        self.assertEqual((got["read"], got["unchanged"]), (4, 6))
        self.assertEqual(self.titles(), ["Scrambled earned First Thing"])
        self.assertEqual(self.posts[0][0], {"bot_token": "t", "channel": "55"})
        again = self.run_check(late, self.store.state)
        self.assertEqual((again["posted"], again["read"], self.posts), ([], 0, []))

    def test_the_meta_follows_the_last_achievement(self):
        self.run_check(raid((102, 9), (101, 5)), {"announced": set()})
        self.assertEqual(self.titles(), ["Scrambled earned First Thing",
                                         "Scrambled earned Second Thing",
                                         "Scrambled completed Glory of the Test Raider"])
        self.assertEqual(self.store.state["announced"], {101, 102, 900})

    def test_the_meta_card_alone_also_reaches_general(self):
        home = dict(CFG, discord_guild_id=handler.SOCIAL_GENERAL_GUILD)
        general = {"bot_token": "t", "channel": handler.SOCIAL_GENERAL_CHANNEL}
        self.run_check(raid((102, 9), (101, 5)), {"announced": set()}, cfg=home)
        self.assertEqual([w["channel"] for w, _t in self.posts], ["55", "55", "55", general["channel"]])
        self.assertEqual(self.posts[-1], (general, "Scrambled completed Glory of the Test Raider"))
        self.assertEqual(self.store.state["announced"], {101, 102, 900, -900})
        self.run_check(raid((102, 9), (101, 5)), self.store.state, cfg=home)
        self.assertEqual(self.posts, [])

    def test_general_is_retried_without_repeating_the_team_card(self):
        home = dict(CFG, discord_guild_id=handler.SOCIAL_GENERAL_GUILD)

        def refuse_general(where, payload, **_kw):
            if where["channel"] == handler.SOCIAL_GENERAL_CHANNEL:
                raise handler.discord.DiscordError("no")
            self.posts.append((where, payload["embeds"][0]["title"]))
        chars = raid((101, 5), (102, 9))
        got = self.run_check(chars, {"announced": set()}, post=refuse_general, cfg=home)
        self.assertEqual((got["ok"], self.store.state["announced"]), (False, {101, 102, 900}))
        self.run_check(chars, self.store.state, cfg=home)
        self.assertEqual([w["channel"] for w, _t in self.posts], [handler.SOCIAL_GENERAL_CHANNEL])

    def test_a_tier_finished_before_the_bot_watched_never_reaches_general(self):
        home = dict(CFG, discord_guild_id=handler.SOCIAL_GENERAL_GUILD)
        self.run_check(raid((101, 5), (102, 9)), None, cfg=home)
        self.run_check(raid((101, 5), (102, 9)), self.store.state, cfg=home)
        self.assertEqual(self.posts, [])

    def test_a_failed_post_is_handed_back_and_retried_without_a_new_read(self):
        def refuse(*_a, **_kw):
            raise handler.discord.DiscordError("no")
        chars = raid((101, 5), (102, 9))
        got = self.run_check(chars, {"announced": set()}, post=refuse)
        self.assertEqual((got["ok"], got["failed"]), (False, ["First Thing"]))
        self.assertEqual(self.store.state["announced"], set())
        got = self.run_check(chars, self.store.state)
        self.assertEqual((got["read"], len(self.posts)), (0, 3))


if __name__ == "__main__":
    unittest.main()
