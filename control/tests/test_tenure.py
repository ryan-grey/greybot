import asyncio
import base64
from datetime import date,datetime,timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from greybot_control import tenure as t
from greybot_control.store import Store


class TenureTests(unittest.TestCase):
    def test_completed_years(self):
        m={'user':{'id':'3'},'joined_at':'2024-09-12T18:00:00Z'}
        self.assertEqual(t.years(m,date(2026,9,11)),1)
        self.assertEqual(t.years(m,date(2026,9,12)),2)
        self.assertEqual(t.years({**m,'joined_at':'2026-09-12T18:00:00Z'},date(2026,9,12)),0)
        self.assertEqual(t.years({**m,'joined_at':'2024-02-29T18:00:00Z'},date(2025,2,28)),1)
        self.assertIsNone(t.years({**m,'user':{'bot':True}},date(2026,9,12)))

    def test_icons_are_png(self):
        for years in (0,1,4,10,25):
            raw=base64.b64decode(t.icon(years).split(',')[1])
            self.assertTrue(raw.startswith(b'\x89PNG'))
            self.assertLess(len(raw),256000)

    def test_reconcile_preserves_staff_and_other_roles(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'store.sqlite3');t.install(store)
            with store.connection() as db:db.execute('INSERT INTO tenure_config VALUES(?)',('1',))
            roles=[{'id':'staff','permissions':'8','icon':'staff-icon'}]
            members=[{'user':{'id':str(i)},'joined_at':'2024-09-12T18:00:00Z','roles':held}
                     for i,held in enumerate((['member'],['staff'],[]),3)]
            writes=[]
            class API:
                async def request(self,method,path,**kw):
                    if method=='GET':return roles if path.endswith('/roles') else members
                    writes.append((method,path,kw))
                    if method=='POST':
                        role={**kw['body'],'id':'badge'};roles.append(role);return role
                    if method=='PUT':
                        next(m for m in members if m['user']['id']==path.split('/')[-3])['roles'].append('badge')
            cfg=SimpleNamespace(guild_id='1',enforce=True)
            now=datetime(2026,9,12,18,tzinfo=timezone.utc)
            with patch.object(t.asyncio,'sleep',new=AsyncMock()):
                self.assertEqual(asyncio.run(t.tick(cfg,store,API(),now)),2)
                self.assertEqual(asyncio.run(t.tick(cfg,store,API(),now)),0)
            self.assertEqual(members[1]['roles'],['staff'])
            self.assertEqual(members[0]['roles'],['member','badge'])
            self.assertEqual(roles[-1]['permissions'],'0')
            self.assertFalse(roles[-1]['hoist'])
            self.assertEqual(len(writes),3)

    def test_uncertain_create_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'store.sqlite3');t.install(store)
            with store.connection() as db:db.execute('INSERT INTO tenure_config VALUES(?)',('1',))
            calls=[]
            class API:
                async def request(self,method,path,**kw):
                    if method=='GET':return [] if path.endswith('/roles') else [{'user':{'id':'3'},'joined_at':'2024-01-01T18:00:00Z','roles':[]}]
                    calls.append(method);raise RuntimeError('Ambiguous create')
            for _ in range(2):
                with self.assertRaises(RuntimeError):asyncio.run(t.tick(SimpleNamespace(guild_id='1',enforce=True),store,API(),datetime(2026,9,12,tzinfo=timezone.utc)))
            self.assertEqual(calls,['POST'])
