import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

from test_control import Base
from greybot_control.audit_details import project
from greybot_control.events import Collector
from greybot_control.feed_dispatch import render
from greybot_control.profiles import display_profile


class AuditDetailsTests(Base):
    def collector(self):
        collector = Collector(replace(self.cfg, capture_content=True), self.store)
        collector.receive({"op": 0, "t": "READY", "d": {"session_id": "synthetic"}})
        return collector

    def test_roles_added_removed_subject_actor_server_identity_and_timestamp(self):
        collector = self.collector()
        entry = {"id": "1546768520027574293", "guild_id": "1", "user_id": "7", "target_id": "8", "action_type": 25,
            "changes": [{"key": "$add", "new_value": [{"id": "4", "name": "Saturday Raiders"}]},
                        {"key": "$remove", "new_value": [{"id": "5", "name": "Raiders"}]}]}
        collector.receive({"op": 0, "s": 1, "t": "GUILD_AUDIT_LOG_ENTRY_CREATE", "d": entry})
        row = self.store.events("1")[0]
        self.assertEqual(row["subject"], "8")
        member = display_profile("1", {"id": "8", "username": "global-handle", "avatar": "a" * 32},
                                 {"nick": "Server nickname", "avatar": "b" * 32})
        directory = {"members": [member, {"id": "7", "name": "Officer nickname"}], "roles": [], "channels": []}
        post = render(row, ["member_roles_changed"], directory, self.store.settings("1")["values"], self.cfg.origin)
        embed = post["embeds"][0]
        self.assertEqual(embed["author"]["name"], "Server nickname")
        self.assertIn("guilds/1/users/8/avatars/", embed["author"]["icon_url"])
        self.assertEqual(embed["footer"]["text"], "greyBot · Member: Server nickname")
        self.assertIn("**By:** Officer nickname", embed["description"])
        self.assertEqual([(f["name"], f["value"]) for f in embed["fields"]],
                         [("Added roles", "Saturday Raiders"), ("Removed roles", "Raiders")])
        self.assertNotIn("global-handle", json.dumps(post))
        expected = datetime.fromtimestamp(project(entry)["occurred_at"], timezone.utc).isoformat()
        self.assertEqual(embed["timestamp"], expected)

    def test_role_edit_before_after_and_permission_names_without_unknown_values(self):
        payload = project({"action_type": 31, "changes": [
            {"key": "name", "old_value": "Old name", "new_value": "New name"},
            {"key": "color", "old_value": 0, "new_value": 0x4493F8},
            {"key": "permissions", "old_value": "1024", "new_value": "8"},
            {"key": "token", "new_value": "excluded-secret"}]})
        row = self.store.append("role-edit", "1", "GUILD_AUDIT_LOG_ENTRY_CREATE", "4",
                                {**payload, "action_type": 31, "target_id": "4"})
        post = render(row, ["role_updated"], {}, self.store.settings("1")["values"], self.cfg.origin)
        fields = {f["name"]: f["value"] for f in post["embeds"][0]["fields"]}
        self.assertEqual(fields["Name"], "**Before:** Old name\n**After:** New name")
        self.assertEqual(fields["Color"], "**Before:** #000000\n**After:** #4493F8")
        self.assertIn("Administrator", fields["Added permissions"])
        self.assertNotIn("excluded-secret", row["payload"])
        self.assertNotIn("**Member:**", post["embeds"][0]["description"])

    def test_message_edits_keep_versions_private_and_unchanged_by_later_edits(self):
        collector = self.collector()
        def event(seq, kind, extra):
            collector.receive({"op": 0, "s": seq, "t": kind, "d": {
                "guild_id": "1", "channel_id": "3", "id": "50", **extra}})
        event(1, "MESSAGE_CREATE", {"author": {"id": "8", "username": "member"}, "content": "First text"})
        event(2, "MESSAGE_UPDATE", {"content": "Second text", "edited_timestamp": "2026-09-08T07:00:00Z"})
        first_edit = self.store.events("1", kind="MESSAGE_UPDATE")[0]
        event(3, "MESSAGE_UPDATE", {"content": "Third text", "edited_timestamp": "2026-09-08T07:01:00Z"})
        self.assertEqual(self.store.details(first_edit), {"content_before": "First text", "content_after": "Second text"})
        self.assertNotIn("First text", first_edit["payload"])
        self.assertNotIn("Second text", first_edit["payload"])
        post = render(first_edit, ["message_updated"], {}, self.store.settings("1")["values"], self.cfg.origin, self.store.details(first_edit))
        self.assertEqual([f["value"] for f in post["embeds"][0]["fields"]], ["First text", "Second text"])
        with self.store.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE event_details SET body='{}'")
        self.assertTrue(self.store.verify())
