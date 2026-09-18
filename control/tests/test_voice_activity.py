import json

from fastapi.testclient import TestClient

from test_control import Base, FakeDiscord
from greybot_control import voice_activity
from greybot_control.discord_api import Denied
from greybot_control.web import create_app


def voice(uid, at, channel):
    return ("VOICE_STATE_UPDATE", uid, at, json.dumps({"channel_id": channel}))


class ComputeTests(Base):
    def test_stays_channels_afk_and_live_members(self):
        rows = [voice("7", 0, "10"), voice("8", 0, "99"), voice("7", 3600, "11"), voice("7", 3630, None),
                voice("8", 1800, None), voice("6", 5000, "10")]
        people, live, gaps = voice_activity.compute(rows, {"99"}, now=5600)
        self.assertEqual((people["7"]["seconds"], people["7"]["stays"], people["7"]["longest"]), (3630, 1, 3600))
        self.assertEqual(people["7"]["channels"], {"10": 3600, "11": 30})
        self.assertEqual((people["8"]["seconds"], people["8"]["afk_seconds"]), (0, 1800))
        self.assertEqual((live, people["6"]["seconds"], gaps), ({"6": "10"}, 600, 0))

    def test_mute_and_deafen_updates_do_not_split_a_stay(self):
        rows = [voice("7", 0, "10"), voice("7", 500, "10"), voice("7", 900, "10"), voice("7", 1200, None)]
        people, _, _ = voice_activity.compute(rows, set(), now=2000)
        self.assertEqual((people["7"]["seconds"], people["7"]["stays"]), (1200, 1))

    def test_time_is_never_invented_across_an_outage(self):
        rows = [voice("7", 0, "10"), ("COLLECTOR_DISCONNECTED", "", 600, "{}"), ("COLLECTOR_CONNECTED", "", 9000, "{}"), voice("7", 9500, None)]
        people, live, gaps = voice_activity.compute(rows, set(), now=10000)
        self.assertEqual((people["7"]["seconds"], gaps, live), (600, 2, {}))


class ActivityDiscord(FakeDiscord):
    def __init__(self):
        super().__init__()
        self.members = {"7": {"user": {"id": "7"}}, "8": {"user": {"id": "8"}}, "9": {"user": {"id": "9", "bot": True}}}

    async def request(self, method, path, **kwargs):
        if path.endswith("/guilds/1"):
            return {"id": "1", "name": "Scrambled", "afk_channel_id": "99"}
        uid = path.rsplit("/", 1)[1]
        if uid not in self.members:
            raise Denied("Discord access unavailable")
        return self.members[uid]


class Directory:
    async def get(self):
        return {"members": [{"id": "7", "name": "Pete", "avatar_url": "", "active": True, "bot": False},
                            {"id": "8", "name": "Tidal", "avatar_url": "", "active": True, "bot": False},
                            {"id": "9", "name": "greyBot", "avatar_url": "", "active": True, "bot": True}],
                "channels": [{"id": "10", "name": "Keys"}, {"id": "99", "name": "Yoshi's Dhurmgeon"}]}


class ActivityWebTests(Base):
    def setUp(self):
        super().setUp()
        self.api = ActivityDiscord()
        self.client = TestClient(create_app(self.cfg, self.store, self.api), base_url=self.cfg.origin)
        self.addCleanup(self.client.close)

    def login(self, user):
        self.client.cookies.set("greybot-local-activity", self.store.session(user))

    def test_page_is_public_shell_and_marked_noindex_but_the_data_needs_a_member(self):
        page = self.client.get("/activity")
        self.assertEqual(page.status_code, 200)
        self.assertIn("noindex", page.text)
        self.assertEqual(self.client.get("/api/activity").status_code, 401)
        self.login("6")
        self.assertEqual(self.client.get("/api/activity").status_code, 403)

    def test_every_script_and_stylesheet_the_page_asks_for_is_actually_served(self):
        import re
        page = self.client.get("/activity").text
        assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page)
        self.assertIn("/assets/activity.js", assets)
        for path in assets:
            self.assertEqual(self.client.get(path).status_code, 200, path)
        # The allowlist in web.py is separate from the files, so check every page, not just this one.
        from pathlib import Path
        import greybot_control
        for html in (Path(greybot_control.__file__).parent / "static").glob("*.html"):
            for path in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html.read_text()):
                self.assertEqual(self.client.get(path).status_code, 200, f"{html.name} asks for {path}")

    def test_bots_cannot_read_it_and_a_member_session_opens_no_admin_data(self):
        self.login("9")
        self.assertEqual(self.client.get("/api/activity").status_code, 403)
        self.login("8")
        self.assertEqual(self.client.get("/api/activity").status_code, 200)
        self.assertEqual(self.client.get("/api/events").status_code, 401)

    def test_report_names_members_excludes_bots_and_afk_and_ranks_by_time(self):
        import time
        now = time.time()
        # The journal stamps its own clock, so the stays are made by moving "now" forward instead.
        for index, (uid, channel) in enumerate((("7", "10"), ("9", "10"), ("8", "99"), ("6", "10"))):
            self.store.append(f"v{index}", "1", "VOICE_STATE_UPDATE", uid, {"channel_id": channel})
        directory = _run(Directory().get())
        directory["members"].append({"id": "6", "name": "Justin", "avatar_url": "", "active": False, "bot": False})
        body = voice_activity.report(self.store, "1", {"99"}, directory, now=now + 7200)
        self.assertEqual([r["name"] for r in body["rows"]], ["Justin", "Pete"])
        self.assertEqual((body["rows"][1]["in_voice"], body["rows"][1]["seconds"] >= 7199, body["in_voice_now"]), ("Keys", True, 2))
        self.assertEqual((body["afk"], body["busiest"][0]["channel"]), (["Yoshi's Dhurmgeon"], "Keys"))


def _run(coroutine):
    import asyncio
    return asyncio.run(coroutine)
