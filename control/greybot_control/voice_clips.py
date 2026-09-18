"""Announced voice listening, short captures and soundboard publishing, all inside Discord.

Members drive it with /clip, buttons and a naming form relayed by the interactions Lambda.
Audio stays in a rolling in-memory buffer. Nothing reaches disk until a member in the
channel saves the last few seconds, and saved captures expire on their own.
"""
import base64
import hashlib
import io
import json
import logging
import re
import threading
import time
import wave
from collections import deque

from .discord_api import ADMINISTRATOR, Denied
from .mutes import effective_permissions

log = logging.getLogger("greybot.control")
# Names of recent VOICE_* gateway events, kept by the worker for diagnosing a failed join.
TRACE = deque(maxlen=40)

RATE = 48000
WIDTH = 2
CHANNELS = 2
BYTES_PER_SECOND = RATE * WIDTH * CHANNELS
ALIGN = WIDTH * CHANNELS
BUFFER_SECONDS = 90
CAPTURE_SECONDS = 30
CAPTURE_TTL = 3600
TOKEN_TTL = 840
# Discord's limit is 5.2 s of decoded audio, and MP3 encoding pads the clip by up to ~0.1 s.
# A 5.2 s selection was rejected live (code 50124); 5.0 s uploads.
MAX_SOUND_SECONDS = 5.0
MAX_SOUND_BYTES = 512 * 1024
SHIFT_SECONDS = 2.0
QUIET = 300
MAX_DRAFTS = 5
MAX_DAILY_SOUNDS = 3
OPUS_SILENCE = b"\xf8\xff\xfe"
VIEW, SEND, CONNECT, CREATE_EXPRESSIONS = 1 << 10, 1 << 11, 1 << 20, 1 << 43
PREFIX = "greybot:clip:"
COMMAND_NAMES = {"clip"}
NAME = re.compile(r"[\w !'&+.-]{2,32}")
NOTICE = ("👂 greyBot is listening in this voice channel so anyone here can type **/clip** to turn the last few seconds "
          "into a soundboard sound. It only remembers the last 90 seconds, nothing is saved unless someone here clips it, "
          "and saved clips are deleted within an hour. It stays until everyone has left the channel or someone presses Stop listening.")


def install(store):
    with store.connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS voice_status(
                guild TEXT NOT NULL, bot TEXT NOT NULL, channel TEXT NOT NULL, since REAL NOT NULL,
                updated REAL NOT NULL, encrypted INTEGER NOT NULL, dropped INTEGER NOT NULL,
                PRIMARY KEY(guild, bot));
            CREATE TABLE IF NOT EXISTS voice_captures(
                id TEXT PRIMARY KEY, guild TEXT NOT NULL, actor TEXT NOT NULL, channel TEXT NOT NULL,
                created REAL NOT NULL, seconds REAL NOT NULL, state TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '', sound TEXT NOT NULL DEFAULT '',
                start REAL NOT NULL DEFAULT 0, length REAL NOT NULL DEFAULT 0);
            -- Interaction tokens are short-lived credentials: kept beside the queue, never in the audit journal.
            CREATE TABLE IF NOT EXISTS voice_tokens(
                job TEXT PRIMARY KEY, token TEXT NOT NULL, created REAL NOT NULL);
        ''')


def directory(cfg):
    path = cfg.state_dir / "voice-captures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def capture_path(cfg, capture_id):
    if not re.fullmatch(r"[0-9a-f]{32}", capture_id):
        raise Denied("Unknown capture")
    return directory(cfg) / (capture_id + ".wav")


class Timeline:
    """Per-speaker PCM placed on a shared clock so overlapping speech mixes in time."""

    def __init__(self, keep=BUFFER_SECONDS, clock=time.time):
        self.keep, self.clock = keep, clock
        self.origin = clock()
        self.tracks = {}
        self.ends = {}
        self.anchors = {}
        self.lock = threading.Lock()

    def _position(self, moment):
        return int((moment - self.origin) * BYTES_PER_SECOND) // ALIGN * ALIGN

    def add(self, speaker, pcm, stamp=None):
        """Place one frame. `stamp` is the sender's RTP sample clock, which is exact where arrival time jitters."""
        now = self.clock()
        with self.lock:
            arrival = self._position(now) - len(pcm)
            expected = self.ends.get(speaker)
            anchor = self.anchors.get(speaker)
            start = None
            if stamp is not None and anchor:
                delta = (stamp - anchor[0] + 2 ** 31) % 2 ** 32 - 2 ** 31
                start = anchor[1] + delta * ALIGN
                # The sender's clock only says how frames relate to each other; drift from ours re-anchors.
                if abs(start - arrival) > BYTES_PER_SECOND:
                    start = None
            if start is None:
                # Without a usable stamp, continuous speech stays sample-contiguous and a pause re-anchors to arrival.
                contiguous = expected is not None and -BYTES_PER_SECOND <= arrival - expected <= BYTES_PER_SECOND // 10
                start = expected if contiguous and stamp is None else arrival
                if stamp is not None:
                    self.anchors[speaker] = (stamp, start)
            self.tracks.setdefault(speaker, deque()).append((start, pcm))
            self.ends[speaker] = start + len(pcm)
            floor = self._position(now - self.keep)
            for key in list(self.tracks):
                track = self.tracks[key]
                while track and track[0][0] + len(track[0][1]) < floor:
                    track.popleft()
                if not track:
                    del self.tracks[key], self.ends[key]
                    self.anchors.pop(key, None)

    def render(self, end, seconds):
        import audioop
        with self.lock:
            last = self._position(end)
            first = last - int(seconds * BYTES_PER_SECOND) // ALIGN * ALIGN
            mixed = bytes(last - first)
            for track in self.tracks.values():
                layer = bytearray(last - first)
                used = False
                for start, pcm in track:
                    low, high = max(start, first), min(start + len(pcm), last)
                    if low < high:
                        layer[low - first:high - first] = pcm[low - start:high - start]
                        used = True
                if used:
                    mixed = audioop.add(mixed, bytes(layer), WIDTH)
            return mixed

    def clear(self):
        with self.lock:
            self.tracks.clear()
            self.ends.clear()
            self.anchors.clear()


def wav_bytes(pcm):
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(CHANNELS); handle.setsampwidth(WIDTH); handle.setframerate(RATE)
        handle.writeframes(pcm)
    return out.getvalue()


def last_sound(pcm):
    """Seconds into the capture where audible sound ends, so a clip is not mostly the quiet spent typing /clip."""
    import audioop
    block = BYTES_PER_SECOND // 10
    for offset in range(len(pcm) - block, -1, -block):
        if audioop.max(pcm[offset:offset + block], WIDTH) > QUIET:
            return min(len(pcm), offset + block + 3 * block) / BYTES_PER_SECOND
    return len(pcm) / BYTES_PER_SECOND


def trim(path, start, seconds):
    with wave.open(str(path), "rb") as handle:
        if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (CHANNELS, WIDTH, RATE):
            raise Denied("Capture format is not recognized")
        total = handle.getnframes()
        first, count = int(start * RATE), int(seconds * RATE)
        if first < 0 or count < RATE // 4 or first + count > total:
            raise Denied("That selection runs past the end of the capture")
        handle.setpos(first)
        return handle.readframes(count)


def encode_mp3(pcm):
    import lameenc
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(128); encoder.set_in_sample_rate(RATE); encoder.set_channels(CHANNELS)
    encoder.set_quality(2); encoder.silence()
    return bytes(encoder.encode(pcm)) + bytes(encoder.flush())


def announcement():
    """'greyBot is now listening for the slash clip command', as the 48 kHz stereo PCM Discord plays."""
    import audioop
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "assets" / "voice" / "listening.wav"
    try:
        with wave.open(str(path), "rb") as handle:
            if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (1, WIDTH, RATE):
                return b""
            # Half a second of quiet first, so the opening word is not lost while the connection settles.
            return bytes(BYTES_PER_SECOND // 2) + audioop.tostereo(handle.readframes(handle.getnframes()), WIDTH, 1, 1)
    except (OSError, wave.Error):
        return b""


async def wait_for_state(self, state, *other_states, timeout=None):
    """Wait for a connection state by looking at it, not by trusting discord.py's wakeup.

    Seen live on the NAS worker (2026-09-18): the state reached `got_both_voice_updates` 200 ms into
    the handshake and the stock waiter still slept until the 20 s timeout. The stock setter signals
    with `Event.set()` immediately followed by `Event.clear()`. Discord's two replies arrived 1 ms
    apart; the first woke the waiter, which saw the intermediate state and started a new wait task,
    and the second signal fired before that task had run its first step, so nobody was listening
    and the wakeup was lost for good. Polling the state cannot lose anything.
    """
    import asyncio
    states = (state, *other_states)
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    while self.state not in states:
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            raise asyncio.TimeoutError()
        await asyncio.sleep(0.02)


def voice_client_class():
    """voice_recv's client, with the handshake state armed BEFORE the join request is sent.

    discord.py 2.7.1 sends the join (gateway op 4) and only then moves its connection state from
    `disconnected` to `set_guild_voice_state`. Replies that arrive while it is still `disconnected`
    are silently ignored. On the NAS worker the event loop is busy journaling greyBot's own notice
    at that moment, Discord's replies (35 ms away) won that race every time, and each join timed
    out after 20 s. Arming first keeps discord.py's own guard and removes the window. Pinned to the
    library version in requirements.txt: `_voice_connect` and `create_connection_state` are private.
    """
    from discord.ext import voice_recv
    from discord.ext.voice_recv.gateway import hook
    from discord.voice_state import ConnectionFlowState, VoiceConnectionState

    class ArmedState(VoiceConnectionState):
        async def _voice_connect(self, *, self_deaf=False, self_mute=False):
            if self.state is ConnectionFlowState.disconnected:
                self.state = ConnectionFlowState.set_guild_voice_state
            await super()._voice_connect(self_deaf=self_deaf, self_mute=self_mute)

        _wait_for_state = wait_for_state

    async def rejoin_hook(ws, msg):
        """voice_recv 0.5.2 blacklists a member's audio stream when they leave and only lifts that when Discord
        re-announces the speaker (voice op 5). Seen live: a member who left and came back was never re-announced,
        kept their stream number, and everything they said was discarded. Lift the blacklist on leave instead;
        `Recorder.receive` works out whose stream it is when audio arrives."""
        vc = ws._connection.voice_client
        left = vc._get_ssrc_from_id(int(msg["d"]["user_id"])) if msg.get("op") == 13 and msg.get("d") else None
        await hook(ws, msg)
        reader = getattr(vc, "_reader", None)
        if left is not None and reader:
            with reader.packet_router._lock:
                if left in reader.packet_router._dropped_ssrcs:
                    reader.packet_router._dropped_ssrcs.remove(left)

    class ArmedClient(voice_recv.VoiceRecvClient):
        def create_connection_state(self):
            return ArmedState(self, hook=rejoin_hook)

    return ArmedClient


class Recorder:
    """One bot account's voice connection and its rolling buffer. Discord allows one per account per server."""

    def __init__(self, cfg, store, client, user_id=""):
        self.cfg, self.store, self.client = cfg, store, client
        self.user_id = user_id or cfg.client_id
        self.timeline = Timeline()
        self.voice = None
        self.since = 0
        self.decoders = {}
        self.encrypted = False
        self.dropped = 0
        self.unknown = 0
        self.failures = {}
        self.retry = 0
        self.attempts = 0

    @property
    def channel(self):
        return str(self.voice.channel.id) if self.voice and self.voice.is_connected() else ""

    def identify(self, ssrc, opus, session):
        """Whose stream is this? Discord did not say, so ask the encryption: only the sender's key opens the frame.

        Returns the member and the opened frame, and records the answer so later frames skip this. Without
        end-to-end encryption the only safe guess is a channel with exactly one unaccounted-for member.
        """
        import davey
        known = set(self.voice._ssrc_to_id.values())
        candidates = [uid for uid in list(self.voice.channel.voice_states) if str(uid) != self.user_id and uid not in known]
        for uid in candidates:
            try:
                opened = session.decrypt(uid, davey.MediaType.audio, opus) if session else opus
            except Exception:
                continue
            if session or len(candidates) == 1:
                self.voice._add_ssrc(uid, ssrc)
                return uid, opened
        return None, opus

    def receive(self, speaker, opus, stamp=None, ssrc=None):
        """Called on the receive thread with one transport-decrypted RTP payload; empty means the packet was lost."""
        import davey
        import discord
        if opus == OPUS_SILENCE or str(speaker) == self.user_id or (not speaker and not opus):
            return
        state = getattr(self.voice, "_connection", None)
        session = getattr(state, "dave_session", None)
        session = session if session is not None and getattr(state, "dave_protocol_version", 0) else None
        try:
            opened = False
            if not speaker:
                speaker, opus = self.identify(ssrc, opus, session)
                if not speaker:
                    self.unknown += 1
                    return
                opened = True
            decoder = self.decoders.get(speaker)
            if not opus:
                # A lost packet mid-speech: let the decoder conceal it instead of leaving a 20 ms hole.
                if decoder is not None:
                    self.timeline.add(speaker, decoder.decode(None, fec=False), stamp)
                return
            if session is not None:
                self.encrypted = True
                if not opened:
                    opus = session.decrypt(speaker, davey.MediaType.audio, opus)
            if decoder is None:
                decoder = self.decoders[speaker] = discord.opus.Decoder()
            self.timeline.add(speaker, decoder.decode(bytes(opus), fec=False), stamp)
        except Exception as exc:
            # Frames that cannot be decrypted or decoded are dropped, never buffered as noise.
            self.dropped += 1
            reason = type(exc).__name__ + ": " + str(exc)[:80]
            self.failures[reason] = self.failures.get(reason, 0) + 1

    async def join(self, channel_id):
        import discord
        from discord.ext import voice_recv
        channel = self.client.get_channel(int(channel_id))
        if not isinstance(channel, discord.VoiceChannel) or str(channel.guild.id) != self.cfg.guild_id:
            raise Denied("That is not a voice channel in this server")
        await self.leave()
        recorder = self

        class Sink(voice_recv.AudioSink):
            def wants_opus(self):
                return True

            def write(self, user, data):
                ssrc = data.packet.ssrc
                recorder.receive(recorder.voice._get_id_from_ssrc(ssrc), data.opus, data.packet.timestamp, ssrc)

            def cleanup(self):
                pass

        TRACE.clear()
        try:
            self.voice = await channel.connect(cls=voice_client_class(), self_deaf=False, self_mute=False, timeout=20)
        except Exception:
            # Event names and states only, never payloads: what reached this process while the handshake waited.
            state = self.client._connection
            registered = state._get_voice_client(channel.guild.id)
            log.error("Voice join failed. gateway voice events: %s | voice client registered: %s | guild cached: %s | "
                      "gateway latency: %.0f ms | voice clients: %d", list(TRACE), type(registered).__name__,
                      state._get_guild(channel.guild.id) is not None, self.client.latency * 1000, len(state._voice_clients))
            raise
        self.since, self.dropped, self.encrypted = time.time(), 0, False
        self.voice.listen(Sink())
        await self.announce(channel)

    async def announce(self, channel):
        """Say out loud that greyBot is listening, then mute. The posted notice stays the gate; this is the courtesy."""
        import asyncio
        import discord
        spoken = announcement()
        if spoken:
            finished = asyncio.Event()
            loop = asyncio.get_running_loop()
            try:
                self.voice.play(discord.PCMAudio(io.BytesIO(spoken)), after=lambda error: loop.call_soon_threadsafe(finished.set))
                await asyncio.wait_for(finished.wait(), timeout=15)
            except Exception:
                self.voice.stop()
        await channel.guild.change_voice_state(channel=channel, self_mute=True, self_deaf=False)

    async def leave(self):
        voice, self.voice = self.voice, None
        self.timeline.clear()
        self.decoders.clear()
        if voice:
            try:
                voice.stop_listening()
            finally:
                await voice.disconnect(force=True)

    def home(self):
        """The channel this account was asked to sit in. It survives restarts; only Stop listening clears it."""
        with self.store.connection() as db:
            row = db.execute("SELECT channel FROM voice_status WHERE guild=? AND bot=?", (self.cfg.guild_id, self.user_id)).fetchone()
        return row["channel"] if row else ""

    def forget(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM voice_status WHERE guild=? AND bot=?", (self.cfg.guild_id, self.user_id))

    def alone(self, channel):
        return not [uid for uid in list(channel.voice_states) if str(uid) != self.user_id]

    async def tick(self, api):
        if self.channel and self.alone(self.voice.channel):
            # Nobody left to clip for: go, and do not come back until someone asks again.
            await self.leave()
            self.forget()
            return
        if self.channel:
            with self.store.connection() as db:
                db.execute("INSERT INTO voice_status VALUES(?,?,?,?,?,?,?) ON CONFLICT(guild,bot) DO UPDATE SET "
                           "channel=excluded.channel,since=excluded.since,updated=excluded.updated,"
                           "encrypted=excluded.encrypted,dropped=excluded.dropped",
                           (self.cfg.guild_id, self.user_id, self.channel, self.since, time.time(), int(self.encrypted),
                            self.dropped + self.unknown))
            return
        # greyBot stays in its channel for good: after a restart or a lost connection it comes back by itself,
        # and says so again, because a new listening session deserves a new notice.
        home = self.home()
        if not home or time.time() < self.retry:
            return
        channel = self.client.get_channel(int(home)) if self.client else None
        if channel is not None and self.alone(channel):
            self.forget()
            return
        self.retry = time.time() + 60
        await self.leave()
        await api.request("POST", f"/channels/{home}/messages", body={"content": NOTICE, "allowed_mentions": {"parse": []}})
        await self.join(home)


class Pool:
    """greyBot plus any helper bots, so several voice channels can be clipped at once: one account each."""

    def __init__(self, recorders):
        self.recorders = list(recorders)

    def listening(self, channel):
        return next((r for r in self.recorders if channel and r.channel == channel), None)

    def idle(self):
        return next((r for r in self.recorders if not r.channel), None)

    async def tick(self, api):
        for recorder in self.recorders:
            try:
                await recorder.tick(api)
                recorder.attempts = 0 if recorder.channel else recorder.attempts
            except Exception as exc:
                # Each try posts a notice, so a channel that cannot be rejoined is given up on, not spammed.
                recorder.attempts += 1
                if isinstance(exc, Denied) or recorder.attempts >= 3:
                    recorder.forget()
                    recorder.attempts = 0
        expire(self.recorders[0].cfg, self.recorders[0].store)

    async def leave(self):
        """Disconnect without forgetting: used at shutdown, so every account returns to its channel on restart."""
        for recorder in self.recorders:
            await recorder.leave()

    def forget(self):
        for recorder in self.recorders:
            recorder.forget()


def expire(cfg, store):
    with store.connection() as db:
        rows = db.execute("SELECT id FROM voice_captures WHERE guild=? AND state IN ('draft','discarded','published') "
                          "AND created<?", (cfg.guild_id, time.time() - CAPTURE_TTL)).fetchall()
        for row in rows:
            capture_path(cfg, row["id"]).unlink(missing_ok=True)
        # Published rows outlive their audio for a day so the daily limit can count them.
        db.executemany("UPDATE voice_captures SET state='expired' WHERE id=? AND state='published'", [(row["id"],) for row in rows])
        db.executemany("DELETE FROM voice_captures WHERE id=? AND state!='expired'", [(row["id"],) for row in rows])
        db.execute("DELETE FROM voice_captures WHERE state='expired' AND created<?", (time.time() - 90000,))
        db.execute("DELETE FROM voice_tokens WHERE created<?", (time.time() - TOKEN_TTL,))


async def authorize(cfg, store, api, user):
    """Any verified human member may use voice clips; returns whether they are also an administrator."""
    context = await api.member_context(user)
    member = context["member"]
    if member.get("user", {}).get("bot") or member.get("pending"):
        raise Denied("Verified server membership is required")
    admin = bool(context["owner"] or context["permissions"] & ADMINISTRATOR)
    verified = store.settings(cfg.guild_id)["values"].get("verification_role", "")
    if verified and not admin:
        roles = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
        base = next((r for r in roles if r["id"] == verified), None)
        held = [r for r in roles if r["id"] in member.get("roles", [])]
        if not base or not any(r["id"] == verified or r["position"] > base["position"] for r in held):
            raise Denied("Verified server membership is required")
    return admin


async def voice_channel(cfg, api, user):
    """The voice channel the member is connected to right now, from Discord rather than the request."""
    try:
        state = await api.request("GET", f"/guilds/{cfg.guild_id}/voice-states/{user}")
    except Denied:
        return ""
    return str((state or {}).get("channel_id") or "")


def owned_capture(cfg, store, capture_id, user, admin):
    with store.connection() as db:
        row = db.execute("SELECT * FROM voice_captures WHERE id=? AND guild=?", (capture_id, cfg.guild_id)).fetchone()
    if not row or (row["actor"] != user and not admin):
        raise Denied("That capture is no longer available")
    return dict(row)


async def bot_permissions(cfg, api, channel_id, bot=""):
    gid = cfg.guild_id
    channel = await api.request("GET", f"/channels/{channel_id}")
    if channel.get("guild_id") != gid or channel.get("type") != 2:
        raise Denied("That is not a voice channel in this server")
    roles = await api.request("GET", f"/guilds/{gid}/roles")
    member = await api.request("GET", f"/guilds/{gid}/members/{bot or cfg.client_id}")
    return effective_permissions(gid, roles, member, channel)


def button(label, action, capture, style=2):
    return {"type": 2, "style": style, "label": label, "custom_id": PREFIX + action + ":" + capture}


def commands():
    # One command, no options: everything after it is a button.
    return [{"name": "clip", "description": "Turn the funny thing that just happened in voice chat into a soundboard sound",
             "dm_permission": False}]


def clip_buttons(capture):
    return [{"type": 1, "components": [button("✅ Add to soundboard", "name", capture, 3), button("⏪ A bit earlier", "earlier", capture),
                                       button("⏩ A bit later", "later", capture), button("🗑️ Throw away", "discard", capture, 4)]}]


def stop_button():
    return [{"type": 1, "components": [button("👋 Stop listening", "stop", "0" * 32)]}]


def receive(cfg, store, packet):
    """Answer one relayed interaction within Discord's deadline; the worker finishes it through the token."""
    member = packet.get("member", {})
    actor = member.get("user", {}).get("id")
    token = packet.get("token", "")
    if (packet.get("guild_id") != cfg.guild_id or packet.get("application_id") != cfg.client_id
            or not actor or member.get("user", {}).get("bot") or member.get("pending")
            or not str(packet.get("id", "")).isdecimal() or not token):
        raise Denied("A valid server interaction is required")
    data = packet.get("data", {})
    subject, body = "", {}
    if packet["type"] == 2:
        # /clip means "start listening" the first time and "grab that" every time after; the worker knows which.
        action = "clip"
        # Derived from the interaction so a relayed retry names the same capture.
        body = {"capture": hashlib.sha256(packet["id"].encode()).hexdigest()[:32]}
    else:
        parts = str(data.get("custom_id", ""))[len(PREFIX):].split(":")
        if len(parts) != 2 or parts[0] not in {"name", "named", "earlier", "later", "discard", "stop"}:
            raise Denied("That button does not work any more. Type /clip to start again.")
        action, subject = parts
        if (packet["type"] == 5) != (action == "named"):
            raise Denied("That button does not work any more. Type /clip to start again.")
        if action == "stop":
            action, subject = "leave", ""
        else:
            owned_capture(cfg, store, subject, actor, bool(int(member.get("permissions", "0")) & ADMINISTRATOR))
        if action == "name":
            return {"type": 9, "data": {"custom_id": PREFIX + "named:" + subject, "title": "Name your sound",
                    "components": [{"type": 1, "components": [{"type": 4, "custom_id": "name", "style": 1, "required": True,
                        "label": "What should we call it?", "placeholder": "Big pull", "min_length": 2, "max_length": 32}]}]}}
        if action == "named":
            values = {c["custom_id"]: c.get("value", "") for r in data.get("components", []) for c in r.get("components", [])}
            name = " ".join(str(values.get("name", "")).split())
            if not NAME.fullmatch(name):
                raise Denied("Pick a name with 2 to 32 letters or numbers, like **Big pull**. Press the green button to try again.")
            action, body = "publish", {"name": name}
        elif action in {"earlier", "later"}:
            action, body = "shift", {"by": -SHIFT_SECONDS if action == "earlier" else SHIFT_SECONDS}
    job = "voice-" + packet["id"]
    with store.connection() as db:
        db.execute("INSERT OR REPLACE INTO voice_tokens VALUES(?,?,?)", (job, token, time.time()))
    store.queue(job, cfg.guild_id, actor, "voice_" + action, subject, body)
    # Deferred and private: only the member sees greyBot thinking, then its answer.
    return {"type": 5, "data": {"flags": 64}}


async def execute(cfg, store, api, job, recorder):
    with store.connection() as db:
        row = db.execute("DELETE FROM voice_tokens WHERE job=? AND created>? RETURNING token",
                         (job["id"], time.time() - TOKEN_TTL)).fetchone()
    token = row["token"] if row else ""

    async def answer(content, components=None, sound=None):
        if token:
            await api.followup(token, content, components, sound)

    try:
        await run(cfg, store, api, job, recorder, answer)
    except Denied as exc:
        await answer(str(exc))
        raise
    except Exception:
        await answer("greyBot could not confirm that. Check before trying again.")
        raise


async def run(cfg, store, api, job, recorder, answer):
    actor = job["actor"]
    admin = await authorize(cfg, store, api, actor)
    body = json.loads(job["body"])
    kind = job["kind"]
    pool = recorder if isinstance(recorder, Pool) else Pool([recorder])
    if kind in {"voice_clip", "voice_leave"}:
        # Presence is read from Discord at execution time: nobody can listen to a channel they are not in.
        present = await voice_channel(cfg, api, actor)
        recorder = pool.listening(present)
    if kind == "voice_clip" and not recorder:
        if not present:
            raise Denied("Hop into a voice channel first, then type **/clip** again.")
        recorder = pool.idle()
        if not recorder:
            raise Denied("greyBot is busy in another voice channel right now. Try again when it is free.")
        # greyBot posts the notice; whichever account is free joins.
        poster = await bot_permissions(cfg, api, present)
        joiner = poster if recorder.user_id == cfg.client_id else await bot_permissions(cfg, api, present, recorder.user_id)
        if poster & (VIEW | SEND) != VIEW | SEND or joiner & (VIEW | CONNECT) != VIEW | CONNECT:
            raise Denied("greyBot is not allowed into your voice channel. Ask an admin to let it in.")
        # No notice, no listening: the announcement is posted before the connection is made.
        await api.request("POST", f"/channels/{present}/messages",
                          body={"content": NOTICE, "allowed_mentions": {"parse": []}})
        await recorder.join(present)
        await answer("👂 greyBot is listening now! When something funny happens, type **/clip** and it grabs the last few seconds.",
                     stop_button())
    elif kind == "voice_clip":
        with store.connection() as db:
            drafts = db.execute("SELECT COUNT(*) FROM voice_captures WHERE guild=? AND actor=? AND state='draft'",
                                (cfg.guild_id, actor)).fetchone()[0]
        if drafts >= MAX_DRAFTS and not admin:
            raise Denied("You have a lot of clips waiting. Add or throw away one first.")
        # The window ends when the member asked, not when the queue reached the job.
        end = min(job["created"], time.time())
        seconds = min(CAPTURE_SECONDS, max(1.0, end - recorder.since))
        pcm = recorder.timeline.render(end, seconds)
        if not any(pcm):
            # Counts only: enough to tell "nobody spoke" from "audio arrived and could not be used".
            log.warning("Clip heard nothing: dropped=%s unidentified=%s failures=%s buffered speakers=%s",
                        getattr(recorder, "dropped", 0), getattr(recorder, "unknown", 0),
                        getattr(recorder, "failures", {}), len(recorder.timeline.tracks))
            raise Denied("greyBot did not hear anybody talking just now. Try again after something happens!")
        length = min(MAX_SOUND_SECONDS, seconds)
        start = max(0.0, last_sound(pcm) - length)
        capture_path(cfg, body["capture"]).write_bytes(wav_bytes(pcm))
        with store.connection() as db:
            db.execute("INSERT INTO voice_captures(id,guild,actor,channel,created,seconds,state,start,length) VALUES(?,?,?,?,?,?,?,?,?)",
                       (body["capture"], cfg.guild_id, actor, recorder.channel, time.time(), seconds, "draft", start, length))
        await preview(cfg, store, body["capture"], answer, "🎬 Got it! Press play to hear your clip.")
    elif kind == "voice_shift":
        row = owned_capture(cfg, store, job["subject"], actor, admin)
        start = min(max(0.0, row["start"] + body["by"]), row["seconds"] - row["length"])
        with store.connection() as db:
            db.execute("UPDATE voice_captures SET start=? WHERE id=? AND state='draft'", (start, job["subject"]))
        edge = "That is as far as it goes. " if start == row["start"] else ""
        await preview(cfg, store, job["subject"], answer, edge + "Here it is, moved a bit. Press play!")
    elif kind == "voice_leave":
        if not recorder and not admin:
            raise Denied("Only someone in the voice channel can ask greyBot to leave.")
        # An administrator outside the channel stops every listener. Forgetting the channel is what makes it final.
        await (recorder or pool).leave()
        (recorder or pool).forget()
        await answer("👋 greyBot stopped listening and left. Type **/clip** any time to bring it back.")
    elif kind == "voice_discard":
        owned_capture(cfg, store, job["subject"], actor, admin)
        capture_path(cfg, job["subject"]).unlink(missing_ok=True)
        with store.connection() as db:
            db.execute("UPDATE voice_captures SET state='discarded' WHERE id=? AND guild=? AND state='draft'", (job["subject"], cfg.guild_id))
        await answer("🗑️ Thrown away. Type **/clip** to grab another one.")
    elif kind == "voice_publish":
        owned_capture(cfg, store, job["subject"], actor, admin)
        with store.connection() as db:
            db.execute("UPDATE voice_captures SET name=? WHERE id=? AND state='draft'", (body["name"], job["subject"]))
        await publish(cfg, store, api, job, owned_capture(cfg, store, job["subject"], actor, admin), admin)
        await answer(f"🎉 **{body['name']}** is on the soundboard! Open the soundboard in a voice channel to play it.")
    else:
        raise Denied("Unsupported action")


async def preview(cfg, store, capture, answer, words):
    with store.connection() as db:
        row = db.execute("SELECT start,length FROM voice_captures WHERE id=?", (capture,)).fetchone()
    sound = encode_mp3(trim(capture_path(cfg, capture), row["start"], row["length"]))
    await answer(words + " If it sounds good, press the green button and give it a name.", clip_buttons(capture), ("clip.mp3", sound))


async def publish(cfg, store, api, job, row, admin):
    with store.connection() as db:
        recent = db.execute("SELECT COUNT(*) FROM voice_captures WHERE guild=? AND actor=? AND state IN ('publishing','published','expired') "
                            "AND created>?", (cfg.guild_id, job["actor"], time.time() - 86400)).fetchone()[0]
    # Soundboard slots are scarce and shared by the whole server.
    if recent >= MAX_DAILY_SOUNDS and not admin:
        raise Denied(f"You already added {MAX_DAILY_SOUNDS} sounds today. You can add more tomorrow!")
    bot = await api.member_context(cfg.client_id)
    if not bot["permissions"] & (ADMINISTRATOR | CREATE_EXPRESSIONS):
        raise Denied("greyBot is not allowed to add sounds yet. Ask an admin to give it Create Expressions.")
    if not NAME.fullmatch(row["name"]) or not 0 < row["length"] <= MAX_SOUND_SECONDS:
        raise Denied("Press the green button and give your clip a name first.")
    sound = encode_mp3(trim(capture_path(cfg, job["subject"]), row["start"], row["length"]))
    if len(sound) > MAX_SOUND_BYTES:
        raise Denied("That clip is too big for the soundboard.")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("UPDATE voice_captures SET state='publishing' WHERE id=? AND guild=? AND state='draft'",
                             (job["subject"], cfg.guild_id)).rowcount
    if not changed:
        raise Denied("That clip was already added or thrown away. Type /clip to grab a new one.")
    # Reserved before sending: an ambiguous upload stays 'publishing' for review instead of duplicating a sound.
    try:
        created = await api.request("POST", f"/guilds/{cfg.guild_id}/soundboard-sounds",
            body={"name": row["name"], "volume": 1, "sound": "data:audio/mpeg;base64," + base64.b64encode(sound).decode()},
            reason=f"greyBot member {job['actor']}: voice clip")
    except Denied:
        with store.connection() as db:
            db.execute("UPDATE voice_captures SET state='draft' WHERE id=?", (job["subject"],))
        raise Denied("The soundboard is full, so it could not be added. Ask an admin to remove an old sound, then press the green button again.") from None
    capture_path(cfg, job["subject"]).unlink(missing_ok=True)
    with store.connection() as db:
        db.execute("UPDATE voice_captures SET state='published',sound=? WHERE id=?", (str(created["sound_id"]), job["subject"]))
