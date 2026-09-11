import asyncio
import copy
import json
from test_control import Base
from greybot_control.feed_dispatch import Feed, render, classify, preflight
from greybot_control.events import Collector
from greybot_control.local_archive import LocalArchive
from greybot_control.discord_api import Unavailable, Denied


DATA = {"members": [{"id": "8", "name": "River", "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png"}],
        "channels": [{"id": "3", "name": "general"}], "roles": [], "server": {"name": "Demo"}}


class FeedAPI:
    def __init__(self):
        self.posts = []
        self.fail = False
    async def request(self, method, path, **kwargs):
        if method == "GET":
            return {"bot": True}
        self.posts.append((path, kwargs["body"]))
        if self.fail:
            raise Unavailable("Lost response")
        return {"id": "55"}


class FeedTests(Base):
    def test_welcome_points_to_start_here(self):
        async def directory():
            data = copy.deepcopy(DATA)
            data["channels"].append({"id": "6", "name": "➡️start-here⬅️"})
            return data
        self.feed.directory.get = directory
        self.configure(welcome_enabled=True, welcome_channel="5", verification_enabled=True)
        self.event("start-here-welcome")
        self.tick()
        body = self.api.posts[0][1]
        self.assertIn("<#6>", body["content"])
        self.assertEqual(body["components"][0]["components"][0]["label"], "Verify to unlock channels")
        self.assertNotIn("Open admin site", json.dumps(body))

    def test_admin_link_only_in_audit_delivery(self):
        self.configure(audit_feed_enabled=True, audit_channel="4",
                       welcome_enabled=True, welcome_channel="5",
                       goodbye_enabled=True, goodbye_channel="5")
        self.event("join-link-check")
        self.tick()
        self.event("leave-link-check", "GUILD_MEMBER_REMOVE")
        self.tick()
        self.assertEqual(len(self.api.posts), 4)
        for path, body in self.api.posts:
            if path == "/channels/4/messages":
                self.assertIn("Open admin site", json.dumps(body))
            else:
                self.assertNotIn(self.cfg.origin, json.dumps(body))

    def setUp(self):
        super().setUp()
        self.api = FeedAPI()
        self.feed = Feed(self.cfg, self.store, self.api)
        async def directory():
            return copy.deepcopy(DATA)
        self.feed.directory.get = directory
        directory_path = self.root / "archive"
        directory_path.mkdir()
        self.archive = LocalArchive(directory_path)

    def configure(self, **changes):
        current = self.store.settings("1")
        self.store.save_settings("1", "7", current["revision"], {**current["values"], **changes})

    def tick(self, archive=True):
        if archive:
            self.archive.flush(self.store)
        asyncio.run(self.feed.tick())

    def event(self, eid, kind="GUILD_MEMBER_ADD", payload=None):
        return self.store.append(eid, "1", kind, "8", payload or {})

    def test_future_only_and_archive_before_delivery(self):
        self.event("old")
        self.configure(audit_feed_enabled=True, audit_channel="4")
        self.event("new")
        self.tick(False)
        self.assertFalse(self.api.posts)
        self.tick()
        self.assertEqual(len(self.api.posts), 1)
        self.tick()
        self.assertEqual(len(self.api.posts), 1)

    def test_filters_leave_full_history_and_prevent_feedback(self):
        self.configure(audit_feed_enabled=True, audit_channel="4", audit_ignored_channels=["3"])
        self.event("ignored", "MESSAGE_DELETE", {"channel_id": "3"})
        self.event("loop", "MESSAGE_DELETE", {"channel_id": "4"})
        self.event("voice", "VOICE_STATE_UPDATE", {"channel_id": "5", "feed_voice_changes": ["voice_joined"]})
        self.event("history", "GUILD_AUDIT_LOG_ENTRY_CREATE", {"action_type": 25, "source": "Discord audit history"})
        self.tick()
        self.assertFalse(self.api.posts)
        self.assertEqual(len(self.store.events("1")), 5)

    def test_arrival_departure_gifs_and_audit_each_once(self):
        self.configure(audit_feed_enabled=True, audit_channel="4", welcome_enabled=True,
                       welcome_channel="5", goodbye_enabled=True, goodbye_channel="5")
        self.store.save_profile("1", "8", {**DATA["members"][0], "name": "Fresh nickname"})
        self.event("join")
        self.event("leave", "GUILD_MEMBER_REMOVE")
        self.tick()
        self.assertEqual(len(self.api.posts), 4)
        gif_posts = [body for path, body in self.api.posts if path == "/channels/5/messages"]
        self.assertEqual(len(gif_posts), 2)
        self.assertIn("giphy.com", gif_posts[0]["embeds"][0]["image"]["url"])
        self.assertIn("tenor.com", gif_posts[1]["embeds"][0]["image"]["url"])
        audit_posts = [body for path, body in self.api.posts if path != "/channels/5/messages"]
        self.assertNotIn("image", audit_posts[0]["embeds"][0])
        self.assertEqual(audit_posts[1]["embeds"][0]["image"]["url"],
                         "https://media.giphy.com/media/bc4pHNmIWVlPoqzV8n/giphy.gif")
        self.assertIn("Fresh nickname", gif_posts[0]["embeds"][0]["description"])
        self.assertEqual(gif_posts[0]["content"], "Welcome <@8>!")
        self.assertEqual(gif_posts[0]["allowed_mentions"], {"parse": [], "users": ["8"]})
        self.assertNotIn("flags", gif_posts[0])
        self.assertEqual(gif_posts[1]["allowed_mentions"], {"parse": []})
        for body in gif_posts:
            self.assertEqual(body["embeds"][0]["color"], 0x4493F8)
        self.tick()
        self.assertEqual(len(self.api.posts), 4)

    def test_ambiguous_delivery_is_not_retried_after_restart(self):
        self.configure(audit_feed_enabled=True, audit_channel="4")
        self.event("joined")
        self.api.fail = True
        self.tick()
        self.tick()
        self.assertEqual(len(self.api.posts), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT state FROM feed_delivery").fetchone()[0], "unknown")

    def test_disable_reenable_never_replays_backlog(self):
        self.configure(audit_feed_enabled=True, audit_channel="4")
        self.event("before-off")
        self.configure(audit_feed_enabled=False)
        self.event("during-off")
        self.configure(audit_feed_enabled=True)
        self.event("after-on")
        self.tick()
        self.assertEqual(len(self.api.posts), 1)

    def test_bot_filter_and_content_never_copied(self):
        self.configure(audit_feed_enabled=True, audit_channel="4", audit_ignore_bots=True)
        self.event("bot")
        self.tick()
        self.assertFalse(self.api.posts)
        row = self.event("text", "MESSAGE_DELETE", {"content": "private-message-sentinel", "channel_id": "3"})
        data = copy.deepcopy(DATA)
        data["members"][0]["name"] = "@everyone [link](https://example.invalid)"
        body = render(row, ["message_deleted"], data, self.store.settings("1")["values"], self.cfg.origin)
        self.assertNotIn("private-message-sentinel", json.dumps(body))
        self.assertIn("\\@everyone", body["embeds"][0]["description"])
        self.assertEqual(body["allowed_mentions"]["parse"], [])

    def test_classification_avoids_embed_updates_and_duplicate_audits(self):
        self.assertEqual(classify(self.event("embed", "MESSAGE_UPDATE")), [])
        self.assertEqual(classify(self.event("edited", "MESSAGE_UPDATE", {"text_edited": True})), ["message_updated"])
        self.assertEqual(classify(self.event("role-audit", "GUILD_AUDIT_LOG_ENTRY_CREATE", {"action_type": 31})), ["role_updated"])
        self.assertEqual(classify(self.event("role-gateway", "GUILD_ROLE_UPDATE")), [])
        self.assertEqual(classify(self.event("channel-gateway", "CHANNEL_UPDATE")), [])

    def test_collector_classifies_voice_without_rewriting_original_channel(self):
        collector = Collector(self.cfg, self.store)
        collector.receive({"op": 0, "t": "READY", "d": {"session_id": "synthetic-session"}})
        collector.receive({"op": 0, "t": "GUILD_CREATE", "d": {"id": "1", "voice_states": [{"user_id": "8", "channel_id": "3"}]}})
        collector.receive({"op": 0, "s": 1, "t": "VOICE_STATE_UPDATE", "d": {"guild_id": "1", "user_id": "8", "channel_id": None}})
        row = self.store.events("1", kind="VOICE_STATE_UPDATE")[0]
        self.assertEqual(classify(row), ["voice_left"])
        self.assertIsNone(json.loads(row["payload"])["channel_id"])
        self.assertEqual(json.loads(row["payload"])["previous_channel_id"], "3")

    def test_preflight_rejects_cross_server_and_missing_permissions(self):
        class API:
            async def request(self, method, path):
                if path == "/channels/3":
                    return {"guild_id": "2", "type": 0}
        with self.assertRaises(Denied):
            asyncio.run(preflight(self.cfg, API(), "3"))
        with self.assertRaises(Denied):
            asyncio.run(preflight(self.cfg, API(), ""))
