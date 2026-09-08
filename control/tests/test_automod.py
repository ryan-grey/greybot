import asyncio
import json
import time
from dataclasses import replace
from unittest.mock import patch

from test_control import Base, FakeDiscord
from greybot_control.automod import Detector, execute
from greybot_control.discord_api import Denied, Unavailable
from greybot_control.local_archive import LocalArchive
from greybot_control.worker import execute_one


class AutomodTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, enforce=True, capture_content=True)
        self.set_enabled(True)
        self.detector = Detector(self.cfg, self.store)
        self.now = time.time() + 1
        self.detector.started = self.now - 10

    def set_enabled(self, value):
        settings = self.store.settings(self.cfg.guild_id)
        self.store.save_settings(self.cfg.guild_id, "7", settings["revision"],
                                 {**settings["values"], "moderation_enabled": value})

    def message(self, offset, content=None, **extra):
        occurred = self.now + offset / 10
        mid = str((int(occurred * 1000) - 1420070400000) << 22)
        return {"op": 0, "t": "MESSAGE_CREATE", "d": {"guild_id": "1", "channel_id": str(3 + offset % 2),
                "id": mid, "author": {"id": "8"}, "content": str(offset) if content is None else content, **extra}}

    def send(self, *packets):
        with patch("greybot_control.automod.time.time", return_value=self.now + 5):
            for packet in packets:
                self.detector.receive(packet)

    def test_fourth_across_channels_and_third_exact_repeat(self):
        self.send(*(self.message(i) for i in range(3)))
        self.assertFalse(self.store.jobs("1"))
        self.send(self.message(3))
        job = self.store.jobs("1")[0]
        self.assertTrue(json.loads(job["body"])["spam"])
        self.set_enabled(False)
        self.send(self.message(4))
        self.set_enabled(True)
        self.send(*(self.message(i + 10, "same") for i in range(3)))
        body = next(json.loads(job["body"]) for job in self.store.jobs("1") if json.loads(job["body"])["repeated_text"])
        self.assertTrue(body["repeated_text"])
        self.assertFalse(body["spam"])

    def test_duplicate_old_bot_webhook_foreign_and_empty_text(self):
        packet = self.message(0)
        self.send(packet, packet, packet, packet)
        self.send(self.message(1, author={"id": "8", "bot": True}),
                  self.message(2, webhook_id="4"), self.message(3, guild_id="2"), self.message(-500))
        self.assertFalse(self.store.jobs("1"))
        self.detector.recent.clear()
        self.send(*(self.message(i, "") for i in range(3)))
        self.assertFalse(self.store.jobs("1"))

    def test_window_expires_and_restart_does_not_replay(self):
        self.send(*(self.message(i) for i in range(3)))
        self.send(self.message(100))  # Future timestamps are ignored.
        self.detector.started = self.now + 20
        self.send(self.message(3))
        self.assertFalse(self.store.jobs("1"))

    def test_silent_delete_only_and_private_escalation(self):
        api = FakeDiscord()
        self.send(*(self.message(i, "same") for i in range(6)))
        jobs = sorted(self.store.jobs("1"), key=lambda job: job["created"])
        self.assertEqual(len(jobs), 4)
        with patch("greybot_control.automod.time.time", return_value=self.now + 5):
            for job in jobs:
                asyncio.run(execute(self.cfg, self.store, api, job))
        self.assertEqual([method for method, _ in api.calls], ["DELETE"] * 4)
        infractions = self.store.events("1", kind="AUTOMOD_INFRACTION")
        self.assertEqual(len(infractions), 3)
        self.assertTrue(all(not json.loads(row["payload"])["warning_sent"] for row in infractions))
        self.assertEqual(sum(job["kind"] == "mute" for job in self.store.jobs("1")), 1)
        self.assertTrue(self.store.verify())

    def test_disabled_stale_and_ambiguous_delete_do_not_punish(self):
        api = FakeDiscord()
        self.send(*(self.message(i) for i in range(4)))
        job = self.store.jobs("1")[0]
        self.set_enabled(False)
        with self.assertRaises(Denied):
            asyncio.run(execute(self.cfg, self.store, api, job))
        self.set_enabled(True)
        with patch("greybot_control.automod.time.time", return_value=self.now + 100):
            with self.assertRaises(Denied):
                asyncio.run(execute(self.cfg, self.store, api, job))
        api.ambiguous = True
        with patch("greybot_control.automod.time.time", return_value=self.now + 5):
            with self.assertRaises(Unavailable):
                asyncio.run(execute(self.cfg, self.store, api, job))
        self.assertFalse(self.store.events("1", kind="AUTOMOD_INFRACTION"))

    def test_revocation_resets_count(self):
        api = FakeDiscord()
        self.send(*(self.message(i) for i in range(5)))
        with patch("greybot_control.automod.time.time", return_value=self.now + 5):
            for job in self.store.jobs("1"):
                asyncio.run(execute(self.cfg, self.store, api, job))
            self.store.append("release-test", "1", "MUTE_RELEASED", "8", {})
            self.send(self.message(6))
            asyncio.run(execute(self.cfg, self.store, api, self.store.jobs("1")[0]))
        self.assertFalse(any(job["kind"] == "mute" for job in self.store.jobs("1")))

    def test_executor_denies_non_admin_before_delete(self):
        self.send(*(self.message(i) for i in range(4)))
        archive_dir = self.root / "archive"
        archive_dir.mkdir()
        api = FakeDiscord()  # Fake only authorizes actor 7, not bot 9.
        asyncio.run(execute_one(self.cfg, self.store, api, LocalArchive(archive_dir)))
        self.assertFalse(any(method == "DELETE" for method, _ in api.calls))
        self.assertEqual(self.store.jobs("1")[0]["state"], "denied")

    def test_executor_archives_before_silent_delete(self):
        self.send(*(self.message(i) for i in range(4)))
        archive_dir = self.root / "archive"
        archive_dir.mkdir()
        store = self.store
        class BotAPI(FakeDiscord):
            async def require_admin(self, user):
                if user != "9":
                    raise Denied("Not bot")
            async def request(self, method, path, **kwargs):
                assert not store.pending(1)
                return await super().request(method, path, **kwargs)
        api = BotAPI()
        with patch("greybot_control.automod.time.time", return_value=self.now + 5):
            asyncio.run(execute_one(self.cfg, self.store, api, LocalArchive(archive_dir)))
        self.assertEqual(api.calls[0][0], "DELETE")
        self.assertEqual(self.store.jobs("1")[0]["state"], "completed")
        self.assertEqual(len(self.store.events("1", kind="AUTOMOD_INFRACTION")), 1)
