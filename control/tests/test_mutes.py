import asyncio
import copy
import json
from unittest.mock import patch

from test_control import Base, FakeDiscord
from greybot_control.discord_api import Denied, Unavailable
from greybot_control.local_archive import LocalArchive
from greybot_control.mutes import Mutes, MUTE_MASK, MANAGE_PERMISSIONS, overwrite


class MuteDiscord(FakeDiscord):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.channels = [{"id": "3", "name": "general", "permission_overwrites": []},
                         {"id": "4", "name": "voice", "permission_overwrites": []}]
        self.permission = MANAGE_PERMISSIONS
        self.fail_after = None
        self.writes = 0

    async def request(self, method, path, **kwargs):
        if method == "GET":
            if path.endswith("/channels"):
                return copy.deepcopy(self.channels)
            if path.endswith("/roles"):
                return [{"id": "1", "permissions": str(self.permission)}]
            return {"user": {"id": "9"}, "roles": []}
        assert not self.store.pending(1), "Permissions changed before archiving"
        channel = next(c for c in self.channels if c["id"] == path.split("/")[2])
        channel["permission_overwrites"] = [o for o in channel["permission_overwrites"] if o["id"] != "8"]
        if method == "PUT":
            channel["permission_overwrites"].append({"id": "8", **kwargs["body"]})
        self.writes += 1
        if self.fail_after == self.writes:
            raise Unavailable("Response lost after applying")


class MuteTests(Base):
    def setUp(self):
        super().setUp()
        directory = self.root / "archive"
        directory.mkdir()
        self.api = MuteDiscord(self.store)
        self.mutes = Mutes(self.cfg, self.store, self.api, LocalArchive(directory))

    def run_async(self, awaitable):
        return asyncio.run(awaitable)

    def test_no_expiry_and_admin_release_restores_original(self):
        original = {"id": "8", "type": 1, "allow": str(1 << 11), "deny": "0"}
        self.api.channels[0]["permission_overwrites"] = [original.copy()]
        self.run_async(self.mutes.apply("7", "8", "Test moderation"))
        self.assertEqual(self.mutes.record("8")["state"], "active")
        self.assertEqual(int(overwrite(self.api.channels[0], "8")["deny"]), MUTE_MASK)
        with patch("greybot_control.mutes.time.time", return_value=10**12):
            self.run_async(self.mutes.reconcile())
        self.assertEqual(self.mutes.record("8")["state"], "active")
        self.run_async(self.mutes.release("7", "8", "Admin revoked"))
        self.assertEqual(overwrite(self.api.channels[0], "8"), original)
        self.assertIsNone(overwrite(self.api.channels[1], "8"))
        self.assertTrue(self.store.verify())

    def test_lost_apply_response_preserves_original_on_retry(self):
        self.api.fail_after = 1
        with self.assertRaises(Unavailable):
            self.run_async(self.mutes.apply("7", "8", "Test"))
        self.assertEqual(self.mutes.record("8")["state"], "applying")
        self.api.fail_after = None
        self.run_async(self.mutes.reconcile())
        self.run_async(self.mutes.release("7", "8", "Release"))
        self.assertTrue(all(not c["permission_overwrites"] for c in self.api.channels))

    def test_release_retry_and_unrelated_admin_edits(self):
        self.run_async(self.mutes.apply("7", "8", "Test"))
        overwrite(self.api.channels[0], "8")["allow"] = str(1 << 10)
        self.api.fail_after = self.api.writes + 1
        with self.assertRaises(Unavailable):
            self.run_async(self.mutes.release("7", "8", "Release"))
        self.assertEqual(self.mutes.record("8")["state"], "releasing")
        self.api.fail_after = None
        self.run_async(self.mutes.release("7", "8", "Retry"))
        self.assertEqual(overwrite(self.api.channels[0], "8")["allow"], str(1 << 10))
        self.assertEqual(overwrite(self.api.channels[0], "8")["deny"], "0")

    def test_conflicting_release_has_no_partial_writes(self):
        self.run_async(self.mutes.apply("7", "8", "Test"))
        overwrite(self.api.channels[1], "8")["deny"] = str(1 << 11)
        before = self.api.writes
        with self.assertRaises(Denied):
            self.run_async(self.mutes.release("7", "8", "Release"))
        self.assertEqual(self.api.writes, before)

    def test_new_channel_reconciled_and_failure_visible(self):
        self.run_async(self.mutes.apply("7", "8", "Test"))
        self.api.channels.append({"id": "5", "name": "new", "permission_overwrites": []})
        self.run_async(self.mutes.reconcile())
        self.assertEqual(int(overwrite(self.api.channels[2], "8")["deny"]), MUTE_MASK)
        self.api.allowed = False
        self.run_async(self.mutes.reconcile())
        self.assertEqual(self.mutes.record("8")["state"], "needs_review")
        count = len(self.store.events("1"))
        self.run_async(self.mutes.reconcile())
        self.assertEqual(len(self.store.events("1")), count)
        self.api.allowed = True
        self.run_async(self.mutes.reconcile())
        self.assertEqual(self.mutes.record("8")["state"], "active")

    def test_permissions_admin_and_archive_fail_closed(self):
        self.api.permission = 0
        with self.assertRaises(Denied):
            self.run_async(self.mutes.apply("7", "8", "Test"))
        self.api.permission = MANAGE_PERMISSIONS
        self.mutes.archive = None
        with self.assertRaises(Unavailable):
            self.run_async(self.mutes.apply("7", "8", "Test"))
        with self.assertRaises(Denied):
            self.run_async(self.mutes.release("8", "8", "Test"))
        self.assertEqual(self.api.writes, 0)
