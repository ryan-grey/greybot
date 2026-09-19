"""Offline tests for the private grey-parse DM: what counts, how it is ordered, and that
it stays quiet and private."""
import base64
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# The smallest valid PNG, so icon fetching can be exercised without a network or a fixture.
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import low_parses

# Raider.IO's ordered encounter list for the tier; the boss number is a position in it.
ENCOUNTERS = ["The Twin Fangs", "Nek'zali the Soulcoiler", "Nymrissa Wavecaller", "The Coiled Altar"]


def row(name, boss, percent, spec="Fury", cls="Warrior"):
    return {"key": name.lower(), "name": name, "server": "Proudmoore", "percent": percent,
            "boss": boss, "role": "dps", "spec": spec, "class": cls}


class CollectTests(unittest.TestCase):
    def test_only_grey_counts_and_green_is_left_alone(self):
        rows = [row("Deathbrewst", "The Coiled Altar", 8.0), row("Kelsi", "The Coiled Altar", 24.9),
                row("Mograin", "The Coiled Altar", 25.0), row("Visande", "The Coiled Altar", 47.0)]
        got = [e["name"] for e in low_parses.collect(rows, ENCOUNTERS)]
        self.assertEqual(got, ["Deathbrewst", "Kelsi"])   # 25.0 is not below 25

    def test_the_threshold_moves_without_touching_the_code(self):
        rows = [row("Kelsi", "The Coiled Altar", 30.0)]
        self.assertEqual(low_parses.collect(rows, ENCOUNTERS), [])
        self.assertEqual(len(low_parses.collect(rows, ENCOUNTERS, threshold=35)), 1)

    def test_bosses_are_numbered_by_tier_order_and_sorted_by_it(self):
        rows = [row("A", "The Coiled Altar", 10.0), row("B", "The Twin Fangs", 12.0)]
        got = low_parses.collect(rows, ENCOUNTERS)
        self.assertEqual([(e["name"], e["number"]) for e in got], [("B", 1), ("A", 4)])

    def test_worst_first_within_a_boss(self):
        rows = [row("A", "The Twin Fangs", 20.0), row("B", "The Twin Fangs", 3.0),
                row("C", "The Twin Fangs", 11.0)]
        self.assertEqual([e["name"] for e in low_parses.collect(rows, ENCOUNTERS)], ["B", "C", "A"])

    def test_one_raider_with_two_bad_bosses_is_two_lines(self):
        rows = [row("Kelsi", "The Twin Fangs", 9.0), row("Kelsi", "The Coiled Altar", 4.0)]
        self.assertEqual(len(low_parses.collect(rows, ENCOUNTERS)), 2)

    def test_a_boss_the_tier_list_does_not_name_keeps_its_name_and_sorts_last(self):
        rows = [row("A", "Some Warm-Up", 5.0), row("B", "The Twin Fangs", 20.0)]
        got = low_parses.collect(rows, ENCOUNTERS)
        self.assertEqual([(e["name"], e["number"], e["boss"]) for e in got],
                         [("B", 1, "The Twin Fangs"), ("A", None, "Some Warm-Up")])

    def test_capitalisation_between_the_two_apis_still_matches(self):
        self.assertEqual(low_parses.collect([row("A", "the coiled ALTAR", 5.0)], ENCOUNTERS)[0]["number"], 4)

    def test_a_missing_percent_is_not_a_zero(self):
        # A raider whose parse did not rank has no number; it must not read as the worst
        # parse of the night.
        self.assertEqual(low_parses.collect([row("A", "The Twin Fangs", None)], ENCOUNTERS), [])


class MessageTests(unittest.TestCase):
    def build(self, rows, **kw):
        entries = low_parses.collect(rows, ENCOUNTERS)
        payload, attachment = low_parses.message(
            entries, team_name="Scrambled", difficulty="Heroic", raid="The Venomous Abyss",
            night_text="Thursday Sept 17", page_url="https://raids.example/x/", **kw)
        self.attachment = attachment
        return entries, payload

    def test_a_good_night_sends_nothing_at_all(self):
        _entries, payload = self.build([row("A", "The Twin Fangs", 80.0)])
        self.assertIsNone(payload)
        self.assertIsNone(self.attachment)

    def test_the_message_carries_every_field_asked_for(self):
        rows = [row("Deathbrewst", "The Coiled Altar", 8.0, spec="Frost", cls="DeathKnight"),
                row("Kelsi", "The Twin Fangs", 19.4)]
        entries, payload = self.build(rows)
        body = payload["embeds"][0]["description"]
        self.assertIn("Boss 1 · The Twin Fangs", body)      # boss name and number
        self.assertIn("Boss 4 · The Coiled Altar", body)
        self.assertIn("Heroic", payload["embeds"][0]["description"])   # difficulty
        self.assertIn("Kelsi", body)                                    # character
        self.assertIn("19.4%", body)                                    # parse %
        self.assertIn("8.0%", body)
        self.assertIn("(Frost)", body)
        self.assertEqual((len(entries), payload["allowed_mentions"]), (2, {"parse": []}))

    def test_it_counts_parses_and_people_separately(self):
        rows = [row("Kelsi", "The Twin Fangs", 9.0), row("Kelsi", "The Coiled Altar", 4.0)]
        _entries, payload = self.build(rows)
        self.assertIn("2 parses under 25% from 1 raider.", payload["embeds"][0]["description"])

    def test_a_pathological_night_is_truncated_rather_than_rejected_by_discord(self):
        rows = [row(f"R{i}", "The Twin Fangs", float(i % 25)) for i in range(200)]
        entries, payload = self.build(rows)
        body = payload["embeds"][0]["description"]
        self.assertTrue(len(entries) > low_parses.MAX_LINES and "and 160 more" in body)
        self.assertLess(len(json.dumps(payload)), 2000)

    def test_nothing_in_it_can_ping_anyone(self):
        rows = [row("@everyone", "The Twin Fangs", 1.0)]
        _entries, payload = self.build(rows)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})


class CardTests(unittest.TestCase):
    """The drawn card: the three things Discord text cannot show."""
    def test_the_card_draws_and_asks_only_for_the_specs_on_it(self):
        import low_parse_card
        from PIL import Image
        rows = [row("Deathbrewst", "The Coiled Altar", 8.0, spec="Frost", cls="DeathKnight"),
                row("Kelsi", "The Coiled Altar", 11.0, spec="Mistweaver", cls="Monk"),
                row("Twin", "The Twin Fangs", 4.0, spec="Frost", cls="DeathKnight")]
        entries = low_parses.collect(rows, ENCOUNTERS)
        asked = []

        class Answer(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(request, timeout):
            asked.append(request.full_url)
            return Answer(PIXEL)

        icons = low_parse_card.fetch_icons(entries, opener=opener)
        self.assertEqual(len(asked), 2)          # two distinct specs, three rows
        png = low_parse_card.render(entries, team_name="Scrambled", difficulty="Heroic",
                                    raid="The Venomous Abyss", night_text="Thursday Sept 17",
                                    icons=icons)
        self.assertEqual(Image.open(io.BytesIO(png)).size[0], low_parse_card.WIDTH * 2)

    def test_an_icon_that_will_not_load_costs_a_row_nothing(self):
        import low_parse_card
        entries = low_parses.collect([row("A", "The Twin Fangs", 5.0)], ENCOUNTERS)
        self.assertTrue(low_parse_card.render(entries, icons={("Warrior", "Fury"): b"not a png"}))
        self.assertTrue(low_parse_card.render(entries, icons={}))

    def test_a_card_never_raises_and_an_empty_night_draws_nothing(self):
        import low_parse_card
        self.assertIsNone(low_parse_card.render([]))
        self.assertIsNone(low_parse_card.render([{"bad": "shape"}]))

    def test_every_class_and_spec_the_raid_can_field_has_artwork(self):
        import spec_icons
        self.assertEqual(len({k.split("|")[0] for k in spec_icons.ICONS}), 13)
        # Warcraft Logs' spelling, the template's abbreviations, and the disambiguating
        # digits all have to land on the same icon.
        self.assertTrue(spec_icons.emoji_id("DeathKnight", "Frost"))
        self.assertTrue(spec_icons.emoji_id("DK", "Frost1"))
        self.assertTrue(spec_icons.emoji_id("demonhunter", "havoc"))
        self.assertTrue(spec_icons.emoji_id("Hunter", "Beast Mastery"))
        self.assertNotEqual(spec_icons.emoji_id("Mage", "Frost"),
                            spec_icons.emoji_id("DeathKnight", "Frost"))
        self.assertIsNone(spec_icons.emoji_id("Warrior", ""))
        self.assertIsNone(spec_icons.emoji_id("Tinker", "Sprocket"))
        self.assertTrue(spec_icons.url("Warrior", "Fury").startswith("https://cdn.discordapp.com/emojis/"))

    def test_the_card_is_attached_to_the_message_not_linked_from_a_public_bucket(self):
        entries = low_parses.collect([row("A", "The Twin Fangs", 5.0)], ENCOUNTERS)
        payload, attachment = low_parses.message(
            entries, team_name="Scrambled", difficulty="Heroic", raid="R",
            night_text="Thursday", card=b"\x89PNG-pretend")
        self.assertEqual(attachment, (low_parses.CARD_NAME, b"\x89PNG-pretend"))
        self.assertEqual(payload["embeds"][0]["image"]["url"], "attachment://" + low_parses.CARD_NAME)
        self.assertNotIn("raids.ryangrey.dev", json.dumps(payload))
        self.assertNotIn("s3", json.dumps(payload).lower())

    def test_without_a_card_the_same_list_still_goes_as_text(self):
        entries = low_parses.collect([row("A", "The Twin Fangs", 5.0)], ENCOUNTERS)
        payload, attachment = low_parses.message(entries, team_name="S", difficulty="Heroic",
                                                 raid="R", night_text="Thursday", card=None)
        self.assertIsNone(attachment)
        self.assertIn("A", payload["embeds"][0]["description"])
        self.assertNotIn("image", payload["embeds"][0])


class WiringTests(unittest.TestCase):
    """The handler's side: off by default, quiet on a good night, and private."""
    TIER = {"meta": {"encounters": ENCOUNTERS}, "label": "The Venomous Abyss"}

    def call(self, cfg, rows, card=b"\x89PNG"):
        import handler
        with patch.object(handler.recap_mod, "parse_rows", return_value=rows), \
             patch.object(handler.low_parse_card, "render", return_value=card), \
             patch.object(handler.discord, "dm_to") as dm:
            sent = handler.send_low_parses(cfg, [], set(), "heroic", self.TIER, "Heroic",
                                           "Thursday Sept 17", "https://raids.example/x/")
        return sent, dm

    def test_unconfigured_sends_nothing_and_does_not_even_look(self):
        sent, dm = self.call({"bot_token": "t", "low_parse_max": 25.0},
                             [row("A", "The Twin Fangs", 2.0)])
        self.assertEqual((sent, dm.call_count), (False, 0))

    def test_a_good_night_stays_quiet_even_when_configured(self):
        sent, dm = self.call({"bot_token": "t", "low_parse_dm": "42", "low_parse_max": 25.0},
                             [row("A", "The Twin Fangs", 90.0)])
        self.assertEqual((sent, dm.call_count), (False, 0))

    def test_a_grey_night_goes_to_one_person_by_direct_message(self):
        sent, dm = self.call({"bot_token": "t", "low_parse_dm": "42", "low_parse_max": 25.0,
                              "guild_name": "Scrambled"}, [row("A", "The Twin Fangs", 2.0)])
        self.assertTrue(sent)
        token, who, payload = dm.call_args[0]
        self.assertEqual((token, who), ("t", "42"))
        self.assertEqual(dm.call_args[1]["attachment"], (low_parses.CARD_NAME, b"\x89PNG"))
        # There is no channel anywhere in this path.
        self.assertNotIn("channel", json.dumps(payload).lower())

    def test_a_card_that_would_not_draw_still_sends_the_list(self):
        sent, dm = self.call({"bot_token": "t", "low_parse_dm": "42", "low_parse_max": 25.0},
                             [row("A", "The Twin Fangs", 2.0)], card=None)
        self.assertTrue(sent)
        self.assertIsNone(dm.call_args[1]["attachment"])
        self.assertIn("A", dm.call_args[0][2]["embeds"][0]["description"])


class UploadTests(unittest.TestCase):
    def test_the_multipart_body_names_the_same_file_the_embed_points_at(self):
        import discord as dis
        payload = {"embeds": [{"image": {"url": "attachment://grey-parses.png"}}]}
        body, content_type = dis._multipart(payload, "grey-parses.png", b"\x89PNGbytes")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=")[1]
        self.assertIn(b'name="files[0]"; filename="grey-parses.png"', body)
        self.assertIn(b'"attachments": [{"id": 0, "filename": "grey-parses.png"}]', body)
        self.assertIn(b"\x89PNGbytes", body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))


if __name__ == "__main__":
    unittest.main()
