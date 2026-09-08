"""Offline validation of poll permissions, input and signed dispatch."""
import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import polls
import interactions


class PollTests(unittest.TestCase):
    def setUp(self):
        self.body = {
            "type": 2, "guild_id": "100", "channel": {"type": 0},
            "member": {"permissions": str(polls.SEND_POLLS | polls.VIEW_CHANNEL | polls.SEND_MESSAGES)},
            "app_permissions": "8",
            "data": {"name": "poll", "options": [
                {"name": "question", "value": "Raid time?"},
                {"name": "answer1", "value": "Early"},
                {"name": "answer2", "value": "Late"}]}}

    def denied(self, body):
        self.assertEqual(polls.response(body, "100")["data"]["flags"], 64)

    def test_native_vote_and_expiry_payload(self):
        result = polls.response(self.body, "100")
        self.assertEqual(result["type"], 4)
        self.assertNotIn("flags", result["data"])
        self.assertEqual(result["data"]["poll"]["duration"], 24)
        self.assertEqual(len(result["data"]["poll"]["answers"]), 2)
        self.assertFalse(result["data"]["poll"]["allow_multiselect"])
        self.assertEqual(result["data"]["allowed_mentions"], {"parse": []})

    def test_permissions_and_server_boundary(self):
        for key, value in [("guild_id", "200"), ("member", {}),
                           ("app_permissions", "0")]:
            body = copy.deepcopy(self.body)
            body[key] = value
            self.denied(body)
        for permissions in ("0", str(polls.SEND_MESSAGES | polls.VIEW_CHANNEL), "bad", "-1"):
            body = copy.deepcopy(self.body)
            body["member"]["permissions"] = permissions
            self.denied(body)
        self.body["member"]["pending"] = True
        self.denied(self.body)

    def test_thread_permission(self):
        self.body["channel"]["type"] = 11
        self.denied(self.body)
        self.body["member"]["permissions"] = str(polls.VIEW_CHANNEL | polls.SEND_POLLS | polls.SEND_MESSAGES_IN_THREADS)
        self.assertIn("poll", polls.response(self.body, "100")["data"])

    def test_invalid_and_duplicate_answers(self):
        for name, value in [("question", " " ), ("question", "x" * 301),
                            ("answer1", "x" * 56), ("answer1", " LATE "),
                            ("answer2", None), ("duration", 0),
                            ("duration", True), ("multiple", "yes")]:
            body = copy.deepcopy(self.body)
            body["data"]["options"] = [o for o in body["data"]["options"] if o["name"] != name]
            body["data"]["options"].append({"name": name, "value": value})
            self.denied(body)

    def test_maximum_choices_and_settings(self):
        self.body["data"]["options"] += [
            {"name": f"answer{i}", "value": str(i)} for i in range(3, 11)]
        self.body["data"]["options"] += [{"name": "duration", "value": 168},
                                           {"name": "multiple", "value": True}]
        poll = polls.response(self.body, "100")["data"]["poll"]
        self.assertEqual(len(poll["answers"]), 10)
        self.assertEqual(poll["duration"], 168)
        self.assertTrue(poll["allow_multiselect"])

    def test_signed_dispatch_and_signature_rejection(self):
        import handler
        from nacl.signing import SigningKey
        key = SigningKey.generate()
        raw = json.dumps(self.body)
        stamp = str(int(datetime.now(timezone.utc).timestamp()))
        sig = key.sign((stamp + raw).encode()).signature.hex()
        event = {"body": raw, "headers": {"x-signature-timestamp": stamp,
                                             "x-signature-ed25519": sig}}
        cfg = {"public_key": key.verify_key.encode().hex(), "discord_guild_id": "100"}
        with patch.object(handler, "log"):
            result = handler.handle_interaction(event, cfg, None, datetime.now(timezone.utc))
            self.assertIn("poll", json.loads(result["body"])["data"])
            event["body"] = raw.replace("Early", "Changed")
            self.assertEqual(handler.handle_interaction(event, cfg, None,
                                                       datetime.now(timezone.utc))["statusCode"], 401)


if __name__ == "__main__":
    unittest.main()
