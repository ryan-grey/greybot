"""Private display profiles, separate from the immutable event journal."""
import re
import time
import asyncio

from .discord_api import Denied, Unavailable


def display_profile(guild, user, member=None):
    member = member or {}
    uid = str(user["id"])
    name = member.get("nick") or user.get("global_name") or user.get("username") or "Unknown member"
    avatar = member.get("avatar") or user.get("avatar")
    if avatar and re.fullmatch(r"(?:a_)?[a-f0-9]{32}", avatar):
        base = f"guilds/{guild}/users/{uid}/avatars" if member.get("avatar") else f"avatars/{uid}"
        url = f"https://cdn.discordapp.com/{base}/{avatar}.png?size=64"
    else:
        discriminator = user.get("discriminator", "0")
        index = int(discriminator) % 5 if discriminator != "0" else (int(uid) >> 22) % 6
        url = f"https://cdn.discordapp.com/embed/avatars/{index}.png"
    return {"id": uid, "name": name, "username": user.get("username", ""), "avatar_url": url}


async def enrich(rows, cfg, store, api, field="subject"):
    profiles = {}
    limit = asyncio.Semaphore(4)

    def member_id(row):
        if row.get("kind") == "GUILD_AUDIT_LOG_ENTRY_CREATE":
            import json
            action = json.loads(row["payload"]).get("action_type")
            if action not in {20, 22, 23, 24, 25, 26, 27, 28, 72}:
                return ""
        return str(row.get(field) or "")

    async def resolve(uid, cached):
        try:
            async with limit:
                member = await api.request("GET", f"/guilds/{cfg.guild_id}/members/{uid}")
            if not isinstance(member, dict) or not member.get("user"):
                raise Unavailable("Member profile unavailable")
            profile = display_profile(cfg.guild_id, member["user"], member)
        except (Denied, Unavailable):
            profile = dict(profiles[uid])
            profile["last_known"] = True
        store.save_profile(cfg.guild_id, uid, profile)
        profiles[uid] = profile

    tasks = []
    # Resolve once per page; persistent cache avoids per-event Discord calls.
    for uid in dict.fromkeys(member_id(row) for row in rows):
        if not uid.isdecimal():
            continue
        cached = store.profile(cfg.guild_id, uid)
        profiles[uid] = dict(cached["value"]) if cached else {"id": uid, "name": "Unknown member", "avatar_url": ""}
        if cached and time.time() - cached["checked"] < 3600:
            profiles[uid] = cached["value"]
            continue
        profiles[uid]["last_known"] = True
        tasks.append(asyncio.create_task(resolve(uid, cached)))
    if tasks:
        try:
            # A Discord outage must not block reading the audit journal.
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=4)
        except TimeoutError:
            pass
    return [{**row, "display_member": profiles.get(member_id(row))} for row in rows]
