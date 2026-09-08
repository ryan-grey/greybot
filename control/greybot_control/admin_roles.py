"""Explicit, idempotent administrator role edits with fresh hierarchy checks."""
import json
from .discord_api import Denied, ADMINISTRATOR


async def authorize_selection(cfg, api, actor, targets, role_id):
    from .insights import server_snapshot
    guild, roles, channels, members = await server_snapshot(cfg, api)
    indexed = {m["user"]["id"]: m for m in members}
    def context(uid):
        member = indexed.get(uid)
        if not member:
            raise Denied("A selected member has left the server")
        held = [r for r in roles if r["id"] == cfg.guild_id or r["id"] in member.get("roles", [])]
        permissions = 0
        for r in held:
            permissions |= int(r["permissions"])
        return {"owner": guild["owner_id"] == uid, "permissions": permissions,
                "position": max(r["position"] for r in held), "member": member}
    admin, bot = context(actor), context(cfg.client_id)
    if not admin["owner"] and not admin["permissions"] & ADMINISTRATOR:
        raise Denied("Current administrator access is required")
    role = next((r for r in roles if r["id"] == role_id), None)
    for target in targets:
        check(cfg, admin, bot, context(target), role)


def check(cfg, admin, bot, member, role):
    if not role or role["id"] == cfg.guild_id or role.get("managed"):
        raise Denied("Choose an assignable role")
    if role["position"] >= bot["position"] or member["position"] >= bot["position"] or member["owner"]:
        raise Denied("Role or member is above greyBot's authority")
    if not admin["owner"] and (role["position"] >= admin["position"] or member["position"] >= admin["position"]):
        raise Denied("Role or member is above your authority")
    if not bot["permissions"] & (ADMINISTRATOR | (1 << 28)):
        raise Denied("greyBot needs Manage Roles")


async def authorize(cfg, api, actor, target, role_id):
    admin = await api.require_admin(actor)
    bot = await api.member_context(cfg.client_id)
    member = await api.member_context(target)
    roles = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
    role = next((r for r in roles if r["id"] == role_id), None)
    check(cfg, admin, bot, member, role)
    return member["member"]


async def execute(cfg, store, api, job):
    body = json.loads(job["body"])
    member = await authorize(cfg, api, job["actor"], job["subject"], body["role"])
    desired = body["operation"] == "add"
    if (body["role"] in member.get("roles", [])) != desired:
        await api.request("PUT" if desired else "DELETE",
            f'/guilds/{cfg.guild_id}/members/{job["subject"]}/roles/{body["role"]}',
            reason=f'greyBot admin {job["actor"]}: {body["operation"]} role')
    current = await api.request("GET", f'/guilds/{cfg.guild_id}/members/{job["subject"]}')
    if (body["role"] in current.get("roles", [])) != desired:
        raise Denied("Discord did not confirm the requested role state")
