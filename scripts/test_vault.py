"""Offline tests for the Tuesday vault and gear check: the week, the slots, gems and
enchants, who is reported, and that nothing is posted until the channel is configured."""
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import vault
import vault_card

START = datetime(2026, 9, 15, 15, tzinfo=timezone.utc)
END = datetime(2026, 9, 22, 15, tzinfo=timezone.utc)
MS = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)   # noqa: E731


def run(level, when, url=None):
    return {"mythic_level": level, "completed_at": when, "url": url or f"run/{level}/{when}"}


def member(uid, nick):
    return {"user": {"id": uid, "username": nick.lower()}, "nick": nick, "roles": ["prog"]}


def item(slot, enchant=None, sockets=(), item_class="Armor"):
    out = {"slot": {"type": slot}, "item_class": {"name": item_class}, "sockets": list(sockets)}
    if enchant is not None:
        out["enchantments"] = [{"display_string": enchant,
                                "enchantment_slot": {"type": "PERMANENT"}}]
    return out


TOP = "Enchanted: X |A:Professions-ChatIcon-Quality-12-Tier2:20:20|a"
RANK1 = "Enchanted: X |A:Professions-ChatIcon-Quality-12-Tier1:20:20|a"
GEMS = {1: ("RARE", 295), 2: ("RARE", 278), 3: ("UNCOMMON", 295), 4: ("EPIC", 295)}


def full_kit(**overrides):
    slots = {s: item(s, TOP) for s in vault.ENCHANT_SLOTS}
    slots["NECK"] = item("NECK", sockets=[{"item": {"id": 4}}])
    slots.update(overrides)
    return {"equipped_items": list(slots.values())}


class WeekTests(unittest.TestCase):
    def test_the_week_is_the_last_two_resets(self):
        # Wednesday: the week that reset yesterday.
        self.assertEqual(vault.week_window(datetime(2026, 9, 23, 12, tzinfo=timezone.utc)),
                         (START, END))
        # Tuesday before reset is still the week before; after it, this one.
        self.assertEqual(vault.week_window(datetime(2026, 9, 22, 14, 59, tzinfo=timezone.utc))[1],
                         datetime(2026, 9, 15, 15, tzinfo=timezone.utc))
        self.assertEqual(vault.week_window(datetime(2026, 9, 22, 16, tzinfo=timezone.utc))[1], END)

    def test_label(self):
        self.assertEqual(vault.week_label(START, END), "Sep 15 – 22")
        self.assertEqual(vault.week_label(datetime(2026, 9, 29, 15, tzinfo=timezone.utc),
                                          datetime(2026, 10, 6, 15, tzinfo=timezone.utc)),
                         "Sep 29 – Oct 6")


class MythicPlusTests(unittest.TestCase):
    def test_slots_are_the_first_fourth_and_eighth_best(self):
        levels = [15, 14, 13, 12, 11, 10, 9, 8]
        self.assertEqual(vault.mplus_slots(levels), (2, [15, 12, 8]))
        self.assertEqual(vault.mplus_slots([14, 13, 10, 10]), (2, [14, 10, None]))
        self.assertEqual(vault.mplus_slots([15, 15, 10]), (1, [15, None, None]))
        self.assertEqual(vault.mplus_slots([]), (0, [None, None, None]))

    def test_only_runs_inside_the_week_count_and_each_counts_once(self):
        profile = {
            "mythic_plus_previous_weekly_highest_level_runs": [
                run(15, "2026-09-16T05:00:00.000Z", "a"), run(12, "2026-09-10T05:00:00.000Z", "b")],
            "mythic_plus_weekly_highest_level_runs": [
                run(15, "2026-09-16T05:00:00.000Z", "a"), run(11, "2026-09-22T16:00:00.000Z", "c")]}
        self.assertEqual(vault.mplus_levels(profile, START, END), [15])

    def test_stale_raiderio_data_is_called_out(self):
        self.assertEqual(vault.stale_since({"last_crawled_at": "2026-09-20T02:00:00.000Z"}, END),
                         "2026-09-20")
        self.assertIsNone(vault.stale_since({"last_crawled_at": "2026-09-22T02:00:00.000Z"}, END))


class RaidTests(unittest.TestCase):
    def test_blizzard_counts_heroic_and_mythic_current_season_kills_in_the_week(self):
        def mode(kind, name, at):
            return {"difficulty": {"type": kind}, "progress": {"encounters": [
                {"encounter": {"name": name}, "last_kill_timestamp": at}]}}
        encounters = {"expansions": [
            {"expansion": {"name": "Current Season"}, "instances": [{"modes": [
                mode("HEROIC", "Sszorak", MS(2026, 9, 16, 1)),
                mode("MYTHIC", "The Twin Fangs", MS(2026, 9, 17, 1)),
                mode("NORMAL", "Ula'tek", MS(2026, 9, 17, 1)),
                mode("HEROIC", "The Coiled Altar", MS(2026, 9, 23, 1))]}]},
            {"expansion": {"name": "Midnight"}, "instances": [{"modes": [
                mode("HEROIC", "Vashnik the Malignant", MS(2026, 9, 16, 1))]}]}]}
        self.assertEqual(vault.blizzard_bosses(encounters, START, END), {"sszorak", "the twin fangs"})

    def test_logs_count_raid_kills_per_character_and_skip_dungeons(self):
        report = {"startTime": MS(2026, 9, 16, 0), "masterData": {"actors": [
            {"id": 1, "name": "Fûrry", "server": "Stormrage"}, {"id": 2, "name": "Kelsi"}]},
            "fights": [
                {"kill": True, "encounterID": 9, "difficulty": 4, "size": 20, "name": "Sszorak",
                 "endTime": 1000, "friendlyPlayers": [1, 2]},
                {"kill": False, "encounterID": 8, "difficulty": 4, "size": 20, "name": "Ula'tek",
                 "endTime": 2000, "friendlyPlayers": [1]},
                {"kill": True, "encounterID": 7, "difficulty": 4, "size": 5, "name": "Dungeon boss",
                 "endTime": 3000, "friendlyPlayers": [1]}]}
        got = vault.wcl_bosses([report], START, END)
        self.assertEqual(got, {"fûrry": {"sszorak"}, "kelsi": {"sszorak"}})
        self.assertEqual(vault.wcl_realms([report]), {"fûrry": "Stormrage"})

    def test_the_two_sources_do_not_double_count_a_subtitled_boss(self):
        self.assertEqual(len(vault.union_bosses({"dimensius the all devouring"}, {"dimensius"})), 1)
        self.assertEqual(vault.filled(5, vault.RAID_THRESHOLDS), 2)


class GearTests(unittest.TestCase):
    def test_a_full_kit_is_clean(self):
        self.assertEqual(vault.gear_check(full_kit(), GEMS, 295),
                         {"enchant_missing": [], "enchant_low": [], "gem_empty": [], "gem_low": []})

    def test_missing_and_low_rank_enchants(self):
        kit = full_kit(LEGS=item("LEGS"), FEET=item("FEET", RANK1))
        got = vault.gear_check(kit, GEMS, 295)
        self.assertEqual((got["enchant_missing"], got["enchant_low"]), (["Legs"], ["Feet"]))

    def test_the_off_hand_needs_one_only_when_it_is_a_weapon(self):
        shield = full_kit(OFF_HAND=item("OFF_HAND"))
        weapon = full_kit(OFF_HAND=item("OFF_HAND", item_class="Weapon"))
        self.assertEqual(vault.gear_check(shield, GEMS, 295)["enchant_missing"], [])
        self.assertEqual(vault.gear_check(weapon, GEMS, 295)["enchant_missing"], ["Off hand"])

    def test_a_runeforge_has_no_rank_and_is_never_low(self):
        kit = full_kit(MAIN_HAND=item("MAIN_HAND", "Enchanted: Rune of the Fallen Crusader"))
        self.assertEqual(vault.gear_check(kit, GEMS, 295)["enchant_low"], [])

    def test_empty_sockets_rank_one_gems_and_lesser_gems(self):
        kit = full_kit(FINGER_1=item("FINGER_1", TOP, [{}]),
                       FINGER_2=item("FINGER_2", TOP, [{"item": {"id": 2}}]),
                       NECK=item("NECK", sockets=[{"item": {"id": 3}}, {"item": {"id": 1}}]))
        got = vault.gear_check(kit, GEMS, vault.best_gem_level(GEMS))
        self.assertEqual(got["gem_empty"], ["Ring 1"])
        self.assertEqual(sorted(got["gem_low"]), ["Neck", "Ring 2"])

    def test_the_top_gem_rank_is_read_from_the_gems(self):
        self.assertEqual(vault.best_gem_level(GEMS), 295)
        self.assertEqual(vault.best_gem_level({3: ("UNCOMMON", 300)}), 0)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {
            "wholepie": {"name": "Wholepie", "realm": "Proudmoore", "class": "Paladin",
                         "active_spec_role": "TANK", "gear": {"item_level_equipped": 324},
                         "mythic_plus_previous_weekly_highest_level_runs": [
                             run(14, f"2026-09-1{d}T05:00:00.000Z") for d in range(6, 10)]},
            "deathbypie": {"name": "Deathbypie", "realm": "Proudmoore",
                           "gear": {"item_level_equipped": 318}},
            "visande": {"name": "Visande", "realm": "Proudmoore", "class": "Mage",
                        "active_spec_role": "DPS", "gear": {"item_level_equipped": 321}}}
        self.members = [member("1", "Pie"), member("2", "Vis"), member("3", "Nobody")]
        self.mapping = {"1": ["Deathbypie", "Wholepie"], "2": ["Visande"]}

    def rows(self, equipment=None):
        return vault.build(self.members, self.mapping, self.profiles, {},
                           {"wholepie": {"sszorak", "the twin fangs"}}, START, END,
                           equipment=equipment, gems=GEMS)

    def test_the_character_that_raided_is_reported_and_short_raiders_come_first(self):
        rows = self.rows()
        by = {r["member"]: r for r in rows}
        self.assertEqual(by["Pie"]["character"], "Wholepie")
        self.assertEqual((by["Pie"]["mplus_slots"], by["Pie"]["raid_slots"]), (2, 1))
        self.assertFalse(by["Pie"]["flagged"])
        self.assertTrue(by["Vis"]["flagged"])
        self.assertTrue(by["Nobody"]["flagged"])
        self.assertIsNone(by["Nobody"]["character"])
        self.assertEqual(rows[-1]["member"], "Pie")

    def test_gear_problems_put_an_otherwise_fine_raider_ahead(self):
        rows = self.rows({"wholepie": full_kit(LEGS=item("LEGS")), "visande": full_kit()})
        pie = next(r for r in rows if r["member"] == "Pie")
        self.assertEqual(pie["gear"]["enchant_missing"], ["Legs"])
        self.assertEqual(pie["issues"], 1)
        self.assertIsNone(next(r for r in rows if r["member"] == "Nobody")["gear"])

    def test_the_post_names_slots_and_pings_nobody(self):
        rows = self.rows({"wholepie": full_kit(LEGS=item("LEGS")), "visande": full_kit()})
        message = vault.payload(rows, START, END, "Smoobies", has_card=True)
        text = message["embeds"][0]["description"]
        self.assertEqual(message["allowed_mentions"], {"parse": []})
        self.assertIn("<@1> → Wholepie · enchants: missing Legs", text)
        self.assertIn("<@2> → Visande · M+ 0 of 4 at +10", text)
        self.assertIn("<@3> → no character on file", text)
        self.assertEqual(message["embeds"][0]["image"]["url"], "attachment://vault.png")

    def test_the_card_draws(self):
        rows = self.rows({"wholepie": full_kit(), "visande": full_kit()})
        png = vault_card.render(rows, START, END, "Smoobies", {})
        self.assertTrue(png and png.startswith(b"\x89PNG"))


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {
            "wholepie": {"name": "Wholepie", "realm": "Proudmoore", "class": "Paladin",
                         "active_spec_name": "Retribution", "active_spec_role": "DPS"},
            "thaydan": {"name": "Thaydan", "realm": "Proudmoore", "class": "Demon Hunter",
                        "active_spec_name": "Havoc", "active_spec_role": "DPS"}}
        self.members = [member("1", "Pie"), member("2", "Straightish")]
        self.mapping = {"1": ["Wholepie"], "2": ["Thaydan"]}
        self.kits = {"wholepie": full_kit(MAIN_HAND=item("MAIN_HAND")), "thaydan": full_kit()}

    def rows(self, played, specs):
        rows = vault.build(self.members, self.mapping, self.profiles, {}, {}, START, END,
                           equipment=self.kits, gems=GEMS, played=played, specs=specs)
        return {r["member"]: r for r in rows}

    def test_the_raid_spec_is_the_one_with_the_most_pulls_across_reports(self):
        details = [
            {"tanks": [{"name": "Wholepie", "specs": [{"spec": "Protection", "count": 23}]}],
             "dps": [{"name": "Thaydan", "specs": [{"spec": "Havoc", "count": 2}]},
                     {"name": "Yòshi", "specs": [{"spec": "BeastMastery", "count": 5}]}]},
            {"tanks": [{"name": "Thaydan", "specs": [{"spec": "Vengeance", "count": 19}]}],
             "dps": [{"name": "Wholepie", "specs": [{"spec": "Retribution", "count": 1}]}]}]
        self.assertEqual(vault.raid_specs(details), {
            "wholepie": {"spec": "Protection", "role": "tank"},
            "thaydan": {"spec": "Vengeance", "role": "tank"},
            "yòshi": {"spec": "Beast Mastery", "role": "dps"}})

    def test_spec_roles(self):
        self.assertEqual([vault.spec_role(s) for s in ("Protection", "Holy", "Augmentation", "")],
                         ["tank", "healer", "dps", ""])

    def test_the_role_raided_wins_over_a_stale_raiderio_spec(self):
        played = {"wholepie": {"spec": "Protection", "role": "tank"}}
        pie = self.rows(played, {"wholepie": "Protection"})["Pie"]
        self.assertEqual((pie["role"], pie["spec"]), ("tank", "Protection"))
        self.assertEqual(pie["gear"]["enchant_missing"], ["Weapon"])
        self.assertNotIn("off_spec", pie)

    def test_gear_worn_for_another_role_is_marked_not_graded(self):
        played = {"thaydan": {"spec": "Vengeance", "role": "tank"}}
        rows = self.rows(played, {"thaydan": "Havoc"})
        dh = rows["Straightish"]
        self.assertEqual(dh["role"], "tank")
        self.assertIsNone(dh["gear"])
        self.assertEqual(dh["off_spec"], {"now": "Havoc", "raids": "Vengeance"})
        text = vault.payload(list(rows.values()), START, END, "Smoobies", True)
        self.assertIn("Thaydan · M+ 0 of 4 at +10 · gear not checked: logged out as Havoc, "
                      "raids Vengeance", text["embeds"][0]["description"])
        png = vault_card.render(list(rows.values()), START, END, "Smoobies", {})
        self.assertTrue(png and png.startswith(b"\x89PNG"))

    def test_without_a_live_spec_nothing_is_called_off_spec(self):
        played = {"thaydan": {"spec": "Vengeance", "role": "tank"}}
        dh = self.rows(played, {})["Straightish"]
        self.assertNotIn("off_spec", dh)
        self.assertEqual(dh["role"], "tank")

    def test_with_no_logs_the_live_spec_sets_the_role(self):
        self.assertEqual(self.rows({}, {"wholepie": "Protection"})["Pie"]["role"], "tank")
        self.assertEqual(self.rows({}, {})["Pie"]["role"], "dps")


class GateTests(unittest.TestCase):
    def test_nothing_runs_until_the_channel_is_configured(self):
        import handler
        with patch.object(handler, "tenant_configs") as tenants, \
                patch.object(handler.discord, "post_to") as post:
            got = handler.vault_week({"mode": "vault"}, {"vault_channel": ""},
                                     datetime(2026, 9, 22, 16, tzinfo=timezone.utc))
        self.assertEqual(got, {"ok": True, "skipped": "vault_disabled"})
        tenants.assert_not_called()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
