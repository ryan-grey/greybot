import asyncio
from dataclasses import replace

from test_control import Base, FakeDiscord
from greybot_control import dm_relay
from greybot_control.discord_api import Denied


class DMDiscord(FakeDiscord):
    def __init__(self):
        super().__init__()
        self.sent, self.reactions, self.closed = [], [], set()

    async def request(self, method, path, **kwargs):
        if path == "/users/@me/channels":
            user = kwargs["body"]["recipient_id"]
            if user in self.closed:
                raise Denied("Discord access unavailable")
            return {"id": "dm-" + user}
        if "/reactions/" in path:
            self.reactions.append(path.split("/reactions/")[1].split("/")[0])
            return None
        self.sent.append((path.split("/")[2], kwargs["body"]["content"]))
        return {"id": f"m{len(self.sent)}"}


def dm(author, content, reference="", bot=False, attachments=()):
    return {"id": "100", "channel": "dm-" + author, "author": author, "bot": bot, "content": content,
            "attachments": list(attachments), "reference": reference}


class RelayTests(Base):
    def setUp(self):
        super().setUp()
        dm_relay.install(self.store)
        self.cfg = replace(self.cfg, dm_owner_id="7")
        self.api = DMDiscord()

    def receive(self, message):
        asyncio.run(dm_relay.receive(self.cfg, self.store, self.api, message))

    def test_a_members_dm_reaches_the_owner_and_the_member_is_told_a_person_reads_it(self):
        self.receive(dm("8", "how do I clip?", attachments=["https://cdn.discordapp.com/x.png"]))
        (to_owner, forwarded), (to_member, notice) = self.api.sent
        self.assertEqual((to_owner, to_member), ("dm-7", "dm-8"))
        self.assertIn("<@8>", forwarded)
        self.assertIn("how do I clip?", forwarded)
        self.assertIn("https://cdn.discordapp.com/x.png", forwarded)
        self.assertIn("<@7>", notice)
        self.receive(dm("8", "second message"))
        self.assertEqual(len(self.api.sent), 3, "the notice is sent once a day, not on every message")

    def test_the_owner_answers_as_greybot_by_replying_to_the_forward(self):
        self.receive(dm("8", "how do I clip?"))
        self.receive(dm("7", "Type /clip in voice!", reference="m1"))
        self.assertEqual(self.api.sent[-1], ("dm-8", "Type /clip in voice!"))
        self.assertEqual(self.api.reactions, ["%E2%9C%85"])

    def test_an_owner_message_that_is_not_a_reply_goes_nowhere(self):
        self.receive(dm("8", "hello"))
        before = len(self.api.sent)
        self.receive(dm("7", "this is not a reply"))
        self.receive(dm("7", "reply to something unknown", reference="m999"))
        self.assertEqual([to for to, _ in self.api.sent[before:]], ["dm-7", "dm-7"])
        self.assertTrue(all("Nothing was sent" in text for _, text in self.api.sent[before:]))

    def test_closed_dms_are_reported_and_never_resent(self):
        self.receive(dm("8", "hello"))
        self.api.closed.add("8")
        self.receive(dm("7", "answer", reference="m1"))
        self.assertEqual(self.api.reactions, ["%E2%9D%8C"])
        self.assertIn("could not confirm delivery", self.api.sent[-1][1])

    def test_bots_and_an_unconfigured_owner_are_ignored_and_no_text_is_journaled(self):
        self.receive(dm("9", "bot chatter", bot=True))
        asyncio.run(dm_relay.receive(replace(self.cfg, dm_owner_id=""), self.store, self.api, dm("8", "hello")))
        self.assertEqual(self.api.sent, [])
        self.receive(dm("8", "a very private sentence"))
        with self.store.connection() as db:
            journal = " ".join(r[0] + r[1] for r in db.execute("SELECT kind,payload FROM events"))
            tables = " ".join(str(tuple(r)) for r in db.execute("SELECT * FROM dm_forwards"))
        self.assertIn("DM_FORWARDED", journal)
        self.assertNotIn("private sentence", journal + tables)
