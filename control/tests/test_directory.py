import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from greybot_control.directory import Directory
from greybot_control.discord_api import Unavailable
from greybot_control.store import Store


class DirectoryTests(unittest.TestCase):
    def test_names_cache_and_deleted_labels_do_not_change_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            store.append("event", "10", "CHANNEL_DELETE", "", {"id": "30"})
            store.save_profile("10", "50", {"id": "50", "name": "Former member", "avatar_url": ""})
            class API:
                deleted = False
                unavailable = False
                calls = 0
                async def request(self, method, path):
                    self.calls += 1
                    if self.unavailable:
                        raise Unavailable("Offline")
                    if path.endswith("/channels"):
                        return [] if self.deleted else [{"id": "30", "name": "general", "type": 0}]
                    if path.endswith("/roles"):
                        return [{"id": "40", "name": "Raiders"}]
                    if "/members?" in path:
                        return [{"nick": "Server nickname", "user": {"id": "20", "username": "handle"}}]
                    return {"id": "10", "name": "Test server"}
            api = API()
            directory = Directory(SimpleNamespace(guild_id="10"), store, api)
            first = asyncio.run(directory.get())
            self.assertEqual(first["members"][0]["name"], "Server nickname")
            self.assertTrue(first["members"][0]["avatar_url"].startswith("https://cdn.discordapp.com/"))
            self.assertFalse(next(m for m in first["members"] if m["id"] == "50")["active"])
            calls = api.calls
            self.assertEqual(asyncio.run(directory.get()), first)
            self.assertEqual(calls, api.calls)
            with store.connection() as db:
                db.execute("UPDATE display_directory SET checked=0")
            api.deleted = True
            second = asyncio.run(directory.get())
            self.assertEqual(second["channels"][0]["name"], "general")
            self.assertFalse(second["channels"][0]["active"])
            with store.connection() as db:
                db.execute("UPDATE display_directory SET checked=0")
            api.unavailable = True
            self.assertTrue(asyncio.run(directory.get())["stale"])
            self.assertTrue(store.verify())
            self.assertEqual(store.events("10")[0]["payload"], '{"id":"30"}')

    def test_pagination_keeps_all_members_and_guild_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            class API:
                paths = []
                async def request(self, method, path):
                    self.paths.append(path)
                    if path.endswith("/channels") or path.endswith("/roles"):
                        return []
                    if "after=0" in path:
                        return [{"user": {"id": str(n), "username": f"Member {n}"}} for n in range(1, 1001)]
                    if "after=1000" in path:
                        return [{"user": {"id": "1001", "username": "Last member"}}]
                    return {"id": "10", "name": "Test server"}
            api = API()
            result = asyncio.run(Directory(SimpleNamespace(guild_id="10"), store, api).get())
            self.assertEqual(len(result["members"]), 1001)
            self.assertTrue(all(path.startswith("/guilds/10") for path in api.paths))
