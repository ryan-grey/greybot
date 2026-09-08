"""Allowlisted self-service roles, applied by the archived worker."""
import json
import time

from .discord_api import Denied, ADMINISTRATOR

# Never offer roles carrying moderation or server management access.
PRIVILEGED = sum(1 << bit for bit in (1, 2, 3, 4, 5, 7, 13, 28, 29, 30, 40))


async def validate(cfg, api, selected):
    if not selected or len(selected) > 3 or len(set(selected)) != len(selected):
        raise Denied("Choose one to three distinct self-service roles")
    roles = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
    bot = await api.member_context(cfg.client_id)
    if not bot["permissions"] & (ADMINISTRATOR | (1 << 28)):
        raise Denied("greyBot needs Manage Roles")
    found = {r["id"]: r for r in roles}
    for rid in selected:
        role = found.get(rid)
        if (not role or rid == cfg.guild_id or role.get("managed")
                or int(role["permissions"]) & PRIVILEGED
                or role["position"] >= bot["position"]):
            raise Denied("Choose ordinary roles below greyBot's role")
    return [found[rid] for rid in selected]


async def execute(cfg, store, api, job):
    body = json.loads(job["body"])
    settings = store.settings(cfg.guild_id)["values"]
    selected = settings["self_roles"]
    if settings.get("verification_role") in selected:
        raise Denied("The verified membership role cannot be self-assigned")
    roles = await validate(cfg, api, selected)
    if job["kind"] == "role_panel":
        await api.require_admin(job["actor"])
        from .feed_dispatch import preflight
        await preflight(cfg, api, body["channel"])
        result = await api.request("POST", f'/channels/{body["channel"]}/messages', body={
            "embeds": [{"title": "Choose your roles", "description": "Use a button to add or remove that role. You can choose more than one.",
                        "color": 0x4493F8, "author": {"name": "greyBot", "url": cfg.origin + "/about"}}],
            "components": [{"type": 1, "components": [{"type": 2, "style": 2,
                "label": r["name"][:80], "custom_id": "greybot:role:" + r["id"]} for r in roles]}],
            "allowed_mentions": {"parse": []}, "nonce": job["id"][:25], "enforce_nonce": True})
        store.append("role-panel:" + job["id"], cfg.guild_id, "ROLE_PANEL_PUBLISHED", "",
                     {"channel_id": body["channel"], "message_id": result["id"], "roles": selected})
        return
    if job["kind"] != "self_role" or job["actor"] != job["subject"]:
        raise Denied("Invalid self-service role request")
    if not 0 <= time.time() - job["created"] <= 120 or body["role"] not in selected:
        raise Denied("Role request expired or role was disabled")
    member = await api.request("GET", f'/guilds/{cfg.guild_id}/members/{job["subject"]}')
    if member.get("pending") or member.get("user", {}).get("bot"):
        raise Denied("Finish server screening before selecting roles")
    settings = store.settings(cfg.guild_id)["values"]
    if (settings.get("verification_enabled") or settings.get("verification_role")) and settings.get("verification_role") not in member.get("roles", []):
        raise Denied("Verify your membership before selecting additional roles")
    from .mutes import effective_permissions
    channel = await api.request("GET", f'/channels/{body["channel"]}')
    all_roles = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
    if channel.get("guild_id") != cfg.guild_id or not effective_permissions(cfg.guild_id, all_roles, member, channel) & (1 << 10):
        raise Denied("You no longer have access to this role panel's channel")
    held = body["role"] in member.get("roles", [])
    await api.request("DELETE" if held else "PUT",
        f'/guilds/{cfg.guild_id}/members/{job["subject"]}/roles/{body["role"]}',
        reason="Member selected their own role through greyBot")


def receive(cfg, store, packet):
    """Called only after the HTTP endpoint verifies Discord's signature."""
    user = packet.get("member", {}).get("user", {})
    custom_id = packet.get("data", {}).get("custom_id", "")
    role = custom_id.removeprefix("greybot:role:")
    message = packet.get("message", {})
    if (not custom_id.startswith("greybot:role:") or packet.get("type") != 3 or packet.get("application_id") != cfg.client_id
            or packet.get("guild_id") != cfg.guild_id or not user.get("id")
            or user.get("bot") or packet.get("member", {}).get("pending")
            or role not in store.settings(cfg.guild_id)["values"]["self_roles"]
            or role == store.settings(cfg.guild_id)["values"].get("verification_role")):
        raise Denied("This role panel is unavailable")
    with store.connection() as db:
        rows = db.execute("SELECT payload FROM events WHERE guild=? AND kind='ROLE_PANEL_PUBLISHED'",
                          (cfg.guild_id,)).fetchall()
    if not any(json.loads(r[0]).get("message_id") == message.get("id")
               and json.loads(r[0]).get("channel_id") == packet.get("channel_id") for r in rows):
        raise Denied("Unrecognized role panel")
    store.queue("role-" + packet["id"], cfg.guild_id, user["id"], "self_role", user["id"],
                {"role": role, "channel": packet["channel_id"]})
    return {"type": 4, "data": {"content": "Your role request is queued. Your server roles will update shortly.",
                                "flags": 64, "allowed_mentions": {"parse": []}}}
