import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from greybot_control import raids
from greybot_control.discord_api import Denied
from greybot_control.store import Store


class RaidTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "control.sqlite3")
        raids.install(self.store)
        self.event = {"title": "Example", "startTime": 2000, "closingTime": 2000,
                      "channelId": "2", "leaderId": "3", "templateId": "example",
                      "classes": [{"name": "Priest", "type": "primary", "specs": [
                          {"name": "Holy", "roleName": "Healers"},
                          {"name": "Shadow", "roleName": "Ranged"}]}],
                      "advancedSettings": {"limit": 1}, "roles": []}
        self.id = raids.create(self.store, "1", "3", "create", self.event)

    def change(self, actor, action, operation, value="", revision=None, now=1000):
        revision = revision or raids.read(self.store, "1", self.id)["revision"]
        return raids.mutate(self.store, "1", actor, action, self.id, revision, operation, value, now=now)

    def roster(self):
        return raids.read(self.store, "1", self.id)["body"]["signUps"]

    def test_repeated_create_and_signup_are_idempotent(self):
        self.assertEqual(raids.create(self.store, "1", "3", "create", self.event), self.id)
        self.change("4", "signup", "signup", "0:0")
        self.change("4", "signup", "signup", "0:0", revision=1)
        self.assertEqual(len(self.roster()), 1)
        self.assertEqual(raids.read(self.store, "1", self.id)["revision"], 2)

    def test_spec_change_note_and_status_preserve_one_member(self):
        self.change("4", "first", "signup", "0:0")
        self.change("4", "note", "note", "Late by ten minutes")
        self.change("4", "spec", "signup", "0:1")
        self.assertEqual(self.roster()[0]["specName"], "Shadow")
        self.assertEqual(self.roster()[0]["note"], "Late by ten minutes")
        self.change("4", "late", "status", "Late")
        self.assertEqual(len(self.roster()), 1)
        self.assertEqual(self.roster()[0]["className"], "Late")
        self.change("4", "withdraw", "withdraw")
        self.assertEqual(self.roster(), [])

    def test_capacity_and_stale_revision_reject_without_roster_loss(self):
        self.change("4", "first", "signup", "0:0")
        with self.assertRaises(Denied):
            self.change("5", "stale", "signup", "0:0", revision=1)
        with self.assertRaises(Denied):
            self.change("5", "full", "signup", "0:0")
        self.assertEqual([s["userId"] for s in self.roster()], ["4"])

    def test_closed_cancelled_and_deadline_reject_signup(self):
        with self.assertRaises(Denied):
            self.change("4", "late", "signup", "0:0", now=2000)
        self.change("3", "close", "close")
        with self.assertRaises(Denied):
            self.change("4", "closed", "signup", "0:0")
        self.change("3", "reopen", "open")
        self.change("3", "cancel", "cancel")
        with self.assertRaises(Denied):
            self.change("4", "cancelled", "signup", "0:0")

    def test_template_index_and_edit_fields_are_validated(self):
        for value in ("1:0", "0:99", "__class__"):
            with self.assertRaises(Denied):
                self.change("4", value, "signup", value)
        with self.assertRaises(Denied):
            self.change("3", "bad-edit", "edit", {"leaderId": "4"})
        self.assertEqual(self.roster(), [])

    def test_roster_survives_store_restart(self):
        self.change("4", "first", "signup", "0:0")
        self.store = Store(self.store.path)
        raids.install(self.store)
        self.assertEqual(self.roster()[0]["userId"], "4")

    def test_live_access_and_leadership_checks(self):
        member = {"user": {"id": "4"}, "roles": []}
        roles = [{"id": "1", "permissions": str((1 << 10) | (1 << 11))}]
        channel = {"guild_id": "1", "permission_overwrites": []}
        class API:
            async def request(self, method, path):
                if path.endswith("/roles"):
                    return roles
                if path.startswith("/channels"):
                    return channel
                return member
        cfg = SimpleNamespace(guild_id="1")
        async def check():
            await raids.authorize(cfg, self.store, API(), "4", self.event)
            with self.assertRaises(Denied):
                await raids.authorize(cfg, self.store, API(), "4", self.event, create=True)
            with self.assertRaises(Denied):
                await raids.authorize(cfg, self.store, API(), "4", self.event, manage=True)
            event = copy.deepcopy(self.event)
            event["advancedSettings"]["allowed_roles"] = "6, 7, "
            with self.assertRaises(Denied):
                await raids.authorize(cfg, self.store, API(), "4", event)
            member["roles"] = ["6"]
            await raids.authorize(cfg, self.store, API(), "4", event)
            event["coLeaders"] = [{"id": "4", "name": "Example"}]
            await raids.authorize(cfg, self.store, API(), "4", event, manage=True)
            channel["permission_overwrites"] = [{"id": "4", "type": 1, "allow": "0", "deny": str(1 << 10)}]
            with self.assertRaises(Denied):
                await raids.authorize(cfg, self.store, API(), "4", event)
        asyncio.run(check())
