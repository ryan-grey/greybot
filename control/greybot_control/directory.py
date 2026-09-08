"""Cached display names only; never used for permission decisions."""
import asyncio
import json
import time
from discord import AuditLogAction, Permissions

from .discord_api import Denied, Unavailable
from .profiles import display_profile
from .store import canonical


class Directory:
    def __init__(self, cfg, store, api):
        self.cfg, self.store, self.api = cfg, store, api
        self.lock = asyncio.Lock()
        self.retry_after = 0

    def saved(self):
        with self.store.connection() as db:
            row = db.execute("SELECT body,checked FROM display_directory WHERE guild=?", (self.cfg.guild_id,)).fetchone()
        return (json.loads(row["body"]), row["checked"]) if row else ({"channels": [], "roles": [], "members": [], "server": {"id": self.cfg.guild_id, "name": "Server"}}, 0)

    async def get(self):
        async with self.lock:
            data, checked = self.saved()
            data["permission_names"] = {str(flag): name.replace("_", " ") for name, flag in Permissions.VALID_FLAGS.items()}
            if time.time() - checked < 300 and all("bot" in m and "roles" in m for m in data["members"] if m.get("active")):
                return data
            if time.time() < self.retry_after:
                return {**data, "stale": True}
            try:
                fresh = await asyncio.wait_for(self.fetch(), timeout=12)
            except (Denied, Unavailable, TimeoutError):
                self.retry_after = time.time() + 30
                return {**data, "stale": True}
            # Keep last-known labels for deleted channels/roles and departed users.
            for kind in ("channels", "roles", "members"):
                current = {item["id"] for item in fresh[kind]}
                fresh[kind].extend({**item, "active": False} for item in data[kind] if item["id"] not in current)
            with self.store.connection() as db:
                known_members = {item["id"] for item in fresh["members"]}
                for row in db.execute("SELECT user,body FROM member_profiles WHERE guild=?", (self.cfg.guild_id,)):
                    if row["user"] not in known_members:
                        fresh["members"].append({**json.loads(row["body"]), "active": False})
                db.execute("INSERT INTO display_directory VALUES(?,?,?) ON CONFLICT(guild) DO UPDATE SET body=excluded.body,checked=excluded.checked",
                           (self.cfg.guild_id, canonical(fresh), time.time()))
                for profile in fresh["members"]:
                    if profile["active"]:
                        db.execute("INSERT INTO member_profiles VALUES(?,?,?,?) ON CONFLICT(guild,user) DO UPDATE SET body=excluded.body,checked=excluded.checked",
                                   (self.cfg.guild_id, profile["id"], canonical(profile), time.time()))
            return fresh

    async def fetch(self):
        base = f"/guilds/{self.cfg.guild_id}"
        guild, channels, roles = await asyncio.gather(*(self.api.request("GET", base + suffix) for suffix in ("", "/channels", "/roles")))
        if not isinstance(guild, dict) or not isinstance(channels, list) or not isinstance(roles, list):
            raise Unavailable("Server directory unavailable")
        members, after = [], "0"
        for _ in range(100):
            page = await self.api.request("GET", base + f"/members?limit=1000&after={after}")
            if not isinstance(page, list):
                raise Unavailable("Member directory unavailable")
            members.extend({**display_profile(self.cfg.guild_id, m["user"], m), "active": True,
                            "bot": bool(m["user"].get("bot")), "roles": m.get("roles", [])} for m in page)
            if len(page) < 1000:
                break
            next_after = str(max(int(m["user"]["id"]) for m in page))
            if int(next_after) <= int(after):
                raise Unavailable("Member directory did not advance")
            after = next_after
        else:
            raise Unavailable("Member directory exceeds page limit")
        return {"server": {"id": guild["id"], "name": guild["name"]},
                "permission_names": {str(flag): name.replace("_", " ") for name, flag in Permissions.VALID_FLAGS.items()},
                "audit_actions": {str(action.value): action.name.replace("_", " ").capitalize() for action in AuditLogAction},
                "channels": [{"id": c["id"], "name": c["name"], "type": c["type"], "parent_id": c.get("parent_id"), "active": True} for c in channels],
                "roles": [{"id": r["id"], "name": r["name"], "active": True} for r in roles],
                "members": members, "stale": False}
