"""Administrator review, observed activity and host health; guild scoped."""
import asyncio
import json
import time
from pathlib import Path
from .mutes import effective_permissions
from .onboarding_gate import VIEW
from .store import canonical, digest


async def server_snapshot(cfg, api):
    base = f"/guilds/{cfg.guild_id}"
    guild, roles, channels = await asyncio.gather(*(api.request("GET", base + suffix)
                                                  for suffix in ("", "/roles", "/channels")))
    members, after = [], "0"
    for _ in range(100):
        page = await api.request("GET", base + f"/members?limit=1000&after={after}")
        members.extend(page)
        if len(page) < 1000:
            return guild, roles, channels, members
        after = str(max(int(m["user"]["id"]) for m in page))
    raise ValueError("Member directory exceeds supported size")


def review_items(guild, roles, channels, members):
    items, by_channel = [], {c["id"]: c for c in channels}
    def add(kind, facts):
        items.append({"id": digest(canonical([kind, facts])), "kind": kind, **facts})
    for role in roles:
        if role.get("name", "").casefold().replace("-", "").replace(" ", "") == "cogm":
            add("retire_role", {"role": role["id"], "members": [m["user"]["id"] for m in members if role["id"] in m.get("roles", [])]})
    def overwrites(c):
        return sorted((o["id"], int(o["type"]), int(o["allow"]), int(o["deny"])) for o in c.get("permission_overwrites", []))
    for channel in channels:
        if channel.get("type") in (10, 11, 12):
            continue
        parent = by_channel.get(channel.get("parent_id"))
        if parent and overwrites(channel) != overwrites(parent):
            add("unsynced_channel", {"channel": channel["id"], "category": parent["id"]})
        # A role-specific View Channel allow is evidence of an access boundary,
        # not proof of intent. Flag alternate access for review, never auto-revoke.
        required = {o["id"] for o in channel.get("permission_overwrites", [])
                    if o["type"] == 0 and o["id"] != guild["id"] and int(o["allow"]) & VIEW}
        if not required or channel.get("type") == 4:
            continue
        for member in members:
            uid = member["user"]["id"]
            if member["user"].get("bot") or uid == guild["owner_id"]:
                continue
            held = set(member.get("roles", []))
            if held & required:
                continue
            perms = effective_permissions(guild["id"], roles, member, channel)
            if perms & VIEW and not perms & 8:
                direct = any(o["type"] == 1 and o["id"] == uid and int(o["allow"]) & VIEW
                             for o in channel.get("permission_overwrites", []))
                add("alternate_access", {"member": uid, "channel": channel["id"],
                    "roles": sorted(required), "source": "Member override" if direct else "Other role or server permissions"})
    return items


def scoreboard(store, guild, directory):
    people = {m["id"]: m for m in directory["members"] if m.get("active") and m.get("bot") is False}
    scores = {uid: {"member": uid, "messages": 0, "reactions": 0, "voice_seconds": 0} for uid in people}
    with store.connection() as db:
        # Includes imported guild messages once, including captured deletions.
        for row in db.execute("SELECT author,COUNT(*) n FROM messages WHERE guild=? GROUP BY author", (guild,)):
            if row["author"] in scores:
                scores[row["author"]]["messages"] = row["n"]
        rows = db.execute("SELECT kind,subject,observed,payload FROM events WHERE guild=? AND kind IN "
                          "('MESSAGE_REACTION_ADD','VOICE_STATE_UPDATE','COLLECTOR_CONNECTED','COLLECTOR_DISCONNECTED','HOST_HEALTH_GAP') ORDER BY seq", (guild,)).fetchall()
        first = db.execute("SELECT MIN(observed) FROM events WHERE guild=?", (guild,)).fetchone()[0]
    voice = {}
    for row in rows:
        if row["kind"] in ("COLLECTOR_CONNECTED", "COLLECTOR_DISCONNECTED", "HOST_HEALTH_GAP"):
            voice.clear()  # Never invent voice time across observation gaps.
            continue
        uid = row["subject"]
        if uid not in scores:
            continue
        if row["kind"] == "MESSAGE_REACTION_ADD":
            scores[uid]["reactions"] += 1
        else:
            p = json.loads(row["payload"])
            if uid in voice:
                scores[uid]["voice_seconds"] += max(0, row["observed"] - voice[uid])
            if p.get("channel_id"):
                voice[uid] = row["observed"]
            else:
                voice.pop(uid, None)
    for score in scores.values():
        score["voice_seconds"] = int(score["voice_seconds"])
        score["score"] = score["messages"] + score["reactions"] + score["voice_seconds"] // 60
    return {"since": first, "rows": sorted(scores.values(), key=lambda s: (-s["score"], people[s["member"]]["name"].casefold())),
            "method": "1 point per captured message, reaction added, or observed voice minute; bots excluded. Imported messages included; unobserved history and voice gaps are not estimated."}


def health_tick(store, guild, now=None, uptime=None):
    now = time.time() if now is None else now
    if uptime is None:
        try:
            uptime = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError):
            uptime = None
    boot = now - uptime if uptime is not None else None
    with store.connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS host_health (guild TEXT PRIMARY KEY, first REAL, last REAL, boot REAL)")
        previous = db.execute("SELECT * FROM host_health WHERE guild=?", (guild,)).fetchone()
    if previous and (now - previous["last"] > 90 or (boot and previous["boot"] and abs(boot - previous["boot"]) > 10)):
        store.append("host-gap:" + str(previous["last"]), guild, "HOST_HEALTH_GAP", "", {
            "last_seen": previous["last"], "recovered": now, "boot": boot,
            "restart_confirmed": bool(boot and previous["boot"] and abs(boot - previous["boot"]) > 10)})
    with store.connection() as db:
        db.execute("INSERT INTO host_health VALUES(?,?,?,?) ON CONFLICT(guild) DO UPDATE SET last=excluded.last,boot=excluded.boot",
                   (guild, now, now, boot))


def health_status(store, guild):
    with store.connection() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='host_health'").fetchone()
        row = db.execute("SELECT * FROM host_health WHERE guild=?", (guild,)).fetchone() if exists else None
        connections = db.execute("SELECT kind,observed FROM events WHERE guild=? AND kind IN ('COLLECTOR_CONNECTED','COLLECTOR_RESUMED','COLLECTOR_DISCONNECTED') ORDER BY seq", (guild,)).fetchall()
    gaps = [{**json.loads(r["payload"]), "observed": r["observed"]} for r in store.events(guild, kind="HOST_HEALTH_GAP")]
    disconnected = None
    for event in connections:
        if event["kind"] == "COLLECTOR_DISCONNECTED":
            if disconnected is None:
                disconnected = event["observed"]
        elif disconnected is not None:
            gaps.append({"last_seen": disconnected, "recovered": event["observed"], "connection": True})
            disconnected = None
    if disconnected is not None:
        gaps.append({"last_seen": disconnected, "recovered": None, "connection": True})
    return {"host": "greyNAS", "purpose": "Hosts greyBot's server and private audit archive",
            "status": dict(row) if row else None,
            "gaps": sorted(gaps, key=lambda g: g["last_seen"], reverse=True)[:100],
            "note": "Sampled every 30 seconds; Discord disconnects and recovery are also recorded. Latest 100 gaps shown; full records remain in Event history. Gap times bound observations, not exact power or network failure times. No earlier outages are inferred."}
