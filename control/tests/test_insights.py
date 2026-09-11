import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from greybot_control.store import Store
from greybot_control.insights import review_items, health_tick, health_status, scoreboard
from greybot_control.channel_choices import choices
from greybot_control.admin_roles import check
from greybot_control.discord_api import Denied
from types import SimpleNamespace

VIEW = 1 << 10

class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/"db")
        self.guild={"id":"1","owner_id":"99","rules_channel_id":"10"}
        self.roles=[{"id":"1","name":"everyone","permissions":"0","position":0},
                    {"id":"2","name":"Baby Dinosaur Princesses","permissions":str(VIEW),"position":1},
                    {"id":"3","name":"Raiders","permissions":"0","position":2},
                    {"id":"4","name":"Co-GM","permissions":"8","position":4}]
        self.member={"user":{"id":"7"},"roles":["2"]}

    def test_review_does_not_claim_admins_need_role_and_detects_unsynced(self):
        category={"id":"8","type":4,"permission_overwrites":[]}
        channel={"id":"9","type":0,"parent_id":"8","permission_overwrites":[{"id":"3","type":0,"allow":str(VIEW),"deny":"0"}]}
        admin={"user":{"id":"6"},"roles":["4"]}
        items=review_items(self.guild,self.roles,[category,channel],[self.member,admin])
        self.assertEqual([r["kind"] for r in items],["retire_role","unsynced_channel","alternate_access"])
        self.assertEqual(items[-1]["member"],"7")
        self.assertEqual(items[-1]["roles"],["3"])

    def test_channel_choices_cannot_restore_access_after_role_loss(self):
        channel={"id":"9","name":"raid","type":0,"permission_overwrites":[
            {"id":"1","type":0,"allow":"0","deny":str(VIEW)},
            {"id":"3","type":0,"allow":str(VIEW),"deny":"0"},
            {"id":"7","type":1,"allow":"0","deny":str(VIEW)}]}
        self.assertEqual(choices(self.guild,self.roles,[channel],self.member,{"9":True}, verified_role="2"),[])
        self.member["roles"].append("3")
        self.assertTrue(choices(self.guild,self.roles,[channel],self.member,{"9":True}, verified_role="2")[0]["hidden"])
        self.assertEqual(choices(self.guild,self.roles,[channel],self.member,{}, verified_role="2"),[])

    def test_protected_channel_id_survives_rename(self):
        channel = {"id": "9", "name": "renamed-arrivals", "type": 0, "permission_overwrites": []}
        self.assertEqual(choices(self.guild, self.roles, [channel], self.member, {}, ("9",), "2"), [])
        channel["id"] = "11"
        channel["name"] = "bots"
        self.assertEqual(choices(self.guild, self.roles, [channel], self.member, {}, ("9",), "2")[0]["id"], "11")

    def test_verified_role_uses_id_and_fails_closed_without_it(self):
        self.roles[1]["name"] = "Renamed membership"
        channel = {"id":"9", "name":"chat", "type":0, "permission_overwrites":[]}
        self.assertEqual(choices(self.guild, self.roles, [channel], self.member, {}, verified_role="2")[0]["id"], "9")
        self.roles.append({"id":"5", "name":"Baby Dinosaur Princesses", "permissions":str(VIEW), "position":1})
        for missing in ("", "deleted"):
            with self.assertRaises(Denied):
                choices(self.guild, self.roles, [channel], self.member, {}, verified_role=missing)

    def test_health_records_bounded_gaps_not_invented_outages(self):
        health_tick(self.store,"1",now=1000,uptime=100)
        health_tick(self.store,"1",now=1030,uptime=130)
        self.assertEqual(health_status(self.store,"1")["gaps"],[])
        health_tick(self.store,"1",now=1200,uptime=300)
        gap=health_status(self.store,"1")["gaps"][0]
        self.assertEqual((gap["last_seen"],gap["recovered"]),(1030,1200))
        self.assertFalse(gap["restart_confirmed"])
        health_tick(self.store,"1",now=1400,uptime=10)
        self.assertTrue(health_status(self.store,"1")["gaps"][0]["restart_confirmed"])
        self.assertTrue(self.store.verify())

    def test_activity_excludes_bots_and_other_guilds_includes_zero_activity(self):
        self.store.index_message("1","10","9","7","message",False)
        self.store.index_message("2","11","9","7","private elsewhere",False)
        self.store.index_message("1","12","9","8","bot",False)
        members=[{"id":uid,"name":uid,"active":True,"bot":uid=="8"} for uid in ("7","8","9")]
        result=scoreboard(self.store,"1",{"members":members})
        self.assertEqual([(r["member"],r["messages"]) for r in result["rows"]],[("7",1),("9",0)])

    def test_role_authority_blocks_managed_and_higher_roles(self):
        cfg=SimpleNamespace(guild_id="1")
        admin={"owner":False,"position":8,"permissions":8}
        bot={"position":7,"permissions":8}
        member={"position":2,"owner":False}
        check(cfg,admin,bot,member,{"id":"3","position":3})
        for role in ({"id":"3","position":7},{"id":"3","position":3,"managed":True}):
            with self.assertRaises(Denied):check(cfg,admin,bot,member,role)
        with self.assertRaises(Denied):check(cfg,admin,bot,{"position":7,"owner":False},{"id":"3","position":3})

    def test_channel_oauth_has_distinct_purpose(self):
        self.assertTrue(self.store.oauth_state("channels").startswith("channels."))

    def test_voice_time_does_not_cross_disconnect_and_health_shows_recovery(self):
        for event,kind,when,payload in (("a","VOICE_STATE_UPDATE",100,{"channel_id":"9"}),
            ("b","VOICE_STATE_UPDATE",160,{"channel_id":"9"}),
            ("c","COLLECTOR_DISCONNECTED",170,{}),("d","COLLECTOR_RESUMED",300,{}),
            ("e","VOICE_STATE_UPDATE",500,{"channel_id":None})):
            with patch("greybot_control.store.time.time",return_value=when):
                self.store.append(event,"1",kind,"7",payload)
        result=scoreboard(self.store,"1",{"members":[{"id":"7","name":"Sky","active":True,"bot":False}]})
        self.assertEqual(result["rows"][0]["voice_seconds"],60)
        gap=health_status(self.store,"1")["gaps"][0]
        self.assertEqual((gap["last_seen"],gap["recovered"]),(170,300))
        self.assertTrue(gap["connection"])
