"""Live voice activity for server members: who is in voice and for how long, from the journal.

Members only. The same figures exist on the admin scoreboard; this page opens them to the people they describe,
not to the internet. The server's AFK channel never counts.
"""
import json
import time

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from .discord_api import Denied

CACHE_SECONDS = 300
GAPS = ("COLLECTOR_CONNECTED", "COLLECTOR_DISCONNECTED", "HOST_HEALTH_GAP")


def compute(rows, afk, now):
    """Per-member stays from ordered journal rows of (kind, subject, observed, payload).

    An observation gap ends every open stay where the gap began: time is never invented across an outage, and a
    member already in voice when the collector returns is not counted until they next join, leave or switch.
    """
    current, people, gaps = {}, {}, 0

    def close(uid, until):
        channel, since = current.pop(uid)
        seconds = max(0.0, until - since)
        person = people.setdefault(uid, {"seconds": 0.0, "afk_seconds": 0.0, "stays": 0, "longest": 0.0, "channels": {}, "last": 0.0})
        if channel in afk:
            person["afk_seconds"] += seconds
            return
        person["seconds"] += seconds
        person["channels"][channel] = person["channels"].get(channel, 0.0) + seconds
        person["last"] = max(person["last"], until)
        if seconds >= 60:
            person["stays"] += 1
            person["longest"] = max(person["longest"], seconds)

    for kind, subject, observed, payload in rows:
        if kind in GAPS:
            gaps += 1
            for uid in list(current):
                close(uid, observed)
            continue
        channel = json.loads(payload or "{}").get("channel_id")
        if subject in current and current[subject][0] != channel:
            close(subject, observed)
        if channel and subject not in current:
            current[subject] = (channel, observed)
    live = {uid: channel for uid, (channel, _) in current.items() if channel not in afk}
    for uid in list(current):
        close(uid, now)
    return people, live, gaps


def report(store, guild, afk, directory, now=None):
    now = now or time.time()
    with store.connection() as db:
        rows = db.execute("SELECT kind,subject,observed,payload FROM events WHERE guild=? AND kind IN "
                          "('VOICE_STATE_UPDATE','COLLECTOR_CONNECTED','COLLECTOR_DISCONNECTED','HOST_HEALTH_GAP') ORDER BY seq", (guild,)).fetchall()
    people, live, gaps = compute([tuple(r) for r in rows], afk, now)
    members = {m["id"]: m for m in directory["members"]}
    channels = {c["id"]: c["name"] for c in directory["channels"]}
    ranked, busiest = [], {}
    for uid, person in people.items():
        profile = members.get(uid)
        if not profile or profile.get("bot") or person["seconds"] < 60:
            continue
        for channel, seconds in person["channels"].items():
            busiest[channel] = busiest.get(channel, 0.0) + seconds
        top = sorted(person["channels"].items(), key=lambda item: -item[1])[:2]
        ranked.append({"name": profile["name"], "avatar_url": profile.get("avatar_url", ""), "active": bool(profile.get("active")),
                       "seconds": int(person["seconds"]), "stays": person["stays"], "longest": int(person["longest"]),
                       "afk_seconds": int(person["afk_seconds"]), "last": int(person["last"]),
                       "in_voice": channels.get(live[uid], "a voice channel") if uid in live else "",
                       "top": [{"channel": channels.get(c, "deleted channel"), "seconds": int(s)} for c, s in top]})
    ranked.sort(key=lambda row: (-row["seconds"], row["name"].casefold()))
    return {"since": int(rows[0]["observed"]) if rows else int(now), "updated": int(now), "gaps": gaps, "refresh_seconds": CACHE_SECONDS,
            "afk": [channels[c] for c in afk if c in channels], "total_seconds": sum(r["seconds"] for r in ranked),
            "in_voice_now": sum(1 for r in ranked if r["in_voice"]),
            "busiest": [{"channel": channels.get(c, "deleted channel"), "seconds": int(s)}
                        for c, s in sorted(busiest.items(), key=lambda item: -item[1])[:6]],
            "rows": ranked}


def install(app, cfg, store, api, cookie, static, directory):
    cache = {"at": 0.0, "body": None}

    async def member(request):
        session = store.get_session(request.cookies.get(cookie + "-activity", "") or request.cookies.get(cookie, ""))
        if not session:
            raise HTTPException(401, "Sign in with Discord to see voice activity")
        profile = await api.request("GET", f"/guilds/{cfg.guild_id}/members/{session['user']}")
        if profile.get("pending") or profile.get("user", {}).get("bot"):
            raise Denied("Current human membership is required")
        return session

    @app.get("/activity")
    async def page():
        return FileResponse(static / "activity.html")

    @app.get("/api/activity")
    async def activity(request: Request):
        await member(request)
        if not cache["body"] or time.time() - cache["at"] >= CACHE_SECONDS:
            guild = await api.request("GET", f"/guilds/{cfg.guild_id}")
            afk = {guild["afk_channel_id"]} if guild.get("afk_channel_id") else set()
            cache["body"] = {"server": guild["name"], **report(store, cfg.guild_id, afk, await directory.get())}
            cache["at"] = time.time()
        return cache["body"]
