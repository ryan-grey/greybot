import unittest
import asyncio
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from greybot_control.onboarding_gate import plan, review_members, VIEW
from greybot_control.onboarding_gate import snapshot, apply
from greybot_control.config import Config
from greybot_control.store import Store
from greybot_control.local_archive import LocalArchive
from greybot_control.discord_api import Denied, Unavailable
from greybot_control.mutes import effective_permissions


class GateAPI:
    def __init__(self, store):
        self.store, self.writes = store, []
        self.roles = [{"id": "1", "permissions": str(VIEW), "position": 0},
                      {"id": "2", "permissions": "0", "position": 1}]
        self.channels = [{"id": "3", "type": 0, "permission_overwrites": []},
                         {"id": "4", "type": 0, "permission_overwrites": []}]

    async def require_admin(self, actor):
        return {"permissions": 8, "position": 10}

    member_context = require_admin

    async def request(self, method, path, body=None, **kwargs):
        if method != "GET":
            with self.store.connection() as db:
                assert not db.execute("SELECT 1 FROM events e LEFT JOIN receipts r ON e.seq=r.seq "
                                      "WHERE e.kind LIKE 'ONBOARDING_GATE_%' AND r.seq IS NULL").fetchone(), "Write happened before archive confirmation"
            self.writes.append((method, path))
            if method == "PATCH":
                next(r for r in self.roles if r["id"] == path.rsplit("/", 1)[-1])["permissions"] = body["permissions"]
            else:
                channel = next(c for c in self.channels if c["id"] == path.split("/")[2])
                channel["permission_overwrites"] = [{"id": "1", **body}]
            return None
        if path.endswith("/roles"):
            return deepcopy(self.roles)
        if path.endswith("/channels"):
            return deepcopy(self.channels)
        if "/members?" in path:
            return [{"user": {"id": "8"}, "roles": ["2"]}]
        if path.startswith("/channels/"):
            return deepcopy(next(c for c in self.channels if c["id"] == path.rsplit("/", 1)[-1]))
        return {"id": "1", "owner_id": "7"}


class GateTests(unittest.TestCase):
    def test_live_collector_does_not_block_archived_cutover_but_missing_receipt_does(self):
        class BusyArchive(LocalArchive):
            counter = 0
            def flush(self, store):
                super().flush(store)
                self.counter += 1
                store.append("background-" + str(self.counter), "1", "MESSAGE_CREATE", "8", {})
        class StalledArchive:
            def flush(self, store):
                pass
        async def run(root, busy):
            cfg = Config("1", "9", "test", "test", "http://localhost", root / "state", enforce=True, archive_dir=root / "archive")
            store = Store(root / "state" / "control.sqlite3")
            values = store.settings("1")["values"]
            store.save_settings("1", "7", 0, {**values, "verification_enabled": True,
                "verification_role": "2", "welcome_channel": "3", "welcome_enabled": True})
            # More than one archive batch must also drain through the plan.
            for i in range(105):
                store.append("older-" + str(i), "1", "MESSAGE_CREATE", "8", {})
            api = GateAPI(store)
            cfg.archive_dir.mkdir()
            archive = BusyArchive(cfg.archive_dir) if busy else StalledArchive()
            preview = await snapshot(cfg, store, api)
            if not busy:
                with self.assertRaises(Unavailable):
                    await apply(cfg, store, api, archive, "7", preview["revision"])
                self.assertEqual(api.writes, [])
                return
            result = await apply(cfg, store, api, archive, "7", preview["revision"])
            self.assertEqual(result["applied"], 3)
            self.assertTrue(store.pending(1))  # New unrelated events may remain.
            self.assertTrue(store.verify())
            self.assertFalse((await snapshot(cfg, store, api))["changes"])
        with patch.dict("os.environ", {"GREYBOT_TURNSTILE_SITE_KEY": "test",
                "GREYBOT_TURNSTILE_SECRET": "test", "GREYBOT_DISCORD_PUBLIC_KEY": "test"}):
            for busy in (True, False):
                with self.subTest(busy=busy), tempfile.TemporaryDirectory() as temp:
                    asyncio.run(run(Path(temp), busy))

    def test_only_designated_server_rules_is_public_before_verification(self):
        roles = [{"id": "1", "permissions": str(VIEW), "position": 0},
                 {"id": "2", "permissions": "0", "position": 1}]
        channels = [{"id": "3", "type": 0, "name": "bots", "permission_overwrites": []},
                    {"id": "4", "type": 0, "name": "rules", "permission_overwrites": [
                        {"id": "1", "type": 0, "allow": str(VIEW), "deny": str(1 << 11)}]},
                    {"id": "5", "type": 0, "name": "rules", "permission_overwrites": [
                        {"id": "1", "type": 0, "allow": "0", "deny": str(VIEW)}]},
                    {"id": "6", "type": 0, "name": "general", "permission_overwrites": []}]
        result = plan("1", roles, channels, "2", "3", "4")
        member = {"roles": [], "user": {"id": "8"}}
        access = {c["id"]: effective_permissions("1", result["roles"], member, c) for c in result["channels"]}
        self.assertEqual([c for c, perms in access.items() if perms & VIEW], ["3", "4"])
        self.assertFalse(access["4"] & (1 << 11))
        self.assertEqual(plan("1", result["roles"], result["channels"], "2", "3", "4")["changes"], [])
        with self.assertRaises(Denied):
            plan("1", roles, channels, "2", "3", "missing")

    def test_cutover_archives_before_writes_and_rejects_stale_review(self):
        async def run(root):
            cfg = Config("1", "9", "test", "test", "http://localhost", root / "state", enforce=True, archive_dir=root / "archive")
            store = Store(root / "state" / "control.sqlite3")
            values = store.settings("1")["values"]
            store.save_settings("1", "7", 0, {**values, "verification_enabled": True,
                "verification_role": "2", "welcome_channel": "3", "welcome_enabled": True})
            api = GateAPI(store)
            cfg.archive_dir.mkdir()
            archive = LocalArchive(cfg.archive_dir)
            preview = await snapshot(cfg, store, api)
            api.roles[0]["permissions"] = str(VIEW | (1 << 11))
            with self.assertRaises(Denied):
                await apply(cfg, store, api, archive, "7", preview["revision"])
            self.assertEqual(api.writes, [])
            api.roles[0]["permissions"] = str(VIEW)
            result = await apply(cfg, store, api, archive, "7", preview["revision"])
            self.assertEqual(result["applied"], 3)
            self.assertEqual(api.writes[-1], ("PATCH", "/guilds/1/roles/1"))
            self.assertFalse((await snapshot(cfg, store, api))["changes"])
            self.assertFalse(store.pending(1))
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {
            "GREYBOT_TURNSTILE_SITE_KEY": "test", "GREYBOT_TURNSTILE_SECRET": "test",
            "GREYBOT_DISCORD_PUBLIC_KEY": "test"}):
            asyncio.run(run(Path(temp)))

    def test_existing_members_only_grandfathered_without_new_permissions(self):
        roles = [{"id": "1", "permissions": str(VIEW), "position": 0},
                 {"id": "2", "permissions": "0", "position": 1}]
        channels = [{"id": "3", "type": 0, "permission_overwrites": []},
                    {"id": "4", "type": 0, "permission_overwrites": []}]
        members = [{"roles": [], "user": {"id": "8"}},
                   {"roles": [], "pending": True, "user": {"id": "9"}}]
        planned = plan("1", roles, channels, "2", "3")
        result = review_members("1", roles, channels, members, "7", "2", planned)
        self.assertEqual(result["starter_role_additions"], ["8"])
        self.assertEqual([r["user_id"] for r in result["blocked"]], ["9"])
        roles[1]["permissions"] = str(1 << 28)
        planned = plan("1", roles, channels, "2", "3")
        result = review_members("1", roles, channels, members, "7", "2", planned)
        self.assertEqual(result["starter_role_additions"], [])
        self.assertEqual(len(result["blocked"]), 2)

    def test_only_welcome_visible_and_verified_private_access_not_expanded(self):
        roles = [{"id": "1", "permissions": str(VIEW), "position": 0},
                 {"id": "2", "permissions": "0", "position": 1}]
        channels = [{"id": "3", "type": 0, "permission_overwrites": []},
                    {"id": "4", "type": 0, "permission_overwrites": []},
                    {"id": "5", "type": 0, "permission_overwrites": [
                        {"id": "1", "type": 0, "allow": "0", "deny": str(VIEW)}]}]
        result = plan("1", roles, channels, "2", "3")
        newcomer = {"roles": [], "user": {"id": "8"}}
        member = {"roles": ["2"], "user": {"id": "8"}}
        self.assertEqual([bool(effective_permissions("1", result["roles"], newcomer, c) & VIEW) for c in result["channels"]], [True, False, False])
        self.assertEqual([bool(effective_permissions("1", result["roles"], member, c) & VIEW) for c in result["channels"]], [True, True, False])
        self.assertEqual(plan("1", result["roles"], result["channels"], "2", "3")["changes"], [])
        self.assertEqual(roles[0]["permissions"], str(VIEW))

    def test_keeps_unrelated_overwrite_bits(self):
        roles = [{"id": "1", "permissions": str(VIEW), "position": 0}, {"id": "2", "permissions": "0", "position": 1}]
        channels = [{"id": "3", "type": 0, "permission_overwrites": [
            {"id": "1", "type": 0, "allow": str(1 << 11), "deny": str(1 << 13)}]}]
        result = plan("1", roles, channels, "2", "3")
        entry = result["channels"][0]["permission_overwrites"][0]
        self.assertTrue(int(entry["allow"]) & (1 << 11))
        self.assertTrue(int(entry["deny"]) & (1 << 13))
