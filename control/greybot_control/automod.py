"""Explicit silent moderation rules; only fresh Gateway messages are eligible."""
from collections import defaultdict, deque
import json
import time

from .discord_api import Denied
from .store import digest


class Detector:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.started = time.time()
        self.recent = defaultdict(deque)

    def receive(self, packet):
        if not self.cfg.enforce or not self.cfg.capture_content:
            return
        if packet.get("op") != 0 or packet.get("t") != "MESSAGE_CREATE":
            return
        data = packet.get("d") or {}
        author = data.get("author") or {}
        if (str(data.get("guild_id")) != self.cfg.guild_id or author.get("bot")
                or data.get("webhook_id") or data.get("type", 0) not in {0, 19}):
            return
        if not self.store.settings(self.cfg.guild_id)["values"].get("moderation_enabled"):
            self.recent.clear()
            return
        mid, user, channel = (str(data.get("id", "")), str(author.get("id", "")), str(data.get("channel_id", "")))
        if not all(v.isascii() and v.isdecimal() for v in (mid, user, channel)):
            return
        occurred = ((int(mid) >> 22) + 1420070400000) / 1000
        now = time.time()
        if occurred < self.started or not 0 <= now - occurred <= 30:
            return  # Never moderate imported history or reconnect backlog.
        for key in list(self.recent):
            while self.recent[key] and self.recent[key][0][0] < now - 30:
                self.recent[key].popleft()
            if not self.recent[key]:
                del self.recent[key]
        recent = self.recent[user]
        if any(item[1] == mid for item in recent):
            return
        content = data.get("content") or ""
        fingerprint = digest(content) if content else None
        recent.append((occurred, mid, fingerprint))
        window = [item for item in recent if occurred - 5 <= item[0] <= occurred]
        spam = len(window) >= 4
        repeated = fingerprint is not None and sum(item[2] == fingerprint for item in window) >= 3
        if not spam and not repeated:
            return
        self.store.queue("automod-" + mid, self.cfg.guild_id, self.cfg.client_id, "automod", user,
                         {"reason": "Automatic spam moderation" if spam else "Repeated text moderation",
                          "message_id": mid, "channel_id": channel, "occurred": occurred,
                          "spam": spam, "repeated_text": repeated})


async def execute(cfg, store, api, job):
    body = json.loads(job["body"])
    if not store.settings(cfg.guild_id)["values"].get("moderation_enabled"):
        raise Denied("Automatic moderation is disabled")
    if not 0 <= time.time() - body["occurred"] <= 60:
        raise Denied("Expired moderation candidate")
    # Caller rechecks live bot permissions, owner/admin immunity and hierarchy.
    # No POST message endpoint is used: there are no warning messages or DMs.
    await api.request("DELETE", f"/channels/{body['channel_id']}/messages/{body['message_id']}",
                      reason="greyBot: " + body["reason"])
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        store._append(db, "automod-deleted:" + job["id"], cfg.guild_id, "AUTOMOD_MESSAGE_DELETED", job["subject"],
                      {"actor": cfg.client_id, "channel_id": body["channel_id"], "message_id": body["message_id"],
                       "spam": body["spam"], "repeated_text": body["repeated_text"]})
        if not body["spam"]:
            return  # Repeated text alone never earns an infraction.
        event = store._append(db, "infraction:" + job["id"], cfg.guild_id, "AUTOMOD_INFRACTION", job["subject"],
                             {"actor": cfg.client_id, "channel_id": body["channel_id"], "message_id": body["message_id"],
                              "occurred": body["occurred"], "reason": "Spam", "warning_sent": False})
        last_release = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE guild=? AND subject=? AND kind='MUTE_RELEASED'",
                                  (cfg.guild_id, job["subject"])).fetchone()[0]
        count = db.execute("SELECT COUNT(*) FROM events WHERE guild=? AND subject=? AND kind='AUTOMOD_INFRACTION' "
                           "AND seq>? AND CAST(json_extract(payload,'$.occurred') AS REAL)>=?",
                           (cfg.guild_id, job["subject"], last_release, body["occurred"] - 1800)).fetchone()[0]
        if count >= 3:
            mute_id = "automute-" + body["message_id"]
            mute_body = {"reason": "Three spam infractions within 30 minutes", "minutes": 10, "automatic": True,
                         "trigger_seq": event["seq"], "occurred": body["occurred"]}
            from .store import canonical
            db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                       (mute_id, cfg.guild_id, cfg.client_id, "mute", job["subject"], canonical(mute_body), "queued", time.time()))
            store._append(db, "requested:" + mute_id, cfg.guild_id, "ACTION_REQUESTED", job["subject"],
                          {"actor": cfg.client_id, "action": "mute", "request": mute_id, "infraction_count": count,
                           "reason": mute_body["reason"], "automatic": True})
