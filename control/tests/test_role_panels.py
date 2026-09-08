import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from greybot_control.config import Config
from greybot_control.discord_api import Denied
from greybot_control.role_panels import execute, receive, validate
from greybot_control.store import Store
from greybot_control.web import create_app


class API:
    def __init__(self):
        self.roles = [{"id": "4", "name": "Raiders", "permissions": "0", "position": 2},
                      {"id": "1", "name": "everyone", "permissions": "1024", "position": 0}]
        self.member = {"roles": [], "user": {"bot": False}}
        self.writes = []

    async def member_context(self, user):
        return {"permissions": 8, "position": 10}

    async def request(self, method, path, **kwargs):
        if method != "GET":
            self.writes.append((method, path))
            return {"id": "55"}
        if path.endswith("/roles"):
            return self.roles
        if path.startswith("/channels/"):
            return {"guild_id": "1", "permission_overwrites": []}
        return self.member

    async def close(self):
        pass


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "state" / "control.sqlite3")
        self.cfg = Config("1", "9", "test", "test", "http://127.0.0.1:8080", root / "state", enforce=True, archive_dir=root / "archive")
        settings = self.store.settings("1")
        self.store.save_settings("1", "7", 0, {**settings["values"], "self_roles": ["4"]})
        self.store.append("panel", "1", "ROLE_PANEL_PUBLISHED", "", {"message_id": "55", "channel_id": "6"})
        self.packet = {"id": "123", "type": 3, "application_id": "9", "guild_id": "1", "channel_id": "6",
                       "data": {"custom_id": "greybot:role:4"}, "message": {"id": "55"},
                       "member": {"user": {"id": "8", "bot": False}}, "token": "never-persist-this"}
        self.api = API()

    def test_verified_request_is_deduplicated_and_token_not_persisted(self):
        receive(self.cfg, self.store, self.packet)
        receive(self.cfg, self.store, self.packet)
        jobs = self.store.jobs("1")
        self.assertEqual(len(jobs), 1)
        self.assertNotIn("never-persist-this", str(jobs) + str(self.store.events("1")))

    def test_wrong_guild_role_panel_and_prefix_rejected(self):
        for delta in ({"guild_id": "2"}, {"application_id": "2"}, {"message": {"id": "56"}},
                      {"data": {"custom_id": "greybot:role:99"}}, {"data": {"custom_id": "4"}},
                      {"channel_id": "99"}):
            with self.subTest(delta=delta), self.assertRaises(Denied):
                receive(self.cfg, self.store, {**self.packet, **delta})

    def test_only_selected_role_is_added_or_removed(self):
        receive(self.cfg, self.store, self.packet)
        job = self.store.jobs("1")[0]
        asyncio.run(execute(self.cfg, self.store, self.api, job))
        self.assertEqual(self.api.writes, [("PUT", "/guilds/1/members/8/roles/4")])
        self.api.member["roles"] = ["4", "unrelated"]
        asyncio.run(execute(self.cfg, self.store, self.api, job))
        self.assertEqual(self.api.writes[-1], ("DELETE", "/guilds/1/members/8/roles/4"))

    def test_role_permission_change_and_screening_fail_closed(self):
        receive(self.cfg, self.store, self.packet)
        job = self.store.jobs("1")[0]
        self.api.roles[0]["permissions"] = "8"
        with self.assertRaises(Denied):
            asyncio.run(execute(self.cfg, self.store, self.api, job))
        self.api.roles[0]["permissions"] = "0"
        self.api.member["pending"] = True
        with self.assertRaises(Denied):
            asyncio.run(execute(self.cfg, self.store, self.api, job))
        self.assertEqual(self.api.writes, [])

    def test_base_role_is_never_a_self_service_role(self):
        values = self.store.settings("1")
        self.store.save_settings("1", "7", values["revision"], {**values["values"], "verification_role":"4"})
        with self.assertRaises(Denied):
            receive(self.cfg, self.store, self.packet)
        job={"body":json.dumps({"role":"4","channel":"6"}),"actor":"8","subject":"8","kind":"self_role","created":time.time()}
        with self.assertRaises(Denied):
            asyncio.run(execute(self.cfg,self.store,self.api,job))
        self.assertFalse(self.api.writes)

    def test_paused_verification_does_not_open_role_grants(self):
        values = self.store.settings("1")
        self.store.save_settings("1", "7", values["revision"], {**values["values"], "verification_role":"5", "verification_enabled":False})
        receive(self.cfg,self.store,self.packet)
        job=self.store.jobs("1")[0]
        with self.assertRaises(Denied):
            asyncio.run(execute(self.cfg,self.store,self.api,job))
        self.assertFalse(self.api.writes)
        self.api.member["roles"]=["5"]
        asyncio.run(execute(self.cfg,self.store,self.api,job))
        self.assertEqual(self.api.writes,[("PUT","/guilds/1/members/8/roles/4")])

    def test_signature_expiry_tampering_and_disabled_worker(self):
        key = SigningKey.generate()
        raw = json.dumps(self.packet).encode()
        now = str(int(time.time()))
        headers = {"x-signature-timestamp": now, "x-signature-ed25519": key.sign(now.encode() + raw).signature.hex()}
        with patch.dict(os.environ, {"GREYBOT_DISCORD_PUBLIC_KEY": key.verify_key.encode().hex()}):
            with TestClient(create_app(self.cfg, self.store, self.api), base_url=self.cfg.origin) as client:
                self.assertEqual(client.post("/discord/roles", content=raw + b" ", headers=headers).status_code, 401)
                self.assertEqual(client.post("/discord/roles", content=raw, headers={**headers, "x-signature-timestamp": "1"}).status_code, 401)
                result = client.post("/discord/roles", content=raw, headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["data"]["flags"], 64)
            with TestClient(create_app(replace(self.cfg, enforce=False), self.store, self.api), base_url=self.cfg.origin) as client:
                self.assertEqual(client.post("/discord/roles", content=raw, headers=headers).status_code, 503)
