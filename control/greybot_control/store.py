"""Private SQLite index and append-only event journal.

SQLite guards catch application mistakes, not a hostile filesystem owner.
Independent S3 locked versions provide the retention boundary.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from .audit_feed import default_feed


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


DEFAULT_SETTINGS = {"welcome_channel": "", "welcome_enabled": False,
                    "verification_enabled": False, "verification_role": "",
                    "goodbye_enabled": False, "goodbye_channel": "",
                    "self_roles": [], "moderation_enabled": False, **default_feed()}


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS feed_cursor(guild TEXT PRIMARY KEY, seq INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS feed_delivery(guild TEXT NOT NULL, seq INTEGER NOT NULL,
                    channel TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL, PRIMARY KEY(guild,seq,channel));
                CREATE TABLE IF NOT EXISTS display_directory(
                    guild TEXT PRIMARY KEY, body TEXT NOT NULL, checked REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS persistent_mutes(
                    guild TEXT NOT NULL, subject TEXT NOT NULL, id TEXT NOT NULL,
                    actor TEXT NOT NULL, state TEXT NOT NULL, plan TEXT NOT NULL,
                    reason TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(guild,subject));
                CREATE TABLE IF NOT EXISTS member_profiles(
                    guild TEXT NOT NULL, user TEXT NOT NULL, body TEXT NOT NULL,
                    checked REAL NOT NULL, PRIMARY KEY(guild,user));
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL, guild TEXT NOT NULL,
                    kind TEXT NOT NULL, subject TEXT NOT NULL,
                    observed REAL NOT NULL, payload TEXT NOT NULL,
                    previous TEXT NOT NULL, hash TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_filter ON events(guild,kind,subject,seq);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'event journal is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'event journal is append-only'); END;
                CREATE TABLE IF NOT EXISTS receipts(
                    seq INTEGER PRIMARY KEY, object_key TEXT NOT NULL,
                    version TEXT NOT NULL, archived REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(
                    token TEXT PRIMARY KEY, user TEXT NOT NULL,
                    csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_states(
                    token TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(
                    guild TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, guild TEXT NOT NULL, actor TEXT NOT NULL,
                    kind TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY, guild TEXT NOT NULL, channel TEXT NOT NULL,
                    author TEXT NOT NULL, content TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0, observed REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS event_details(
                    event_id TEXT PRIMARY KEY, guild TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS details_no_update BEFORE UPDATE ON event_details
                    BEGIN SELECT RAISE(ABORT,'event details are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS details_no_delete BEFORE DELETE ON event_details
                    BEGIN SELECT RAISE(ABORT,'event details are append-only'); END;
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit_seen(self, guild, event_id):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM events WHERE guild=? AND kind='GUILD_AUDIT_LOG_ENTRY_CREATE' AND json_extract(payload,'$.id')=? LIMIT 1",
                              (guild, event_id)).fetchone() is not None

    def profile(self, guild, user):
        with self.connection() as db:
            row = db.execute("SELECT body,checked FROM member_profiles WHERE guild=? AND user=?", (guild, user)).fetchone()
            return {"value": json.loads(row["body"]), "checked": row["checked"]} if row else None

    def save_profile(self, guild, user, value):
        with self.connection() as db:
            db.execute("INSERT INTO member_profiles VALUES(?,?,?,?) ON CONFLICT(guild,user) DO UPDATE SET body=excluded.body,checked=excluded.checked",
                       (guild, user, canonical(value), time.time()))

    def _append(self, db, event_id, guild, kind, subject, payload):
        row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row:
            return dict(row)
        last = db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = last[0] if last else "0" * 64
        observed = time.time()
        body = canonical(payload)
        hashed = digest(canonical([event_id, guild, kind, subject, observed, body, previous]))
        db.execute("INSERT INTO events(event_id,guild,kind,subject,observed,payload,previous,hash) "
                   "VALUES(?,?,?,?,?,?,?,?)", (event_id, guild, kind, subject, observed, body, previous, hashed))
        return dict(db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone())

    def append(self, event_id, guild, kind, subject, payload):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._append(db, event_id, guild, kind, subject, payload)

    def seen(self, event_id):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone() is not None

    def record_event(self, event_id, guild, kind, subject, payload, messages, details=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                return
            if details:
                body = canonical(details)
                payload = {**payload, "detail_digest": digest(body)}
                db.execute("INSERT INTO event_details VALUES(?,?,?)", (event_id, guild, body))
            self._append(db, event_id, guild, kind, subject, payload)
            for message in messages:
                self._index_message(db, guild, *message)

    def details(self, row):
        expected = json.loads(row["payload"]).get("detail_digest")
        if not expected:
            return {}
        with self.connection() as db:
            saved = db.execute("SELECT body FROM event_details WHERE guild=? AND event_id=?", (row["guild"], row["event_id"])).fetchone()
        if not saved or digest(saved[0]) != expected:
            return {"details_unavailable": True}
        return json.loads(saved[0])

    def events(self, guild, *, kind="", subject="", before=0, limit=100):
        clauses, args = ["guild=?"], [guild]
        for field, val in (("kind", kind), ("subject", subject)):
            if val:
                clauses.append(field + "=?")
                args.append(val)
        if before:
            clauses.append("seq<?")
            args.append(before)
        args.append(min(max(limit, 1), 200))
        with self.connection() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM events WHERE " + " AND ".join(clauses) + " ORDER BY seq DESC LIMIT ?", args)]

    def pending(self, limit=100):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT e.* FROM events e LEFT JOIN receipts r "
                    "ON e.seq=r.seq WHERE r.seq IS NULL ORDER BY e.seq LIMIT ?", (limit,))]

    def receipt(self, seq, key, version):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?,?)", (seq, key, version, time.time()))

    def verify(self):
        previous = "0" * 64
        with self.connection() as db:
            for row in db.execute("SELECT * FROM events ORDER BY seq"):
                value = [row[k] for k in ("event_id", "guild", "kind", "subject", "observed", "payload", "previous")]
                if row["previous"] != previous or digest(canonical(value)) != row["hash"]:
                    return False
                previous = row["hash"]
        return True

    def oauth_state(self, purpose="admin"):
        if purpose not in {"admin", "verify", "channels", "raids"}:
            raise ValueError("Invalid login purpose")
        state = purpose + "." + secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("DELETE FROM oauth_states WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO oauth_states VALUES(?,?)", (digest(state), time.time() + 600))
        return state

    def consume_state(self, state):
        with self.connection() as db:
            row = db.execute("DELETE FROM oauth_states WHERE token=? AND expires>? RETURNING token",
                             (digest(state), time.time())).fetchone()
        return bool(row)

    def session(self, user, ttl=28800):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest(token), user, csrf, time.time() + ttl))
        return token

    def get_session(self, token):
        with self.connection() as db:
            row = db.execute("SELECT user,csrf FROM sessions WHERE token=? AND expires>?",
                             (digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (digest(token),))

    def settings(self, guild):
        with self.connection() as db:
            row = db.execute("SELECT revision,body FROM settings WHERE guild=?", (guild,)).fetchone()
        return {"revision": row["revision"] if row else 0,
                "values": {**DEFAULT_SETTINGS, **(json.loads(row["body"]) if row else {})}}

    def save_settings(self, guild, actor, revision, values):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision,body FROM settings WHERE guild=?", (guild,)).fetchone()
            if (current[0] if current else 0) != revision:
                raise ValueError("Settings changed; reload before saving")
            previous = json.loads(current[1]) if current else {}
            if any(values.get(enabled) and (not previous.get(enabled) or previous.get(channel) != values.get(channel)) for enabled, channel in (("audit_feed_enabled", "audit_channel"), ("goodbye_enabled", "goodbye_channel"), ("welcome_enabled", "welcome_channel"))):
                db.execute("INSERT INTO feed_cursor VALUES(?,(SELECT COALESCE(MAX(seq),0) FROM events)) ON CONFLICT(guild) DO UPDATE SET seq=excluded.seq", (guild,))
            db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(guild) DO UPDATE SET "
                       "revision=excluded.revision,body=excluded.body", (guild, revision + 1, canonical(values)))
            self._append(db, "settings:" + secrets.token_hex(16), guild, "SETTINGS_CHANGED", actor,
                         {"actor": actor, "revision": revision + 1, "values": values})

    def queue(self, job_id, guild, actor, kind, subject, body):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if existing:
                if any(existing[k] != v for k, v in {"guild": guild, "actor": actor,
                        "kind": kind, "subject": subject, "body": canonical(body)}.items()):
                    raise ValueError("Request ID already used for a different action")
                return dict(existing)
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)", (job_id, guild, actor, kind,
                       subject, canonical(body), "queued", time.time()))
            self._append(db, "requested:" + job_id, guild, "ACTION_REQUESTED", subject,
                         {"actor": actor, "action": kind, "request": job_id, **body})
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def claim_job(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='executing' WHERE id=?", (row["id"],))
                return dict(row)

    def finish_job(self, job, state):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job["id"]))
            self._append(db, "result:" + job["id"], job["guild"], "ACTION_RESULT", job["subject"],
                         {"actor": job["actor"], "action": job["kind"], "request": job["id"], "state": state})

    def jobs(self, guild):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs WHERE guild=? ORDER BY created DESC LIMIT 100", (guild,))]

    def message(self, guild, message_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM messages WHERE guild=? AND id=?", (guild, message_id)).fetchone()
        return dict(row) if row else None

    def index_message(self, guild, message_id, channel, author, content, deleted=False):
        with self.connection() as db:
            self._index_message(db, guild, message_id, channel, author, content, deleted)

    def _index_message(self, db, guild, message_id, channel, author, content, deleted=False):
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "content=excluded.content,deleted=excluded.deleted,observed=excluded.observed",
                   (message_id, guild, channel, author, content, int(deleted), time.time()))

    def search(self, guild, query, channel="", author="", limit=100):
        clauses, args = ["guild=?", "instr(lower(content), lower(?))>0"], [guild, query]
        for field, value in (("channel", channel), ("author", author)):
            if value:
                clauses.append(field + "=?")
                args.append(value)
        args.append(max(1, min(limit, 200)))
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM messages WHERE " + " AND ".join(clauses)
                    + " ORDER BY observed DESC LIMIT ?", args)]
