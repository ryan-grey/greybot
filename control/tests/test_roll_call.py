import json
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from test_control import Base, FakeDiscord
from greybot_control import roll_call
from greybot_control.web import create_app

SECRET = "shared-secret"


LISTING = {"server": {"id": "1", "name": "Scrambled"}, "roles": [], "channels": [{"id": "10", "name": "Smoobies", "active": True}],
           "members": [{"id": "7", "name": "Pete (Mograin)", "avatar_url": "https://cdn.discordapp.com/a.png", "bot": False, "roles": [], "active": True},
                       {"id": "8", "name": "Pie", "avatar_url": "", "bot": False, "roles": [], "active": True},
                       {"id": "9", "name": "greyBot", "avatar_url": "", "bot": True, "roles": [], "active": True}]}


class PresentTests(Base):
    def voice(self, uid, channel):
        self.store.append(f"v{uid}{channel}{time.time_ns()}", self.cfg.guild_id, "VOICE_STATE_UPDATE", uid, {"channel_id": channel})

    def test_presence_is_as_of_the_moment_asked_about_not_now(self):
        self.voice("7", "10")
        self.voice("8", "11")
        before_leaving = time.time()
        time.sleep(0.01)
        self.voice("7", None)
        self.assertEqual(roll_call.present(self.store, self.cfg.guild_id, "10", before_leaving), ["7"])
        self.assertEqual(roll_call.present(self.store, self.cfg.guild_id, "10", time.time()), [])

    def test_a_resumed_connection_loses_nobody_but_a_fresh_session_vouches_for_no_one(self):
        self.voice("7", "10")
        self.store.append("drop", self.cfg.guild_id, "COLLECTOR_DISCONNECTED", "", {})
        self.store.append("back", self.cfg.guild_id, "COLLECTOR_RESUMED", "", {})
        self.assertEqual(roll_call.present(self.store, self.cfg.guild_id, "10", time.time()), ["7"])
        self.store.append("ready", self.cfg.guild_id, "COLLECTOR_CONNECTED", "", {})
        self.assertEqual(roll_call.present(self.store, self.cfg.guild_id, "10", time.time()), [])


class SignatureTests(Base):
    def test_signature_binds_the_body_and_expires(self):
        body = b'{"channel_id":"10","at":1}'
        good = f"t=1000,v1={roll_call.signature(SECRET, '1000', body)}"
        self.assertTrue(roll_call.verify(SECRET, good, body, now=1100))
        self.assertFalse(roll_call.verify(SECRET, good, body + b" ", now=1100))
        self.assertFalse(roll_call.verify(SECRET, good, body, now=1000 + roll_call.WINDOW + 1))
        self.assertFalse(roll_call.verify("other", good, body, now=1100))
        for junk in (None, "", "t=abc,v1=x", "v1=only"):
            self.assertFalse(roll_call.verify(SECRET, junk, body, now=1100))


class RouteTests(Base):
    def client(self, cfg):
        # A fresh saved directory, so the route answers from it rather than asking Discord.
        with self.store.connection() as db:
            db.execute("INSERT INTO display_directory VALUES(?,?,?)", (cfg.guild_id, json.dumps(LISTING), time.time()))
        client = TestClient(create_app(cfg, self.store, FakeDiscord()), base_url=cfg.origin)
        self.addCleanup(client.close)
        return client

    def post(self, client, payload, secret=SECRET):
        body = json.dumps(payload).encode()
        stamp = str(int(time.time()))
        return client.post("/internal/roll-call", content=body,
                           headers={"X-Greybot-Signature": f"t={stamp},v1={roll_call.signature(secret, stamp, body)}"})

    def test_route_does_not_exist_without_a_secret(self):
        self.assertEqual(self.post(self.client(self.cfg), {"channel_id": "10", "at": time.time()}).status_code, 404)

    def test_unsigned_and_wrongly_signed_requests_learn_nothing(self):
        client = self.client(replace(self.cfg, rollcall_secret=SECRET))
        self.assertEqual(client.post("/internal/roll-call", content=b"{}").status_code, 404)
        self.assertEqual(self.post(client, {"channel_id": "10", "at": time.time()}, secret="guess").status_code, 404)

    def test_signed_request_names_the_humans_in_that_channel(self):
        for index, (uid, channel) in enumerate((("7", "10"), ("8", "10"), ("9", "10"), ("6", "10"))):
            self.store.append(f"v{index}", self.cfg.guild_id, "VOICE_STATE_UPDATE", uid, {"channel_id": channel})
        client = self.client(replace(self.cfg, rollcall_secret=SECRET))
        answer = self.post(client, {"channel_id": "10", "at": time.time()})
        self.assertEqual(answer.status_code, 200)
        body = answer.json()
        # The bot and the member who has since left the server are not roll-call material.
        self.assertEqual((body["channel"], [m["id"] for m in body["members"]]), ("Smoobies", ["7", "8"]))
        self.assertEqual(self.post(client, {"channel_id": "x", "at": 1}).status_code, 400)
