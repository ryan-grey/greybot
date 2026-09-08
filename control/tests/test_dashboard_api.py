import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from fastapi.testclient import TestClient
from greybot_control.config import Config
from greybot_control.store import Store
from greybot_control.web import create_app
from greybot_control.local_archive import LocalArchive
from greybot_control.worker import execute_one
from greybot_control.discord_api import Denied


class API:
    def __init__(self):
        self.roles=[{"id":"1","name":"everyone","position":0,"permissions":"0"},
            {"id":"2","name":"Baby Dinosaur Princesses","position":1,"permissions":"1024"},
            {"id":"3","name":"Raiders","position":2,"permissions":"0"},
            {"id":"4","name":"Admin","position":4,"permissions":"8"},
            {"id":"5","name":"Bot","position":3,"permissions":"8"}]
        self.members=[{"user":{"id":uid,"username":"member"+uid,"bot":uid=="9"},"nick":"Server "+uid,"roles":roles}
                      for uid,roles in (("7",["4"]),("8",["2"]),("9",["5"]))]
        self.channels=[{"id":"10","guild_id":"1","type":0,"name":"general","permission_overwrites":[]}]
        self.writes=[]
    async def close(self):pass
    async def require_admin(self, user):
        if user!="7":raise Denied("Not admin")
        return await self.member_context(user)
    async def member_context(self,user):
        m=next(m for m in self.members if m["user"]["id"]==user)
        held=[r for r in self.roles if r["id"] in m["roles"]]
        return {"owner":False,"position":max(r["position"] for r in held),"permissions":8 if user in ("7","9") else 1024,"member":deepcopy(m)}
    async def request(self,method,path,body=None,**kwargs):
        if method!="GET":
            self.writes.append((method,path,body))
            if "/roles/" in path:
                uid,rid=path.split("/")[4],path.split("/")[6]
                m=next(m for m in self.members if m["user"]["id"]==uid)
                if method=="PUT":m["roles"].append(rid)
                else:m["roles"].remove(rid)
            else:
                self.channels[0]["permission_overwrites"]=[{"id":"8",**body}]
            return None
        if path.endswith("/roles"):return deepcopy(self.roles)
        if path.endswith("/channels"):return deepcopy(self.channels)
        if "/members?" in path:return deepcopy(self.members)
        if "/members/" in path:return deepcopy(next(m for m in self.members if m["user"]["id"]==path.rsplit("/",1)[1]))
        if path.startswith("/channels/"):return deepcopy(self.channels[0])
        return {"id":"1","name":"Scrambled test","owner_id":"99"}


class DashboardAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.store=Store(root/"db")
        self.cfg=Config("1","9","test","test","http://127.0.0.1:8080",root,enforce=True,archive_dir=root/"archive")
        self.cfg.archive_dir.mkdir()
        self.api=API();self.client=TestClient(create_app(self.cfg,self.store,self.api),base_url=self.cfg.origin)
        self.addCleanup(self.client.close)
    def login(self,uid="7",suffix=""):
        token=self.store.session(uid);self.client.cookies.set("greybot-local"+suffix,token)
        return {"Origin":self.cfg.origin,"X-CSRF-Token":self.store.get_session(token)["csrf"]}
    def test_admin_routes_recheck_auth_and_separate_member_session(self):
        for route in ("dashboard","review","activity"):
            self.assertEqual(self.client.get("/api/"+route).status_code,401)
        self.login("8","-channels")
        self.assertEqual(self.client.get("/api/dashboard").status_code,401)
        self.login("8")
        self.assertEqual(self.client.get("/api/dashboard").status_code,403)
        self.login()
        self.assertEqual(self.client.get("/api/dashboard").json()["member"]["name"],"Server 7")
    def test_roles_queued_once_then_archived_and_rechecked(self):
        headers=self.login();body={"request_id":"role-request-123456","members":["8"],"role":"3","operation":"add"}
        self.assertEqual(self.client.post("/api/member-roles",json=body).status_code,403)
        for _ in range(2):self.assertEqual(self.client.post("/api/member-roles",json=body,headers=headers).status_code,200)
        self.assertEqual(len(self.store.jobs("1")),1);self.assertEqual(self.api.writes,[])
        archive=LocalArchive(self.cfg.archive_dir)
        asyncio.run(execute_one(self.cfg,self.store,self.api,archive))
        self.assertEqual(self.store.jobs("1")[0]["state"],"completed")
        self.assertEqual(self.api.members[1]["roles"],["2","3"])
    def test_channel_preferences_cannot_leak_or_escalate(self):
        headers=self.login("8","-channels")
        listing=self.client.get("/api/channel-choices");self.assertEqual(listing.status_code,200)
        self.assertEqual([c["id"] for c in listing.json()["channels"]],["10"])
        body={"request_id":"channel-request-123456","channel":"10","hidden":True}
        self.assertEqual(self.client.post("/api/channel-choices",json={**body,"channel":"999"},headers=headers).status_code,400)
        self.assertEqual(self.client.post("/api/channel-choices",json=body,headers=headers).status_code,200)
        asyncio.run(execute_one(self.cfg,self.store,self.api,LocalArchive(self.cfg.archive_dir)))
        self.assertEqual(self.store.jobs("1")[0]["state"],"completed")
        self.assertEqual(self.api.channels[0]["permission_overwrites"][0]["allow"],"0")
        self.assertTrue(self.client.get("/api/channel-choices").json()["channels"][0]["hidden"])
        body.update(request_id="channel-request-654321",hidden=False)
        self.assertEqual(self.client.post("/api/channel-choices",json=body,headers=headers).status_code,200)
        asyncio.run(execute_one(self.cfg,self.store,self.api,LocalArchive(self.cfg.archive_dir)))
        self.assertEqual(self.api.channels[0]["permission_overwrites"][0]["deny"],"0")
