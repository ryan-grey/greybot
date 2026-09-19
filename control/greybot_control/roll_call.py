"""Who was in one voice channel at a past moment, for the raid roll call the Lambda draws.

The Lambda sees a raid's first kill of the night up to fifteen minutes late; the journal knows who was in voice
at the second it happened. This answers that one question and nothing else: no history, no other channels, no
message data. It is not a member endpoint, so there is no cookie; the caller proves itself by signing the
request with a secret both sides read from SSM. Unset, the route does not exist.
"""
import hashlib
import hmac
import json
import time

from fastapi import HTTPException, Request

from .voice_activity import compute

WINDOW = 300            # a signature is good for five minutes either side, which bounds replay
MAX_BODY = 512
# What actually loses voice events. A bare COLLECTOR_DISCONNECTED does not: the gateway drops
# and RESUMES several times a day and Discord replays everything missed, so counting it emptied
# the channel at 9:49 PM on a night nineteen people raided until 11. A fresh session (READY,
# journalled as COLLECTOR_CONNECTED) and a host outage do lose them.
LOSSES = ("COLLECTOR_CONNECTED", "HOST_HEALTH_GAP")


def signature(secret, stamp, body):
    return hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(secret, header, body, now=None):
    parts = dict(item.split("=", 1) for item in (header or "").split(",") if "=" in item)
    stamp, given = parts.get("t", ""), parts.get("v1", "")
    if not stamp.isdecimal() or abs((now or time.time()) - int(stamp)) > WINDOW:
        return False
    return hmac.compare_digest(signature(secret, stamp, body), given)


def present(store, guild, channel, at):
    """Member IDs in `channel` at `at`. A stay cut short by a real loss of events is not counted: nobody is
    reported present on the strength of a join the journal could not see through."""
    with store.connection() as db:
        rows = db.execute("SELECT kind,subject,observed,payload FROM events WHERE guild=? AND observed<=? AND kind IN "
                          "(" + ",".join("?" * (len(LOSSES) + 1)) + ") ORDER BY seq",
                          (guild, at, "VOICE_STATE_UPDATE", *LOSSES)).fetchall()
    _, live, _ = compute([tuple(r) for r in rows], set(), at)
    return [uid for uid, where in live.items() if where == channel]


def install(app, cfg, store, directory):
    if not cfg.rollcall_secret:
        return

    @app.post("/internal/roll-call")
    async def roll_call(request: Request):
        body = await request.body()
        if len(body) > MAX_BODY or not verify(cfg.rollcall_secret, request.headers.get("X-Greybot-Signature"), body):
            raise HTTPException(404)
        try:
            asked = json.loads(body)
            channel, at = str(asked["channel_id"]), float(asked["at"])
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "channel_id and at are required") from None
        if not channel.isdecimal() or at > time.time() + WINDOW:
            raise HTTPException(400, "channel_id and at are required")
        listing = await directory.get()
        members = {m["id"]: m for m in listing["members"]}
        names = {c["id"]: c["name"] for c in listing["channels"]}
        people = [{"id": uid, "name": members[uid]["name"], "avatar_url": members[uid].get("avatar_url", "")}
                  for uid in present(store, cfg.guild_id, channel, at)
                  if uid in members and not members[uid].get("bot")]
        people.sort(key=lambda p: p["name"].casefold())
        return {"channel": names.get(channel, ""), "at": at, "members": people}
