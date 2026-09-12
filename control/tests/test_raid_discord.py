import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from greybot_control import raids, raid_discord as service
from greybot_control.discord_api import Denied, Unavailable
from greybot_control.store import Store


class RaidDiscordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "control.sqlite3")
        raids.install(self.store)
        self.cfg = SimpleNamespace(guild_id="1", client_id="9", origin="https://example.test", enforce=True)
        self.env = patch.dict("os.environ", {"GREYBOT_RAIDS_ENABLED": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.packet = {"id": "123", "type": 2, "guild_id": "1", "application_id": "9", "channel_id": "2",
                       "member": {"user": {"id": "3"}, "permissions": "32"},
                       "data": {"name": "create"}, "token": "never-store-interaction-token"}

    def test_creation_form_requires_management_and_never_persists_token(self):
        result = service.receive(self.cfg, self.store, self.packet)
        self.assertEqual(result["type"], 9)
        self.assertEqual(self.store.jobs("1"), [])
        self.packet["member"]["permissions"] = "0"
        with self.assertRaises(Denied):
            service.receive(self.cfg, self.store, self.packet)

    def test_quickcreate_queues_only_allowlisted_fields(self):
        self.packet["data"] = {"name": "quickcreate", "options": [
            {"name": "title", "value": "Example"}, {"name": "when", "value": self.when}]}
        service.receive(self.cfg, self.store, self.packet)
        service.receive(self.cfg, self.store, self.packet)
        self.assertEqual(len(self.store.jobs("1")), 1)
        self.assertNotIn("never-store", str(self.store.jobs("1")) + str(self.store.events("1")))
        with patch.dict("os.environ", {"GREYBOT_RAIDS_ENABLED": "0"}), self.assertRaises(Denied):
            service.receive(self.cfg, self.store, self.packet)

    def test_wrong_server_and_unzoned_time_rejected(self):
        with self.assertRaises(Denied):
            service.receive(self.cfg, self.store, {**self.packet, "guild_id": "99"})
        for value in ("2026-09-12T18:00", "tomorrow", "2000-01-01T00:00Z"):
            with self.assertRaises(Denied):
                service.parse_start(value)

    def test_role_buttons_filter_private_specializations_without_page_buttons(self):
        event = {"title": "Raid", "leaderId": "3", "channelId": "2", "startTime": 9999999999,
                 "closingTime": 9999999999, "state": "open", "signUps": [],
                 "classes": [{"name": role, "specs": [{"name": role+str(i), "roleName": role} for i in range(10)]}
                             for role in ("Tank", "Melee", "Ranged", "Healer")]}
        eid = raids.create(self.store, "1", "3", "role-test", event)
        packet = {**self.packet, "type": 3, "message": {"author": {"id": "9"}, "flags": 64}}
        card = service.card(self.cfg, raids.read(self.store, "1", eid), {})
        self.assertEqual([b["label"] for b in card["components"][0]["components"]], ["Tank", "Melee", "Ranged", "Healer"])
        for role in service.ROLE_EMOJIS:
            packet["data"] = {"custom_id": service.PREFIX+"group:"+eid+":"+role}
            result = service.receive(self.cfg, self.store, packet)
            self.assertEqual(result["data"]["flags"], 64)
            self.assertEqual(len(result["data"]["components"]), 1)
            options = result["data"]["components"][0]["components"][0]["options"]
            self.assertEqual(len(options), 10)
            self.assertTrue(all(o["label"].startswith(role+" · ") for o in options))

    def test_missing_cached_identity_uses_preserved_signup_name(self):
        event = {"title": "Raid", "description": "", "leaderId": "3", "startTime": 9999999999,
                 "closingTime": 9999999999, "state": "open", "signUps": [
                     {"userId": "4", "name": "Original nickname", "className": "Tank"}]}
        card = service.card(self.cfg, {"id": "abc", "body": event}, {"4": {"name": "Unknown member"}})
        self.assertEqual(card["embeds"][0]["fields"][0]["value"], "1. Original nickname")

    def test_numbered_spec_keys_are_only_cleaned_for_display(self):
        event={'title':'Raid','leaderId':'3','startTime':9999999999,'closingTime':9999999999,'state':'open',
               'classes':[{'name':'Death Knight','specs':[{'name':'Frost1','roleName':'Melee'}]}],
               'signUps':[{'userId':'4','name':'Example','className':'Death Knight','specName':'Frost1','roleName':'Melee'}]}
        embed=service.card(self.cfg,{'id':'abc','body':event},{})['embeds'][0]
        self.assertTrue(embed['fields'][0]['value'].endswith(' · Frost'))
        self.assertEqual(event['signUps'][0]['specName'],'Frost1')
        self.assertEqual(raids.choices(event)[0]['label'],'Death Knight · Frost')
        self.assertEqual(raids.choices(event)[0]['specName'],'Frost1')
        self.assertEqual(raids.spec_label('Restoration1'),'Restoration')

    def test_totals_exclude_absence_and_bench_and_space_members(self):
        groups = ['Tanks'] * 2 + ['Melee'] * 3 + ['Ranged'] * 6 + ['Healers'] * 4 + ['Late'] + ['Tentative'] * 2 + ['Absence'] * 3 + ['Bench']
        event = {'title':'Raid','leaderId':'3','startTime':9999999999,'closingTime':9999999999,
                 'state':'open','signUps':[{'userId':str(i),'name':f'Member{i}','roleName':role} for i,role in enumerate(groups)]}
        embed = service.card(self.cfg, {'id':'abc','body':event}, {})['embeds'][0]
        self.assertTrue(embed['description'].startswith('**Signups: 15 (+3)**'))
        counts = embed['description'].split('\n')[1].split('\u00a0' * 5)
        self.assertEqual([part.rsplit(' ',1)[1] for part in counts],['2','4','9'])
        self.assertNotIn(';',embed['description'].split('\n')[1])
        self.assertNotIn('confirmed',embed['description'])
        self.assertEqual(len(embed['fields']),11)
        self.assertIn('1. Member0\n2. Member1',embed['fields'][0]['value'])
        self.assertTrue(all('\n\n' not in f['value'] for f in embed['fields']))
        self.assertIn('Healers',embed['fields'][1]['name'])
        self.assertEqual(embed['fields'][2],{'name':'\u200b','value':'\u200b','inline':False})
        self.assertIn('Melee',embed['fields'][3]['name'])
        self.assertIn('Ranged',embed['fields'][4]['name'])
        self.assertEqual([f['name'].split(' · ')[0] for f in embed['fields'][-3:]],['❌ Absence','❔ Tentative','🪑 Bench'])
        self.assertTrue(all(f['inline'] for f in embed['fields'][-3:]))
        self.assertTrue(all(f['inline'] for f in embed['fields'][:2]+embed['fields'][3:5]))

    def test_card_is_bounded_and_mentions_are_disabled(self):
        event = {"title": "x"*200, "description": "y"*3500, "leaderId": "3", "startTime": 9999999999,
                 "closingTime": 9999999999, "state": "open", "signUps": [
                     {"userId": str(i), "roleName": "R"*100+str(i%6), "name": "@everyone"*10,
                      "specName": "z"*100} for i in range(300)]}
        result = service.card(self.cfg, {"id": "abc", "body": event}, {})
        embed = result["embeds"][0]
        total = len(embed["title"])+len(embed["description"])+len(embed["author"]["name"])+len(embed["footer"]["text"])
        total += sum(len(f["name"])+len(f["value"]) for f in embed["fields"])
        self.assertLessEqual(total, 6000)
        self.assertEqual(result["allowed_mentions"], {"parse": []})
        self.assertNotIn("@everyone", json.dumps(embed))
        self.assertNotIn("open roster", json.dumps(embed))
        self.assertTrue(all(line.split('. ',1)[0].isdigit() for f in embed['fields'] if f['name']!='\u200b' for line in f['value'].split('\n')))
        self.assertEqual(sum(len(f['value'].removesuffix('\n\u200b').split('\n')) for f in embed['fields']),300)

    def test_ambiguous_first_post_is_not_repeated(self):
        event = {**service.template(self.store, "1", "standard"), "title": "Example", "description": "",
                 "leaderId": "3", "channelId": "2", "startTime": 9999999999, "closingTime": 9999999999}
        eid = raids.create(self.store, "1", "3", "new", event)
        class API:
            calls = 0
            async def request(self, method, path, **kwargs):
                self.calls += 1
                raise Unavailable("Ambiguous write")
        class Archive:
            def flush(self, store):
                pass
        api = API()
        async def directory(*args):
            return {"members": []}
        with patch.object(self.store, "pending", return_value=[]), patch("greybot_control.directory.Directory.get", directory):
            with self.assertRaises(Unavailable):
                asyncio.run(service.deliver_one(self.cfg, self.store, api, Archive()))
            asyncio.run(service.deliver_one(self.cfg, self.store, api, Archive()))
        self.assertEqual(api.calls, 1)
        self.assertEqual(raids.read(self.store, "1", eid)["delivery"], "unknown")

    def test_text_refresh_edits_same_message_and_clears_old_image(self):
        event = {**service.template(self.store, '1', 'standard'), 'title':'Example', 'description':'',
                 'leaderId':'3','channelId':'2','startTime':9999999999,'closingTime':9999999999}
        eid=raids.create(self.store,'1','3','image-test',event)
        calls=[]
        class API:
            async def request(self,method,path,**kwargs):
                calls.append((method,path,kwargs))
                return {'id':'99'}
        class Archive:
            def flush(self,store): pass
        async def directory(*args): return {'members':[]}
        with patch.object(self.store,'pending',return_value=[]), patch('greybot_control.directory.Directory.get',directory):
            asyncio.run(service.deliver_one(self.cfg,self.store,API(),Archive()))
            with self.store.connection() as db:
                db.execute('UPDATE raid_events SET revision=revision+1 WHERE id=?',(eid,))
            asyncio.run(service.deliver_one(self.cfg,self.store,API(),Archive()))
        self.assertEqual([c[0] for c in calls],['POST','PATCH'])
        self.assertEqual(calls[1][1],'/channels/2/messages/99')
        for index,(_,_,kwargs) in enumerate(calls):
            self.assertTrue(kwargs['body']['components'])
            self.assertIn('fields',kwargs['body']['embeds'][0])
            self.assertNotIn('image',kwargs['body']['embeds'][0])
            self.assertNotIn('files',kwargs)
            self.assertEqual(kwargs['body']['attachments'],[])
