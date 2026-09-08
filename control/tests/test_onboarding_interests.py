import unittest
from copy import deepcopy
from greybot_control.discord_api import Denied
from greybot_control.mutes import effective_permissions
from greybot_control.onboarding_interests import interest_payload


class InterestTests(unittest.TestCase):
    def setUp(self):
        self.roles = [{"id":"1","permissions":"0"}, {"id":"2","permissions":"1024"},
                      {"id":"3","permissions":"0"}, {"id":"4","permissions":"0"}]
        self.channels = [{"id":"10","permission_overwrites":[{"id":"1","type":0,"allow":"1024","deny":"0"}]},
                         {"id":"11","permission_overwrites":[]}]
        self.onboarding = {"guild_id":"1","prompts":[{"id":"20","options":[
            {"id":"21","role_ids":["2"],"channel_ids":[]},
            {"id":"22","role_ids":["2"],"channel_ids":["11"]}]}],
            "default_channel_ids":["10"],"enabled":True,"mode":0}

    def test_all_choices_cannot_unlock_channels_but_verified_members_can(self):
        original = deepcopy(self.onboarding)
        payload = interest_payload(self.onboarding,self.roles,self.channels,{"21":"3","22":"4"},"2")
        selected = [r for p in payload["prompts"] for o in p["options"] for r in o["role_ids"]]
        for held in ([],["3"],["4"],selected):
            member={"user":{"id":"99"},"roles":held}
            self.assertTrue(effective_permissions("1",self.roles,member,self.channels[0]) & 1024)
            self.assertFalse(effective_permissions("1",self.roles,member,self.channels[1]) & 1024)
        member={"user":{"id":"99"},"roles":selected+["2"]}
        self.assertTrue(effective_permissions("1",self.roles,member,self.channels[1]) & 1024)
        self.assertEqual(self.onboarding,original)
        self.assertEqual(payload["prompts"][0]["options"][1]["channel_ids"],["11"])

    def test_reject_base_permissions_overrides_and_incomplete_mapping(self):
        for mode in ("base","permissions","override","missing"):
            roles=deepcopy(self.roles); channels=deepcopy(self.channels); mapping={"21":"3","22":"4"}
            if mode == "base": mapping["21"]="2"
            if mode == "permissions": roles[2]["permissions"]="1024"
            if mode == "override": channels[1]["permission_overwrites"]=[{"id":"3","type":0,"allow":"1024","deny":"0"}]
            if mode == "missing": mapping.pop("22")
            with self.subTest(mode=mode), self.assertRaises(Denied):
                interest_payload(self.onboarding,roles,channels,mapping,"2")
