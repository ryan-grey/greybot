"""Filtered, future-only Discord feed; private history is never filtered."""
import json
import re
from datetime import datetime, timezone

from .audit_feed import AUDIT_EVENTS
from .discord_api import Denied, Unavailable
from .directory import Directory
from .mutes import effective_permissions
from .store import DEFAULT_SETTINGS


def classify(row):
    p = json.loads(row["payload"])
    if p.get("source") == "Discord audit history":
        return []
    kind = row["kind"]
    simple = {"GUILD_MEMBER_ADD": ["member_joined"], "GUILD_MEMBER_REMOVE": ["member_left"],
              "GUILD_BAN_ADD": ["member_banned", "moderation_ban"],
              "GUILD_BAN_REMOVE": ["member_unbanned", "moderation_unban"],
              "GUILD_EMOJIS_UPDATE": ["emojis_updated"],
              "MESSAGE_DELETE": ["message_deleted"], "MESSAGE_DELETE_BULK": ["message_deleted"],
              "MUTE_APPLIED": ["member_muted"], "MUTE_RELEASED": ["member_unmuted"]}
    if kind == "MESSAGE_UPDATE":
        return ["message_updated"] if p.get("text_edited") else []
    if kind == "MESSAGE_CREATE":
        return ["invite_posted"] if p.get("contains_invite") else []
    if kind == "GUILD_MEMBER_UPDATE":
        return ["user_updated"] if p.get("profile_changed") else []
    if kind == "VOICE_STATE_UPDATE":
        return p.get("feed_voice_changes", [])
    if kind == "GUILD_AUDIT_LOG_ENTRY_CREATE":
        management = {1: "server_edited", 10: "channel_created", 11: "channel_updated", 12: "channel_deleted",
                      13: "channel_updated", 14: "channel_updated", 15: "channel_updated",
                      30: "role_created", 31: "role_updated", 32: "role_deleted"}
        if p.get("action_type") in management:
            return [management[p["action_type"]]]
        if p.get("action_type") == 25:
            return ["member_roles_changed"]
        if p.get("action_type") == 24:
            keys = ["nickname_changed"] if "nick" in p.get("changed_fields", []) else []
            if "timeout_active" in p:
                keys.append("member_muted" if p["timeout_active"] else "member_unmuted")
            return keys
    return simple.get(kind, [])


def safe_label(value, limit=160):
    # Names remain text, never mentions, Markdown links or arbitrary URLs.
    return re.sub(r"([\\`*_{}\[\]()<>#@|~])", r"\\\1", str(value).replace("\n", " ").replace("\r", " "))[:limit]


def change_fields(payload, maps):
    """Bounded Discord fields with explicit additions, removals, and old/new values."""
    import discord
    fields, budget = [], 4400

    def add(name, value):
        nonlocal budget
        if budget < 100 or len(fields) >= 24:
            return
        value = str(value)
        limit = min(1000, budget - len(name))
        if len(value) > limit:
            value = value[:limit - 40] + "… See full history in the admin site."
        fields.append({"name": name, "value": value or "None", "inline": False})
        budget -= len(name) + len(value)

    def permissions(value):
        bits = int(value or 0)
        labels = [name.replace("_", " ").capitalize() for name, enabled in discord.Permissions(bits) if enabled]
        known = discord.Permissions.all().value
        if bits & ~known:
            labels.append("Additional Discord permissions")
        return ", ".join(labels) or "None"

    def value(key, raw):
        if raw is None:
            return "None"
        if isinstance(raw, bool):
            return "Yes" if raw else "No"
        if key in {"permissions", "allow", "deny"}:
            return permissions(raw)
        if key == "color":
            return f"#{int(raw):06X}"
        if key in {"parent_id", "afk_channel_id", "system_channel_id", "rules_channel_id"}:
            return "#" + safe_label(maps["channels"].get(str(raw), {}).get("name", "Deleted or unavailable channel"))
        if key == "communication_disabled_until":
            try:
                return f"<t:{int(datetime.fromisoformat(str(raw).replace('Z', '+00:00')).timestamp())}:F>"
            except (ValueError, OverflowError):
                return "Unavailable time"
        if key == "type":
            return str(discord.ChannelType(int(raw))).replace("_", " ").capitalize()
        return safe_label(raw, 900)

    for key, label in (("roles_added", "Added roles"), ("roles_removed", "Removed roles")):
        if payload.get(key):
            add(label, "\n".join(safe_label(role.get("name") or maps["roles"].get(str(role["id"]), {}).get("name", "Deleted or unavailable role"))
                                 for role in payload[key]))
    for change in payload.get("changes", []):
        key = change["field"]
        label = {"nick": "Nickname", "hoist": "Display separately", "rate_limit_per_user": "Slow mode (seconds)",
                 "communication_disabled_until": "Timeout until", "allow": "Allowed permissions", "deny": "Denied permissions",
                 "parent_id": "Category"}.get(key, key.replace("_", " ").capitalize())
        if key == "permissions" and "before" in change and "after" in change:
            before, after = int(change["before"] or 0), int(change["after"] or 0)
            if after & ~before:
                add("Added permissions", permissions(after & ~before))
            if before & ~after:
                add("Removed permissions", permissions(before & ~after))
        elif "before" in change and "after" in change:
            add(label, "**Before:** " + value(key, change["before"]) + "\n**After:** " + value(key, change["after"]))
        elif "after" in change:
            add(label + " · New", value(key, change["after"]))
        else:
            add(label + " · Previous", value(key, change.get("before")))
    def content(text):
        text = re.sub(r"<(@!?|@&|#)(\d+)>", lambda match: maps[
            "channels" if match[1] == "#" else "roles" if match[1] == "@&" else "members"
        ].get(match[2], {}).get("name", "Unavailable reference"), str(text))
        return safe_label(text, 1000) or "Empty message"
    for key, label in (("content_before", "Before"), ("content_after", "After")):
        if key in payload:
            add(label, content(payload[key]))
    if payload.get("action_type") == 25 and not any(payload.get(k) for k in ("roles_added", "roles_removed")):
        add("Role details", "The original entry did not retain the added or removed roles.")
    return fields


def render(row, keys, directory, settings, origin, details=None, *, admin_link=False):
    p = json.loads(row["payload"])
    maps = {kind: {str(item["id"]): item for item in directory.get(kind, [])}
            for kind in ("members", "roles", "channels")}
    member = maps["members"].get(row["subject"], {})
    lines = []
    action = p.get("action_type")
    channel_action = action in {10, 11, 12, 13, 14, 15}
    role_action = action in {30, 31, 32}
    member_subject = row["subject"] and not channel_action and not role_action and action != 1
    footer = "greyBot"
    if member_subject:
        lines.append("**Member:** " + safe_label(member.get("name", "Unavailable member")))
        footer += " · Member: " + str(member.get("name", "Unavailable member"))
    actor = str(p.get("actor") or p.get("user_id") or "") if row["kind"] == "GUILD_AUDIT_LOG_ENTRY_CREATE" else str(p.get("actor") or "")
    if actor:
        lines.append("**By:** " + safe_label(maps["members"].get(actor, {}).get("name", "Unavailable member")))
    cid = str(p.get("channel_id") or p.get("previous_channel_id") or (p.get("target_id") if channel_action else "") or (p.get("id") if row["kind"].startswith("CHANNEL_") else "") or "")
    recorded_name = next((c.get("after") or c.get("before") for c in p.get("changes", []) if c.get("field") == "name"), None)
    if cid:
        name = p.get("name") if row["kind"].startswith("CHANNEL_") else None
        channel_name = name or recorded_name or maps["channels"].get(cid, {}).get("name", "unavailable-channel")
        lines.append("**Channel:** #" + safe_label(channel_name))
        footer += " · Channel: #" + channel_name
    if row["kind"].startswith("GUILD_ROLE_") or role_action:
        role = p.get("role") or {}
        rid = str(role.get("id") or p.get("role_id") or p.get("target_id") or "")
        role_name = role.get("name") or recorded_name or maps["roles"].get(rid, {}).get("name", "Unavailable role")
        lines.append("**Role:** " + safe_label(role_name))
        footer += " · Role: " + role_name
    target = p.get("overwrite_target")
    if target:
        kind, label = ("roles", "Role") if target["type"] == 0 else ("members", "Member")
        name = target.get("name") or maps[kind].get(target["id"], {}).get("name", "Unavailable " + label.lower())
        lines.append("**Permissions for:** " + safe_label(name))
        footer += " · " + label + ": " + name
    if p.get("ids"):
        lines.append("**Messages:** " + str(len(p["ids"])))
    if not lines:
        lines.append("Server activity recorded.")
    icons = {"member_joined": "📥", "member_left": "📤", "member_roles_changed": "⚔️",
             "role_created": "⚔️", "role_updated": "⚔️", "role_deleted": "⚔️",
             "channel_created": "🆕", "channel_updated": "📝", "channel_deleted": "🗑",
             "message_updated": "📝", "message_deleted": "🗑", "nickname_changed": "📝"}
    if p.get("action_type") in (13, 14, 15):
        icons["channel_updated"] = "⚔️"
    embed = {"title": " · ".join((icons.get(key, "") + " " + AUDIT_EVENTS[key][1]).strip() for key in keys)[:256],
             "description": "\n".join(lines)[:700], "color": 0x4493F8,
             "author": {"name": "greyBot"},
             "timestamp": datetime.fromtimestamp(p.get("occurred_at", row["observed"]), timezone.utc).isoformat(),
             "footer": {"text": footer[:300]}}
    if admin_link:
        embed["description"] += f"\n\n[Open admin site]({origin}/#events)"
        embed["url"] = origin + "/"
    fields = change_fields({**p, **(details or {})}, maps)
    if fields:
        embed["fields"] = fields
    avatar = member.get("avatar_url", "")
    if member_subject:
        embed["author"] = {"name": str(member.get("name", "Unavailable member"))[:256]}
    if settings["audit_show_avatars"] and re.fullmatch(r"https://cdn\.discordapp\.com/[a-zA-Z0-9_/.?=]+", avatar):
        embed["thumbnail"] = {"url": avatar}
        if member_subject:
            embed["author"]["icon_url"] = avatar
    return {"embeds": [embed], "allowed_mentions": {"parse": []}, "flags": 4096,
            "nonce": "audit-" + str(row["seq"]), "enforce_nonce": True}


async def preflight(cfg, api, channel_id):
    if not channel_id:
        raise Denied("Choose an audit channel")
    channel = await api.request("GET", f"/channels/{channel_id}")
    if channel.get("guild_id") != cfg.guild_id or channel.get("type") not in {0, 5}:
        raise Denied("Audit destination must be a text channel in this server")
    bot = await api.request("GET", f"/guilds/{cfg.guild_id}/members/{cfg.client_id}")
    roles = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
    needed = (1 << 10) | (1 << 11) | (1 << 14)
    if effective_permissions(cfg.guild_id, roles, bot, channel) & needed != needed:
        raise Denied("greyBot needs View Channel, Send Messages and Embed Links in the audit channel")


class Feed:
    def __init__(self, cfg, store, api):
        self.cfg, self.store, self.api = cfg, store, api
        self.directory = Directory(cfg, store, api)

    async def tick(self):
        settings = self.store.settings(self.cfg.guild_id)["values"]
        with self.store.connection() as db:
            current = db.execute("SELECT seq FROM feed_cursor WHERE guild=?", (self.cfg.guild_id,)).fetchone()
            if not any(settings.get(key) for key in ("audit_feed_enabled", "goodbye_enabled", "welcome_enabled")) or not current:
                db.execute("INSERT INTO feed_cursor VALUES(?,(SELECT COALESCE(MAX(seq),0) FROM events)) ON CONFLICT(guild) DO UPDATE SET seq=excluded.seq", (self.cfg.guild_id,))
                return
            rows = [dict(row) for row in db.execute("SELECT e.*,r.seq AS archived FROM events e LEFT JOIN receipts r ON r.seq=e.seq WHERE e.guild=? AND e.seq>? ORDER BY e.seq LIMIT 30", (self.cfg.guild_id, current[0]))]
        posted = 0
        for row in rows:
            if row["archived"] is None:
                return
            # Re-read so turning off posting during a batch takes effect.
            latest = self.store.settings(self.cfg.guild_id)["values"]
            if latest != settings:
                return
            p = json.loads(row["payload"])
            keys = [key for key in classify(row) if key in settings["audit_events"]] if settings["audit_feed_enabled"] else []
            cid = str(p.get("channel_id") or p.get("previous_channel_id")
                      or (p.get("target_id") if p.get("action_type") in {10, 11, 12, 13, 14, 15} else "")
                      or (p.get("id") if row["kind"].startswith("CHANNEL_") else "") or "")
            if cid == settings["audit_channel"] or cid in settings["audit_ignored_channels"]:
                keys = []  # Never feed the audit channel back into itself.
            if keys and settings["audit_ignore_bots"]:
                uid = str(p.get("actor") or row["subject"] or "")
                if uid:
                    try:
                        user = await self.api.request("GET", f"/users/{uid}")
                        if user.get("bot"):
                            keys = []
                    except (Denied, Unavailable):
                        return  # No guessing when the selected filter needs identity.
                else:
                    keys = []
            goodbye = row["kind"] == "GUILD_MEMBER_REMOVE" and settings.get("goodbye_enabled")
            welcome = row["kind"] == "GUILD_MEMBER_ADD" and settings.get("welcome_enabled")
            data = await self.directory.get() if keys or goodbye or welcome else None
            if data:
                for uid in {row["subject"], str(p.get("actor") or "")}:
                    known = self.store.profile(self.cfg.guild_id, uid) if uid else None
                    if known:
                        data["members"] = [member for member in data["members"] if member["id"] != uid] + [known["value"]]
            targets = {}
            if keys:
                targets[settings["audit_channel"]] = render(row, keys, data, settings, self.cfg.origin, self.store.details(row), admin_link=True)
                if row["kind"] == "GUILD_MEMBER_REMOVE":
                    targets[settings["audit_channel"]]["embeds"][0]["image"] = {
                        "url": "https://media.giphy.com/media/bc4pHNmIWVlPoqzV8n/giphy.gif"}
            if goodbye:
                body = render(row, ["member_left"], data, settings, self.cfg.origin)
                body["embeds"][0]["image"] = {"url": "https://media.tenor.com/pfd3ov2DcoMAAAAM/coffin-dance-dancing-pallbearers.gif"}
                body["embeds"][0]["title"] = "Left the server"
                body["embeds"][0]["url"] = "https://tenor.com/view/coffin-dance-dancing-pallbearers-meme-funeral-dancers-gif-17562712"
                body["nonce"] = "leave-" + str(row["seq"])
                targets[settings["goodbye_channel"]] = body
            if welcome:
                body = render(row, ["member_joined"], data, settings, self.cfg.origin)
                body["content"] = "Welcome <@" + row["subject"] + ">!"
                body["allowed_mentions"] = {"parse": [], "users": [row["subject"]]}
                body.pop("flags", None)  # The joining member's welcome may notify them.
                body["embeds"][0]["image"] = {"url": "https://media.giphy.com/media/9w7YtTycjeLzW8V6io/giphy.gif"}
                body["embeds"][0]["title"] = "Joined the server"
                body["embeds"][0]["url"] = "https://giphy.com/gifs/lizard-tom-the-wave-9w7YtTycjeLzW8V6io"
                body["nonce"] = "join-" + str(row["seq"])
                if settings.get("verification_enabled"):
                    start = next((channel for channel in data.get("channels", [])
                                  if channel.get("name") in {"start-here", "verify-membership"}), None)
                    body["content"] += " Verify you're human to unlock the server."
                    if start:
                        body["content"] += " Start here: <#" + str(start["id"]) + ">."
                    body["components"] = [{"type": 1, "components": [{"type": 2, "style": 1,
                        "label": "Verify to unlock channels", "custom_id": "greybot:verify"}]}]
                targets[settings["welcome_channel"]] = body
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                configured = db.execute("SELECT body FROM settings WHERE guild=?", (self.cfg.guild_id,)).fetchone()
                if {**DEFAULT_SETTINGS, **(json.loads(configured[0]) if configured else {})} != settings:
                    return
                cursor = db.execute("SELECT seq FROM feed_cursor WHERE guild=?", (self.cfg.guild_id,)).fetchone()[0]
                if cursor >= row["seq"]:
                    return
                db.execute("UPDATE feed_cursor SET seq=? WHERE guild=?", (row["seq"], self.cfg.guild_id))
                for channel in targets:
                    db.execute("INSERT OR IGNORE INTO feed_delivery VALUES(?,?,?,?,?)", (self.cfg.guild_id, row["seq"], channel, "sending", ""))
            for channel, body in targets.items():
                try:
                    result = await self.api.request("POST", f"/channels/{channel}/messages", body=body)
                    state, mid = "sent", str(result["id"])
                except Exception:
                    state, mid = "unknown", ""
                with self.store.connection() as db:
                    db.execute("UPDATE feed_delivery SET state=?,message=? WHERE guild=? AND seq=? AND channel=?", (state, mid, self.cfg.guild_id, row["seq"], channel))
                posted += 1
            if posted >= 3:
                return
