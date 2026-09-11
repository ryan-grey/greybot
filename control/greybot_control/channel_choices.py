"""Member channel visibility: remove access only, never create a View allow."""
import json
from copy import deepcopy
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from .discord_api import Denied
from .mutes import effective_permissions, overwrite
from .insights import server_snapshot
from .profiles import display_profile
from .onboarding_gate import VIEW


def hidden_channels(store, guild, user):
    with store.connection() as db:
        rows = db.execute("SELECT payload FROM events WHERE guild=? AND subject=? AND kind='CHANNEL_VISIBILITY_APPLIED' ORDER BY seq", (guild, user)).fetchall()
    return {p["channel"]: p["hidden"] for r in rows if (p := json.loads(r[0]))}


def choices(guild, roles, channels, member, owned, protected_channels=(), verified_role=""):
    base = next((r for r in roles if r["id"] == verified_role), None)
    held = [r for r in roles if r["id"] in member.get("roles", [])]
    if (not base or member.get("pending") or member["user"].get("bot") or not
            (base["id"] in member.get("roles", []) or any(r["position"] > base["position"] for r in held))):
        raise Denied("Verified server membership is required")
    # Administrator bypass makes member visibility denies ineffective.
    if member["user"]["id"] == guild["owner_id"] or any(int(r["permissions"]) & 8 for r in held):
        return []
    result = []
    for channel in channels:
        if channel.get("type") not in (0, 2, 5, 13) or channel["id"] == guild.get("rules_channel_id") or channel["id"] in protected_channels:
            continue
        current = overwrite(channel, member["user"]["id"])
        prior_hidden = bool(owned.get(channel["id"]))
        base_channel = deepcopy(channel)
        if current and (int(current["allow"]) | int(current["deny"])) & VIEW:
            if not prior_hidden or int(current["allow"]) & VIEW or not int(current["deny"]) & VIEW:
                continue  # Administrator-owned visibility overrides are not ours.
            for entry in base_channel["permission_overwrites"]:
                if entry["id"] == member["user"]["id"] and entry["type"] == 1:
                    entry["deny"] = str(int(entry["deny"]) & ~VIEW)
        if effective_permissions(guild["id"], roles, member, base_channel) & VIEW:
            result.append({"id": channel["id"], "name": channel["name"], "type": channel["type"], "hidden": prior_hidden})
    return result


async def execute(cfg, store, api, job):
    if job["actor"] != job["subject"]:
        raise Denied("Only your own channel preferences can be changed")
    body = json.loads(job["body"])
    guild, roles, channels, members = await server_snapshot(cfg, api)
    member = next((m for m in members if m["user"]["id"] == job["subject"]), None)
    if not member:
        raise Denied("Membership ended")
    owned = hidden_channels(store, cfg.guild_id, job["subject"])
    protected = (*cfg.public_channel_ids, cfg.start_channel_id, store.settings(cfg.guild_id)["values"].get("welcome_channel", ""))
    verified_role = store.settings(cfg.guild_id)["values"].get("verification_role", "")
    allowed = choices(guild, roles, channels, member, owned, protected, verified_role)
    if body["channel"] not in {c["id"] for c in allowed}:
        raise Denied("Channel is no longer eligible")
    channel = await api.request("GET", f'/channels/{body["channel"]}')
    # Fresh channel check immediately before changing only the View deny bit.
    if not choices(guild, roles, [channel], member, owned, protected, verified_role):
        raise Denied("Channel access changed")
    current = overwrite(channel, job["subject"]) or {"allow": "0", "deny": "0"}
    deny = int(current["deny"])
    after = {"type": 1, "allow": current["allow"], "deny": str(deny | VIEW if body["hidden"] else deny & ~VIEW)}
    await api.request("PUT", f'/channels/{channel["id"]}/permissions/{job["subject"]}', body=after,
                      reason="Member chose their own channel visibility through greyBot")
    check = overwrite(await api.request("GET", f'/channels/{channel["id"]}'), job["subject"])
    if not check or any(str(check[k]) != str(after[k]) for k in ("allow", "deny")):
        raise Denied("Channel preference was not confirmed")
    store.append("visibility:" + job["id"], cfg.guild_id, "CHANNEL_VISIBILITY_APPLIED", job["subject"], body)


def install(app, cfg, store, api, cookie, static):
    async def context(request, write=False):
        import secrets
        session = store.get_session(request.cookies.get(cookie + "-channels", ""))
        if not session:
            raise HTTPException(401, "Sign in to choose channels")
        if write and (request.headers.get("origin") != cfg.origin or not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"])):
            raise HTTPException(403, "Invalid origin or CSRF token")
        guild, roles, channels, members = await server_snapshot(cfg, api)
        member = next((m for m in members if m["user"]["id"] == session["user"]), None)
        if not member:
            raise Denied("Server membership is required")
        protected = (*cfg.public_channel_ids, cfg.start_channel_id, store.settings(cfg.guild_id)["values"].get("welcome_channel", ""))
        verified_role = store.settings(cfg.guild_id)["values"].get("verification_role", "")
        allowed = choices(guild, roles, channels, member, hidden_channels(store, cfg.guild_id, session["user"]), protected, verified_role)
        return session, member, allowed

    @app.get("/channels")
    async def page():
        return FileResponse(static / "channels.html")

    @app.get("/api/channel-choices")
    async def get_choices(request: Request):
        session, member, allowed = await context(request)
        return {"csrf": session["csrf"], "member": display_profile(cfg.guild_id, member["user"], member), "channels": allowed}

    @app.post("/api/channel-choices")
    async def set_choices(request: Request):
        session, member, allowed = await context(request, True)
        if not cfg.enforce or not (cfg.archive_dir or cfg.archive_bucket):
            raise HTTPException(409, "Channel preference worker is unavailable")
        body = await request.json()
        if not isinstance(body, dict) or type(body.get("hidden")) is not bool or body.get("channel") not in {c["id"] for c in allowed}:
            raise HTTPException(400, "Choose an available channel")
        request_id = body.get("request_id", "")
        if not isinstance(request_id, str) or not 16 <= len(request_id) <= 64 or not request_id.replace("-", "").isalnum():
            raise HTTPException(400, "Invalid request ID")
        return store.queue("visibility-" + request_id, cfg.guild_id, session["user"], "channel_visibility", session["user"],
                           {"channel": body["channel"], "hidden": body["hidden"]})
