"""Direct messages to greyBot reach a person, and that person can answer as greyBot.

A member who DMs the bot is told, once a day, that a human will read it. The message is passed to the owner's
own DM with greyBot. The owner answers by using Discord's Reply on that forwarded message; greyBot delivers the
answer and ticks it. Message text is relayed, never stored: the only record kept is which forwarded message
belongs to which member, plus contentless journal entries.
"""
import secrets
import time

from .discord_api import Denied, Unavailable

NOTICE_SECONDS = 86400
KEEP_SECONDS = 30 * 86400
LIMIT = 1900


def install(store):
    with store.connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS dm_forwards(
                message TEXT PRIMARY KEY, member TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS dm_notices(
                member TEXT PRIMARY KEY, sent REAL NOT NULL);
        ''')


async def send(api, user, content):
    channel = await api.request("POST", "/users/@me/channels", body={"recipient_id": user})
    return await api.request("POST", f"/channels/{channel['id']}/messages",
                             body={"content": content[:2000], "allowed_mentions": {"parse": []}})


def body_of(message):
    parts = [message.get("content") or ""] + [a for a in message.get("attachments", [])]
    return "\n".join(p for p in parts if p).strip()


async def receive(cfg, store, api, message):
    """`message` is a plain dict: author, bot, content, attachments (URLs), id, channel, reference (message id or '')."""
    owner = cfg.dm_owner_id
    if not owner or message["bot"]:
        return
    if message["author"] == owner:
        await answer(cfg, store, api, message)
        return
    text = body_of(message)
    if not text:
        return
    forwarded = await send(api, owner, f"📨 **DM to greyBot** from <@{message['author']}>\n{text[:LIMIT]}\n"
                                       "-# Use Reply on this message to answer as greyBot.")
    now = time.time()
    with store.connection() as db:
        db.execute("DELETE FROM dm_forwards WHERE created<?", (now - KEEP_SECONDS,))
        db.execute("INSERT OR REPLACE INTO dm_forwards VALUES(?,?,?)", (forwarded["id"], message["author"], now))
        noticed = db.execute("SELECT sent FROM dm_notices WHERE member=?", (message["author"],)).fetchone()
    store.append("dm-in:" + secrets.token_hex(16), cfg.guild_id, "DM_FORWARDED", message["author"], {})
    if not noticed or now - noticed["sent"] > NOTICE_SECONDS:
        # Nobody should think they are talking to software alone when a person is reading.
        await send(api, message["author"], f"greyBot is a bot, so I passed your message to <@{owner}>, who runs it. "
                                           "They may answer you here.")
        with store.connection() as db:
            db.execute("INSERT OR REPLACE INTO dm_notices VALUES(?,?)", (message["author"], now))


async def answer(cfg, store, api, message):
    async def react(emoji):
        try:
            await api.request("PUT", f"/channels/{message['channel']}/messages/{message['id']}/reactions/{emoji}/@me")
        except (Denied, Unavailable):
            pass

    member = None
    if message["reference"]:
        with store.connection() as db:
            row = db.execute("SELECT member FROM dm_forwards WHERE message=?", (message["reference"],)).fetchone()
        member = row["member"] if row else None
    text = body_of(message)
    if not member or not text:
        if text:
            await send(api, cfg.dm_owner_id, "To answer someone as greyBot, use Discord's **Reply** on the forwarded message you are "
                                             "answering. Nothing was sent.")
        return
    try:
        await send(api, member, text)
    except (Denied, Unavailable):
        # Closed DMs or a lost response: say so rather than let a reply vanish. Never resent automatically.
        await react("%E2%9D%8C")
        await send(api, cfg.dm_owner_id, f"greyBot could not confirm delivery to <@{member}>. They may have DMs closed. Check before resending.")
        return
    store.append("dm-out:" + secrets.token_hex(16), cfg.guild_id, "DM_REPLIED", member, {"actor": cfg.dm_owner_id})
    await react("%E2%9C%85")
