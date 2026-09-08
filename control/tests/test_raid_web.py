import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch

from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from greybot_control import raids
from greybot_control.config import Config
from greybot_control.discord_api import Denied
from greybot_control.store import Store, canonical
from greybot_control.web import create_app


class API:
    def __init__(self):
        self.roles = [{"id": "1", "permissions": "3072"}, {"id": "6", "permissions": "32"}]
        self.channel = {"id": "2", "guild_id": "1", "name": "signups", "type": 0, "permission_overwrites": []}
    async def close(self):
        pass
    async def identity(self, code):
        return "4"
    async def require_admin(self, user):
        raise Denied("Not an administrator")
    async def request(self, method, path, **kwargs):
        assert method == "GET"
        if path.endswith("/roles"):
            return self.roles
        if path.endswith("/channels"):
            return [self.channel]
        if path.startswith("/channels/"):
            return self.channel
        user = path.rsplit("/", 1)[1]
        return {"user": {"id": user, "username": "Example"}, "roles": ["6"] if user == "3" else []}


class RaidWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "control.sqlite3")
        self.cfg = Config("1", "9", "test", "test", "http://127.0.0.1:8080", root, enforce=True, archive_dir=root / "archive")
        self.api = API()
        self.client = TestClient(create_app(self.cfg, self.store, self.api), base_url=self.cfg.origin)
        self.addCleanup(self.client.close)
        self.token = self.store.session("4")
        self.client.cookies.set("greybot-local-raids", self.token)
        self.csrf = self.store.get_session(self.token)["csrf"]
        self.headers = {"origin": self.cfg.origin, "x-csrf-token": self.csrf}
        self.event = {"title": "Example", "startTime": time.time()+86400, "closingTime": time.time()+86400,
                      "channelId": "2", "leaderId": "3", "classes": [{"name": "Attending", "type": "primary"}]}
        self.id = raids.create(self.store, "1", "3", "create", self.event)
        with self.store.connection() as db:
            db.execute("INSERT INTO display_directory VALUES(?,?,?)", ("1",canonical({"members": [
                {"id": "4", "name": "Server nickname", "active": True, "bot": False, "roles": []}],
                "roles": [], "channels": [], "server": {"id": "1", "name": "Example"}}), time.time()))
        self.env = patch.dict("os.environ", {"GREYBOT_RAIDS_ENABLED": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_no_session_cannot_read_rosters_and_member_session_cannot_read_admin_logs(self):
        self.assertEqual(self.client.get("/api/events").status_code, 401)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/raids").status_code, 401)

    def test_current_channel_access_filters_rosters(self):
        result = self.client.get("/api/raids")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["events"]), 1)
        self.assertEqual(result.json()["channels"], [])
        self.api.channel["permission_overwrites"] = [{"type": 1, "id": "4", "allow": "0", "deny": "1024"}]
        self.assertEqual(self.client.get("/api/raids").json()["events"], [])

    def test_signup_requires_csrf_and_rechecks_access(self):
        body = {"operation": "signup", "raid_id": self.id, "value": "0:0", "request_id": "a"*32}
        self.assertEqual(self.client.post("/api/raids",json=body).status_code, 403)
        self.assertEqual(self.client.post("/api/raids",json=body,headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/api/raids",json=body,headers=self.headers).status_code, 200)
        self.assertEqual(len(self.store.jobs("1")), 1)
        self.api.channel["permission_overwrites"] = [{"type": 1, "id": "4", "allow": "0", "deny": "1024"}]
        self.assertEqual(self.client.post("/api/raids",json={**body,"request_id":"b"*32},headers=self.headers).status_code, 403)

    def test_member_cannot_manage_another_leaders_event(self):
        body = {"operation": "close", "raid_id": self.id, "revision": 1, "request_id": "c"*32}
        self.assertEqual(self.client.post("/api/raids",json=body,headers=self.headers).status_code, 403)
        self.assertEqual(self.store.jobs("1"), [])

    def test_worker_switch_blocks_mutations(self):
        with patch.dict("os.environ", {"GREYBOT_RAIDS_ENABLED":"0"}):
            result = self.client.post("/api/raids",json={},headers=self.headers)
        self.assertEqual(result.status_code, 503)

    def test_calendar_download_rechecks_channel_access(self):
        result = self.client.get(f"/api/raids/{self.id}/calendar.ics")
        self.assertEqual(result.status_code, 200)
        self.assertIn("BEGIN:VCALENDAR", result.text)
        self.api.channel["permission_overwrites"] = [{"type": 1, "id": "4", "allow": "0", "deny": "1024"}]
        self.assertNotEqual(self.client.get(f"/api/raids/{self.id}/calendar.ics").status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/raids/{self.id}/calendar.ics").status_code, 401)

    def test_calendar_escapes_text_and_folds_utf8_without_splitting_characters(self):
        from greybot_control.raid_web import calendar
        body = calendar("test", {"startTime": 1789261200, "title": "Raid, heroic; "+"é"*90,
                                 "description": "First\r\nEND:VEVENT\nBack\\slash"})
        self.assertIn("DTSTART:20260913T010000Z\r\n", body)
        self.assertIn("SUMMARY:Raid\\, heroic\\;", body)
        self.assertIn("DESCRIPTION:First\\nEND:VEVENT\\nBack\\\\slash", body)
        self.assertEqual(body.split("\r\n").count("END:VEVENT"), 1)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in body.split("\r\n")))

    def test_member_oauth_returns_to_raids_without_admin_access(self):
        self.client.cookies.clear()
        login = self.client.get("/auth/login?destination=raids", follow_redirects=False)
        self.assertEqual(login.status_code, 303)
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        response = self.client.get("/auth/callback", params={"state":state,"code":"test"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/raids")
        self.assertEqual(self.client.get("/api/raids").status_code, 200)
        self.assertEqual(self.client.get("/api/events").status_code, 401)

    def test_signed_create_returns_modal_and_tampering_is_rejected(self):
        packet = {"id":"111","type":2,"application_id":"9","guild_id":"1","channel_id":"2",
                  "member":{"user":{"id":"3"},"permissions":"32"},"data":{"name":"create"}}
        raw=json.dumps(packet).encode()
        key=SigningKey.generate()
        timestamp=str(int(time.time()))
        headers={"x-signature-timestamp":timestamp,"x-signature-ed25519":key.sign(timestamp.encode()+raw).signature.hex()}
        with patch.dict("os.environ",{"GREYBOT_DISCORD_PUBLIC_KEY":key.verify_key.encode().hex()}):
            self.assertEqual(self.client.post("/discord/roles",content=raw,headers=headers).json()["type"],9)
            self.assertEqual(self.client.post("/discord/roles",content=raw+b" ",headers=headers).status_code,401)
