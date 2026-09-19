"""Offline tests for the raid-night roll call: which kill, who is who, the claim, and the card."""
import hashlib
import hmac
import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import rollcall
import rollcall_card

TZ = "America/New_York"
NOW = datetime(2026, 9, 18, 2, 30, tzinfo=timezone.utc)          # Thursday 10:30 PM Eastern
MS = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)   # noqa: E731

LINEUP = [{"name": "Wholepie", "class": "Paladin", "server": "Proudmoore", "role": "tank"},
          {"name": "Fûrry", "class": "Paladin", "server": "Proudmoore", "role": "dps"},
          {"name": "Mograin", "class": "Paladin", "server": "Proudmoore", "role": "dps"},
          {"name": "Deathbrewst", "class": "DeathKnight", "server": "Proudmoore", "role": "dps"}]
VOICE = [{"id": "1", "name": "Pie", "avatar_url": ""}, {"id": "2", "name": "Fûrry", "avatar_url": ""},
         {"id": "3", "name": "Pete  (Mograin)", "avatar_url": ""}, {"id": "4", "name": "NotBrewst", "avatar_url": ""},
         {"id": "5", "name": "Justin", "avatar_url": ""}]


def kill(name, at, report="R1", start=None):
    return {"name": name, "encounterID": 9, "killedAtMs": at, "reportCode": report,
            "reportStartMs": start or at - 3600_000, "zoneName": "The Venomous Abyss"}


class WhichKillTests(unittest.TestCase):
    def test_earliest_kill_of_the_night_across_difficulties_and_reports(self):
        first, later = kill("Altar", MS(2026, 9, 18, 2, 15)), kill("Second", MS(2026, 9, 18, 2, 25))
        relog = kill("Third", MS(2026, 9, 18, 4, 20), report="R2", start=MS(2026, 9, 18, 4, 5))   # 12:05 AM relog
        found = rollcall.first_kills([("heroic", [later, relog]), ("normal", [first])], int(NOW.timestamp() * 1000) + 3 * 3600_000, TZ)
        self.assertEqual([(k["name"], k["difficulty"], k["night"]) for k in found], [("Altar", "normal", "2026-09-17")])

    def test_last_nights_kill_is_not_called_the_morning_after(self):
        old = kill("Altar", MS(2026, 9, 17, 2, 15))
        self.assertEqual(rollcall.first_kills([("heroic", [old])], int(NOW.timestamp() * 1000), TZ), [])
        self.assertEqual(len(rollcall.first_kills([("heroic", [old])], int(NOW.timestamp() * 1000), TZ, max_age_hours=72)), 1)


class WhoIsWhoTests(unittest.TestCase):
    def test_mapping_wins_and_guessing_only_fills_in_for_the_unmapped(self):
        members = {"4": ["Deathbrewst"], "5": ["Slimmjr"]}
        got = {m["id"]: (m["character"] or {}).get("name") for m in rollcall.assign(LINEUP, VOICE, members)}
        self.assertEqual(got, {"1": "Wholepie", "2": "Fûrry", "3": "Mograin", "4": "Deathbrewst", "5": None})

    def test_a_guess_never_takes_a_character_a_mapped_member_claims(self):
        voice = [{"id": "8", "name": "Mograin fan", "avatar_url": ""}, {"id": "9", "name": "Pete", "avatar_url": ""}]
        got = {m["id"]: (m["character"] or {}).get("name") for m in rollcall.assign(LINEUP, voice, {"9": ["Mograin"]})}
        self.assertEqual(got, {"8": None, "9": "Mograin"})

    def test_a_mapped_member_whose_characters_did_not_raid_is_not_guessed_into_the_kill(self):
        voice = [{"id": "7", "name": "Pie", "avatar_url": ""}]
        self.assertIsNone(rollcall.assign(LINEUP, voice, {"7": ["Someoneelse"]})[0]["character"])


class QuestionTests(unittest.TestCase):
    def test_the_question_is_signed_the_way_the_nas_verifies_it(self):
        seen = {}

        class Answer(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(request, timeout):
            seen["header"], seen["body"] = request.headers["X-greybot-signature"], request.data
            return Answer(b'{"channel":"Smoobies","members":[]}')

        self.assertEqual(rollcall.ask_voice("s3cret", "10", 5.5, opener=opener)["channel"], "Smoobies")
        parts = dict(p.split("=") for p in seen["header"].split(","))
        expected = hmac.new(b"s3cret", parts["t"].encode() + b"." + seen["body"], hashlib.sha256).hexdigest()
        self.assertEqual(parts["v1"], expected)
        self.assertEqual(json.loads(seen["body"]), {"channel_id": "10", "at": 5.5})

    def test_no_picture_falls_back_to_discords_default_and_other_hosts_are_never_fetched(self):
        self.assertTrue(rollcall.picture_url({"id": "100000000000000000", "avatar_url": ""}).startswith("https://cdn.discordapp.com/embed/avatars/"))
        self.assertIn("/embed/avatars/", rollcall.picture_url({"id": "1", "avatar_url": "https://evil.example/x.png"}))


class RunTests(unittest.TestCase):
    CFG = {"rollcall_secret": "s", "recap_page_bucket": "b", "recap_page_url": "https://raids.example"}
    SCOPE = SimpleNamespace(team=None, tenant="T")

    def run_it(self, voice, dry=False, setup=True, claim=True, lineup=LINEUP, live=True):
        calls = {"posts": [], "published": [], "released": [], "claimed": []}
        with patch.object(rollcall.store, "get_rollcall_setup", return_value={"voice_channel": "10", "members": {}, "label": "Smoobies", "live": live} if setup else None), \
             patch.object(rollcall.store, "claim_rollcall", side_effect=lambda s, n: calls["claimed"].append(n) or claim), \
             patch.object(rollcall.store, "release_rollcall", side_effect=lambda s, n: calls["released"].append(n)), \
             patch.object(rollcall.wcl, "kill_lineup", return_value=(lineup, None)), \
             patch.object(rollcall, "ask_voice", return_value={"channel": "Smoobies", "members": voice}), \
             patch.object(rollcall, "pictures", return_value={}):
            results = rollcall.run(self.CFG, self.SCOPE, "token", [("heroic", [kill("Altar", MS(2026, 9, 18, 2, 15))])], NOW, TZ,
                                   team_name="", destination={"channel": "c"},
                                   publish=lambda key, body: calls["published"].append(key) or "https://raids.example/" + key,
                                   post=lambda where, payload: calls["posts"].append(payload), dry=dry)
        return results, calls

    def test_posts_once_with_the_card_and_names_who_was_not_in_the_kill(self):
        results, calls = self.run_it(VOICE)
        self.assertEqual((len(calls["posts"]), calls["released"], results[0]["notInKill"]), (1, [], 2))
        payload = calls["posts"][0]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("In Discord but not in kill: NotBrewst, Justin", payload["embeds"][0]["description"])
        self.assertIn("In kill but not in Discord: Deathbrewst", payload["embeds"][0]["description"])
        # Members are described as being in Discord. How they were found is not the post's to say.
        self.assertNotIn("voice", json.dumps(payload).lower())
        self.assertTrue(payload["embeds"][0]["image"]["url"].startswith("https://raids.example/rollcall/guild/2026-09-17/"))

    def test_a_lost_claim_a_missing_setup_and_a_missing_secret_all_stay_silent(self):
        self.assertEqual(self.run_it(VOICE, claim=False)[1]["posts"], [])
        results, calls = self.run_it(VOICE, setup=False)
        self.assertEqual((results, calls["claimed"]), ([], []))
        with patch.dict(self.CFG, {"rollcall_secret": ""}):
            self.assertEqual(self.run_it(VOICE)[1]["claimed"], [])

    def test_a_log_that_is_not_ready_hands_the_night_back_for_the_next_poll(self):
        _results, calls = self.run_it(VOICE, lineup=[])
        self.assertEqual((calls["posts"], calls["released"]), ([], ["2026-09-17"]))

    def test_another_teams_night_is_decided_once_and_never_posted(self):
        _results, calls = self.run_it([{"id": "5", "name": "Justin", "avatar_url": ""}])
        self.assertEqual((calls["posts"], calls["released"], calls["claimed"]), ([], [], ["2026-09-17"]))

    def test_a_saved_setup_that_is_not_live_can_be_previewed_but_never_posts(self):
        _results, calls = self.run_it(VOICE, live=False)
        self.assertEqual((calls["posts"], calls["claimed"]), ([], []))
        results, calls = self.run_it(VOICE, live=False, dry=True)
        self.assertEqual((len(results), calls["posts"]), (1, []))

    def test_a_dry_run_publishes_a_preview_and_touches_nothing_else(self):
        results, calls = self.run_it(VOICE, dry=True)
        self.assertEqual((calls["posts"], calls["claimed"], calls["released"]), ([], [], []))
        self.assertTrue(calls["published"][0].startswith("rollcall/preview/guild/"))
        self.assertTrue(results[0]["dry"])

    def test_a_post_discord_may_have_accepted_is_never_retried(self):
        with patch.object(rollcall.store, "get_rollcall_setup", return_value={"voice_channel": "10", "members": {}, "label": "", "live": True}), \
             patch.object(rollcall.store, "claim_rollcall", return_value=True), \
             patch.object(rollcall.store, "release_rollcall") as release, \
             patch.object(rollcall.wcl, "kill_lineup", return_value=(LINEUP, None)), \
             patch.object(rollcall, "ask_voice", return_value={"channel": "Smoobies", "members": VOICE}), \
             patch.object(rollcall, "pictures", return_value={}):
            def post(where, payload):
                raise TimeoutError("no answer")
            rollcall.run(self.CFG, self.SCOPE, "t", [("heroic", [kill("Altar", MS(2026, 9, 18, 2, 15))])], NOW, TZ,
                         team_name="", destination={}, publish=lambda k, b: "u", post=post)
        release.assert_not_called()

    def test_a_reviewed_team_is_held_and_sent_to_the_reviewer_not_the_channel(self):
        held, dms, posts = [], [], []
        scope = SimpleNamespace(team="meers-raid", tenant="T")
        with patch.object(rollcall.store, "get_rollcall_setup", return_value={"voice_channel": "10", "members": {}, "label": "Meer's Raid", "live": True, "review": "42"}), \
             patch.object(rollcall.store, "claim_rollcall", return_value=True), \
             patch.object(rollcall.store, "release_rollcall") as release, \
             patch.object(rollcall.store, "put_rollcall_pending", side_effect=lambda s, n, p, at: held.append((n, p))), \
             patch.object(rollcall.wcl, "kill_lineup", return_value=(LINEUP, None)), \
             patch.object(rollcall, "ask_voice", return_value={"channel": "x", "members": VOICE}), \
             patch.object(rollcall, "pictures", return_value={}):
            rollcall.run(self.CFG, scope, "t", [("heroic", [kill("Altar", MS(2026, 9, 18, 2, 15))])], NOW, TZ,
                         team_name="Meer's Raid", destination={"channel": "c"}, publish=lambda k, b: "https://raids.example/" + k,
                         post=lambda w, p: posts.append(p), review=lambda user, p: dms.append((user, p)))
        self.assertEqual((posts, len(held), dms[0][0]), ([], 1, "42"))
        release.assert_not_called()
        buttons = dms[0][1]["components"][0]["components"]
        self.assertEqual([b["custom_id"] for b in buttons],
                         ["greybot:rollcall:post:meers-raid:2026-09-17", "greybot:rollcall:skip:meers-raid:2026-09-17"])
        # What is held is exactly the channel post: no buttons, no note to the reviewer.
        self.assertEqual((set(held[0][1]), held[0][1]["embeds"]), ({"allowed_mentions", "nonce", "enforce_nonce", "embeds"}, dms[0][1]["embeds"]))
        self.assertNotIn("voice", json.dumps(dms[0][1]).lower())


class ClickTests(unittest.TestCase):
    """Post and Skip arrive from a DM, so nothing about the request can be trusted but its signature
    (checked before this) and who pressed it."""
    def setUp(self):
        import handler
        self.handler = handler
        self.scope = SimpleNamespace(team="meers-raid", tenant="T")
        self.state = {"state": "pending", "payload": {"embeds": [{"title": "Roll call"}]}}
        self.posts = []
        patches = [patch.object(handler, "tenant_configs", return_value=[(self.scope, {"channel_id": "77", "bot_token": "b"})]),
                   patch.object(handler.store, "get_rollcall_setup", return_value={"review": "42"}),
                   patch.object(handler.store, "get_rollcall_pending", side_effect=lambda s, n: dict(self.state)),
                   patch.object(handler.store, "settle_rollcall_pending", side_effect=self.settle),
                   patch.object(handler.discord, "post_to", side_effect=lambda where, payload, **k: self.posts.append((where, payload)))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def settle(self, scope, night, state):
        if self.state["state"] != "pending":
            return False
        self.state["state"] = state
        return True

    def click(self, verb, user="42"):
        out = self.handler.rollcall_click({"data": {"custom_id": f"greybot:rollcall:{verb}:meers-raid:2026-09-17"}, "user": {"id": user}}, {})
        return json.loads(out["body"])

    def test_post_sends_the_held_card_to_the_teams_channel_once(self):
        first, second = self.click("post"), self.click("post")
        self.assertEqual((first["type"], first["data"]["components"], len(self.posts)), (7, [], 1))
        self.assertEqual((self.posts[0][0]["channel"], self.posts[0][1]), ("77", self.state["payload"]))
        self.assertIn("Already posted", second["data"]["content"])

    def test_skip_posts_nothing_and_cannot_be_undone_into_a_post(self):
        self.click("skip")
        self.click("post")
        self.assertEqual((self.posts, self.state["state"]), ([], "skipped"))

    def test_anyone_but_the_reviewer_is_refused_privately(self):
        answer = self.click("post", user="99")
        self.assertEqual((answer["type"], answer["data"]["flags"], self.posts, self.state["state"]), (4, 64, [], "pending"))


class CardTests(unittest.TestCase):
    def test_card_draws_with_and_without_leftovers_and_never_raises(self):
        from PIL import Image
        members = rollcall.assign(LINEUP, VOICE, {})
        png = rollcall_card.render("Smoobies", "The Coiled Altar", "Heroic The Venomous Abyss", LINEUP, members, {"1": b"not an image"})
        tall = Image.open(io.BytesIO(png)).size
        everyone = rollcall.assign(LINEUP, VOICE[:4], {"4": ["Deathbrewst"]})
        short = Image.open(io.BytesIO(rollcall_card.render("", "Boss", "", LINEUP, everyone))).size
        self.assertEqual((tall[0], short[0], tall[1] > short[1]), (880, 880, True))   # no leftovers, no bottom panels
        self.assertEqual(rollcall_card.render("", "Boss", "", [], [])[:4], b"\x89PNG")
        self.assertIsNone(rollcall_card.render("", "Boss", "", object(), []))

    def test_every_character_is_paired_with_its_member_and_the_rest_is_split_two_ways(self):
        rows, unclaimed, outside = rollcall_card.pair(LINEUP, rollcall.assign(LINEUP, VOICE, {}))
        self.assertEqual([(p["name"], m and m["name"]) for p, m in rows],
                         [("Wholepie", "Pie"), ("Fûrry", "Fûrry"), ("Mograin", "Pete  (Mograin)"), ("Deathbrewst", None)])
        self.assertEqual(([p["name"] for p in unclaimed], [m["name"] for m in outside]), (["Deathbrewst"], ["NotBrewst", "Justin"]))

    def test_the_public_wording_never_says_how_members_were_found(self):
        for text in (rollcall_card.NOT_IN_DISCORD, rollcall_card.NOT_IN_KILL):
            self.assertNotIn("voice", text.lower())


if __name__ == "__main__":
    unittest.main()
