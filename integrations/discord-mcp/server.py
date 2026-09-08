#!/usr/bin/env python3
"""Discord, as a local MCP server for the Claude desktop app.

One searchable index over three sources, each row tagged with where it came
from so a search returns everything and the caller can always tell:

  source=bot     servers greyBot has been invited to. Read via
                 the REST API, incrementally by message snowflake. This is
                 the ONLY source that can write (send / edit / delete), and it
                 writes as the bot, never as you.
  source=export  Discord's official data-request package (Settings > Data &
                 Privacy > Request my data). It contains only the messages
                 YOU sent, across every server and DM, back to the beginning.
                 A one-time backfill, entirely within Discord's terms.
  source=local   an optional passive capture database written by a separate
                 client-side project (discord-local-log). Read-only here.

What this server deliberately does NOT do: it never uses a user token, never
automates your own account, and refuses to send, edit or delete anywhere the
bot cannot see. Discord forbids automating user accounts; there is no
compliant way to write as yourself, so the refusal says so plainly instead of
failing obscurely.

Setup, once:

    python3 -m venv ~/.local/discord-mcp/.venv
    ~/.local/discord-mcp/.venv/bin/pip install mcp

then register in claude_desktop_config.json:

    "discord": {
      "command": "/Users/<you>/.local/discord-mcp/.venv/bin/python",
      "args": ["/Users/<you>/Developer/greybot/integrations/discord-mcp/server.py"],
      "env": {"DISCORD_BOT_TOKEN_SSM": "/discord-mcp/bot-token"}
    }

The bot token comes from DISCORD_BOT_TOKEN, or from the AWS SSM parameter
named by DISCORD_BOT_TOKEN_SSM (read with the aws CLI, profile
DISCORD_AWS_PROFILE, default "infra"). It is never written to disk by this
file and never returned by any tool.

State lives outside the repo in ~/.local/discord-mcp: index.db (SQLite +
FTS5) and discord-changes.log (before/after of every write).
"""

import csv
import datetime
import io
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

HOME = Path.home()
STATE_DIR = Path(os.environ.get("DISCORD_MCP_STATE_DIR", HOME / ".local/discord-mcp"))
INDEX_DB = Path(os.environ.get("DISCORD_INDEX_DB", STATE_DIR / "index.db"))
CHANGES_LOG = Path(os.environ.get("DISCORD_CHANGES_LOG", STATE_DIR / "discord-changes.log"))
LOCAL_DB = Path(
    os.environ.get(
        "DISCORD_LOCAL_DB",
        HOME / "Library/Application Support/discord-local-log/discord.db",
    )
)
API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/ryan-grey/greybot/tree/main/integrations/discord-mcp, 0.1)"
DISCORD_EPOCH_MS = 1420070400000

# Channel types (https://discord.com/developers/docs/resources/channel)
GUILD_TEXT, DM, GUILD_ANNOUNCEMENT, GROUP_DM = 0, 1, 5, 3
ANNOUNCEMENT_THREAD, PUBLIC_THREAD, PRIVATE_THREAD = 10, 11, 12
TEXT_TYPES = {GUILD_TEXT, GUILD_ANNOUNCEMENT}
THREAD_TYPES = {ANNOUNCEMENT_THREAD, PUBLIC_THREAD, PRIVATE_THREAD}
DM_TYPES = {DM, GROUP_DM}

# Application flags that mean the Message Content privileged intent is on.
FLAG_MESSAGE_CONTENT = 1 << 18
FLAG_MESSAGE_CONTENT_LIMITED = 1 << 19

mcp = MCPServer(
    name="discord",
    instructions=(
        "Local index of Discord: search, read channels and DMs, list servers, "
        "import the official data export, and send/edit/delete as the bot in "
        "servers the bot is in. Reads span all sources (bot, export, local); "
        "writes are bot-only and require confirm=true."
    ),
)


# -------------------------------------------------------------------- time --


def _ts(ms):
    """Unix milliseconds (UTC) -> ISO local time."""
    if ms is None:
        return None
    return (
        datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def _ms(value):
    """ISO date/datetime (naive = local) or Discord timestamp -> unix ms."""
    s = str(value).strip().replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1000)


def _snowflake_ms(snowflake):
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


# ------------------------------------------------------------------- index --

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds(
    id TEXT PRIMARY KEY, name TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS channels(
    id TEXT PRIMARY KEY, guild_id TEXT, name TEXT, type INTEGER,
    source TEXT, recipients TEXT, last_synced_id INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY, source TEXT, guild_id TEXT, channel_id TEXT,
    author_id TEXT, author_name TEXT, content TEXT, ts INTEGER,
    edited_ts INTEGER, attachments TEXT, deleted INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel_id, ts);
CREATE INDEX IF NOT EXISTS messages_source ON messages(source);
CREATE VIRTUAL TABLE IF NOT EXISTS fts
    USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO fts(fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO fts(fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

# Which source "owns" a row when the same message id arrives from two of
# them. The bot sees the full message (author, edits, attachments); the
# export sees only your side; local capture sits in between.
SOURCE_RANK = {"bot": 3, "local": 2, "export": 1}


def _index_con():
    import sqlite3

    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(INDEX_DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _upsert_guild(con, gid, name, source):
    if not gid:
        return
    con.execute(
        """INSERT INTO guilds(id, name, source) VALUES (?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             name = COALESCE(excluded.name, guilds.name)""",
        (str(gid), name, source),
    )
    # The higher-ranked source keeps the row (bot > local > export).
    row = con.execute("SELECT source FROM guilds WHERE id = ?", (str(gid),)).fetchone()
    if SOURCE_RANK.get(row["source"], 0) < SOURCE_RANK[source]:
        con.execute("UPDATE guilds SET source = ? WHERE id = ?", (source, str(gid)))


def _upsert_channel(con, cid, guild_id, name, ctype, source, recipients=None):
    con.execute(
        """INSERT INTO channels(id, guild_id, name, type, source, recipients)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             guild_id = COALESCE(excluded.guild_id, channels.guild_id),
             name = COALESCE(excluded.name, channels.name),
             type = COALESCE(excluded.type, channels.type),
             recipients = COALESCE(excluded.recipients, channels.recipients)""",
        (
            str(cid),
            str(guild_id) if guild_id else None,
            name,
            ctype,
            source,
            json.dumps(recipients) if recipients else None,
        ),
    )
    row = con.execute("SELECT source FROM channels WHERE id = ?", (str(cid),)).fetchone()
    if SOURCE_RANK.get(row["source"], 0) < SOURCE_RANK[source]:
        con.execute("UPDATE channels SET source = ? WHERE id = ?", (source, str(cid)))


def _upsert_message(con, m):
    """Insert or refresh one message. A higher-ranked source overwrites a
    lower one; a lower-ranked source never clobbers a higher one, so
    re-importing the export after a bot sync changes nothing."""
    existing = con.execute(
        "SELECT source FROM messages WHERE id = ?", (int(m["id"]),)
    ).fetchone()
    if existing and SOURCE_RANK[existing["source"]] > SOURCE_RANK[m["source"]]:
        return "kept"
    con.execute(
        """INSERT INTO messages(id, source, guild_id, channel_id, author_id,
                                author_name, content, ts, edited_ts,
                                attachments, deleted)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             source = excluded.source, guild_id = excluded.guild_id,
             channel_id = excluded.channel_id, author_id = excluded.author_id,
             author_name = excluded.author_name, content = excluded.content,
             ts = excluded.ts, edited_ts = excluded.edited_ts,
             attachments = excluded.attachments, deleted = excluded.deleted""",
        (
            int(m["id"]),
            m["source"],
            str(m["guild_id"]) if m.get("guild_id") else None,
            str(m["channel_id"]),
            str(m["author_id"]) if m.get("author_id") else None,
            m.get("author_name"),
            m.get("content") or "",
            m.get("ts") or _snowflake_ms(m["id"]),
            m.get("edited_ts"),
            json.dumps(m.get("attachments") or []),
            1 if m.get("deleted") else 0,
        ),
    )
    return "updated" if existing else "inserted"


MSG_SELECT = """
SELECT m.*, c.name AS channel_name, c.type AS channel_type,
       g.name AS guild_name
FROM messages m
LEFT JOIN channels c ON c.id = m.channel_id
LEFT JOIN guilds g ON g.id = m.guild_id
"""


def _shape(rows):
    return [
        {
            "id": str(r["id"]),
            "source": r["source"],
            "time": _ts(r["ts"]),
            "guild": r["guild_name"],
            "guild_id": r["guild_id"],
            "channel": r["channel_name"],
            "channel_id": r["channel_id"],
            "author": r["author_name"],
            "author_id": r["author_id"],
            "content": r["content"],
            "attachments": json.loads(r["attachments"] or "[]"),
            "edited": _ts(r["edited_ts"]) if r["edited_ts"] else None,
            "deleted": bool(r["deleted"]),
        }
        for r in rows
    ]


def _scope(where, args, guild="", channel="", author="", since="", until="", source=""):
    """Shared filters: ids match exactly, names match as substrings."""
    if guild:
        where.append(
            "(m.guild_id = ? OR m.guild_id IN (SELECT id FROM guilds WHERE name LIKE ?))"
        )
        args += [guild, f"%{guild}%"]
    if channel:
        where.append(
            "(m.channel_id = ? OR m.channel_id IN "
            "(SELECT id FROM channels WHERE name LIKE ?))"
        )
        args += [channel, f"%{channel}%"]
    if author:
        where.append("(m.author_id = ? OR m.author_name LIKE ?)")
        args += [author, f"%{author}%"]
    if since:
        where.append("m.ts >= ?")
        args.append(_ms(since))
    if until:
        where.append("m.ts <= ?")
        args.append(_ms(until))
    if source:
        where.append("m.source = ?")
        args.append(source)


# --------------------------------------------------------------------- bot --

_TOKEN = {"value": None}


def _aws_cli():
    return shutil.which("aws") or next(
        (p for p in ("/opt/homebrew/bin/aws", "/usr/local/bin/aws") if os.path.exists(p)),
        "aws",
    )


def _token():
    """Bot token from the environment or SSM. Cached in memory only."""
    if _TOKEN["value"]:
        return _TOKEN["value"]
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if not tok:
        name = os.environ.get("DISCORD_BOT_TOKEN_SSM")
        if name:
            cmd = [
                _aws_cli(), "ssm", "get-parameter", "--name", name,
                "--with-decryption", "--query", "Parameter.Value",
                "--output", "text",
            ]
            profile = os.environ.get("DISCORD_AWS_PROFILE", "infra")
            if profile:
                cmd += ["--profile", profile]
            region = os.environ.get("AWS_REGION", "us-east-1")
            if region:
                cmd += ["--region", region]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise RuntimeError(f"could not read {name} from SSM: {e}")
            if proc.returncode != 0:
                raise RuntimeError(
                    f"could not read {name} from SSM: {proc.stderr.strip()[:200]}"
                )
            tok = proc.stdout.strip()
    if not tok:
        raise RuntimeError(
            "no bot token: set DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN_SSM in the "
            "server's env (claude_desktop_config.json). See README, Bot setup."
        )
    _TOKEN["value"] = tok
    return tok


def _api(method, path, body=None, params=None):
    """One authenticated REST call. Honors rate limits; raises RuntimeError
    with Discord's message on any other error. Tests replace this."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bot {_token()}")
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("X-RateLimit-Remaining") == "0":
                    time.sleep(float(resp.headers.get("X-RateLimit-Reset-After", "1")))
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < 4:
                try:
                    wait = float(json.loads(payload).get("retry_after", 1))
                except (ValueError, AttributeError):
                    wait = 1.0
                time.sleep(min(wait, 30))
                continue
            try:
                msg = json.loads(payload).get("message", payload)
            except ValueError:
                msg = payload
            raise RuntimeError(f"Discord {e.code} on {method} {path}: {msg}")
    raise RuntimeError(f"Discord rate limit persisted on {method} {path}")


def _bot_message(m, guild_id):
    author = m.get("author") or {}
    return {
        "id": m["id"],
        "source": "bot",
        "guild_id": guild_id,
        "channel_id": m["channel_id"],
        "author_id": author.get("id"),
        "author_name": author.get("global_name") or author.get("username"),
        "content": m.get("content") or "",
        "ts": _ms(m["timestamp"]) if m.get("timestamp") else _snowflake_ms(m["id"]),
        "edited_ts": _ms(m["edited_timestamp"]) if m.get("edited_timestamp") else None,
        "attachments": [
            {"name": a.get("filename"), "url": a.get("url")}
            for a in m.get("attachments") or []
        ],
    }


def _sync_channel(con, ch, guild_id, max_pages):
    """Pull messages newer than the last synced snowflake, oldest first."""
    cid = str(ch["id"])
    row = con.execute("SELECT last_synced_id FROM channels WHERE id = ?", (cid,)).fetchone()
    after = int(row["last_synced_id"] or 0) if row else 0
    fetched = empty = pages = 0
    more = False
    while True:
        batch = _api(
            "GET", f"/channels/{cid}/messages", params={"limit": 100, "after": after}
        ) or []
        if not batch:
            break
        for m in batch:
            shaped = _bot_message(m, guild_id)
            _upsert_message(con, shaped)
            fetched += 1
            if not shaped["content"] and not shaped["attachments"]:
                empty += 1
            after = max(after, int(m["id"]))
        con.execute("UPDATE channels SET last_synced_id = ? WHERE id = ?", (after, cid))
        con.commit()
        pages += 1
        if len(batch) < 100:
            break
        if pages >= max_pages:
            more = True
            break
    return {"fetched": fetched, "empty_content": empty, "more": more}


# ------------------------------------------------------------------ export --


def _export_entries(zf):
    """Yield (channel_dir, channel.json entry) for every channel folder in the
    package, whatever the top-level folder or the c-prefix convention is."""
    for name in zf.namelist():
        parts = name.split("/")
        if len(parts) >= 3 and parts[-1] == "channel.json" and parts[-3] == "messages":
            yield "/".join(parts[:-1]), name


def _export_messages(zf, chan_dir):
    """Rows from messages.json (current packages) or messages.csv (older)."""
    js = f"{chan_dir}/messages.json"
    cs = f"{chan_dir}/messages.csv"
    names = set(zf.namelist())
    if js in names:
        for r in json.loads(zf.read(js).decode("utf-8")):
            yield r
    elif cs in names:
        text = zf.read(cs).decode("utf-8-sig")
        for r in csv.DictReader(io.StringIO(text)):
            yield r


# ------------------------------------------------------------------- tools --


def search_messages(
    query: str,
    guild: str = "",
    channel: str = "",
    author: str = "",
    since: str = "",
    until: str = "",
    source: str = "",
    limit: int = 50,
) -> str:
    """Full-text search across every source, newest first.

    guild/channel/author take an id or a name substring; since/until are
    ISO dates (local time if no zone); source narrows to bot, export or
    local. Every row says which source it came from.
    """
    con = _index_con()
    try:
        match = " ".join('"' + t.replace('"', '""') + '"' for t in query.split())
        where, args = ["fts MATCH ?"], [match]
        _scope(where, args, guild, channel, author, since, until, source)
        sql = (
            "SELECT m.*, c.name AS channel_name, c.type AS channel_type, "
            "g.name AS guild_name FROM fts JOIN messages m ON m.id = fts.rowid "
            "LEFT JOIN channels c ON c.id = m.channel_id "
            "LEFT JOIN guilds g ON g.id = m.guild_id WHERE "
            + " AND ".join(where)
            + " ORDER BY m.ts DESC LIMIT ?"
        )
        args.append(max(1, min(int(limit), 500)))
        rows = con.execute(sql, args).fetchall()
        return json.dumps({"count": len(rows), "messages": _shape(rows)}, ensure_ascii=False)
    finally:
        con.close()


def _channel_row(con, channel_id):
    return con.execute(
        "SELECT * FROM channels WHERE id = ? OR name = ? ORDER BY id LIMIT 1",
        (str(channel_id), str(channel_id)),
    ).fetchone()


def get_channel(channel_id: str, since: str = "", until: str = "", limit: int = 200) -> str:
    """Ordered messages for one channel (id or exact name), oldest first
    within the window; the window is the newest `limit` messages."""
    con = _index_con()
    try:
        ch = _channel_row(con, channel_id)
        if ch is None:
            return json.dumps({"error": f"no indexed channel matching {channel_id!r}"})
        where, args = ["m.channel_id = ?"], [ch["id"]]
        _scope(where, args, since=since, until=until)
        sql = MSG_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY m.ts DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        rows = list(reversed(con.execute(sql, args).fetchall()))
        return json.dumps(
            {
                "channel": ch["name"], "channel_id": ch["id"],
                "source": ch["source"], "count": len(rows),
                "messages": _shape(rows),
            },
            ensure_ascii=False,
        )
    finally:
        con.close()


def get_dm(handle_or_user: str, since: str = "", until: str = "", limit: int = 200) -> str:
    """A direct-message or group-DM conversation, oldest first within the
    window. Matches the DM's name (e.g. a username) or a participant id.
    DMs only ever come from the export or local sources: bots cannot read
    DMs, so a DM here holds only what those sources captured (the export is
    your side of the conversation only)."""
    con = _index_con()
    try:
        chans = con.execute(
            "SELECT * FROM channels WHERE type IN (1, 3) AND "
            "(id = ? OR name LIKE ? OR recipients LIKE ?) ORDER BY id",
            (str(handle_or_user), f"%{handle_or_user}%", f"%{handle_or_user}%"),
        ).fetchall()
        if not chans:
            return json.dumps({"error": f"no indexed DM matching {handle_or_user!r}"})
        marks = ",".join("?" * len(chans))
        where, args = [f"m.channel_id IN ({marks})"], [c["id"] for c in chans]
        _scope(where, args, since=since, until=until)
        sql = MSG_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY m.ts DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        rows = list(reversed(con.execute(sql, args).fetchall()))
        return json.dumps(
            {
                "dms": [
                    {"channel_id": c["id"], "name": c["name"], "source": c["source"],
                     "type": "group" if c["type"] == GROUP_DM else "dm"}
                    for c in chans
                ],
                "count": len(rows),
                "messages": _shape(rows),
            },
            ensure_ascii=False,
        )
    finally:
        con.close()


def list_guilds() -> str:
    """Every server the index knows, with which source covers it and how
    many messages are indexed per source."""
    con = _index_con()
    try:
        out = []
        for g in con.execute("SELECT * FROM guilds ORDER BY name").fetchall():
            per = {
                r["source"]: r["n"]
                for r in con.execute(
                    "SELECT source, COUNT(*) AS n FROM messages WHERE guild_id = ? "
                    "GROUP BY source",
                    (g["id"],),
                )
            }
            out.append(
                {
                    "id": g["id"], "name": g["name"], "source": g["source"],
                    "writable": g["source"] == "bot",
                    "channels": con.execute(
                        "SELECT COUNT(*) FROM channels WHERE guild_id = ?", (g["id"],)
                    ).fetchone()[0],
                    "messages": per,
                }
            )
        return json.dumps({"count": len(out), "guilds": out}, ensure_ascii=False)
    finally:
        con.close()


def list_channels(guild: str = "") -> str:
    """Channels for one server (id or name substring), or DMs when guild is
    empty. Each shows its source, message count and newest message."""
    con = _index_con()
    try:
        if guild:
            sql = (
                "SELECT c.*, g.name AS guild_name FROM channels c "
                "LEFT JOIN guilds g ON g.id = c.guild_id "
                "WHERE c.guild_id = ? OR g.name LIKE ? ORDER BY c.name"
            )
            args = (guild, f"%{guild}%")
        else:
            sql = (
                "SELECT c.*, NULL AS guild_name FROM channels c "
                "WHERE c.type IN (1, 3) ORDER BY c.name"
            )
            args = ()
        out = []
        for c in con.execute(sql, args).fetchall():
            st = con.execute(
                "SELECT COUNT(*) AS n, MAX(ts) AS newest FROM messages WHERE channel_id = ?",
                (c["id"],),
            ).fetchone()
            out.append(
                {
                    "id": c["id"], "name": c["name"], "type": c["type"],
                    "guild": c["guild_name"], "source": c["source"],
                    "writable": c["source"] == "bot",
                    "messages": st["n"], "newest": _ts(st["newest"]),
                }
            )
        return json.dumps({"count": len(out), "channels": out}, ensure_ascii=False)
    finally:
        con.close()


def index_status() -> str:
    """Per-source row counts and newest message, which sources are live, and
    whether the bot's Message Content intent is on. This is what makes a
    coverage gap visible instead of silent."""
    con = _index_con()
    try:
        sources = {}
        for s in ("bot", "export", "local"):
            r = con.execute(
                "SELECT COUNT(*) AS n, MAX(ts) AS newest, "
                "COUNT(DISTINCT channel_id) AS channels FROM messages WHERE source = ?",
                (s,),
            ).fetchone()
            sources[s] = {"messages": r["n"], "channels": r["channels"], "newest": _ts(r["newest"])}
        meta = {r["key"]: r["value"] for r in con.execute("SELECT * FROM meta")}
    finally:
        con.close()
    status = {
        "index_db": str(INDEX_DB),
        "changes_log": str(CHANGES_LOG),
        "sources": sources,
        "last_bot_sync": meta.get("last_bot_sync"),
        "last_export_import": meta.get("last_export_import"),
        "local_db": str(LOCAL_DB) if LOCAL_DB.exists() else "not installed (Phase 2, optional)",
    }
    bot = {"configured": False}
    try:
        _token()
        bot["configured"] = True
        me = _api("GET", "/users/@me") or {}
        bot["user"] = f'{me.get("username")} ({me.get("id")})'
        app = _api("GET", "/applications/@me") or {}
        flags = int(app.get("flags") or 0)
        on = bool(flags & (FLAG_MESSAGE_CONTENT | FLAG_MESSAGE_CONTENT_LIMITED))
        bot["message_content_intent"] = on
        if not on:
            bot["warning"] = (
                "Message Content intent is OFF: bot rows will index with empty "
                "text. Enable it in the Developer Portal > Bot > Privileged "
                "Gateway Intents, then run sync_bot(full=true)."
            )
        bot["guilds"] = [g.get("name") for g in _api("GET", "/users/@me/guilds") or []]
    except Exception as e:  # token missing, no network, bad token: say which
        bot["error"] = str(e)
    status["bot"] = bot
    return json.dumps(status, ensure_ascii=False)


def sync_bot(guild: str = "", full: bool = False, max_pages: int = 20) -> str:
    """Pull new messages from every server the bot is in (or one server by
    id/name) into the index. Incremental by snowflake; full=true re-reads
    from the beginning. Each channel fetches at most max_pages*100 messages
    per call and reports more=true if it stopped early - call again."""
    con = _index_con()
    try:
        guilds = _api("GET", "/users/@me/guilds") or []
        if guild:
            guilds = [g for g in guilds if g["id"] == guild or guild.lower() in g["name"].lower()]
            if not guilds:
                return json.dumps({"error": f"the bot is not in a server matching {guild!r}"})
        report = {"guilds": {}, "skipped": []}
        for g in guilds:
            gid = str(g["id"])
            _upsert_guild(con, gid, g.get("name"), "bot")
            try:
                chans = _api("GET", f"/guilds/{gid}/channels") or []
            except RuntimeError as e:
                report["skipped"].append({"guild": g.get("name"), "reason": str(e)})
                continue
            chans = [c for c in chans if c.get("type") in TEXT_TYPES]
            try:
                active = _api("GET", f"/guilds/{gid}/threads/active") or {}
                chans += [t for t in active.get("threads", []) if t.get("type") in THREAD_TYPES]
            except RuntimeError:
                pass
            per = {}
            for c in chans:
                _upsert_channel(con, c["id"], gid, c.get("name"), c.get("type"), "bot")
                if full:
                    con.execute("UPDATE channels SET last_synced_id = 0 WHERE id = ?", (str(c["id"]),))
                try:
                    per[c.get("name") or c["id"]] = _sync_channel(con, c, gid, max_pages)
                except RuntimeError as e:
                    report["skipped"].append({"channel": c.get("name"), "reason": str(e)})
            report["guilds"][g.get("name") or gid] = per
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('last_bot_sync', ?)",
            (datetime.datetime.now().astimezone().isoformat(timespec="seconds"),),
        )
        con.commit()
        fetched = sum(v["fetched"] for per in report["guilds"].values() for v in per.values())
        empty = sum(v["empty_content"] for per in report["guilds"].values() for v in per.values())
        report["fetched"] = fetched
        if fetched >= 20 and empty / fetched > 0.9:
            report["warning"] = (
                "almost every fetched message has empty text: the Message Content "
                "intent is probably off. Check index_status()."
            )
        return json.dumps(report, ensure_ascii=False)
    finally:
        con.close()


def import_export(zip_path: str) -> str:
    """Load Discord's official data package (the ZIP from Settings > Data &
    Privacy > Request my data) into the index as source=export. It holds
    only messages you sent, in every server and DM. Re-importing the same
    ZIP is a no-op; it never overwrites rows the bot already saw."""
    path = Path(zip_path).expanduser()
    if not path.is_file():
        return json.dumps({"error": f"no such file: {path}"})
    con = _index_con()
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            me = {}
            for n in names:
                if n.endswith("account/user.json"):
                    me = json.loads(zf.read(n).decode("utf-8"))
                    break
            my_id = str(me.get("id") or "")
            my_name = me.get("global_name") or me.get("username") or "me"
            index = {}
            for n in names:
                if n.endswith("messages/index.json"):
                    index = json.loads(zf.read(n).decode("utf-8"))
                    break
            report = {"account": my_name, "channels": {}, "inserted": 0, "kept": 0}
            for chan_dir, cj in _export_entries(zf):
                ch = json.loads(zf.read(cj).decode("utf-8"))
                cid = str(ch["id"])
                ctype = ch.get("type")
                guild = ch.get("guild") or {}
                name = ch.get("name") or index.get(cid) or cid
                if guild.get("id"):
                    _upsert_guild(con, guild["id"], guild.get("name"), "export")
                _upsert_channel(
                    con, cid, guild.get("id"), name, ctype, "export",
                    recipients=[str(r) for r in ch.get("recipients") or []] or None,
                )
                n_ins = n_kept = 0
                for r in _export_messages(zf, chan_dir):
                    mid = r.get("ID") or r.get("id")
                    if not mid:
                        continue
                    att = [
                        {"name": a.rsplit("/", 1)[-1], "url": a}
                        for a in str(r.get("Attachments") or "").split()
                    ]
                    stamp = r.get("Timestamp") or r.get("timestamp")
                    res = _upsert_message(
                        con,
                        {
                            "id": mid, "source": "export",
                            "guild_id": guild.get("id"), "channel_id": cid,
                            "author_id": my_id, "author_name": my_name,
                            "content": r.get("Contents") or r.get("contents") or "",
                            "ts": _ms(stamp) if stamp else _snowflake_ms(mid),
                            "attachments": att,
                        },
                    )
                    if res == "kept":
                        n_kept += 1
                    else:
                        n_ins += 1
                report["channels"][name] = {"messages": n_ins + n_kept}
                report["inserted"] += n_ins
                report["kept"] += n_kept
            con.execute(
                "INSERT OR REPLACE INTO meta VALUES ('last_export_import', ?)",
                (datetime.datetime.now().astimezone().isoformat(timespec="seconds"),),
            )
            con.commit()
        return json.dumps(report, ensure_ascii=False)
    finally:
        con.close()


def sync_local() -> str:
    """Pull changes from the optional discord-local-log capture database
    (source=local). Read-only; the capture side is a separate project
    (github.com/ryan-grey/discord-local-log). Its tables: channels(id,
    guild_id, guild_name, name, type, recipients) and messages(id, guild_id,
    channel_id, author_id, author_name, content, ts, edited_ts, attachments,
    deleted, rev). `rev` bumps on every insert, edit or delete, so this pulls
    edits and deletions too, not just new ids."""
    if not LOCAL_DB.exists():
        return json.dumps({"error": f"no local capture db at {LOCAL_DB}"})
    import sqlite3

    src = sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    con = _index_con()
    try:
        last = int(
            (con.execute("SELECT value FROM meta WHERE key = 'last_local_rev'").fetchone() or [0])[0]
        )
        for c in src.execute("SELECT * FROM channels"):
            if c["guild_id"]:
                _upsert_guild(con, c["guild_id"], c["guild_name"], "local")
            _upsert_channel(
                con, c["id"], c["guild_id"], c["name"], c["type"], "local",
                recipients=json.loads(c["recipients"]) if c["recipients"] else None,
            )
        n = 0
        newest = last
        for r in src.execute("SELECT * FROM messages WHERE rev > ? ORDER BY rev", (last,)):
            m = dict(r)
            m["source"] = "local"
            m["attachments"] = json.loads(m.get("attachments") or "[]")
            _upsert_message(con, m)
            newest = max(newest, int(m["rev"]))
            n += 1
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_local_rev', ?)", (str(newest),))
        con.commit()
        return json.dumps({"imported": n, "last_local_rev": newest})
    finally:
        src.close()
        con.close()


# ------------------------------------------------------------------ writes --


def _log_change(action, channel_id, message_id, before, after, result):
    CHANGES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CHANGES_LOG, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "action": action, "channel_id": str(channel_id),
                    "message_id": str(message_id) if message_id else None,
                    "before": before, "after": after, "result": result,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def _refuse_unless_bot(con, channel_id):
    """The one rule that matters: writes only where the bot can act.
    Returns an error payload, or None when the channel is bot-visible."""
    ch = _channel_row(con, channel_id)
    if ch is not None and ch["source"] != "bot":
        return {
            "error": (
                f"refused: channel {ch['name']!r} ({ch['id']}) is known only from "
                f"the {ch['source']} source, so the bot cannot act there. Discord "
                "bots can only send, edit or delete in servers they have been "
                "invited to, and there is no permitted way to act as your own "
                "account. Send it yourself from the Discord app."
            )
        }
    return None


def _confirm_gate(confirm, what):
    if confirm is not True:
        return json.dumps(
            {
                "error": f"refused: pass confirm=true to actually {what}. Show the "
                "user the exact channel and text first."
            }
        )
    return None


def send_message(channel_id: str, text: str, confirm: bool = False) -> str:
    """Send a message AS THE BOT to a channel the bot is in. Requires
    confirm=true. Refuses DMs and any channel the bot cannot see; there is
    no way to send as you. Logged to discord-changes.log."""
    gate = _confirm_gate(confirm, "send")
    if gate:
        return gate
    if not text or not text.strip():
        return json.dumps({"error": "text is empty"})
    con = _index_con()
    try:
        refusal = _refuse_unless_bot(con, channel_id)
        if refusal:
            return json.dumps(refusal)
        ch = _channel_row(con, channel_id)
        cid = ch["id"] if ch else str(channel_id)
        try:
            m = _api("POST", f"/channels/{cid}/messages", {"content": text})
        except RuntimeError as e:
            _log_change("send", cid, None, None, text, str(e))
            return json.dumps({"sent": False, "error": str(e)})
        gid = ch["guild_id"] if ch else None
        _upsert_message(con, _bot_message(m, gid))
        con.commit()
        _log_change("send", cid, m["id"], None, text, "ok")
        return json.dumps(
            {"sent": True, "message_id": str(m["id"]), "channel_id": cid,
             "time": _ts(_ms(m["timestamp"])) if m.get("timestamp") else None}
        )
    finally:
        con.close()


def edit_message(channel_id: str, message_id: str, text: str, confirm: bool = False) -> str:
    """Edit one of the bot's own messages. Requires confirm=true. Discord
    only lets a bot edit messages it sent; anything else is refused by
    Discord and reported here. Before/after go to discord-changes.log."""
    gate = _confirm_gate(confirm, "edit")
    if gate:
        return gate
    if not text or not text.strip():
        return json.dumps({"error": "text is empty"})
    con = _index_con()
    try:
        refusal = _refuse_unless_bot(con, channel_id)
        if refusal:
            return json.dumps(refusal)
        ch = _channel_row(con, channel_id)
        cid = ch["id"] if ch else str(channel_id)
        old = con.execute("SELECT content FROM messages WHERE id = ?", (int(message_id),)).fetchone()
        before = old["content"] if old else None
        try:
            m = _api("PATCH", f"/channels/{cid}/messages/{message_id}", {"content": text})
        except RuntimeError as e:
            _log_change("edit", cid, message_id, before, text, str(e))
            return json.dumps({"edited": False, "error": str(e)})
        _upsert_message(con, _bot_message(m, ch["guild_id"] if ch else None))
        con.commit()
        _log_change("edit", cid, message_id, before, text, "ok")
        return json.dumps({"edited": True, "message_id": str(message_id), "before": before, "after": text})
    finally:
        con.close()


def delete_message(channel_id: str, message_id: str, confirm: bool = False) -> str:
    """Delete a message as the bot (its own, or others' where the bot holds
    Manage Messages). Requires confirm=true. The row stays in the index
    flagged deleted=true; the text is preserved in discord-changes.log."""
    gate = _confirm_gate(confirm, "delete")
    if gate:
        return gate
    con = _index_con()
    try:
        refusal = _refuse_unless_bot(con, channel_id)
        if refusal:
            return json.dumps(refusal)
        ch = _channel_row(con, channel_id)
        cid = ch["id"] if ch else str(channel_id)
        old = con.execute(
            "SELECT content, author_name FROM messages WHERE id = ?", (int(message_id),)
        ).fetchone()
        before = {"author": old["author_name"], "content": old["content"]} if old else None
        try:
            _api("DELETE", f"/channels/{cid}/messages/{message_id}")
        except RuntimeError as e:
            _log_change("delete", cid, message_id, before, None, str(e))
            return json.dumps({"deleted": False, "error": str(e)})
        con.execute("UPDATE messages SET deleted = 1 WHERE id = ?", (int(message_id),))
        con.commit()
        _log_change("delete", cid, message_id, before, None, "ok")
        return json.dumps({"deleted": True, "message_id": str(message_id), "before": before})
    finally:
        con.close()


# Registered here rather than via decorators so the functions above stay
# plain callables for the test suite.
for _fn in (
    search_messages,
    get_channel,
    get_dm,
    list_guilds,
    list_channels,
    index_status,
    sync_bot,
    import_export,
    sync_local,
    send_message,
    edit_message,
    delete_message,
):
    mcp.tool()(_fn)


if __name__ == "__main__":
    mcp.run()
