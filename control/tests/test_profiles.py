import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from greybot_control.profiles import display_profile, enrich
from greybot_control.store import Store
from greybot_control.discord_api import Denied


class Profiles(unittest.TestCase):
    def test_server_identity_precedes_global_identity(self):
        p = display_profile("10", {"id": "20", "username": "handle", "global_name": "Global", "avatar": "a" * 32},
                            {"nick": "Server nickname", "avatar": "b" * 32})
        self.assertEqual(p["name"], "Server nickname")
        self.assertIn("guilds/10/users/20/avatars/", p["avatar_url"])

    def test_fallbacks_and_untrusted_avatar(self):
        p = display_profile("10", {"id": "20", "username": "handle", "avatar": "https://evil.test/x"})
        self.assertEqual(p["name"], "handle")
        self.assertEqual(p["avatar_url"], "https://cdn.discordapp.com/embed/avatars/0.png")

    def test_cache_preserves_departed_member_and_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            event = store.append("e", "10", "GUILD_MEMBER_REMOVE", "20", {})
            store.save_profile("10", "20", {"id": "20", "name": "Former nickname", "avatar_url": ""})
            with store.connection() as db:
                db.execute("UPDATE member_profiles SET checked=0")
            class API:
                async def request(self, *args):
                    raise Denied("No longer a member")
            result = asyncio.run(enrich([event], SimpleNamespace(guild_id="10"), store, API()))
            self.assertEqual(result[0]["display_member"]["name"], "Former nickname")
            self.assertTrue(result[0]["display_member"]["last_known"])
            self.assertTrue(store.verify())
            self.assertNotIn("display_member", store.events("10")[0])

    def test_duplicate_users_resolved_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            class API:
                calls = 0
                async def request(self, *args):
                    self.calls += 1
                    return {"nick": "Nickname", "user": {"id": "20", "username": "handle"}}
            api = API()
            rows = [{"subject": "20"}, {"subject": "20"}]
            result = asyncio.run(enrich(rows, SimpleNamespace(guild_id="10"), store, api))
            self.assertEqual(api.calls, 1)
            self.assertEqual(result[1]["display_member"]["name"], "Nickname")
