import array
import asyncio
import base64
import json
import math
import sys
import time
import wave
from pathlib import Path

from test_control import Base, FakeDiscord
from greybot_control import voice_clips
from greybot_control.discord_api import Denied, Unavailable
from greybot_control.voice_clips import BYTES_PER_SECOND, PREFIX, Timeline


def tone(seconds, level=8000):
    samples = array.array("h")
    for i in range(int(seconds * voice_clips.RATE)):
        value = int(level * math.sin(i / 20))
        samples.extend((value, value))
    return samples.tobytes()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeRecorder:
    def __init__(self, clock, user_id="9"):
        self.user_id = user_id
        self.timeline = Timeline(clock=clock)
        self.channel = ""
        self.since = clock()
        self.joined = []

    async def join(self, channel):
        self.channel = channel
        self.joined.append(channel)

    async def leave(self):
        self.channel = ""

    def forget(self):
        self.forgotten = True


class VoiceDiscord(FakeDiscord):
    def __init__(self):
        super().__init__()
        self.permissions = voice_clips.VIEW | voice_clips.SEND | voice_clips.CONNECT | voice_clips.CREATE_EXPRESSIONS
        self.requests = []
        self.replies = []
        self.fail = None
        # 7 is an administrator, 8 an ordinary member, 9 greyBot; 6 is a member who is not in voice.
        self.voice = {"7": "4", "8": "4"}
        self.bots = {"9"}

    async def member_context(self, user):
        permissions = self.permissions if user == "9" else 8 if user == "7" else 0
        return {"owner": False, "permissions": permissions, "position": 1,
                "member": {"user": {"id": user, "bot": user in self.bots}, "roles": []}}

    async def followup(self, token, content, components=None, attachment=None):
        self.replies.append({"token": token, "content": content, "components": components, "attachment": attachment})

    async def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if method == "GET" and "/voice-states/" in path:
            channel = self.voice.get(path.rsplit("/", 1)[1])
            if not channel:
                raise Denied("Discord access unavailable")
            return {"channel_id": channel}
        if method == "GET" and path.startswith("/channels/"):
            return {"id": path.split("/")[2], "name": "Raid", "guild_id": "1", "type": 2, "permission_overwrites": []}
        if method == "GET" and path.endswith("/roles"):
            return [{"id": "1", "permissions": str(self.permissions)}]
        if method == "GET":
            return {"user": {"id": "9"}, "roles": []}
        if self.fail:
            raise self.fail
        return {"sound_id": "555"} if path.endswith("/soundboard-sounds") else {"id": "77"}


class TimelineTests(Base):
    def test_overlapping_speakers_mix_and_silence_stays_silent(self):
        clock = Clock()
        line = Timeline(clock=clock)
        clock.now += 1
        line.add(7, tone(1))
        line.add(8, tone(1, 4000))
        clock.now += 2
        mixed = line.render(clock.now, 3)
        self.assertEqual(len(mixed), 3 * BYTES_PER_SECOND)
        self.assertFalse(any(mixed[BYTES_PER_SECOND:]))
        loud = array.array("h", mixed[:BYTES_PER_SECOND])
        self.assertGreater(max(loud), 8000)

    def test_continuous_frames_are_contiguous_and_pauses_reanchor(self):
        clock = Clock()
        line = Timeline(clock=clock)
        frame = tone(0.02)
        for _ in range(5):
            clock.now += 0.021
            line.add(7, frame)
        starts = [start for start, _ in line.tracks[7]]
        self.assertEqual([b - a for a, b in zip(starts, starts[1:])], [len(frame)] * 4)
        clock.now += 3
        line.add(7, frame)
        self.assertGreater(line.tracks[7][-1][0] - starts[-1], 2 * BYTES_PER_SECOND)

    def test_sender_timestamps_place_frames_exactly_despite_bursty_arrival(self):
        clock = Clock()
        line = Timeline(clock=clock)
        frame = tone(0.02)
        clock.now += 1
        # Frames 0-2 arrive in one burst, frame 4 before frame 3, and the stamp wraps past 2**32.
        base = 2 ** 32 - 1920
        for index, wait in ((0, 0), (1, 0), (2, 0), (4, 0.09), (3, 0.001)):
            clock.now += wait
            line.add(7, frame, (base + index * 960) % 2 ** 32)
        starts = sorted(start for start, _ in line.tracks[7])
        self.assertEqual([b - a for a, b in zip(starts, starts[1:])], [len(frame)] * 4)
        clock.now += 1
        mixed = line.render(clock.now, 2)
        voiced = [i for i in range(0, len(mixed), 4) if mixed[i:i + 2] != b"\x00\x00"]
        self.assertLessEqual(voiced[-1] - voiced[0], 5 * len(frame))

    def test_a_pause_in_speech_is_kept_as_a_pause(self):
        clock = Clock()
        line = Timeline(clock=clock)
        frame = tone(0.02)
        line.add(7, frame, 1000)
        clock.now += 0.5
        line.add(7, frame, 1000 + 24000)
        starts = [start for start, _ in line.tracks[7]]
        self.assertEqual(starts[1] - starts[0], 24000 * 4)

    def test_a_restarted_sender_clock_reanchors_to_arrival(self):
        clock = Clock()
        line = Timeline(clock=clock)
        frame = tone(0.02)
        line.add(7, frame, 1000)
        clock.now += 5
        line.add(7, frame, 5)
        self.assertAlmostEqual((line.tracks[7][1][0] - line.tracks[7][0][0]) / BYTES_PER_SECOND, 5, delta=0.05)

    def test_old_audio_is_forgotten(self):
        clock = Clock()
        line = Timeline(keep=5, clock=clock)
        line.add(7, tone(1))
        clock.now += 10
        line.add(8, tone(1))
        self.assertEqual(list(line.tracks), [8])


class VoiceJobTests(Base):
    def setUp(self):
        super().setUp()
        voice_clips.install(self.store)
        self.clock = Clock()
        self.api = VoiceDiscord()
        self.recorder = FakeRecorder(self.clock)
        self.jobs = 0

    def run_job(self, kind, subject="", body=None, actor="7"):
        self.jobs += 1
        job = {"id": f"voice-{self.jobs}", "actor": actor, "kind": kind, "subject": subject,
               "body": json.dumps(body or {}), "created": self.clock.now}
        with self.store.connection() as db:
            db.execute("INSERT INTO voice_tokens VALUES(?,?,?)", (job["id"], "token-" + job["id"], time.time()))
        return asyncio.run(voice_clips.execute(self.cfg, self.store, self.api, job, self.recorder))

    def clip(self, actor="8", capture="ab" * 16, spoken=6):
        self.recorder.channel = "4"
        self.clock.now += spoken
        self.recorder.timeline.add(7, tone(spoken))
        real = time.time
        time.time = self.clock
        try:
            self.run_job("voice_clip", body={"capture": capture}, actor=actor)
        finally:
            time.time = real
        return capture

    def state(self, capture):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM voice_captures WHERE id=?", (capture,)).fetchone()
        return dict(row) if row else None

    def labels(self, reply):
        return [c["custom_id"].split(":")[2] for row in reply["components"] for c in row["components"]]

    def test_first_clip_brings_greybot_to_the_members_own_channel_after_the_notice(self):
        self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="8")
        posted = [r for r in self.api.requests if r[0] == "POST"]
        self.assertEqual(posted[0][1], "/channels/4/messages")
        self.assertEqual(posted[0][2]["body"]["content"], voice_clips.NOTICE)
        self.assertEqual(self.recorder.joined, ["4"])
        reply = self.api.replies[-1]
        self.assertIn("listening now", reply["content"])
        self.assertEqual(self.labels(reply), ["stop"])
        self.assertIsNone(self.state("ab" * 16))

    def test_join_is_refused_when_the_notice_cannot_be_posted(self):
        self.api.fail = Denied("Discord access unavailable")
        with self.assertRaises(Denied):
            self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="8")
        self.assertEqual(self.recorder.joined, [])

    def test_nobody_can_listen_to_a_channel_they_are_not_in_and_is_told_what_to_do(self):
        self.api.voice["7"] = ""
        for actor in ("6", "7"):
            with self.assertRaises(Denied):
                self.run_job("voice_clip", body={"capture": "ab" * 16}, actor=actor)
        self.assertIn("Hop into a voice channel first", self.api.replies[-1]["content"])
        self.recorder.channel = "5"
        self.recorder.timeline.add(7, tone(1))
        for kind in ("voice_clip", "voice_leave"):
            with self.assertRaises(Denied):
                self.run_job(kind, body={"capture": "cd" * 16}, actor="8")
        self.assertEqual((self.recorder.joined, self.recorder.channel), ([], "5"))
        self.assertIsNone(self.state("cd" * 16))
        self.run_job("voice_leave", actor="7")
        self.assertEqual(self.recorder.channel, "")

    def test_a_helper_bot_takes_a_second_channel_while_greybot_posts_the_notice(self):
        helper = FakeRecorder(self.clock, user_id="10")
        self.recorder = voice_clips.Pool([self.recorder, helper])
        self.api.voice["6"] = "5"
        self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="8")
        self.run_job("voice_clip", body={"capture": "cd" * 16}, actor="6")
        self.assertEqual((self.recorder.recorders[0].joined, helper.joined), (["4"], ["5"]))
        notices = [r[1] for r in self.api.requests if r[0] == "POST"]
        self.assertEqual(notices, ["/channels/4/messages", "/channels/5/messages"])
        self.assertIn("/guilds/1/members/10", [r[1] for r in self.api.requests])
        # Each member's clip comes from the bot in their own channel.
        self.clock.now += 3
        helper.timeline.add(7, tone(3))
        real = time.time
        time.time = self.clock
        try:
            self.run_job("voice_clip", body={"capture": "ef" * 16}, actor="6")
        finally:
            time.time = real
        self.assertEqual(self.state("ef" * 16)["channel"], "5")

    def test_a_third_channel_is_told_greybot_is_busy_and_stop_frees_a_bot(self):
        helper = FakeRecorder(self.clock, user_id="10")
        self.recorder = voice_clips.Pool([self.recorder, helper])
        self.api.voice.update({"6": "5", "11": "12"})
        for actor in ("8", "6"):
            self.run_job("voice_clip", body={"capture": "ab" * 16}, actor=actor)
        with self.assertRaises(Denied):
            self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="11")
        self.assertIn("busy", self.api.replies[-1]["content"])
        self.run_job("voice_leave", actor="6")
        self.assertEqual((self.recorder.recorders[0].channel, helper.channel), ("4", ""))
        self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="11")
        self.assertEqual(helper.channel, "12")
        self.api.voice["7"] = ""
        self.run_job("voice_leave", actor="7")
        self.assertEqual([r.channel for r in self.recorder.recorders], ["", ""])

    def test_only_stop_listening_makes_greybot_forget_its_channel(self):
        self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="8")
        self.assertFalse(getattr(self.recorder, "forgotten", False))
        self.run_job("voice_leave", actor="8")
        self.assertTrue(self.recorder.forgotten)

    def test_greybot_returns_to_its_channel_after_a_restart_and_says_so_again(self):
        recorder = voice_clips.Recorder(self.cfg, self.store, None)
        joined = []

        async def join(channel):
            joined.append(channel)
        recorder.join = join
        pool = voice_clips.Pool([recorder])
        asyncio.run(pool.tick(self.api))
        self.assertEqual((joined, self.api.requests), ([], []))
        with self.store.connection() as db:
            db.execute("INSERT INTO voice_status VALUES('1','9','4',1.0,1.0,1,0)")
        asyncio.run(pool.tick(self.api))
        self.assertEqual(joined, ["4"])
        self.assertEqual([(r[0], r[1], r[2]["body"]["content"]) for r in self.api.requests],
                         [("POST", "/channels/4/messages", voice_clips.NOTICE)])
        asyncio.run(pool.tick(self.api))
        self.assertEqual(joined, ["4"], "a failed or pending return is not retried every tick")

    def test_greybot_leaves_and_forgets_an_empty_channel_but_stays_while_anyone_is_there(self):
        class Channel:
            id = 4
            voice_states = {9: object(), 8: object()}

        class Live:
            channel = Channel()
            left = False

            def is_connected(self):
                return not self.left

            def stop_listening(self):
                pass

            async def disconnect(self, force=False):
                self.left = True
        recorder = voice_clips.Recorder(self.cfg, self.store, None)
        recorder.voice = live = Live()
        pool = voice_clips.Pool([recorder])
        asyncio.run(pool.tick(self.api))
        self.assertEqual((live.left, recorder.home()), (False, "4"))
        del Channel.voice_states[8]
        asyncio.run(pool.tick(self.api))
        self.assertEqual((live.left, recorder.home()), (True, ""))
        asyncio.run(pool.tick(self.api))
        self.assertEqual(self.api.requests, [], "no notice and no rejoin for an empty channel")

    def test_a_channel_that_cannot_be_rejoined_is_given_up_on_not_spammed(self):
        recorder = voice_clips.Recorder(self.cfg, self.store, None)

        async def join(channel):
            raise TimeoutError()
        recorder.join = join
        pool = voice_clips.Pool([recorder])
        with self.store.connection() as db:
            db.execute("INSERT INTO voice_status VALUES('1','9','4',1.0,1.0,1,0)")
        for _ in range(3):
            recorder.retry = 0
            asyncio.run(pool.tick(self.api))
        self.assertEqual(len([r for r in self.api.requests if r[0] == "POST"]), 3)
        self.assertEqual(recorder.home(), "")
        recorder.retry = 0
        asyncio.run(pool.tick(self.api))
        self.assertEqual(len([r for r in self.api.requests if r[0] == "POST"]), 3)

    def test_bots_and_unverified_members_are_refused(self):
        self.api.bots.add("8")
        with self.assertRaises(Denied):
            self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="8")
        self.api.bots.discard("8")
        self.store.save_settings("1", "7", 0, {"verification_role": "3"})
        with self.assertRaises(Denied):
            self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="8")
        self.assertEqual(self.recorder.joined, [])

    def test_join_requires_greybot_channel_permissions(self):
        self.api.permissions = voice_clips.VIEW
        with self.assertRaises(Denied):
            self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="8")
        self.assertEqual(self.recorder.joined, [])

    def test_clip_while_listening_replies_privately_with_the_last_few_seconds_ready_to_add(self):
        capture = self.clip(spoken=12)
        row = self.state(capture)
        self.assertEqual((round(row["seconds"]), row["length"], round(row["start"], 1)), (12, 5.0, 7.0))
        reply = self.api.replies[-1]
        self.assertEqual(reply["token"], "token-voice-1")
        self.assertEqual((reply["attachment"][0], reply["attachment"][1][:2]), ("clip.mp3", b"\xff\xfb"))
        self.assertEqual(self.labels(reply), ["name", "earlier", "later", "discard"])
        self.assertEqual(self.recorder.joined, [])

    def test_the_clip_ends_at_the_last_sound_not_in_the_quiet_spent_typing(self):
        self.recorder.channel = "4"
        self.clock.now += 10
        self.recorder.timeline.add(7, tone(10))
        self.clock.now += 8
        real = time.time
        time.time = self.clock
        try:
            self.run_job("voice_clip", body={"capture": "ab" * 16}, actor="8")
        finally:
            time.time = real
        row = self.state("ab" * 16)
        self.assertEqual(round(row["seconds"]), 18)
        self.assertAlmostEqual(row["start"] + row["length"], 10.3, delta=0.15)
        with wave.open(str(voice_clips.capture_path(self.cfg, "ab" * 16)), "rb") as handle:
            handle.setpos(int(row["start"] * voice_clips.RATE))
            self.assertTrue(any(handle.readframes(int(4.5 * voice_clips.RATE))))

    def test_a_short_wait_still_makes_a_clip(self):
        capture = self.clip(spoken=3)
        row = self.state(capture)
        self.assertEqual((row["start"], round(row["length"])), (0, 3))

    def test_earlier_and_later_move_the_clip_and_stop_at_the_edges(self):
        capture = self.clip(spoken=12)
        self.run_job("voice_shift", capture, {"by": -2.0}, actor="8")
        self.assertEqual(round(self.state(capture)["start"], 1), 5.0)
        self.assertEqual(self.api.replies[-1]["attachment"][0], "clip.mp3")
        for _ in range(3):
            self.run_job("voice_shift", capture, {"by": 2.0}, actor="8")
        self.assertEqual(round(self.state(capture)["start"], 1), 7.0)
        self.assertIn("as far as it goes", self.api.replies[-1]["content"])
        for _ in range(5):
            self.run_job("voice_shift", capture, {"by": -2.0}, actor="8")
        self.assertEqual(self.state(capture)["start"], 0)

    def test_naming_a_clip_adds_it_to_the_soundboard_under_that_name(self):
        capture = self.clip()
        self.run_job("voice_publish", capture, {"name": "Big pull"}, actor="8")
        method, route, kwargs = self.api.requests[-1]
        self.assertEqual((method, route, kwargs["body"]["name"]), ("POST", "/guilds/1/soundboard-sounds", "Big pull"))
        sound = base64.b64decode(kwargs["body"]["sound"].split(",", 1)[1])
        self.assertLess(len(sound), voice_clips.MAX_SOUND_BYTES)
        self.assertEqual(sound[:2], b"\xff\xfb")
        self.assertEqual((self.state(capture)["state"], self.state(capture)["sound"]), ("published", "555"))
        self.assertFalse(voice_clips.capture_path(self.cfg, capture).exists())
        self.assertIn("Big pull", self.api.replies[-1]["content"])

    def test_publish_needs_the_permission(self):
        capture = self.clip()
        self.api.permissions = 0
        with self.assertRaises(Denied):
            self.run_job("voice_publish", capture, {"name": "Fine"}, actor="8")
        self.assertFalse([r for r in self.api.requests if r[1].endswith("/soundboard-sounds")])

    def test_members_manage_only_their_own_clips_and_administrators_any(self):
        capture = self.clip(actor="8")
        self.api.voice["6"] = "4"
        for kind, body in (("voice_shift", {"by": -2.0}), ("voice_publish", {"name": "Fine"}), ("voice_discard", {})):
            with self.assertRaises(Denied):
                self.run_job(kind, capture, body, actor="6")
        self.assertTrue(voice_clips.capture_path(self.cfg, capture).exists())
        self.run_job("voice_discard", capture, actor="7")
        self.assertFalse(voice_clips.capture_path(self.cfg, capture).exists())
        self.assertEqual(self.state(capture)["state"], "discarded")

    def test_members_are_limited_in_waiting_clips_and_daily_sounds(self):
        with self.store.connection() as db:
            for index in range(voice_clips.MAX_DRAFTS - 1):
                db.execute("INSERT INTO voice_captures(id,guild,actor,channel,created,seconds,state) VALUES(?,?,?,?,?,?,?)",
                           (f"{index:032x}", "1", "8", "4", time.time(), 5.0, "published"))
        capture = self.clip(actor="8")
        with self.assertRaises(Denied):
            self.run_job("voice_publish", capture, {"name": "Fine"}, actor="8")
        self.assertIn("tomorrow", self.api.replies[-1]["content"])
        with self.store.connection() as db:
            db.execute("UPDATE voice_captures SET state='draft'")
        with self.assertRaises(Denied):
            self.clip(actor="8", capture="cd" * 16)

    def test_clip_when_nobody_spoke_is_refused_kindly(self):
        self.recorder.channel = "4"
        with self.assertRaises(Denied):
            self.run_job("voice_clip", body={"capture": "cd" * 16}, actor="8")
        self.assertIn("did not hear anybody", self.api.replies[-1]["content"])
        self.assertFalse(voice_clips.capture_path(self.cfg, "cd" * 16).exists())

    def test_ambiguous_upload_is_never_retried(self):
        capture = self.clip()
        self.api.fail = Unavailable("Discord request did not complete")
        with self.assertRaises(Unavailable):
            self.run_job("voice_publish", capture, {"name": "Fine"}, actor="8")
        self.assertIn("could not confirm", self.api.replies[-1]["content"])
        self.api.fail = None
        with self.assertRaises(Denied):
            self.run_job("voice_publish", capture, {"name": "Fine"}, actor="8")
        self.assertEqual(self.state(capture)["state"], "publishing")

    def test_a_full_soundboard_returns_the_clip_to_draft_so_the_button_works_again(self):
        capture = self.clip()
        self.api.fail = Denied("Discord access unavailable")
        with self.assertRaises(Denied):
            self.run_job("voice_publish", capture, {"name": "Fine"}, actor="8")
        self.assertIn("soundboard is full", self.api.replies[-1]["content"])
        self.assertEqual(self.state(capture)["state"], "draft")

    def test_tokens_are_single_use_and_expire_with_old_clips(self):
        capture = self.clip()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM voice_tokens").fetchone()[0], 0)
            db.execute("INSERT INTO voice_tokens VALUES('stale','secret',1.0)")
            db.execute("UPDATE voice_captures SET created=1.0")
        voice_clips.expire(self.cfg, self.store)
        self.assertFalse(voice_clips.capture_path(self.cfg, capture).exists())
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM voice_tokens").fetchone()[0], 0)
        self.assertIsNone(self.state(capture))

    def test_capture_ids_cannot_escape_the_capture_directory(self):
        for value in ("../control", "AB" * 16, "ab" * 15):
            with self.assertRaises(Denied):
                voice_clips.capture_path(self.cfg, value)


class InteractionTests(Base):
    def setUp(self):
        super().setUp()
        voice_clips.install(self.store)

    def packet(self, kind, data, user="8", **member):
        return {"id": "1001", "type": kind, "guild_id": "1", "application_id": "9", "token": "secret-token",
                "member": {"user": {"id": user}, "permissions": "0", **member}, "data": data}

    def own(self, capture="ab" * 16, actor="8", seconds=30.0):
        with self.store.connection() as db:
            db.execute("INSERT INTO voice_captures(id,guild,actor,channel,created,seconds,state) VALUES(?,?,?,?,?,?,?)",
                       (capture, "1", actor, "4", time.time(), seconds, "draft"))
        return capture

    def test_the_only_command_is_clip_with_nothing_to_fill_in(self):
        self.assertEqual([(c["name"], c.get("options")) for c in voice_clips.commands()], [("clip", None)])

    def test_clip_defers_privately_queues_a_job_and_keeps_the_token_out_of_the_journal(self):
        reply = voice_clips.receive(self.cfg, self.store, self.packet(2, {"name": "clip"}))
        self.assertEqual(reply, {"type": 5, "data": {"flags": 64}})
        job = self.store.jobs("1")[0]
        self.assertEqual((job["id"], job["kind"], job["actor"]), ("voice-1001", "voice_clip", "8"))
        self.assertRegex(json.loads(job["body"])["capture"], r"^[0-9a-f]{32}$")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT token FROM voice_tokens WHERE job='voice-1001'").fetchone()[0], "secret-token")
            journal = " ".join(r[0] for r in db.execute("SELECT payload FROM events"))
        self.assertNotIn("secret-token", journal + job["body"])

    def test_a_relayed_retry_is_idempotent(self):
        packet = self.packet(2, {"name": "clip"})
        voice_clips.receive(self.cfg, self.store, packet)
        voice_clips.receive(self.cfg, self.store, packet)
        self.assertEqual(len(self.store.jobs("1")), 1)

    def test_green_button_asks_one_question(self):
        capture = self.own()
        reply = voice_clips.receive(self.cfg, self.store, self.packet(3, {"custom_id": PREFIX + "name:" + capture}))
        self.assertEqual((reply["type"], reply["data"]["custom_id"]), (9, PREFIX + "named:" + capture))
        fields = [r["components"][0] for r in reply["data"]["components"]]
        self.assertEqual([(f["custom_id"], f["label"]) for f in fields], [("name", "What should we call it?")])
        self.assertEqual(self.store.jobs("1"), [])

    def submit(self, capture, name="Big pull"):
        return voice_clips.receive(self.cfg, self.store, self.packet(5, {"custom_id": PREFIX + "named:" + capture,
            "components": [{"components": [{"custom_id": "name", "value": name}]}]}))

    def test_typing_a_name_queues_the_soundboard_upload(self):
        capture = self.own()
        self.assertEqual(self.submit(capture, "  Big   pull ")["type"], 5)
        job = self.store.jobs("1")[0]
        self.assertEqual((job["kind"], job["subject"], json.loads(job["body"])), ("voice_publish", capture, {"name": "Big pull"}))

    def test_bad_names_are_explained_without_queueing(self):
        capture = self.own()
        for name in ("x", "<@1> ping", "", "a" * 33):
            with self.assertRaises(Denied) as caught:
                self.submit(capture, name)
            self.assertIn("green button", str(caught.exception))
        self.assertEqual(self.store.jobs("1"), [])

    def test_buttons_map_to_shift_discard_and_leave(self):
        capture = self.own()
        expected = {"earlier": ("voice_shift", capture, {"by": -2.0}), "later": ("voice_shift", capture, {"by": 2.0}),
                    "discard": ("voice_discard", capture, {}), "stop": ("voice_leave", "", {})}
        for index, (action, result) in enumerate(expected.items()):
            packet = self.packet(3, {"custom_id": PREFIX + action + ":" + (capture if action != "stop" else "0" * 32)})
            packet["id"] = str(3000 + index)
            voice_clips.receive(self.cfg, self.store, packet)
            job = self.store.jobs("1")[0]
            self.assertEqual((job["kind"], job["subject"], json.loads(job["body"])), result, action)

    def test_other_members_cannot_act_on_a_clip_but_administrators_can(self):
        capture = self.own(actor="8")
        for action in ("name", "earlier", "later", "discard"):
            with self.assertRaises(Denied):
                voice_clips.receive(self.cfg, self.store, self.packet(3, {"custom_id": PREFIX + action + ":" + capture}, user="6"))
        with self.assertRaises(Denied):
            voice_clips.receive(self.cfg, self.store, self.packet(
                5, {"custom_id": PREFIX + "named:" + capture, "components": []}, user="6"))
        self.assertEqual(self.store.jobs("1"), [])
        voice_clips.receive(self.cfg, self.store, self.packet(3, {"custom_id": PREFIX + "discard:" + capture}, user="7", permissions="8"))
        self.assertEqual(self.store.jobs("1")[0]["kind"], "voice_discard")

    def test_foreign_bot_pending_and_malformed_interactions_are_refused(self):
        clip = {"name": "clip"}
        bad = [self.packet(2, clip) | {"guild_id": "2"}, self.packet(2, clip) | {"application_id": "5"},
               self.packet(2, clip) | {"token": ""}, self.packet(2, clip, pending=True),
               self.packet(3, {"custom_id": PREFIX + "wipe:" + "ab" * 16}), self.packet(3, {"custom_id": PREFIX + "discard:../x"}),
               self.packet(3, {"custom_id": PREFIX + "named:" + "ab" * 16}), self.packet(5, {"custom_id": PREFIX + "discard:" + "ab" * 16})]
        bot = self.packet(2, clip)
        bot["member"]["user"]["bot"] = True
        for packet in (*bad, bot):
            with self.assertRaises(Denied):
                voice_clips.receive(self.cfg, self.store, packet)
        self.assertEqual(self.store.jobs("1"), [])

    def test_signed_relay_reaches_voice_clips_and_denials_are_private_messages(self):
        from dataclasses import replace
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from nacl.signing import SigningKey
        from greybot_control.web import create_app
        (self.root / "archive").mkdir()
        cfg = replace(self.cfg, enforce=True, archive_dir=self.root / "archive")
        client = TestClient(create_app(cfg, self.store, VoiceDiscord()), base_url=cfg.origin)
        self.addCleanup(client.close)
        key = SigningKey.generate()

        def post(packet):
            raw, stamp = json.dumps(packet).encode(), str(int(time.time()))
            return client.post("/discord/roles", content=raw, headers={
                "x-signature-timestamp": stamp, "x-signature-ed25519": key.sign(stamp.encode() + raw).signature.hex()})

        with patch.dict("os.environ", {"GREYBOT_DISCORD_PUBLIC_KEY": key.verify_key.encode().hex()}):
            clipped = post(self.packet(2, {"name": "clip"}))
            denied = post(self.packet(3, {"custom_id": PREFIX + "discard:" + "ab" * 16}))
            unsigned = client.post("/discord/roles", content=b"{}")
        self.assertEqual(clipped.json(), {"type": 5, "data": {"flags": 64}})
        self.assertEqual((denied.json()["type"], denied.json()["data"]["flags"]), (4, 64))
        self.assertEqual(unsigned.status_code, 401)
        self.assertEqual([j["kind"] for j in self.store.jobs("1")], ["voice_clip"])

    def test_lambda_registers_the_same_command_the_service_answers(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        try:
            from raid_commands import commands
        finally:
            sys.path.pop(0)
        self.assertEqual([c for c in commands() if c["name"] == "clip"], voice_clips.commands())


class HandshakeWaitTests(Base):
    """discord.py's state waiter loses a wakeup when two state changes land a few loop steps apart."""

    class Flow:
        def __init__(self):
            self._state, self._state_event = "sent", asyncio.Event()

        @property
        def state(self):
            return self._state

        @state.setter
        def state(self, value):
            # Exactly discord.py 2.7.1's setter: signal, then clear at once.
            self._state = value
            self._state_event.set()
            self._state_event.clear()

    async def outcome(self, waiter, hops):
        flow = self.Flow()
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(waiter(flow, "both", timeout=None))
        await asyncio.sleep(0.01)

        def second(remaining):
            if remaining:
                loop.call_soon(second, remaining - 1)
            else:
                flow.state = "both"

        flow.state = "first"
        loop.call_soon(second, hops)
        try:
            await asyncio.wait_for(task, 0.3)
            return True
        except asyncio.TimeoutError:
            return False

    def test_the_stock_waiter_can_sleep_through_the_final_state_and_ours_cannot(self):
        from discord.voice_state import VoiceConnectionState
        stock = [asyncio.run(self.outcome(VoiceConnectionState._wait_for_state, hops)) for hops in range(8)]
        ours = [asyncio.run(self.outcome(voice_clips.wait_for_state, hops)) for hops in range(8)]
        self.assertIn(False, stock, "this test must reproduce the library's lost wakeup to mean anything")
        self.assertEqual(ours, [True] * 8)

    def test_our_waiter_still_times_out(self):
        async def run():
            with self.assertRaises(asyncio.TimeoutError):
                await voice_clips.wait_for_state(self.Flow(), "both", timeout=0.1)
        asyncio.run(run())

    def test_the_voice_client_uses_the_armed_state_and_our_waiter(self):
        from discord.voice_state import VoiceConnectionState
        cls = voice_clips.voice_client_class()
        state_class = cls.create_connection_state.__code__.co_freevars
        self.assertIn("ArmedState", state_class)
        self.assertTrue(issubclass(cls, __import__("discord.ext.voice_recv", fromlist=["x"]).VoiceRecvClient))
        self.assertIsNot(voice_clips.wait_for_state, VoiceConnectionState._wait_for_state)


class Session:
    def __init__(self, fail=False):
        self.fail, self.seen = fail, []

    def decrypt(self, speaker, media, packet):
        self.seen.append((speaker, str(media)))
        if self.fail:
            raise RuntimeError("no key for this epoch")
        return bytes(b ^ 0x5A for b in packet)


class Connection:
    def __init__(self, session=None, version=0):
        self.dave_session, self.dave_protocol_version = session, version


class Voice:
    def __init__(self, connection):
        self._connection = connection


class ReceiveTests(Base):
    def setUp(self):
        super().setUp()
        import discord
        discord.opus._load_default()
        self.frames = []
        encoder = discord.opus.Encoder()
        pcm = tone(0.2)
        for offset in range(0, len(pcm), 3840):
            self.frames.append(encoder.encode(pcm[offset:offset + 3840], 960))
        self.recorder = voice_clips.Recorder(self.cfg, self.store, None)

    def heard(self):
        return sum(len(pcm) for track in self.recorder.timeline.tracks.values() for _, pcm in track)

    def test_plain_opus_is_decoded_into_the_buffer(self):
        self.recorder.voice = Voice(Connection())
        for frame in self.frames:
            self.recorder.receive(7, frame)
        self.assertEqual(self.heard(), 3840 * len(self.frames))
        self.assertFalse(self.recorder.encrypted)

    def test_end_to_end_encrypted_frames_are_decrypted_per_speaker_first(self):
        session = Session()
        self.recorder.voice = Voice(Connection(session, 1))
        for frame in self.frames:
            self.recorder.receive(7, bytes(b ^ 0x5A for b in frame))
        self.assertEqual(self.heard(), 3840 * len(self.frames))
        self.assertEqual(session.seen[0], (7, "MediaType.audio"))
        self.assertTrue(self.recorder.encrypted)

    def test_the_spoken_announcement_is_playable_stereo_pcm_with_a_quiet_lead_in(self):
        spoken = voice_clips.announcement()
        self.assertEqual(len(spoken) % 4, 0)
        self.assertTrue(3 < len(spoken) / BYTES_PER_SECOND < 5)
        self.assertFalse(any(spoken[:BYTES_PER_SECOND // 2]))
        self.assertTrue(any(spoken[BYTES_PER_SECOND:]))
        self.assertEqual(spoken[BYTES_PER_SECOND:BYTES_PER_SECOND + 2], spoken[BYTES_PER_SECOND + 2:BYTES_PER_SECOND + 4])

    def test_a_lost_packet_is_concealed_and_the_clip_stays_continuous(self):
        self.recorder.voice = Voice(Connection())
        for index, frame in enumerate(self.frames):
            self.recorder.receive(7, b"" if index == 4 else frame, 5000 + index * 960)
        self.assertEqual(self.heard(), 3840 * len(self.frames))
        starts = sorted(start for start, _ in self.recorder.timeline.tracks[7])
        self.assertEqual({b - a for a, b in zip(starts, starts[1:])}, {3840})
        self.assertEqual(self.recorder.dropped, 0)

    def test_a_lost_packet_before_any_speech_is_ignored(self):
        self.recorder.voice = Voice(Connection())
        self.recorder.receive(7, b"", 5000)
        self.assertEqual((self.heard(), self.recorder.dropped), (0, 0))

    def returning(self, session, members):
        class Channel:
            voice_states = {uid: object() for uid in members}

        class ReturningVoice(Voice):
            def __init__(self, connection):
                super().__init__(connection)
                self.channel, self._ssrc_to_id = Channel(), {111: 5}

            def _add_ssrc(self, uid, ssrc):
                self._ssrc_to_id[ssrc] = uid
        self.recorder.voice = ReturningVoice(Connection(session, 1 if session else 0))

    def test_a_member_who_left_and_came_back_is_recognised_by_whose_key_opens_their_audio(self):
        class Keyed(Session):
            def decrypt(self, speaker, media, packet):
                if speaker != 7:
                    raise ValueError("Failed to decrypt")
                return super().decrypt(speaker, media, packet)
        session = Keyed()
        self.returning(session, [9, 5, 6, 7])
        for index, frame in enumerate(self.frames):
            # As the sink does: the stream's member is looked up first, and is unknown only until identified.
            known = self.recorder.voice._ssrc_to_id.get(222)
            self.recorder.receive(known, bytes(b ^ 0x5A for b in frame), 1000 + index * 960, ssrc=222)
        self.assertEqual(self.heard(), 3840 * len(self.frames))
        self.assertEqual(self.recorder.voice._ssrc_to_id[222], 7)
        self.assertEqual((self.recorder.unknown, self.recorder.dropped), (0, 0))
        self.assertEqual(len([s for s in session.seen if s[0] == 7]), len(self.frames), "each frame is opened exactly once")
        self.assertLessEqual(len([s for s in session.seen if s[0] == 6]), 1, "other members' keys are tried only while identifying")
        self.assertFalse([s for s in session.seen if s[0] in (5, 9)], "already-known members and greyBot are never tried")

    def test_unidentifiable_audio_is_counted_and_never_buffered(self):
        self.returning(Session(fail=True), [9, 6, 7])
        self.recorder.receive(None, self.frames[0], 1000, ssrc=222)
        self.assertEqual((self.heard(), self.recorder.unknown), (0, 1))
        self.returning(None, [9, 6, 7])
        self.recorder.receive(None, self.frames[0], 1000, ssrc=222)
        self.assertEqual((self.heard(), self.recorder.unknown), (0, 2), "two unaccounted members and no encryption: no guessing")
        self.returning(None, [9, 5, 7])
        self.recorder.receive(None, self.frames[0], 1000, ssrc=222)
        self.assertEqual(self.heard(), 3840)

    def test_undecryptable_silent_and_own_frames_are_dropped_not_buffered(self):
        self.returning(Session(fail=True), [9, 7])
        self.recorder.receive(7, self.frames[0])
        self.recorder.receive(7, voice_clips.OPUS_SILENCE)
        self.recorder.receive(int(self.cfg.client_id), self.frames[0])
        self.recorder.receive(None, self.frames[0], 1000, ssrc=222)
        self.assertEqual((self.heard(), self.recorder.dropped, self.recorder.unknown), (0, 1, 1))
