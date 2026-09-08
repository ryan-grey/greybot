#!/usr/bin/env python3
"""Fold the plugin's events.jsonl into discord.db.

The Vencord plugin appends one JSON object per line to

    ~/Library/Application Support/discord-local-log/events.jsonl

and this script - run by launchd whenever that file changes, or by hand -
applies those events to

    ~/Library/Application Support/discord-local-log/discord.db

which discord-mcp reads as its `local` source. Stdlib only, no network.

Schema (the contract discord-mcp's sync_local depends on):

    channels(id, guild_id, guild_name, name, type, recipients)
    messages(id, guild_id, channel_id, author_id, author_name, content,
             ts, edited_ts, attachments, deleted, rev)

`rev` is a change counter: every insert, update or delete bumps it, so a
reader can pull "everything changed since rev N" and see edits and deletes,
not just new ids. Timestamps are unix milliseconds UTC.

The file is read from a saved byte offset; after a successful pass the
processed file is rotated away, and any lines the client appended during the
pass are picked up from the rotated file before it is removed.
"""

import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

STATE_DIR = Path(
    os.environ.get(
        "DISCORD_LOCAL_LOG_DIR",
        Path.home() / "Library/Application Support/discord-local-log",
    )
)
EVENTS = STATE_DIR / "events.jsonl"
DB = STATE_DIR / "discord.db"
ROTATE_AT = 32 * 1024 * 1024  # bytes; rotate once the log is this big

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels(
    id TEXT PRIMARY KEY, guild_id TEXT, guild_name TEXT, name TEXT,
    type INTEGER, recipients TEXT);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY, guild_id TEXT, channel_id TEXT, author_id TEXT,
    author_name TEXT, content TEXT, ts INTEGER, edited_ts INTEGER,
    attachments TEXT, deleted INTEGER DEFAULT 0, rev INTEGER);
CREATE INDEX IF NOT EXISTS messages_rev ON messages(rev);
CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel_id, ts);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def _ms(iso):
    if not iso:
        return None
    s = str(iso).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _snowflake_ms(snowflake):
    return (int(snowflake) >> 22) + 1420070400000


def _open(db=DB):
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _meta(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def _set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))


class Ingester:
    def __init__(self, con):
        self.con = con
        self.rev = int(_meta(con, "rev", 0))
        self.applied = {"message": 0, "update": 0, "delete": 0, "bad": 0}

    def _next_rev(self):
        self.rev += 1
        return self.rev

    def channel(self, ch):
        if not ch or not ch.get("id"):
            return
        self.con.execute(
            """INSERT INTO channels(id, guild_id, guild_name, name, type, recipients)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 guild_id = COALESCE(excluded.guild_id, channels.guild_id),
                 guild_name = COALESCE(excluded.guild_name, channels.guild_name),
                 name = COALESCE(excluded.name, channels.name),
                 type = COALESCE(excluded.type, channels.type),
                 recipients = COALESCE(excluded.recipients, channels.recipients)""",
            (
                str(ch["id"]), ch.get("guild_id"), ch.get("guild_name"),
                ch.get("name"), ch.get("type"),
                json.dumps(ch["recipients"]) if ch.get("recipients") else None,
            ),
        )

    def message(self, ev):
        m = ev.get("message") or {}
        ch = ev.get("channel") or {}
        if not m.get("id"):
            self.applied["bad"] += 1
            return
        self.channel(ch)
        mid = int(m["id"])
        if ev.get("op") == "update":
            # A partial payload: only touch what it carries.
            sets, args = ["rev = ?"], [self._next_rev()]
            if m.get("content") is not None:
                sets.append("content = ?")
                args.append(m["content"])
            if m.get("edited_ts"):
                sets.append("edited_ts = ?")
                args.append(_ms(m["edited_ts"]))
            if m.get("attachments"):
                sets.append("attachments = ?")
                args.append(json.dumps(m["attachments"]))
            args.append(mid)
            cur = self.con.execute(
                f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", args
            )
            if cur.rowcount:
                self.applied["update"] += 1
                return
            # Never seen it: fall through and insert what we have.
        self.con.execute(
            """INSERT INTO messages(id, guild_id, channel_id, author_id, author_name,
                                    content, ts, edited_ts, attachments, deleted, rev)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(id) DO UPDATE SET
                 guild_id = COALESCE(excluded.guild_id, messages.guild_id),
                 author_id = COALESCE(excluded.author_id, messages.author_id),
                 author_name = COALESCE(excluded.author_name, messages.author_name),
                 content = COALESCE(excluded.content, messages.content),
                 ts = COALESCE(excluded.ts, messages.ts),
                 edited_ts = COALESCE(excluded.edited_ts, messages.edited_ts),
                 attachments = excluded.attachments,
                 rev = excluded.rev""",
            (
                mid,
                ch.get("guild_id"),
                str(m.get("channel_id") or ch.get("id")),
                m.get("author_id"),
                m.get("author_name"),
                m.get("content"),
                _ms(m.get("ts")) or _snowflake_ms(mid),
                _ms(m.get("edited_ts")),
                json.dumps(m.get("attachments") or []),
                self._next_rev(),
            ),
        )
        self.applied["message"] += 1

    def delete(self, ev):
        ids = ev.get("ids") or ([ev["id"]] if ev.get("id") else [])
        for mid in ids:
            cur = self.con.execute(
                "UPDATE messages SET deleted = 1, rev = ? WHERE id = ?",
                (self._next_rev(), int(mid)),
            )
            if not cur.rowcount:
                # Deleted before we ever saw it: keep a tombstone so the id
                # is known, with the channel at least.
                self.con.execute(
                    """INSERT OR IGNORE INTO messages(id, channel_id, ts, attachments,
                                                      deleted, rev)
                       VALUES (?, ?, ?, '[]', 1, ?)""",
                    (int(mid), str(ev.get("channel_id")), _snowflake_ms(mid), self.rev),
                )
            self.applied["delete"] += 1

    def line(self, raw):
        raw = raw.strip()
        if not raw:
            return
        try:
            ev = json.loads(raw)
        except ValueError:
            self.applied["bad"] += 1
            return
        kind = ev.get("kind")
        if kind == "message":
            self.message(ev)
        elif kind == "delete":
            self.delete(ev)
        else:
            self.applied["bad"] += 1

    def finish(self):
        _set_meta(self.con, "rev", self.rev)


def _apply_file(con, path, offset):
    """Apply lines from `offset` to end of file. Returns the new offset;
    a trailing partial line (client mid-write) is left for next time."""
    ing = Ingester(con)
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end == -1:
        return offset, ing.applied
    for raw in data[: end + 1].decode("utf-8", "replace").split("\n"):
        ing.line(raw)
    ing.finish()
    con.commit()
    return offset + end + 1, ing.applied


def ingest(events=EVENTS, db=DB, rotate_at=ROTATE_AT):
    """One pass. Safe to run any time, including while Discord is writing."""
    con = _open(db)
    try:
        if not events.exists():
            return {"applied": {}, "note": "no events file yet"}
        offset = int(_meta(con, "offset", 0))
        if offset > events.stat().st_size:
            offset = 0  # file was replaced under us
        offset, applied = _apply_file(con, events, offset)
        _set_meta(con, "offset", offset)
        con.commit()
        if events.stat().st_size >= rotate_at:
            rotated = events.with_name(
                f"events.{datetime.datetime.now():%Y%m%d-%H%M%S}.jsonl"
            )
            os.rename(events, rotated)
            # Anything appended between the read and the rename is in the
            # rotated file past our offset; drain it, then drop the file.
            _, late = _apply_file(con, rotated, offset)
            for k, v in late.items():
                applied[k] = applied.get(k, 0) + v
            os.remove(rotated)
            _set_meta(con, "offset", 0)
            con.commit()
        return {"applied": applied, "offset": offset, "rev": int(_meta(con, "rev", 0))}
    finally:
        con.close()


if __name__ == "__main__":
    print(json.dumps(ingest()))
    sys.exit(0)

