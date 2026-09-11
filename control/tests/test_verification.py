import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from greybot_control.config import Config
from greybot_control.discord_api import Denied
from greybot_control.store import Store
from greybot_control.verification import execute, receive, validate_challenge
from greybot_control.web import create_app


class API:
    def __init__(self):
        self.member = {"user": {"id": "8", "username": "new-member"}, "roles": [], "pending": False, "joined_at": "first-join"}
        self.writes = []

    async def request(self, method, path, **kwargs):
        if method != "GET":
            self.writes.append((method, path)); return None
        if path.endswith("/roles"):
            return [{"id": "4", "name": "Members", "permissions": "0", "position": 1}]
        return self.member

    async def member_context(self, user):
        return {"permissions": 8, "position": 10}

    async def require_admin(self, user):
        raise Denied("Not an admin")

    async def identity(self, code):
        return "8"

    async def close(self):
        pass


class VerificationTests(unittest.TestCase):
    def test_help_is_private_and_never_queues_role_grants(self):
        from dataclasses import replace
        from greybot_control.verification import receive_help
        cfg = replace(self.cfg, start_channel_id="12")
        packet = {"type":3, "application_id":cfg.client_id, "guild_id":cfg.guild_id,
                  "channel_id":"12", "member":{"user":{"id":"8"}},
                  "message":{"author":{"id":cfg.client_id}},
                  "data":{"custom_id":"greybot:verification_help"}}
        response = receive_help(cfg, self.store, packet)
        self.assertEqual(response["data"]["flags"], 64)
        self.assertEqual(response["data"]["allowed_mentions"], {"parse":[]})
        self.assertIn("does not notify staff", response["data"]["content"])
        self.assertEqual(self.store.jobs(cfg.guild_id), [])
        import time
        from nacl.signing import SigningKey
        key = SigningKey.generate()
        raw = json.dumps(packet).encode()
        timestamp = str(int(time.time()))
        headers = {"x-signature-timestamp":timestamp,
                   "x-signature-ed25519":key.sign(timestamp.encode() + raw).signature.hex()}
        with patch.dict(os.environ, {"GREYBOT_DISCORD_PUBLIC_KEY":key.verify_key.encode().hex()}):
            with TestClient(create_app(cfg, self.store, self.api), base_url=cfg.origin) as client:
                result = client.post("/discord/roles", content=raw, headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["data"]["flags"], 64)
                self.assertEqual(client.post("/discord/roles", content=raw).status_code, 401)
        for field, value in (("guild_id", "other"), ("channel_id", "private"), ("application_id", "other")):
            with self.assertRaises(Denied):
                receive_help(cfg, self.store, {**packet, field:value})

    def test_non_admin_full_flow_archives_grant_without_private_channel_access(self):
        from greybot_control.local_archive import LocalArchive
        from greybot_control.worker import execute_one
        from greybot_control.onboarding_gate import plan, VIEW
        from greybot_control.mutes import effective_permissions
        roles = [{"id": "1", "permissions": str(VIEW), "position": 0},
                 {"id": "4", "name": "Members", "permissions": "0", "position": 1}]
        channels = [{"id": "6", "type": 0, "permission_overwrites": []},
                    {"id": "10", "type": 0, "permission_overwrites": []},
                    {"id": "11", "type": 0, "permission_overwrites": [
                        {"id": "1", "type": 0, "allow": "0", "deny": str(VIEW)}]}]
        planned = plan("1", roles, channels, "4", "6")
        def visible():
            return [c["id"] for c in planned["channels"] if
                    effective_permissions("1", planned["roles"], self.api.member, c) & VIEW]
        self.assertEqual(visible(), ["6"])
        original = self.api.request
        async def request(method, path, **kwargs):
            result = await original(method, path, **kwargs)
            if method == "PUT":
                self.assertFalse(self.store.pending(1), "Role grant preceded archival")
                self.assertEqual(path, "/guilds/1/members/8/roles/4")
                self.api.member["roles"].append("4")
            return result
        self.api.request = request
        self.cfg.archive_dir.mkdir()
        archive = LocalArchive(self.cfg.archive_dir)
        with TestClient(create_app(self.cfg, self.store, self.api), base_url=self.cfg.origin) as client:
            state = self.store.oauth_state("verify")
            client.cookies.set("greybot-local-login", state)
            result = client.get("/auth/callback", params={"state": state, "code": "test"}, follow_redirects=False)
            self.assertEqual(result.status_code, 303)
            status = client.get("/api/verification").json()
            self.assertFalse(status["verified"])
            headers = {"origin": self.cfg.origin, "x-csrf-token": status["csrf"]}
            proof = {"success": True, "hostname": "127.0.0.1", "action": "greybot-verify", "cdata": "8"}
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
                       return_value=httpx.Response(200, json=proof)):
                result = client.post("/api/verification", headers=headers, json={"token": "isolated-test-proof"})
            self.assertEqual(result.json(), {"queued": True})
            asyncio.run(execute_one(self.cfg, self.store, self.api, archive))
            archive.flush(self.store)
            self.assertEqual(self.store.jobs("1")[0]["state"], "completed")
            self.assertTrue(client.get("/api/verification").json()["verified"])
            self.assertEqual(visible(), ["6", "10"])
            self.assertEqual(client.get("/api/events").status_code, 401)
            client.cookies.set("greybot-local", client.cookies.get("greybot-local-verify"))
            self.assertEqual(client.get("/api/events").status_code, 403)
            self.assertTrue(self.store.verify())
            self.assertFalse(self.store.pending(1))
            self.assertNotIn("isolated-test-proof", str(self.store.events("1")))

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.cfg = Config("1", "9", "test", "test", "http://127.0.0.1:8080", root / "state", enforce=True, archive_dir=root / "archive")
        self.store = Store(root / "state" / "control.sqlite3")
        values = self.store.settings("1")["values"]
        self.store.save_settings("1", "7", 0, {**values, "verification_enabled": True, "verification_role": "4", "welcome_channel": "6"})
        self.api = API()
        self.env = patch.dict(os.environ, {"GREYBOT_TURNSTILE_SITE_KEY": "test-site", "GREYBOT_TURNSTILE_SECRET": "test-provider-secret"})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_private_click_response_is_bound_to_server_and_bot_message(self):
        packet = {"type": 3, "application_id": "9", "guild_id": "1", "channel_id": "6",
                  "member": {"user": {"id": "8"}}, "message": {"author": {"id": "9"}},
                  "data": {"custom_id": "greybot:verify"}}
        result = receive(self.cfg, self.store, packet)
        self.assertEqual(result["data"]["flags"], 64)
        self.assertEqual(self.store.jobs("1"), [])
        for delta in ({"guild_id": "2"}, {"channel_id": "2"}, {"message": {"author": {"id": "2"}}}):
            with self.assertRaises(Denied): receive(self.cfg, self.store, {**packet, **delta})

    def test_verification_cookie_does_not_open_admin_routes(self):
        token = self.store.session("8", ttl=1800)
        with TestClient(create_app(self.cfg, self.store, self.api), base_url=self.cfg.origin) as client:
            client.cookies.set("greybot-local-verify", token)
            self.assertEqual(client.get("/api/verification").status_code, 200)
            self.assertEqual(client.get("/api/events").status_code, 401)
            client.cookies.set("greybot-local", token)
            self.assertEqual(client.get("/api/events").status_code, 403)

    def test_csrf_screening_and_proof_before_queue(self):
        token = self.store.session("8", ttl=1800)
        headers = {"origin": self.cfg.origin, "x-csrf-token": self.store.get_session(token)["csrf"]}
        with TestClient(create_app(self.cfg, self.store, self.api), base_url=self.cfg.origin) as client:
            client.cookies.set("greybot-local-verify", token)
            self.assertEqual(client.post("/api/verification", json={"token": "proof"}).status_code, 403)
            self.api.member["pending"] = True
            with patch("greybot_control.verification.validate_challenge", new_callable=AsyncMock) as proof:
                self.assertEqual(client.post("/api/verification", headers=headers, json={"token": "proof"}).status_code, 403)
                proof.assert_not_called()
                self.api.member["pending"] = False
                self.assertEqual(client.post("/api/verification", headers=headers, json={"token": "private-provider-proof"}).status_code, 200)
                proof.assert_awaited_once_with("private-provider-proof", "8", self.cfg.origin)
        self.assertEqual(len(self.store.jobs("1")), 1)
        self.assertNotIn("private-provider-proof", str(self.store.jobs("1")) + str(self.store.events("1")))

    def test_provider_checks_hostname_action_and_user(self):
        valid = {"success": True, "hostname": "127.0.0.1", "action": "greybot-verify", "cdata": "8"}
        for result in (valid, {**valid, "success": False}, {**valid, "hostname": "other.example"},
                       {**valid, "action": "other"}, {**valid, "cdata": "7"}, []):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=httpx.Response(200, json=result)):
                if result == valid:
                    asyncio.run(validate_challenge("proof", "8", self.cfg.origin))
                else:
                    with self.assertRaises(Denied): asyncio.run(validate_challenge("proof", "8", self.cfg.origin))

    def test_worker_rechecks_membership_after_proof(self):
        self.store.queue("verified-job", "1", "8", "verify_role", "8", {"role": "4", "joined_at": "first-join"})
        job = self.store.jobs("1")[0]
        self.api.member["joined_at"] = "rejoined"
        with self.assertRaises(Denied): asyncio.run(execute(self.cfg, self.store, self.api, job))
        self.assertEqual(self.api.writes, [])
        self.api.member["joined_at"] = "first-join"
        asyncio.run(execute(self.cfg, self.store, self.api, job))
        self.assertEqual(self.api.writes, [("PUT", "/guilds/1/members/8/roles/4")])

    def test_expired_verification_never_assigns_role(self):
        self.store.queue("expired-proof", "1", "8", "verify_role", "8", {"role":"4", "joined_at":"first-join"})
        job = dict(self.store.jobs("1")[0])
        job["created"] = 0
        with self.assertRaises(Denied):
            asyncio.run(execute(self.cfg,self.store,self.api,job))
        self.assertEqual(self.api.writes, [])

    def test_member_login_uses_separate_short_session(self):
        with TestClient(create_app(self.cfg, self.store, self.api), base_url=self.cfg.origin) as client:
            state = self.store.oauth_state("verify")
            client.cookies.set("greybot-local-login", state)
            response = client.get("/auth/callback", params={"state": state, "code": "test"}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/verify")
            self.assertIn("greybot-local-verify", response.cookies)
            self.assertNotIn("greybot-local", response.cookies)
