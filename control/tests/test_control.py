import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from botocore.exceptions import ClientError

from greybot_control.archive import Archive
from greybot_control.config import Config
from greybot_control.discord_api import Denied, DiscordAPI, Unavailable
from greybot_control.events import Collector
from greybot_control.reader import import_guild
from greybot_control.store import Store, canonical
from greybot_control.web import create_app
from greybot_control.worker import execute_one


class FakeDiscord:
    def __init__(self):
        self.allowed = True
        self.calls = []
        self.ambiguous = False

    async def require_admin(self, user):
        self.calls.append(("authorize", user))
        if not self.allowed or user != "7":
            raise Denied("Not an administrator")

    async def identity(self, code):
        return "7"

    async def authorize_moderation(self, actor, target, action):
        await self.require_admin(actor)
        if target in {"7", "9"}:
            raise Denied("Protected member")

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if self.ambiguous:
            raise Unavailable("Response lost")

    async def close(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state" / "control.sqlite3")
        self.cfg = Config("1", "9", "test-placeholder", "test-placeholder",
                          "http://127.0.0.1:8080", self.root / "state")


class JournalTests(Base):
    def test_append_guards_and_chain(self):
        a = self.store.append("a", "1", "JOIN", "7", {})
        b = self.store.append("b", "1", "LEAVE", "7", {})
        self.assertEqual(b["previous"], a["hash"])
        self.assertTrue(self.store.verify())
        with self.store.connection() as db:
            for sql in ("DELETE FROM events", "UPDATE events SET payload='{}'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)

    def test_concurrent_append_serializes_and_deduplicates(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda n: self.store.append(str(n % 10), "1", "JOIN", "7", {}), range(40)))
        self.assertEqual(len(self.store.events("1")), 10)
        self.assertTrue(self.store.verify())

    def test_guild_scope_and_pagination(self):
        for i in range(5):
            self.store.append(str(i), "1", "JOIN", "7", {})
        self.store.append("private", "2", "JOIN", "7", {})
        newest = self.store.events("1", limit=2)
        older = self.store.events("1", before=newest[-1]["seq"], limit=10)
        self.assertEqual(len(older), 3)
        self.assertFalse(self.store.events("1", subject="' OR 1=1 --"))

    def test_settings_conflict_does_not_write_an_event(self):
        self.store.save_settings("1", "7", 0, {"welcome_channel": "3"})
        with self.assertRaises(ValueError):
            self.store.save_settings("1", "7", 0, {})
        self.assertEqual(len(self.store.events("1")), 1)

    def test_idempotency_cannot_change_actor_target_or_action(self):
        self.store.queue("key", "1", "7", "kick", "8", {"reason": "test"})
        self.store.queue("key", "1", "7", "kick", "8", {"reason": "test"})
        with self.assertRaises(ValueError):
            self.store.queue("key", "1", "7", "ban", "8", {"reason": "test"})
        self.assertEqual(len(self.store.jobs("1")), 1)


class WebTests(Base):
    def setUp(self):
        super().setUp()
        self.discord = FakeDiscord()
        self.client = TestClient(create_app(self.cfg, self.store, self.discord), base_url=self.cfg.origin)
        self.addCleanup(self.client.close)

    def login(self):
        token = self.store.session("7")
        self.client.cookies.set("greybot-local", token)
        return {"origin": self.cfg.origin, "x-csrf-token": self.store.get_session(token)["csrf"]}

    def test_all_private_endpoints_require_login(self):
        for path in ("/api/status", "/api/events", "/api/messages", "/api/settings", "/api/actions", "/api/directory", "/api/mutes"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertNotIn("Server timeline", self.client.get("/").text)

    def test_revocation_applies_to_existing_session(self):
        self.login()
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.discord.allowed = False
        self.assertEqual(self.client.get("/api/events").status_code, 403)
        self.assertEqual(self.client.get("/api/directory").status_code, 403)
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_csrf_and_origin_both_required(self):
        headers = self.login()
        body = {"revision": 0, "welcome_channel": "3"}
        self.assertEqual(self.client.put("/api/settings", json=body).status_code, 403)
        self.assertEqual(self.client.put("/api/settings", json=body,
            headers={**headers, "origin": "https://elsewhere.invalid"}).status_code, 403)
        self.assertEqual(self.client.put("/api/settings", json=body, headers=headers).status_code, 200)
        self.assertEqual(self.client.put("/api/settings", json=body, headers=headers).status_code, 409)

    def test_incomplete_features_cannot_be_activated(self):
        headers = self.login()
        for body in ({"welcome_enabled": True}, {"self_roles": ["2"]}, {"moderation_enabled": True}, {"audit_feed_enabled": True}):
            self.assertEqual(self.client.put("/api/settings", headers=headers,
                             json={"revision": 0, **body}).status_code, 409)

    def test_discord_feed_filters_do_not_filter_private_history(self):
        headers = self.login()
        self.store.append("voice-test", self.cfg.guild_id, "VOICE_STATE_UPDATE", "7", {"channel_id": "3"})
        self.store.append("message-test", self.cfg.guild_id, "MESSAGE_CREATE", "7", {"channel_id": "3"})
        initial = self.client.get("/api/settings").json()
        self.assertNotIn("voice_joined", initial["values"]["audit_events"])
        response = self.client.put("/api/settings", headers=headers, json={
            "revision": 0, "audit_channel": "3", "audit_events": [],
            "audit_ignore_bots": True, "audit_ignored_channels": ["3"]})
        self.assertEqual(response.status_code, 200)
        kinds = {row["kind"] for row in self.client.get("/api/events").json()}
        self.assertTrue({"VOICE_STATE_UPDATE", "MESSAGE_CREATE"} <= kinds)
        self.assertFalse(response.json()["values"]["audit_feed_enabled"])
        self.assertTrue(self.store.verify())

    def test_invalid_feed_filters_rejected_without_mutation(self):
        headers = self.login()
        for change in ({"audit_events": ["anything"]},
                       {"audit_events": ["member_joined", "member_joined"]},
                       {"audit_ignored_channels": ["not-a-channel"]}):
            response = self.client.put("/api/settings", headers=headers, json={"revision": 0, **change})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.settings(self.cfg.guild_id)["revision"], 0)

    def test_oauth_state_bound_to_browser_and_single_use(self):
        response = self.client.get("/auth/login", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        state = self.client.cookies.get("greybot-local-login")
        with TestClient(create_app(self.cfg, self.store, self.discord), base_url=self.cfg.origin) as other:
            self.assertEqual(other.get("/auth/callback", params={"state": state, "code": "test"}).status_code, 403)
        response = self.client.get("/auth/callback", params={"state": state, "code": "test"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.get("/auth/callback", params={"state": state, "code": "test"}).status_code, 403)
        self.assertEqual(self.client.get("/api/status").status_code, 200)

    def test_denied_admin_login_renders_page_without_granting_access(self):
        self.discord.allowed = False
        self.client.get("/auth/login", follow_redirects=False)
        state = self.client.cookies.get("greybot-local-login")
        response = self.client.get("/auth/callback", params={"state": state, "code": "test"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Admin access required", response.text)
        self.assertIn('name="viewport"', response.text)
        self.assertFalse(self.client.cookies.get("greybot-local"))
        self.assertFalse(self.client.cookies.get("greybot-local-login"))
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_revoked_admin_gets_page_but_api_keeps_json(self):
        self.login()
        self.discord.allowed = False
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "Not an administrator"})
        response = self.client.get("/")
        self.assertEqual(response.status_code, 403)
        self.assertIn("Admin access required", response.text)

    def test_default_mode_blocks_moderation(self):
        headers = self.login()
        response = self.client.post("/api/actions", headers=headers, json={
            "request_id": "a" * 20, "user": "8", "action": "kick", "reason": "test"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.store.jobs("1"))

    def test_security_headers_and_body_limit(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(self.client.get("/", headers={"host": "elsewhere.invalid"}).status_code, 400)
        self.assertEqual(self.client.post("/api/actions", content="x" * 17000).status_code, 413)

    def test_logout_invalidates_server_session(self):
        headers = self.login()
        token = self.client.cookies.get("greybot-local")
        self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 200)
        self.assertIsNone(self.store.get_session(token))


class EventTests(Base):
    def setUp(self):
        super().setUp()
        self.collector = Collector(replace(self.cfg, capture_content=True), self.store)
        self.collector.receive({"op": 0, "t": "READY", "d": {"session_id": "session-do-not-store"}, "s": 1})

    def message(self, kind, seq, **extra):
        self.collector.receive({"op": 0, "t": kind, "s": seq, "d": {"guild_id": "1", "id": "30", "channel_id": "3", **extra}})

    def test_replay_does_not_regress_message_index(self):
        self.message("MESSAGE_CREATE", 2, author={"id": "8"}, content="first")
        self.message("MESSAGE_UPDATE", 3, content="second")
        self.message("MESSAGE_CREATE", 2, author={"id": "8"}, content="first")
        self.assertEqual(self.store.message("1", "30")["content"], "second")
        self.assertEqual(len(self.store.events("1")), 3)

    def test_secrets_and_content_do_not_enter_journal(self):
        self.message("MESSAGE_CREATE", 2, author={"id": "8"}, content="private-text",
                     attachments=[{"url": "signed-secret"}], token="secret")
        data = canonical(self.store.events("1"))
        for secret in ("private-text", "signed-secret", "session-do-not-store", '"token"'):
            self.assertNotIn(secret, data)
        self.assertEqual(len(self.store.search("1", "private-text")), 1)

    def test_other_guild_and_dm_events_excluded(self):
        for guild in ("2", None):
            self.collector.receive({"op": 0, "t": "MESSAGE_CREATE", "s": 2,
                "d": {"guild_id": guild, "id": "99", "content": "elsewhere"}})
        self.assertEqual(len(self.store.events("1")), 1)

    def test_unseen_deletion_does_not_invent_content(self):
        self.message("MESSAGE_DELETE", 2)
        event = self.store.events("1")[0]
        self.assertFalse(json.loads(event["payload"])["prior_message_observed"])

    def test_bootstrap_does_not_generate_member_joins(self):
        self.collector.receive({"op": 0, "t": "GUILD_CREATE", "s": 2,
            "d": {"id": "1", "members": [{"user": {"id": "8"}}]}})
        self.assertEqual(len(self.store.events("1")), 1)


class WorkerTests(Base):
    def test_unknown_delivery_never_retried(self):
        api = FakeDiscord()
        api.ambiguous = True
        self.store.queue("x", "1", "7", "kick", "8", {"reason": "test", "minutes": 10})
        class Archived:
            def flush(_, store):
                for event in store.pending():
                    store.receipt(event["seq"], "test", "v1")
        cfg = replace(self.cfg, enforce=True)
        asyncio.run(execute_one(cfg, self.store, api, Archived()))
        asyncio.run(execute_one(cfg, self.store, api, Archived()))
        self.assertEqual(self.store.jobs("1")[0]["state"], "unknown")
        self.assertEqual(sum(c[0] == "DELETE" for c in api.calls), 1)

    def test_archive_failure_does_not_claim_action(self):
        self.store.queue("x", "1", "7", "kick", "8", {"reason": "test", "minutes": 10})
        class Broken:
            def flush(_, store):
                raise RuntimeError("Archive unavailable")
        with self.assertRaises(RuntimeError):
            asyncio.run(execute_one(replace(self.cfg, enforce=True), self.store, FakeDiscord(), Broken()))
        self.assertEqual(self.store.jobs("1")[0]["state"], "queued")

    def test_admin_revoked_after_queue_is_denied(self):
        self.store.queue("x", "1", "7", "kick", "8", {"reason": "test", "minutes": 10})
        api = FakeDiscord()
        api.allowed = False
        class Archived:
            def flush(_, store):
                for event in store.pending():
                    store.receipt(event["seq"], "test", "v1")
        asyncio.run(execute_one(replace(self.cfg, enforce=True), self.store, api, Archived()))
        self.assertEqual(self.store.jobs("1")[0]["state"], "denied")
        self.assertFalse(any(c[0] == "DELETE" for c in api.calls))


class ImportTests(Base):
    def test_only_target_guild_bot_records_import_without_changing_source(self):
        source = self.root / "old.sqlite3"
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("CREATE TABLE messages(id,channel_id,author_id,content,deleted,source,guild_id)")
            db.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", [
                (1, 3, 8, "guild", 0, "bot", "1"), (2, 3, 8, "other", 0, "bot", "2"),
                (3, 4, 8, "personal", 0, "local", "1")])
        before = source.read_bytes()
        self.assertEqual(import_guild(self.store, source, "1"), 1)
        self.assertEqual(import_guild(self.store, source, "1"), 0)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(len(self.store.search("1", "")), 1)


class ArchiveTests(Base):
    class S3:
        class exceptions:
            ClientError = ClientError

        def __init__(self):
            self.objects = {}
            self.mode = "COMPLIANCE"
            self.corrupt = False
            self.expired = False

        def get_object_lock_configuration(self, **kwargs):
            return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled", "Rule": {
                "DefaultRetention": {"Mode": self.mode, "Days": 30}}}}

        def get_bucket_versioning(self, **kwargs):
            return {"Status": "Enabled"}

        def put_object(self, **kwargs):
            if kwargs["Key"] in self.objects:
                raise ClientError({"Error": {"Code": "PreconditionFailed"},
                                   "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
            self.objects[kwargs["Key"]] = kwargs["Body"]
            return {"VersionId": "v1"}

        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(b"changed" if self.corrupt else self.objects[kwargs["Key"]]),
                    "VersionId": "v1", "ObjectLockMode": self.mode,
                    "ObjectLockRetainUntilDate": datetime.now(timezone.utc) + timedelta(days=-1 if self.expired else 30)}

    def test_governance_archive_rejected(self):
        s3 = self.S3()
        s3.mode = "GOVERNANCE"
        with self.assertRaises(RuntimeError):
            Archive(s3, "test-bucket", 30).check()

    def test_wrong_retention_rejected(self):
        with self.assertRaises(RuntimeError):
            Archive(self.S3(), "test-bucket", 365).check()

    def test_crash_after_upload_before_receipt_recovers_same_version(self):
        self.store.append("test", "1", "JOIN", "8", {})
        s3 = self.S3()
        archive = Archive(s3, "test-bucket", 30)
        with patch.object(self.store, "receipt", side_effect=RuntimeError("Crash")):
            with self.assertRaises(RuntimeError):
                archive.flush(self.store)
        self.assertEqual(len(self.store.pending()), 1)
        archive.flush(self.store)
        self.assertFalse(self.store.pending())
        self.assertEqual(len(s3.objects), 1)

    def test_corrupt_readback_or_expired_lock_never_receipted(self):
        for field in ("corrupt", "expired"):
            self.store.append(field, "1", "JOIN", "8", {})
            s3 = self.S3()
            setattr(s3, field, True)
            with self.assertRaises(RuntimeError):
                Archive(s3, "test-bucket", 30).flush(self.store)
            self.assertTrue(self.store.pending())


class HierarchyTests(Base):
    def api(self, *, actor_owner=False, target_owner=False, target_admin=False, bot_position=5, bot_permissions=8):
        api = object.__new__(DiscordAPI)
        api.cfg = self.cfg
        async def context(user):
            return {"7": {"owner": actor_owner, "permissions": 8, "position": 4},
                    "8": {"owner": target_owner, "permissions": 8 if target_admin else 0, "position": 3},
                    "9": {"owner": False, "permissions": bot_permissions, "position": bot_position}}[user]
        api.member_context = context
        return api

    def test_owner_and_self_are_protected(self):
        for target, api in (("8", self.api(target_owner=True)), ("7", self.api()), ("9", self.api())):
            with self.assertRaises(Denied):
                asyncio.run(api.authorize_moderation("7", target, "kick"))

    def test_admin_target_cannot_be_timed_out(self):
        with self.assertRaises(Denied):
            asyncio.run(self.api(target_admin=True).authorize_moderation("7", "8", "timeout"))

    def test_owner_does_not_bypass_bot_hierarchy(self):
        with self.assertRaises(Denied):
            asyncio.run(self.api(actor_owner=True, bot_position=2).authorize_moderation("7", "8", "kick"))

    def test_permission_is_action_specific(self):
        api = self.api(bot_permissions=1 << 1)
        asyncio.run(api.authorize_moderation("7", "8", "kick"))
        with self.assertRaises(Denied):
            asyncio.run(api.authorize_moderation("7", "8", "ban"))


class ReaderBridgeTests(Base):
    def test_tools_scope_reads_and_database_is_read_only(self):
        from greybot_control import mcp_reader
        self.store.index_message("1", "30", "3", "8", "visible")
        self.store.index_message("2", "31", "4", "8", "hidden")
        self.store.append("join", "1", "GUILD_MEMBER_ADD", "8", {})
        with patch.dict("os.environ", {"GREYBOT_GUILD_ID": "1", "GREYBOT_STATE_DIR": str(self.cfg.state_dir)}):
            result = json.loads(mcp_reader.search_messages())
            self.assertEqual([m["content"] for m in result["messages"]], ["visible"])
            self.assertEqual(json.loads(mcp_reader.get_channel("4"))["count"], 0)
            self.assertEqual(json.loads(mcp_reader.read_events(user_id="8"))["count"], 1)
            with mcp_reader.connection() as db:
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("DELETE FROM messages")


if __name__ == "__main__":
    unittest.main()
