"""Synthetic fixtures: a fake Discord data-export ZIP and a fake REST API.

Every id, name and message here is invented. No real Discord data, ever -
the repository never contains message content.
"""

import datetime
import json
import zipfile

DISCORD_EPOCH_MS = 1420070400000

ME = {"id": "900000000000000001", "username": "fixture_me", "global_name": "Fixture Me"}
FRIEND = {"id": "900000000000000002", "username": "alexfixture", "global_name": "Alex Fixture"}
BOT = {"id": "900000000000000099", "username": "Grey Reader", "bot": True}

GUILD = {"id": "800000000000000001", "name": "Fixture Guild"}
GENERAL = {"id": "700000000000000001", "name": "general", "type": 0}
RAIDS = {"id": "700000000000000002", "name": "raids", "type": 0}
DM_CHANNEL = {"id": "700000000000000101", "type": 1, "recipients": [FRIEND["id"]]}
GROUP_DM = {"id": "700000000000000102", "type": 3, "name": "weekend plans",
            "recipients": [FRIEND["id"], "900000000000000003"]}


def snowflake(iso, seq=0):
    """A Discord id whose embedded timestamp is the given instant."""
    dt = datetime.datetime.fromisoformat(iso)
    ms = int(dt.timestamp() * 1000)
    return ((ms - DISCORD_EPOCH_MS) << 22) | seq


def export_stamp(iso):
    """The package writes timestamps like '2026-08-01 10:00:00.123000+00:00'."""
    return iso.replace("T", " ").replace("+00:00", ".000000+00:00")


# ------------------------------------------------------------ export zip --

EXPORT_DM = [
    ("2026-08-01T10:00:00+00:00", "pizza on friday at seven?"),
    ("2026-08-01T10:05:00+00:00", "bring the board game"),
    ("2026-08-03T09:00:00+00:00", "raid went well last night"),
]
EXPORT_GENERAL = [  # my own messages in the guild; the bot sees these too
    ("2026-08-02T18:00:00+00:00", "gg everyone, pizza next week"),
]
EXPORT_GROUP = [
    ("2026-08-04T12:00:00+00:00", "who is driving saturday"),
]


def build_export_zip(path, seq=0):
    """messages/index.json + one folder per channel. The DM and group DM use
    the current messages.json layout; the guild channel uses the older
    messages.csv layout so both parsers are exercised."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("package/account/user.json", json.dumps(ME))
        zf.writestr(
            "package/messages/index.json",
            json.dumps(
                {
                    DM_CHANNEL["id"]: "Direct Message with alexfixture",
                    GROUP_DM["id"]: "weekend plans",
                    GENERAL["id"]: "general in Fixture Guild",
                }
            ),
        )
        zf.writestr(
            f"package/messages/c{DM_CHANNEL['id']}/channel.json", json.dumps(DM_CHANNEL)
        )
        zf.writestr(
            f"package/messages/c{DM_CHANNEL['id']}/messages.json",
            json.dumps(
                [
                    {"ID": snowflake(t, seq), "Timestamp": export_stamp(t),
                     "Contents": body, "Attachments": ""}
                    for t, body in EXPORT_DM
                ]
            ),
        )
        zf.writestr(
            f"package/messages/c{GROUP_DM['id']}/channel.json", json.dumps(GROUP_DM)
        )
        zf.writestr(
            f"package/messages/c{GROUP_DM['id']}/messages.json",
            json.dumps(
                [
                    {"ID": snowflake(t, seq), "Timestamp": export_stamp(t),
                     "Contents": body,
                     "Attachments": "https://cdn.example.invalid/a/map.png"}
                    for t, body in EXPORT_GROUP
                ]
            ),
        )
        guild_chan = dict(GENERAL, guild=GUILD)
        zf.writestr(
            f"package/messages/c{GENERAL['id']}/channel.json", json.dumps(guild_chan)
        )
        rows = ["ID,Timestamp,Contents,Attachments"]
        for t, body in EXPORT_GENERAL:
            rows.append(f'{snowflake(t, seq)},{export_stamp(t)},"{body}",')
        zf.writestr(
            f"package/messages/c{GENERAL['id']}/messages.csv", "\n".join(rows) + "\n"
        )


# --------------------------------------------------------------- fake API --


class FakeDiscord:
    """Enough of the REST API for sync and the write tools. Records every
    call so tests can assert what was sent."""

    def __init__(self, message_content_intent=True):
        self.calls = []
        self.intent = message_content_intent
        self.guilds = [GUILD]
        self.channels = {GUILD["id"]: [dict(GENERAL), dict(RAIDS)]}
        self.messages = {GENERAL["id"]: [], RAIDS["id"]: []}
        self.next_seq = 1000
        # The bot sees other people's messages AND my own in the guild.
        self.add(GENERAL["id"], FRIEND, "anyone up for pizza tonight", "2026-08-02T17:00:00+00:00")
        self.add(GENERAL["id"], ME, "gg everyone, pizza next week", "2026-08-02T18:00:00+00:00",
                 mid=snowflake("2026-08-02T18:00:00+00:00", 0))
        self.add(RAIDS["id"], FRIEND, "raid at 8, be there", "2026-08-03T01:00:00+00:00")

    def add(self, cid, author, content, iso, mid=None, attachments=None):
        if mid is None:
            self.next_seq += 1
            mid = snowflake(iso, self.next_seq)
        m = {
            "id": str(mid), "channel_id": cid, "author": dict(author),
            "content": content if self.intent or author is BOT else "",
            "timestamp": iso, "edited_timestamp": None,
            "attachments": attachments or [],
        }
        self.messages.setdefault(cid, []).append(m)
        return m

    def __call__(self, method, path, body=None, params=None):
        self.calls.append((method, path, body, params))
        parts = path.strip("/").split("/")
        if path == "/users/@me":
            return dict(BOT)
        if path == "/users/@me/guilds":
            return [dict(g) for g in self.guilds]
        if path == "/applications/@me":
            return {"id": BOT["id"], "flags": (1 << 18) if self.intent else 0}
        if parts[0] == "guilds" and parts[-1] == "channels":
            return [dict(c) for c in self.channels.get(parts[1], [])]
        if parts[0] == "guilds" and parts[-2:] == ["threads", "active"]:
            return {"threads": []}
        if parts[0] == "channels" and len(parts) == 3 and parts[2] == "messages":
            cid = parts[1]
            if cid not in self.messages:
                raise RuntimeError("Discord 403 on GET: Missing Access")
            if method == "GET":
                after = int((params or {}).get("after", 0))
                limit = int((params or {}).get("limit", 50))
                rows = sorted(
                    (m for m in self.messages[cid] if int(m["id"]) > after),
                    key=lambda m: int(m["id"]),
                )[:limit]
                return [dict(m) for m in reversed(rows)]  # newest first, like Discord
            if method == "POST":
                now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                return dict(self.add(cid, BOT, body["content"], now))
        if parts[0] == "channels" and len(parts) == 4 and parts[2] == "messages":
            cid, mid = parts[1], parts[3]
            for m in self.messages.get(cid, []):
                if m["id"] == mid:
                    if method == "PATCH":
                        if m["author"]["id"] != BOT["id"]:
                            raise RuntimeError(
                                "Discord 403 on PATCH: Cannot edit a message authored by another user"
                            )
                        m["content"] = body["content"]
                        m["edited_timestamp"] = "2026-08-05T00:00:00+00:00"
                        return dict(m)
                    if method == "DELETE":
                        self.messages[cid].remove(m)
                        return None
            raise RuntimeError("Discord 404 on %s: Unknown Message" % method)
        raise RuntimeError(f"FakeDiscord: unhandled {method} {path}")

