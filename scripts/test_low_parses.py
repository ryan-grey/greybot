"""Offline tests for the private grey-parse DM: what counts, how it is ordered, and that
it stays quiet and private."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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
        return entries, low_parses.message(entries, team_name="Scrambled", difficulty="Heroic",
                                           raid="The Venomous Abyss", night_text="Thursday Sept 17",
                                           page_url="https://raids.example/x/", **kw)

    def test_a_good_night_sends_nothing_at_all(self):
        _entries, payload = self.build([row("A", "The Twin Fangs", 80.0)])
        self.assertIsNone(payload)

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


class WiringTests(unittest.TestCase):
    """The handler's side: off by default, quiet on a good night, and private."""
    TIER = {"meta": {"encounters": ENCOUNTERS}, "label": "The Venomous Abyss"}

    def call(self, cfg, rows):
        import handler
        with patch.object(handler.recap_mod, "parse_rows", return_value=rows), \
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
        # There is no channel anywhere in this path.
        self.assertNotIn("channel", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main()
