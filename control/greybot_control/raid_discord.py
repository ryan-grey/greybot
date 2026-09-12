"""Discord raid commands, private selection menus, and archived card delivery."""
import copy
import asyncio
from datetime import datetime, timezone
import json
import os
import time

from discord.utils import escape_markdown, escape_mentions

from . import raids
from .raid_emojis import EMOJIS
from .discord_api import Denied

PREFIX = "greybot:raid:"
COMMAND_NAMES = {"create", "quickcreate", "raid"}
ROLE_EMOJIS = {"Tank": "878310168289505301", "Melee": "734439523328720913",
               "Ranged": "592446395596931072", "Healer": "898011741735235645"}


def combat_role(choice):
    name = choice.get("roleName") or choice.get("className", "")
    return {"Tanks": "Tank", "Healers": "Healer"}.get(name, name)


def role_buttons(event, event_id):
    available = {combat_role(c) for c in raids.choices({**event, "classes": event.get("classes", [])})}
    return [{**button(role, "group", event_id + ":" + role),
             "emoji": EMOJIS.get(emoji, {"id": emoji, "name": role})} for role, emoji in ROLE_EMOJIS.items() if role in available]


def emoji_text(source):
    icon = EMOJIS.get(str(source))
    return ("<:" + icon["name"] + ":" + icon["id"] + "> ") if icon else ""


def enabled():
    return os.environ.get("GREYBOT_RAIDS_ENABLED") == "1"


def reply(content, components=None):
    return {"type": 4, "data": {"content": content, "flags": 64,
            "allowed_mentions": {"parse": []}, **({"components": components} if components else {})}}


def button(label, action, event_id):
    return {"type": 2, "style": 2, "label": label, "custom_id": PREFIX + action + ":" + event_id}


def modal(custom_id, title, fields):
    return {"type": 9, "data": {"custom_id": custom_id, "title": title,
        "components": [{"type": 1, "components": [{"type": 4, "custom_id": name,
            "label": label, "style": style, "required": required, "max_length": limit}]}
            for name, label, style, required, limit in fields]}}


def template(store, guild, name):
    source = {"classes": [{"name": "Attending", "type": "primary"}], "roles": [],
              "advancedSettings": {"notes_enabled": True, "limit": 9999}, "templateId": "standard"}
    with store.connection() as db:
        rows = db.execute("SELECT d.body FROM event_details d JOIN events e ON e.event_id=d.event_id "
                          "WHERE e.guild=? AND e.kind='RAID_HISTORY_IMPORTED' AND json_extract(d.body,'$.templateId')=? "
                          "ORDER BY json_extract(d.body,'$.startTime') DESC LIMIT 1", (guild, name)).fetchall()
    if rows:
        source = json.loads(rows[0][0])
    elif name != "standard":
        raise Denied("That template has not been installed yet")
    # Historical channel/leader restrictions must not accidentally become defaults.
    return {"classes": copy.deepcopy(source["classes"]), "roles": copy.deepcopy(source.get("roles", [])),
            "templateId": name, "advancedSettings": {"notes_enabled": True, "limit": 9999, "bench_overflow": True}}


def parse_start(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        timestamp = int(dt.timestamp())
        if not time.time() < timestamp < time.time() + 366 * 86400:
            raise ValueError()
        return timestamp
    except (ValueError, TypeError, AttributeError):
        raise Denied("Use a future date with UTC offset, for example 2026-09-12T18:00-07:00") from None


def receive(cfg, store, packet):
    if not enabled():
        raise Denied("Raid signups are not enabled yet")
    member = packet.get("member", {})
    actor = member.get("user", {}).get("id")
    if (packet.get("guild_id") != cfg.guild_id or packet.get("application_id") != cfg.client_id
            or not actor or member.get("user", {}).get("bot") or member.get("pending")
            or not str(packet.get("id", "")).isdecimal()):
        raise Denied("A valid server interaction is required")
    data = packet.get("data", {})
    if packet["type"] == 2:
        name = data.get("name")
        if name == "raid":
            return reply("Open your server's raid events: " + cfg.origin + "/raids")
        if name not in {"create", "quickcreate"} or not int(member.get("permissions", "0")) & (8 | 32):
            raise Denied("Administrator or Manage Server permission is required to create events")
        options = {o["name"]: o.get("value") for o in data.get("options", [])}
        channel = str(options.get("channel") or packet.get("channel_id", ""))
        kind = options.get("template", "standard")
        if kind not in {"standard", "wowretail1", "wowretail2"} or not channel.isdecimal():
            raise Denied("Invalid event channel or template")
        if name == "create":
            return modal(PREFIX + "create:" + channel + ":" + kind, "Create raid signup", [
                ("title", "Event title", 1, True, 200),
                ("when", "Date + offset: 2026-09-12T18:00-07:00", 1, True, 40),
                ("description", "Details", 2, False, 3500)])
        body = {"operation": "create", "channel": channel, "template": kind,
                "title": options.get("title", ""), "when": options.get("when", ""),
                "description": options.get("description", "")}
        parse_start(body["when"])
    else:
        custom = data.get("custom_id", "")
        if not custom.startswith(PREFIX):
            raise Denied("Invalid raid interaction")
        parts = custom[len(PREFIX):].split(":")
        operation = parts[0]
        fields = {c["custom_id"]: c.get("value", "") for row in data.get("components", []) for c in row.get("components", [])}
        if operation == "create" and packet["type"] == 5 and len(parts) == 3:
            if not int(member.get("permissions", "0")) & (8 | 32):
                raise Denied("Administrator or Manage Server permission is required")
            body = {"operation": "create", "channel": parts[1], "template": parts[2],
                    "title": fields.get("title", ""), "when": fields.get("when", ""),
                    "description": fields.get("description", "")}
            parse_start(body["when"])
        else:
            if len(parts) not in {2, 3}:
                raise Denied("Invalid raid control")
            row = raids.read(store, cfg.guild_id, parts[1])
            event = row["body"]
            # Public buttons must belong to the recorded event card. Ephemeral
            # selections can only be created by this application in its channel.
            if packet.get("channel_id") != event["channelId"]:
                raise Denied("This control belongs to a different channel")
            if packet["type"] == 3:
                message = packet.get("message", {})
                if message.get("author", {}).get("id") != cfg.client_id:
                    raise Denied("Unrecognized raid message")
                if not int(message.get("flags", 0)) & 64 and message.get("id") != row["message"]:
                    raise Denied("This raid card has been replaced")
            if operation in {"signup", "page", "group"} and packet["type"] == 3:
                if operation != "group" and role_buttons(event, row["id"]):
                    return reply("Choose your combat role for **" + safe(event["title"]) + "**.",
                                 [{"type": 1, "components": role_buttons(event, row["id"])}])
                group = parts[2] if operation == "group" and len(parts) == 3 else None
                if operation == "group" and group not in ROLE_EMOJIS:
                    raise Denied("Choose a valid combat role")
                page = int(parts[2]) if operation == "page" and len(parts) == 3 else 0
                available = raids.choices(event)
                if group:
                    available = [c for c in available if combat_role(c) == group]
                if not 0 <= page <= (len(available) - 1) // 25:
                    raise Denied("Invalid choice page")
                selected = available[page * 25:(page + 1) * 25]
                preferred = next((s for s in event["signUps"] if str(s["userId"]) == actor), {})
                if not preferred:
                    with store.connection() as db:
                        saved = db.execute("SELECT choice FROM raid_preferences WHERE guild=? AND user=? AND template=?",
                                           (cfg.guild_id, actor, event.get("templateId", "standard"))).fetchone()
                    preferred = json.loads(saved[0]) if saved else {}
                components = [{"type": 1, "components": [{"type": 3, "custom_id": PREFIX + "choose:" + row["id"],
                    "placeholder": "Choose your " + group.lower() + " specialization" if group else "Choose your class / specialization", "options": [
                        {"label": c["label"][:100], "value": c["value"],
                         **({"emoji": EMOJIS[c["emoji_id"]]} if c.get("emoji_id") in EMOJIS else {}), "default": bool(preferred) and
                         all(c[k] == preferred.get(k) for k in ("className", "specName"))} for c in selected]}]}]
                pages = [button(str(p + 1), "page", row["id"] + ":" + str(p)) for p in range((len(available) + 24) // 25)]
                if len(pages) > 1:
                    components.append({"type": 1, "components": pages[:5]})
                return reply("Choose your " + (group.lower() + " specialization" if group else "signup") + " for **" + safe(event["title"]) + "**.", components)
            if operation == "note" and packet["type"] == 3:
                return modal(PREFIX + "save-note:" + row["id"], "Your raid note", [("note", "Note (leave empty to remove)", 2, False, 500)])
            if operation == "status" and packet["type"] == 3:
                return reply("Choose your attendance status.", [{"type": 1, "components": [
                    button(s, "set-status", row["id"] + ":" + s) for s in ("Bench", "Late", "Tentative", "Absence")]}])
            value = ""
            if operation == "choose" and packet["type"] == 3:
                values = data.get("values", [])
                if len(values) != 1:
                    raise Denied("Choose one signup")
                operation, value = "signup", values[0]
            elif operation == "save-note" and packet["type"] == 5:
                operation, value = "note", fields.get("note", "")
            elif operation == "set-status" and packet["type"] == 3 and len(parts) == 3:
                operation, value = "status", parts[2]
            elif operation != "withdraw" or packet["type"] != 3:
                raise Denied("Unsupported raid control")
            body = {"operation": operation, "raid_id": row["id"], "value": value}
    store.queue("raid-" + packet["id"], cfg.guild_id, actor, "raid", actor, body)
    return reply("Your raid request is queued. The signup card will update after your access is checked.")


async def execute(cfg, store, api, job):
    if not enabled() or time.time() - job["created"] > 300:
        raise Denied("Raid request is disabled or expired")
    body = json.loads(job["body"])
    actor = job["actor"]
    if body["operation"] == "create":
        start = parse_start(body["when"])
        event = {**template(store, cfg.guild_id, body["template"]), "title": body["title"],
                 "description": body["description"], "startTime": start, "closingTime": start,
                 "leaderId": actor, "channelId": body["channel"]}
        await raids.authorize(cfg, store, api, actor, event, create=True)
        raids.create(store, cfg.guild_id, actor, job["id"], event)
    else:
        row = raids.read(store, cfg.guild_id, body["raid_id"])
        if "revision" in body and body["revision"] != row["revision"]:
            raise Denied("The event changed after this edit was queued")
        await raids.authorize(cfg, store, api, actor, row["body"],
                              manage=body["operation"] in {"edit", "close", "open", "cancel"})
        raids.mutate(store, cfg.guild_id, actor, job["id"], row["id"], row["revision"], body["operation"], body.get("value", ""))


def safe(value):
    return escape_mentions(escape_markdown(str(value)))


def card(cfg, row, profiles):
    event = row["body"]
    leader = profiles.get(str(event["leaderId"]), {})
    opened = event["state"] == "open" and time.time() < event["closingTime"]
    groups = {}
    names_only = {}
    choices = {(c["className"], c["specName"]): c for c in raids.choices({**event, "classes": event.get("classes", [])})}
    for signup in event["signUps"]:
        name = profiles.get(str(signup["userId"]), {}).get("name")
        if not name or name == "Unknown member":
            name = signup.get("name") or "Former member"
        group = signup.get("roleName") or signup.get("className") or "Attending"
        spec = signup.get("specName", "")
        icon = signup.get("specEmoteId") or choices.get((signup.get("className"), spec), {}).get("emoji_id")
        groups.setdefault(group, []).append(emoji_text(icon) + safe(name[:45]) + (" · " + safe(spec[:25]) if spec else ""))
        names_only.setdefault(group, []).append(safe(name[:45]))
    provisional = sum(len(v) for k, v in groups.items() if k in {"Late", "Tentative"})
    confirmed = sum(len(v) for k, v in groups.items() if k not in {"Late", "Tentative", "Absence", "Bench"})
    fields = []
    # Keep every signup. Very large rosters use compact names instead of hiding
    # members behind a link; split long categories at Discord's field limit.
    display_groups = groups
    if sum(len("\n".join(v)) for v in groups.values()) > 4000:
        count = sum(map(len, names_only.values()))
        width = max(1, 4000 // max(1, count) - 1)
        display_groups = {k: [name[:width] for name in v] for k, v in names_only.items()}
    status_columns = ('Absence', 'Tentative', 'Bench')
    ordered_groups = [(k,v) for k,v in display_groups.items() if k not in status_columns]
    ordered_groups += [(k,display_groups[k]) for k in status_columns if k in display_groups]
    for k, members in ordered_groups:
        chunks, chunk = [], []
        for member in members:
            if chunk and len("\n".join(chunk + [member])) > 1020:
                chunks.append(chunk)
                chunk = []
            chunk.append(member)
        if chunk:
            chunks.append(chunk)
        for index, chunk in enumerate(chunks):
            label = safe(k)[:60] + (f" · {len(members)}" if index == 0 else " · continued")
            fields.append({"name": emoji_text(ROLE_EMOJIS.get(combat_role({"roleName": k}))) + label,
                           "value": "\n".join(chunk) + "\n\u200b", "inline": k in status_columns})
    role_counts = {role: sum(len(v) for k, v in groups.items() if combat_role({"roleName": k}) == role)
                   for role in ("Tank", "Healer", "Ranged", "Melee")}
    counts_line = ("\u00a0" * 5).join(emoji_text(ROLE_EMOJIS[role]).rstrip() + f"; {count}"
                                    for role, count in role_counts.items())
    totals = f"**Signups: {confirmed} (+{provisional})**\n{counts_line}\n\n"
    embed = {"title": event["title"][:200], "description": totals + event.get("description", "")[:1900] +
             f"\n\n<t:{int(event['startTime'])}:F> · <t:{int(event['startTime'])}:R>",
             "color": 0x4493F8, "url": cfg.origin + "/raids#" + row["id"],
             "author": {"name": (leader.get("name") or event.get("leaderName") or "Raid leader")[:256]},
             "fields": fields, "footer": {"text": "greyBot · " + ("Signups open" if opened else event["state"].capitalize() if event["state"] != "open" else "Signups closed")},
             "timestamp": datetime.fromtimestamp(event["startTime"], timezone.utc).isoformat()}
    if leader.get("avatar_url"):
        embed["author"]["icon_url"] = leader["avatar_url"]
    fixed = len(embed['title']) + len(embed['author']['name']) + len(embed['footer']['text']) + sum(len(f['name']) + len(f['value']) for f in fields)
    if fixed + len(embed['description']) > 5900:
        date_line = f"\n\n<t:{int(event['startTime'])}:F> · <t:{int(event['startTime'])}:R>"
        room = max(0, 5900 - fixed - len(totals) - len(date_line))
        embed['description'] = totals + event.get('description', '')[:room] + date_line
    controls = []
    if opened:
        controls.append({"type": 1, "components": role_buttons(event, row["id"]) or [button("Sign up / change", "signup", row["id"])]})
        controls.append({"type": 1, "components": [button("Status", "status", row["id"]),
                         button("Note", "note", row["id"]), button("Withdraw", "withdraw", row["id"])]})
    controls.append({"type": 1, "components": [{"type": 2, "style": 5, "label": "Full roster & event details", "url": cfg.origin + "/raids#" + row["id"]}]})
    return {"embeds": [embed], "components": controls, "allowed_mentions": {"parse": []}}


async def deliver_one(cfg, store, api, archive):
    if not enabled() or not cfg.enforce:
        return
    with store.connection() as db:
        # A crash during first publication has an uncertain result; never repost
        # automatically after Discord's short nonce deduplication window.
        raw = db.execute("SELECT * FROM raid_events WHERE guild=? AND revision>published_revision "
                         "AND delivery NOT IN ('sending','unknown') LIMIT 1", (cfg.guild_id,)).fetchone()
    if not raw:
        return
    await asyncio.to_thread(archive.flush, store)
    if store.pending(1):
        return
    row = {**dict(raw), "body": json.loads(raw["body"])}
    from .directory import Directory
    directory = await Directory(cfg, store, api).get()
    payload = card(cfg, row, {p["id"]: p for p in directory["members"]})
    with store.connection() as db:
        db.execute("UPDATE raid_events SET delivery='sending' WHERE guild=? AND id=?", (cfg.guild_id, row["id"]))
    try:
        base = f"/channels/{row['body']['channelId']}/messages"
        if row["message"]:
            result = await api.request("PATCH", base + "/" + row["message"], body=payload)
        else:
            result = await api.request("POST", base, body={**payload, "nonce": row["id"], "enforce_nonce": True})
    except Exception:
        with store.connection() as db:
            db.execute("UPDATE raid_events SET delivery=? WHERE guild=? AND id=?",
                       ("pending" if row["message"] else "unknown", cfg.guild_id, row["id"]))
        raise
    with store.connection() as db:
        db.execute("UPDATE raid_events SET message=?,published_revision=?,delivery='published' WHERE guild=? AND id=?",
                   (result["id"], row["revision"], cfg.guild_id, row["id"]))


def commands():
    common = [{"type": 7, "name": "channel", "description": "Where to publish", "channel_types": [0, 5]},
              {"type": 3, "name": "template", "description": "Signup choices", "choices": [
                  {"name": "Simple attendance", "value": "standard"}, {"name": "WoW classes and specs", "value": "wowretail1"},
                  {"name": "WoW combat roles", "value": "wowretail2"}]}]
    return [{"name": "create", "description": "Create a raid signup with a guided form", "default_member_permissions": "32", "dm_permission": False, "options": common},
            {"name": "quickcreate", "description": "Create a raid signup in one command", "default_member_permissions": "32", "dm_permission": False,
             "options": [{"type": 3, "name": "title", "description": "Event title", "required": True, "max_length": 200},
                         {"type": 3, "name": "when", "description": "Date with UTC offset, e.g. 2026-09-12T18:00-07:00", "required": True},
                         *common, {"type": 3, "name": "description", "description": "Event details", "max_length": 3500}]},
            {"name": "raid", "description": "Open raid signups, rosters and attendance", "dm_permission": False}]
