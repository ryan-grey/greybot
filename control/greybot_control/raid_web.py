"""Member-authenticated raid pages; the admin dashboard remains admin-only."""
import copy
import json
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from datetime import datetime, timezone

from . import raids
from .discord_api import Denied
from .mutes import effective_permissions
from .profiles import display_profile
from .raid_discord import enabled, template, parse_start


def history(store, guild):
    result = {}
    with store.connection() as db:
        rows = db.execute("SELECT d.body FROM event_details d JOIN events e ON d.event_id=e.event_id "
                          "WHERE e.guild=? AND e.kind='RAID_HISTORY_IMPORTED' ORDER BY e.seq DESC", (guild,)).fetchall()
    for row in rows:
        event = json.loads(row[0])
        key = "history-" + str(event["id"])
        if key not in result:
            result[key] = {"id": key, "body": {**event, "state": "archived"}, "historical": True,
                           "message": str(event["id"]), "revision": 0}
    return list(result.values())


def calendar(event_id, event):
    def stamp(value):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    def text(value):
        return str(value).replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")
    lines=["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//greyBot//Raid Events//EN","BEGIN:VEVENT",
           "UID:"+event_id+"@greybot", "DTSTAMP:"+stamp(time.time()),
           "DTSTART:"+stamp(event["startTime"]),"SUMMARY:"+text(event["title"]),
           "DESCRIPTION:"+text(event.get("description", ""))]
    if event.get("endTime", 0) > event["startTime"]:
        lines.append("DTEND:"+stamp(event["endTime"]))
    lines += ["END:VEVENT","END:VCALENDAR"]
    folded=[]
    for line in lines:
        part=""
        for char in line:
            if len((part+char).encode("utf-8")) > 75:
                folded.append(part)
                part=" "
            part+=char
        folded.append(part)
    return "\r\n".join(folded)+"\r\n"


def install(app, cfg, store, api, cookie, static, directory):
    async def context(request, write=False):
        session = store.get_session(request.cookies.get(cookie + "-raids", "") or request.cookies.get(cookie, ""))
        if not session:
            raise HTTPException(401, "Sign in with Discord to see server raids")
        if write and (request.headers.get("origin") != cfg.origin or not secrets.compare_digest(
                request.headers.get("x-csrf-token", ""), session["csrf"])):
            raise HTTPException(403, "Invalid origin or CSRF token")
        uid = session["user"]
        member = await api.request("GET", f"/guilds/{cfg.guild_id}/members/{uid}")
        roles = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
        channels = await api.request("GET", f"/guilds/{cfg.guild_id}/channels")
        if member.get("pending") or member.get("user", {}).get("bot"):
            raise Denied("Current human membership is required")
        class Snapshot:
            async def request(self, method, path):
                if path.endswith("/roles"):
                    return roles
                if path.startswith("/channels/"):
                    found = next((c for c in channels if c["id"] == path.rsplit("/", 1)[1]), None)
                    if not found:
                        raise Denied("This event's channel is unavailable")
                    return {**found, "guild_id": cfg.guild_id}
                return member
        return session, member, roles, channels, Snapshot()

    @app.get("/raids")
    async def page():
        return FileResponse(static / "raids.html")

    @app.get("/api/raids")
    async def index(request: Request):
        session, member, roles, channels, snapshot = await context(request)
        all_rows = raids.list_events(store, cfg.guild_id)
        migrated = {str(r["body"].get("source_event_id", "")) for r in all_rows}
        all_rows += [r for r in history(store, cfg.guild_id) if r["message"] not in migrated]
        profiles = {p["id"]: p for p in (await directory.get())["members"]}
        result = []
        for row in all_rows:
            event = row["body"]
            try:
                await raids.authorize(cfg, store, snapshot, session["user"], event)
            except Denied:
                continue
            can_manage = False
            if not row.get("historical"):
                try:
                    await raids.authorize(cfg, store, snapshot, session["user"], event, manage=True)
                    can_manage = True
                except Denied:
                    pass
            display = copy.deepcopy(event)
            for signup in display["signUps"]:
                profile = profiles.get(str(signup["userId"]), {})
                known_name = profile.get("name")
                signup["display"] = {"name": (known_name if known_name != "Unknown member" else None) or signup.get("name") or "Former member",
                                     "avatar_url": profile.get("avatar_url", "")}
            display["leader"] = {k: profiles.get(str(event["leaderId"]), {}).get(k, "") for k in ("name", "avatar_url")}
            display["leader"]["name"] = display["leader"]["name"] or event.get("leaderName") or "Former member"
            display["channel_name"] = next((c["name"] for c in channels if c["id"] == event["channelId"]), "Unavailable channel")
            display["choices"] = raids.choices(event)
            result.append({"id": row["id"], "event": display, "can_manage": can_manage,
                           "historical": bool(row.get("historical")), "revision": row["revision"],
                           "delivery": row.get("delivery", "archived")})
        create_channels = []
        for channel in channels:
            perms = effective_permissions(cfg.guild_id, roles, member, channel)
            if channel.get("type") in {0, 5} and perms & (8 | 32) and perms & (1 << 10) and perms & (1 << 11):
                create_channels.append({"id": channel["id"], "name": channel["name"]})
        with store.connection() as db:
            preferences = {r["template"]: json.loads(r["choice"]) for r in db.execute(
                "SELECT template,choice FROM raid_preferences WHERE guild=? AND user=?", (cfg.guild_id, session["user"]))}
            denials = {p["request"]: p["reason"] for r in db.execute(
                "SELECT payload FROM events WHERE guild=? AND subject=? AND kind='RAID_REQUEST_REJECTED' ORDER BY seq DESC LIMIT 100",
                (cfg.guild_id, session["user"])) if (p := json.loads(r[0]))}
        return {"csrf": session["csrf"], "member": display_profile(cfg.guild_id, member["user"], member),
                "preferences": preferences,
                "events": sorted(result, key=lambda r: r["event"]["startTime"], reverse=True),
                "channels": create_channels, "enabled": enabled(),
                "requests": [{"state": j["state"], "created": j["created"], "reason": denials.get(j["id"], "")} for j in store.jobs(cfg.guild_id)
                             if j["actor"] == session["user"] and j["kind"] == "raid"][:5]}

    @app.get("/api/raids/{event_id}/calendar.ics")
    async def calendar_download(event_id: str, request: Request):
        session, member, roles, channels, snapshot = await context(request)
        if event_id.startswith("history-"):
            row = next((r for r in history(store, cfg.guild_id) if r["id"] == event_id), None)
            if not row:
                raise HTTPException(404, "Event unavailable")
        else:
            row = raids.read(store, cfg.guild_id, event_id)
        await raids.authorize(cfg, store, snapshot, session["user"], row["body"])
        return Response(calendar(event_id,row["body"]), media_type="text/calendar",
                        headers={"Content-Disposition":'attachment; filename="raid-event.ics"'})

    @app.post("/api/raids")
    async def change(request: Request):
        if not enabled() or not cfg.enforce:
            raise HTTPException(503, "Raid changes are not enabled")
        session, member, roles, channels, snapshot = await context(request, True)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Invalid raid request")
        operation = body.get("operation")
        actor = session["user"]
        if operation == "create":
            start = parse_start(body.get("when"))
            event = {**template(store, cfg.guild_id, body.get("template", "standard")),
                     "title": body.get("title", ""), "description": body.get("description", ""),
                     "startTime": start, "closingTime": start, "state": "open", "leaderId": actor,
                     "channelId": str(body.get("channel", ""))}
            raids.validate_event(event)
            await raids.authorize(cfg, store, snapshot, actor, event, create=True)
            job_body = {"operation": operation, "channel": event["channelId"], "title": event["title"],
                        "description": event["description"], "template": event["templateId"], "when": body["when"]}
        else:
            if operation not in {"signup", "withdraw", "status", "note", "edit", "close", "open", "cancel"}:
                raise HTTPException(400, "Unsupported raid action")
            row = raids.read(store, cfg.guild_id, str(body.get("raid_id", "")))
            await raids.authorize(cfg, store, snapshot, actor, row["body"], manage=operation in {"edit", "close", "open", "cancel"})
            job_body = {"operation": operation, "raid_id": row["id"], "value": body.get("value", "")}
            if operation in {"edit", "close", "open", "cancel"}:
                if body.get("revision") != row["revision"]:
                    raise HTTPException(409, "The event changed; refresh before editing")
                job_body["revision"] = row["revision"]
        request_id = body.get("request_id", "")
        if not isinstance(request_id, str) or len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            raise HTTPException(400, "A unique request ID is required")
        store.queue("raid-web-" + actor + "-" + request_id, cfg.guild_id, actor, "raid", actor, job_body)
        return {"queued": True}
