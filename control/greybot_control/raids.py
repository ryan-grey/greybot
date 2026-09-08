"""Transactional raid rosters and permissions, independent of Discord delivery."""
import copy
import json
import secrets
import time

from .discord_api import ADMINISTRATOR, Denied
from .mutes import effective_permissions
from .store import canonical


def install(store):
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS raid_events(
                guild TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                revision INTEGER NOT NULL, published_revision INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '', delivery TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(guild,id));
            CREATE TABLE IF NOT EXISTS raid_preferences(
                guild TEXT NOT NULL, user TEXT NOT NULL, template TEXT NOT NULL,
                choice TEXT NOT NULL, PRIMARY KEY(guild,user,template));
        """)


def read(store, guild, event_id):
    with store.connection() as db:
        row = db.execute("SELECT * FROM raid_events WHERE guild=? AND id=?", (guild, event_id)).fetchone()
    if not row:
        raise Denied("Raid event is unavailable")
    return {**dict(row), "body": json.loads(row["body"])}


def list_events(store, guild):
    with store.connection() as db:
        return [{**dict(r), "body": json.loads(r["body"])} for r in db.execute(
            "SELECT * FROM raid_events WHERE guild=? ORDER BY json_extract(body,'$.startTime') DESC", (guild,))]


def choices(event):
    """Return stable template-local choices; class names alone aren't unique specs."""
    result = []
    for ci, cls in enumerate(event["classes"]):
        if cls.get("type", "primary") != "primary":
            continue
        specs = cls.get("specs") or [None]
        for si, spec in enumerate(specs):
            role = (spec or cls).get("roleName", cls.get("name", ""))
            label = cls.get("cName") or cls["name"]
            if spec:
                label += " · " + (spec.get("cName") or spec["name"])
            result.append({"value": f"{ci}:{si}", "label": label, "className": cls["name"],
                           "specName": (spec or {}).get("name", ""), "roleName": role,
                           "emoji_id": str((spec or cls).get("emoteId", "")),
                           "class_index": ci, "spec_index": si if spec else None})
    return result


async def authorize(cfg, store, api, actor, event, *, manage=False, create=False):
    """Refresh membership and channel permissions even for signed button requests."""
    member = await api.request("GET", f"/guilds/{cfg.guild_id}/members/{actor}")
    roles = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
    channel = await api.request("GET", f"/channels/{event['channelId']}")
    if channel.get("guild_id") != cfg.guild_id or member.get("user", {}).get("bot") or member.get("pending"):
        raise Denied("A current server membership is required")
    perms = effective_permissions(cfg.guild_id, roles, member, channel)
    if not perms & (1 << 10) or (create and not perms & (1 << 11)):
        raise Denied("You cannot access or post in this event's channel")
    if create and not perms & (ADMINISTRATOR | (1 << 5)):
        raise Denied("Administrator or Manage Server permission is required to create events")
    settings = store.settings(cfg.guild_id)["values"]
    if settings.get("verification_enabled") and settings.get("verification_role") not in member.get("roles", []) and not perms & ADMINISTRATOR:
        raise Denied("Verify your membership before using raid signups")
    if manage:
        leaders = {str(event["leaderId"]), *(str(c.get("id")) if isinstance(c, dict) else str(c) for c in event.get("coLeaders", []))}
        if actor not in leaders and not perms & (ADMINISTRATOR | (1 << 5)):
            raise Denied("Only this event's leaders or server administrators can manage it")
    elif not create:
        allowed = event.get("advancedSettings", {}).get("allowed_roles", [])
        if isinstance(allowed, str):
            allowed = [] if allowed in {"none", "default", ""} else [r.strip() for r in allowed.split(",") if r.strip()]
        if allowed and not set(map(str, allowed)).intersection(member.get("roles", [])) and not perms & ADMINISTRATOR:
            raise Denied("You do not have a role allowed for this event")
    return member


def validate_event(event):
    if not isinstance(event.get("title"), str) or not 1 <= len(event["title"].strip()) <= 200:
        raise Denied("Use an event title between 1 and 200 characters")
    if not isinstance(event.get("description", ""), str) or len(event.get("description", "")) > 3500:
        raise Denied("Keep the description within 3,500 characters")
    for key in ("startTime", "closingTime"):
        if isinstance(event.get(key), bool) or not isinstance(event.get(key), (float, int)) or event[key] <= 0:
            raise Denied("Choose a valid event date and closing time")
    if not str(event.get("channelId", "")).isdecimal() or not str(event.get("leaderId", "")).isdecimal():
        raise Denied("Choose an event channel and leader")
    if not isinstance(event.get("classes"), list) or not choices(event):
        raise Denied("Choose a signup template")
    if event.get("state") not in {"open", "closed", "cancelled"}:
        raise Denied("Invalid event state")


def create(store, guild, actor, action_id, event):
    """Caller must authorize creation; retries return the same created event."""
    event = copy.deepcopy(event)
    event.setdefault("state", "open")
    event.setdefault("signUps", [])
    event.setdefault("advancedSettings", {})
    event.setdefault("coLeaders", [])
    validate_event(event)
    key = f"raid-action:{guild}:{action_id}"
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT payload FROM events WHERE event_id=?", (key,)).fetchone()
        if prior:
            return json.loads(prior[0])["raid_id"]
        event_id = secrets.token_hex(12)
        db.execute("INSERT INTO raid_events(guild,id,body,revision) VALUES(?,?,?,1)",
                   (guild, event_id, canonical(event)))
        store._append(db, key, guild, "RAID_CREATED", actor, {
            "actor": actor, "raid_id": event_id, "channel_id": event["channelId"], "title": event["title"]})
    return event_id


def _limit(settings, key, default):
    try:
        return max(1, int(settings.get(key, default)))
    except (TypeError, ValueError):
        raise Denied("This event has an invalid signup limit") from None


def mutate(store, guild, actor, action_id, event_id, revision, operation, value, *, now=None):
    """Apply one authorized operation and journal it in the same transaction.

    Authorization is deliberately outside this synchronous transaction; callers
    must pass the revision they authorized, so concurrent edits cannot change its
    channel, leaders or role policy underneath that decision.
    """
    now = time.time() if now is None else now
    key = f"raid-action:{guild}:{action_id}"
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT payload FROM events WHERE event_id=?", (key,)).fetchone()
        if prior:
            return json.loads(prior[0])["revision"]
        row = db.execute("SELECT * FROM raid_events WHERE guild=? AND id=?", (guild, event_id)).fetchone()
        if not row or row["revision"] != revision:
            raise Denied("The raid changed while you were editing; refresh and try again")
        event = json.loads(row["body"])
        before = copy.deepcopy(event)
        roster = event["signUps"]
        settings = event["advancedSettings"]
        existing = next((s for s in roster if str(s["userId"]) == actor), None)
        if operation in {"signup", "withdraw", "note", "status"}:
            if event["state"] != "open" or now >= event["closingTime"]:
                raise Denied("Signups are closed for this event")
            if operation == "withdraw":
                event["signUps"] = [s for s in roster if str(s["userId"]) != actor]
            elif operation == "note":
                if not existing:
                    raise Denied("Sign up before adding a note")
                if settings.get("notes_enabled") is False:
                    raise Denied("Notes are disabled for this event")
                if not isinstance(value, str) or len(value) > 500:
                    raise Denied("Keep your note within 500 characters")
                existing["note"] = value
            elif operation == "status":
                if value not in {"Bench", "Late", "Tentative", "Absence"}:
                    raise Denied("Choose a valid signup status")
                if not existing:
                    existing = {"userId": actor, "entryTime": now, "position": len(roster) + 1}
                    roster.append(existing)
                existing.update({"className": value, "specName": "", "roleName": value, "status": "secondary"})
            else:
                choice = next((c for c in choices(event) if c["value"] == value), None)
                if not choice:
                    raise Denied("This signup choice is unavailable")
                primary_classes = {c["name"] for c in event["classes"] if c.get("type", "primary") == "primary"}
                attending = [s for s in roster if str(s["userId"]) != actor and s.get("className") in primary_classes]
                cls = event["classes"][choice["class_index"]]
                spec = cls.get("specs", [])[choice["spec_index"]] if choice["spec_index"] is not None else {}
                full = len(attending) >= _limit(settings, "limit", 9999)
                full |= sum(s.get("className") == choice["className"] for s in attending) >= _limit(cls, "limit", 999)
                full |= sum(s.get("className") == choice["className"] and s.get("specName") == choice["specName"] for s in attending) >= _limit(spec, "limit", 999)
                role = next((r for r in event.get("roles", []) if r.get("name") == choice["roleName"]), {})
                full |= sum(s.get("roleName") == choice["roleName"] for s in attending) >= _limit(role, "limit", 999)
                if full and not settings.get("bench_overflow", False):
                    raise Denied("This signup slot is full")
                if not existing:
                    existing = {"userId": actor, "entryTime": now, "position": len(roster) + 1}
                    roster.append(existing)
                existing.update({k: choice[k] for k in ("className", "specName", "roleName")})
                existing["status"] = "secondary" if full else "primary"
                if full:
                    existing["className"] = existing["roleName"] = "Bench"
                db.execute("INSERT INTO raid_preferences VALUES(?,?,?,?) ON CONFLICT(guild,user,template) DO UPDATE SET choice=excluded.choice",
                           (guild, actor, event.get("templateId", "standard"), canonical(choice)))
        elif operation == "edit":
            if not isinstance(value, dict) or set(value) - {"title", "description", "startTime", "closingTime"}:
                raise Denied("Unsupported event edit")
            event.update(value)
            validate_event(event)
        elif operation in {"close", "open", "cancel"}:
            if operation == "open" and event["closingTime"] <= now:
                raise Denied("Set a future closing time before reopening this event")
            event["state"] = {"close": "closed", "open": "open", "cancel": "cancelled"}[operation]
        else:
            raise Denied("Unsupported raid operation")
        revision += 1
        db.execute("UPDATE raid_events SET body=?,revision=? WHERE guild=? AND id=?",
                   (canonical(event), revision, guild, event_id))
        store._append(db, key, guild, "RAID_CHANGED", actor, {
            "actor": actor, "raid_id": event_id, "channel_id": event["channelId"], "title": event["title"],
            "operation": operation, "revision": revision,
            "previous_state": before["state"], "state": event["state"],
            "before": before if operation == "edit" else next((s for s in before["signUps"] if str(s["userId"]) == actor), None),
            "after": event if operation == "edit" else next((s for s in event["signUps"] if str(s["userId"]) == actor), None)})
    return revision
