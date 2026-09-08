"""Persistent communication restrictions, released only by an administrator.

Member overrides take precedence over role allows. There is no expiry timer.
Only owned permission bits are restored; unrelated admin changes survive.
"""
import asyncio
import json
import secrets
import time

from .discord_api import ADMINISTRATOR, Denied, Unavailable
from .store import canonical

# Text, thread creation/replies, reactions, voice connections/speech/streaming.
MUTE_MASK = sum(1 << bit for bit in (6, 9, 11, 12, 20, 21, 31, 35, 36, 38, 46, 49))
MANAGE_PERMISSIONS = 1 << 28


def effective_permissions(guild, roles, member, channel):
    held = set(member.get("roles", [])) | {guild}
    value = 0
    for role in roles:
        if role["id"] in held:
            value |= int(role["permissions"])
    if value & ADMINISTRATOR:
        return (1 << 64) - 1
    overwrites = channel.get("permission_overwrites", [])
    for group in ([o for o in overwrites if o["id"] == guild],
                  [o for o in overwrites if o["type"] == 0 and o["id"] in held and o["id"] != guild],
                  [o for o in overwrites if o["type"] == 1 and o["id"] == member["user"]["id"]]):
        allow = deny = 0
        for item in group:
            allow |= int(item["allow"]); deny |= int(item["deny"])
        value = (value & ~deny) | allow
    return value


def overwrite(channel, user):
    return next((o for o in channel.get("permission_overwrites", []) if o["type"] == 1 and o["id"] == user), None)


def restrict(prior):
    return {"type": 1, "allow": str(int((prior or {}).get("allow", 0)) & ~MUTE_MASK),
            "deny": str(int((prior or {}).get("deny", 0)) | MUTE_MASK)}


def restore(current, prior):
    """Reject conflicting edits to owned bits instead of overwriting an admin."""
    current = current or {"allow": "0", "deny": "0"}
    before = prior or {"allow": "0", "deny": "0"}
    if all((int(current[k]) & MUTE_MASK) == (int(before[k]) & MUTE_MASK) for k in ("allow", "deny")):
        return current  # Already restored, including after a lost response.
    if int(current["allow"]) & MUTE_MASK or int(current["deny"]) & MUTE_MASK != MUTE_MASK:
        raise Denied("Mute permissions were changed outside greyBot; review that channel before release")
    return {"type": 1, **{k: str((int(current[k]) & ~MUTE_MASK) | (int(before[k]) & MUTE_MASK)) for k in ("allow", "deny")}}


class Mutes:
    def __init__(self, cfg, store, api, archive):
        self.cfg, self.store, self.api, self.archive = cfg, store, api, archive

    def records(self):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM persistent_mutes WHERE guild=? AND state!='released' ORDER BY created DESC", (self.cfg.guild_id,))]

    def record(self, user):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM persistent_mutes WHERE guild=? AND subject=?", (self.cfg.guild_id, user)).fetchone()
            return dict(row) if row else None

    async def channels(self):
        channels = await self.api.request("GET", f"/guilds/{self.cfg.guild_id}/channels")
        roles = await self.api.request("GET", f"/guilds/{self.cfg.guild_id}/roles")
        bot = await self.api.request("GET", f"/guilds/{self.cfg.guild_id}/members/{self.cfg.client_id}")
        for channel in channels:
            permissions = effective_permissions(self.cfg.guild_id, roles, bot, channel)
            # Manage Permissions on this channel permits changing arbitrary bits.
            if not permissions & MANAGE_PERMISSIONS:
                raise Denied("greyBot needs Manage Permissions in #" + channel["name"] + " before persistent mutes can run")
            if len(channel.get("permission_overwrites", [])) >= 1000:
                raise Denied("A channel permission list is full")
        return channels

    async def archive_first(self):
        if not self.archive:
            raise Unavailable("Mute changes require an archive")
        await asyncio.to_thread(self.archive.flush, self.store)
        if self.store.pending(1):
            raise Unavailable("Waiting for mute audit archive")

    async def apply(self, actor, user, reason):
        await self.api.authorize_moderation(actor, user, "mute")
        channels = await self.channels()  # Preflight all channels before any write.
        previous = self.record(user)
        if previous and previous["state"] not in {"released", "active", "applying", "needs_review"}:
            raise Denied("Resolve the existing mute release before muting again")
        plan = json.loads(previous["plan"]) if previous and previous["state"] != "released" else {}
        if previous and previous["state"] == "active" and all(c["id"] in plan and overwrite(c, user) and
                all(str(overwrite(c, user)[k]) == restrict(overwrite(c, user))[k] for k in ("allow", "deny")) for c in channels):
            return
        for channel in channels:
            plan.setdefault(channel["id"], {"before": overwrite(channel, user), "name": channel["name"]})
        mute_id = previous["id"] if previous and previous["state"] != "released" else secrets.token_hex(16)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO persistent_mutes VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(guild,subject) DO UPDATE SET id=excluded.id,actor=excluded.actor,state=excluded.state,plan=excluded.plan,reason=excluded.reason",
                       (self.cfg.guild_id, user, mute_id, actor, "applying", canonical(plan), reason, time.time()))
            self.store._append(db, "mute-plan:" + secrets.token_hex(16), self.cfg.guild_id, "MUTE_PREPARED", user,
                               {"actor": actor, "channels": list(plan), "expiry": "Until an administrator revokes it"})
        await self.archive_first()
        for channel in channels:
            current = overwrite(channel, user)
            desired = restrict(current)
            if current and all(str(current[k]) == desired[k] for k in ("allow", "deny")):
                continue
            await self.api.request("PUT", f"/channels/{channel['id']}/permissions/{user}", body=desired,
                                   reason="greyBot persistent mute: " + reason)
        self.finish(user, "active", "MUTE_APPLIED", actor)

    def finish(self, user, state, event, actor):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE persistent_mutes SET state=? WHERE guild=? AND subject=?", (state, self.cfg.guild_id, user))
            self.store._append(db, "mute-state:" + secrets.token_hex(16), self.cfg.guild_id, event, user, {"actor": actor, "state": state})

    async def release(self, actor, user, reason):
        await self.api.require_admin(actor)
        record = self.record(user)
        if not record or record["state"] == "released":
            raise Denied("No persistent mute is recorded for this member")
        channels = {c["id"]: c for c in await self.channels()}
        plan = json.loads(record["plan"])
        desired = {cid: restore(overwrite(channels[cid], user), entry["before"]) for cid, entry in plan.items() if cid in channels}
        self.finish(user, "releasing", "MUTE_RELEASE_REQUESTED", actor)
        await self.archive_first()
        for cid, body in desired.items():
            current = overwrite(channels[cid], user)
            if current and all(str(current[k]) == str(body[k]) for k in ("allow", "deny")):
                continue
            if not plan[cid]["before"] and not int(body["allow"]) and not int(body["deny"]):
                if current:
                    await self.api.request("DELETE", f"/channels/{cid}/permissions/{user}", reason="greyBot mute revoked: " + reason)
            else:
                await self.api.request("PUT", f"/channels/{cid}/permissions/{user}", body={"type": 1, "allow": body["allow"], "deny": body["deny"]}, reason="greyBot mute revoked: " + reason)
        self.finish(user, "released", "MUTE_RELEASED", actor)

    async def reconcile(self):
        for row in self.records():
            try:
                if row["state"] in {"active", "applying", "needs_review"}:
                    await self.apply(row["actor"], row["subject"], row["reason"])
            except (Denied, Unavailable):
                if row["state"] != "needs_review":
                    self.finish(row["subject"], "needs_review", "MUTE_NEEDS_REVIEW", row["actor"])
