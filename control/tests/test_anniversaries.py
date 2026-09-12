import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from greybot_control import anniversaries as a
from greybot_control.store import Store


class AnniversaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'test.sqlite3')
        a.install(self.store)
        with self.store.connection() as db:
            db.execute('INSERT INTO anniversary_config VALUES(?,?)',('1','2'))
        self.cfg = SimpleNamespace(guild_id='1',enforce=True)
        self.member = {'user':{'id':'3','username':'AccountName'},'nick':'Server Name','joined_at':'2024-09-12T18:00:00Z'}
        self.now = datetime(2026,9,12,14,tzinfo=timezone.utc)

    def test_dates_leap_day_rejoin_and_bots(self):
        self.assertEqual(a.years_due(self.member,date(2026,9,12)),2)
        self.assertEqual(a.years_due(self.member,date(2026,9,13)),0)
        self.assertEqual(a.years_due({**self.member,'joined_at':'2026-09-12T12:00:00Z'},date(2026,9,12)),0)
        self.assertEqual(a.years_due({**self.member,'joined_at':'2024-02-29T18:00:00Z'},date(2025,2,28)),1)
        self.assertEqual(a.years_due({**self.member,'joined_at':'2024-09-13T01:00:00Z'},date(2026,9,12)),2)
        self.assertEqual(a.years_due({**self.member,'user':{'bot':True}},date(2026,9,12)),0)
        self.assertEqual(a.years_due({**self.member,'joined_at':None},date(2026,9,12)),0)

    def test_live_name_and_only_member_mentioned(self):
        body=a.post('1',self.member,2)
        self.assertEqual(body['embeds'][0]['author']['name'],'Server Name')
        self.assertEqual(body['allowed_mentions'],{'parse':[],'users':['3']})
        self.assertNotIn('admin',str(body))

    def test_delivery_restart_and_ambiguous_error_never_duplicate(self):
        for fail in (False,True):
            with self.store.connection() as db:db.execute('DELETE FROM anniversary_delivery')
            calls=[]
            member=self.member
            class API:
                async def request(self,method,path,**kwargs):
                    if method=='GET':return [member]
                    calls.append(kwargs['body'])
                    if fail:raise RuntimeError('Uncertain delivery')
                    return {'id':'4'}
            for _ in range(2):
                try:asyncio.run(a.tick(self.cfg,self.store,API(),self.now))
                except RuntimeError:pass
            self.assertEqual(len(calls),1)
            with self.store.connection() as db:
                state=db.execute('SELECT state FROM anniversary_delivery').fetchone()['state']
            self.assertEqual(state,'unknown' if fail else 'published')

    def test_before_ten_and_disabled_do_not_fetch(self):
        class API:
            async def request(self,*args,**kwargs):raise AssertionError('Unexpected request')
        asyncio.run(a.tick(self.cfg,self.store,API(),self.now.replace(hour=13)))
        self.cfg.enforce=False
        asyncio.run(a.tick(self.cfg,self.store,API(),self.now))
