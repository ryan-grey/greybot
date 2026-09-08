import copy
from pathlib import Path
import tempfile
import time
import unittest

from greybot_control import raids
from greybot_control.raid_migrate import prepare
from greybot_control.store import Store


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "state.sqlite3")
        raids.install(self.store)
        self.event = {"serverId":"1","id":"11","channelId":"2","leaderId":"3",
                      "title":"Example", "startTime":time.time()+86400,"closingTime":time.time()+86400,
                      "classes":[{"name":"Attending","type":"primary"}],
                      "advancedSettings":{"allowed_roles":"6, 7, "},"coLeaders":[{"id":"4","name":"Example"}],
                      "signUps":[{"userId":"5","className":"Attending","specName":"","roleName":"Attending","note":"Keep this note"}]}

    def test_history_only_never_creates_live_cards(self):
        result = prepare(self.store,"1",[self.event],[],"3")
        self.assertEqual(result["imported"],1)
        self.assertEqual(raids.list_events(self.store,"1"),[])

    def test_activation_preserves_original_policy_and_never_overwrites_new_signups(self):
        first = prepare(self.store,"1",[self.event],["11"],"3")
        eid=first["prepared"][0]["raid_id"]
        row=raids.read(self.store,"1",eid)
        for key in ("coLeaders","signUps","advancedSettings","channelId","leaderId"):
            self.assertEqual(row["body"][key],self.event[key])
        raids.mutate(self.store,"1","8","new-signup",eid,1,"signup","0:0")
        refreshed=copy.deepcopy(self.event)
        refreshed["signUps"]=[]
        second=prepare(self.store,"1",[refreshed],["11"],"3")
        self.assertEqual(second["prepared"][0]["raid_id"],eid)
        self.assertEqual(len(raids.read(self.store,"1",eid)["body"]["signUps"]),2)

    def test_missing_or_expired_activation_is_rejected_before_import(self):
        with self.assertRaises(ValueError):
            prepare(self.store,"1",[self.event],["12"],"3")
        with self.assertRaises(ValueError):
            prepare(self.store,"1",[{**self.event,"closingTime":1}],["11"],"3")
        self.assertEqual(self.store.events("1"),[])
