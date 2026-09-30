"""Independent provider counts: identity, reset boundaries, duplicates and outages."""
import sys
from pathlib import Path
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_vault import START, END, MS, member, run
import vault
import vault_sources as sources

PROFILE = {"name": "Example", "realm": "Proudmoore"}


def report(at=None, level=10, **changes):
    fight = {"id": 1, "encounterID": 100, "endTime": 1000, "keystoneLevel": level,
             "kill": True, "inProgress": False, "keystoneTime": 1_800_000,
             "keystoneBonus": 0, "friendlyPlayers": [1], **changes}
    return {"startTime": at or MS(2026, 9, 20), "masterData": {"actors": [
        {"id": 1, "name": "Example", "server": "Proudmoore"}]}, "fights": [fight]}


class SelectionTests(unittest.TestCase):
    def test_each_source_can_win_and_qualifying_slots_beat_higher_keys(self):
        for winner in sources.SOURCES:
            candidates = {name: [18] * 7 for name in sources.SOURCES}
            candidates[winner] = [10] * 8
            self.assertEqual(sources.best_source(candidates), (winner, [10] * 8))

    def test_no_combining_providers_to_invent_a_slot(self):
        self.assertEqual(sources.best_source({s: [10] * 3 for s in sources.SOURCES}),
                         ("Raider.IO", [10] * 3))

    def test_ties_and_unavailable_sources(self):
        self.assertEqual(sources.best_source({"Raider.IO": [20] * 4, "Blizzard": [10] * 7})[0],
                         "Blizzard")
        self.assertEqual(sources.best_source({s: None for s in sources.SOURCES}), (None, []))
        self.assertEqual(sources.best_source({"Raider.IO": None, "Blizzard": []}), ("Blizzard", []))

    def test_build_and_message_use_only_the_winners_slots_and_source(self):
        profile = {**PROFILE, "last_crawled_at": "2026-09-18T00:00:00Z",
                   "mythic_plus_previous_weekly_highest_level_runs": [
                       run(18, f"2026-09-20T0{i}:00:00Z") for i in range(4)]}
        rows = vault.build([member("1", "Test")], {"1": ["Example"]}, {"example": profile},
                           {}, {}, START, END, key_sources={"example": {
                               "Blizzard": [10] * 8, "Warcraft Logs": [15] * 7}})
        self.assertEqual(rows[0]["mplus_source"], "Blizzard")
        self.assertEqual(rows[0]["mplus_levels"], [10, 10, 10])
        self.assertIsNone(rows[0]["seen"])  # Do not attach RIO staleness to Blizzard.
        text = vault.payload(rows, START, END, "Test", False)["embeds"][0]["description"]
        self.assertIn("3/3 slots at +10 (8 qualifying runs seen) · Blizzard", text)
        self.assertNotIn("Raider.IO", text)
        self.assertNotIn("Warcraft Logs", text)

    def test_raiderio_failure_does_not_hide_other_providers(self):
        def unavailable(*args):
            raise vault.raiderio.RaiderIOError("unavailable")
        profiles = vault.fetch_profiles(["Example"], {}, {}, "us", "Proudmoore", get=unavailable)
        rows = vault.build([member("1", "Test")], {"1": ["Example"]}, profiles, {}, {}, START, END,
                           key_sources={"example": {"Warcraft Logs": [10] * 8}})
        self.assertEqual(rows[0]["mplus_source"], "Warcraft Logs")
        self.assertEqual(rows[0]["mplus_slots"], 3)
        self.assertIsNone(rows[0]["mplus_sources"]["Raider.IO"])


class BlizzardTests(unittest.TestCase):
    def test_reset_filter_dedup_repeats_and_untimed_runs(self):
        one = {"completed_timestamp": MS(2026, 9, 16), "keystone_level": 10,
               "dungeon": {"id": 1}, "is_completed_within_time": False}
        two = {**one, "completed_timestamp": MS(2026, 9, 17)}
        next_week = {**one, "completed_timestamp": int(END.timestamp() * 1000)}
        self.assertEqual(sources.blizzard_levels([{"best_runs": [one, two, next_week]},
                                                  {"best_runs": [one]}], START, END), [10, 10])

    def test_season_discovery_and_identity_validation(self):
        calls = []
        def get(token, path, **kwargs):
            calls.append(path)
            if path.endswith("/index"):
                return {"seasons": [{"id": 999}]}
            if path == "/data/wow/mythic-keystone/season/999":
                return {"start_timestamp": MS(2026, 9, 1)}
            return {"character": {"name": "Wrong", "realm": {"slug": "proudmoore"}},
                    "best_runs": [{"completed_timestamp": MS(2026, 9, 16),
                                   "keystone_level": 10, "dungeon": {"id": 1}}]}
        result = sources.fetch_blizzard({"example": PROFILE}, "token", get, START, END)
        self.assertIsNone(result["example"])
        self.assertIn("/profile/wow/character/proudmoore/example/mythic-keystone-profile/season/999", calls)


class WCLTests(unittest.TestCase):
    def test_completed_untimed_run_counts_but_incomplete_and_raid_do_not(self):
        records = [report(), report(level=0), report(kill=False), report(inProgress=True),
                   report(keystoneTime=None)]
        self.assertEqual(sources.wcl_levels(records, PROFILE, START, END), [10])

    def test_duplicate_uploads_do_not_create_extra_slots(self):
        records = [report(MS(2026, 9, 20) + offset) for offset in (0, 1000, 20_000, 40_000)]
        self.assertEqual(sources.wcl_levels(records, PROFILE, START, END), [10])
        records.append(report(MS(2026, 9, 20) + 3_600_000))
        self.assertEqual(sources.wcl_levels(records, PROFILE, START, END), [10, 10])
        copy = report(MS(2026, 9, 20) + 2000)
        copy["masterData"]["actors"].append({"id": 2, "name": "Extra", "server": "Proudmoore"})
        copy["fights"][0]["friendlyPlayers"].append(2)
        # A logger missing a party member must not make the same character's run unique.
        self.assertEqual(sources.wcl_levels(records + [copy], PROFILE, START, END), [10, 10])

    def test_other_realms_and_weeks_never_count(self):
        wrong = report()
        wrong["masterData"]["actors"][0]["server"] = "Stormrage"
        self.assertEqual(sources.wcl_levels([wrong, report(MS(2026, 9, 23))],
                                            PROFILE, START, END), [])

    def test_character_reports_page_and_shared_reports_are_fetched_once(self):
        calls = []
        def query(token, document, args):
            calls.append(args)
            if document == sources.CHARACTER_REPORTS:
                return {"characterData": {"character": {"recentReports": {
                    "has_more_pages": args["page"] == 1,
                    "data": [{"code": "shared", "startTime": MS(2026, 9, 20),
                              "endTime": MS(2026, 9, 21)}]}}}}
            return {"reportData": {"report": report()}}
        result = sources.fetch_wcl({"example": PROFILE, "other": {**PROFILE, "name": "Other"}},
                                   "token", START, END, query=query)
        self.assertEqual(result, {"example": [10], "other": []})
        self.assertEqual(sum("code" in args for args in calls), 1)
        self.assertEqual(sum(args.get("page") == 2 for args in calls), 2)

    def test_provider_outage_is_not_a_zero(self):
        def query(*args):
            raise RuntimeError("unavailable")
        self.assertEqual(sources.fetch_wcl({"example": PROFILE}, "token", START, END, query=query),
                         {"example": None})


class HandlerTests(unittest.TestCase):
    def test_wcl_and_raiderio_outages_still_allow_blizzard_in_a_dry_check(self):
        import handler
        cfg = {"wcl_client_id": "id", "wcl_client_secret": "secret",
               "blizzard_client_id": "id", "blizzard_client_secret": "secret",
               "discord_guild_id": "guild", "role_id": "role", "guild_region": "us",
               "guild_realm": "Proudmoore", "guild_name": "Test"}
        with ExitStack() as stack:
            def mock(obj, name, **kwargs):
                return stack.enter_context(patch.object(obj, name, **kwargs))
            mock(handler, "tenant_configs", return_value=[(SimpleNamespace(team=handler.VAULT_TEAM), cfg)])
            shared = {"1": ["Alt", "Example"]}
            mock(handler.store, "get_rollcall_setup", return_value={"members": shared})
            mock(handler.store, "get_vault_characters", return_value={"1": "Example"})
            mock(vault, "fetch_members", return_value=[member("1", "Test")])
            mock(handler.wcl, "get_token", side_effect=handler.wcl.WCLError("unavailable"))
            mock(handler.raiderio, "_get", side_effect=handler.raiderio.RaiderIOError("unavailable"))
            profiles = mock(vault, "fetch_profiles", return_value={"example": {**PROFILE, "rio_unavailable": True}})
            mock(handler.blizzard, "get_token", return_value="token")
            mock(sources, "fetch_blizzard", return_value={"example": [10] * 8})
            wcl_keys = mock(sources, "fetch_wcl")
            for name in ("fetch_encounters", "fetch_equipment", "fetch_specs", "fetch_gems"):
                mock(vault, name, return_value={})
            post = mock(handler.discord, "post_to")
            result = handler.vault_week({"dry": True}, cfg, END)
        self.assertEqual(result["rows"][0]["mplus_source"], "Blizzard")
        self.assertEqual(result["rows"][0]["mplus_slots"], 3)
        self.assertEqual(profiles.call_args.args[0], ["Example"])
        self.assertEqual(shared, {"1": ["Alt", "Example"]})
        post.assert_not_called()
        wcl_keys.assert_not_called()


if __name__ == "__main__":
    unittest.main()
