"""Member verification without access to the administration workspace."""
import os
import secrets
import time
import json
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from .discord_api import Denied, Unavailable
from .profiles import display_profile
from .role_panels import validate


def configured():
    return bool(os.environ.get("GREYBOT_TURNSTILE_SITE_KEY") and os.environ.get("GREYBOT_TURNSTILE_SECRET"))


def receive(cfg, store, packet):
    """A shared welcome button answers only the member who clicked it."""
    settings = store.settings(cfg.guild_id)["values"]
    user = packet.get("member", {}).get("user", {})
    message = packet.get("message", {})
    if (packet.get("type") != 3 or packet.get("application_id") != cfg.client_id
            or packet.get("guild_id") != cfg.guild_id or not user.get("id") or user.get("bot")
            or packet.get("data", {}).get("custom_id") != "greybot:verify"
            or not settings.get("verification_enabled") or not configured()
            or message.get("author", {}).get("id") != cfg.client_id
            or packet.get("channel_id") != settings.get("welcome_channel")):
        raise Denied("This verification button is unavailable")
    return {"type": 4, "data": {"content": "Accept the server rules in Discord, then continue below to verify you're human. This response is visible only to you.",
        "flags": 64, "allowed_mentions": {"parse": []},
        "components": [{"type": 1, "components": [{"type": 2, "style": 5,
            "label": "Continue verification", "url": cfg.origin + "/verify"}]}]}}


async def validate_challenge(token, user, origin):
    if not configured():
        raise Unavailable("Member verification is not configured")
    if not isinstance(token, str) or not 1 <= len(token) <= 2048:
        raise Denied("Complete the verification challenge")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data={
                "secret": os.environ["GREYBOT_TURNSTILE_SECRET"], "response": token})
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise Unavailable("Verification provider is unavailable") from None
    if (response.status_code != 200 or not isinstance(result, dict) or not result.get("success")
            or result.get("hostname") != urlsplit(origin).hostname
            or result.get("action") != "greybot-verify" or result.get("cdata") != user):
        raise Denied("Verification expired or failed; complete a new challenge")


def install(app, cfg, store, api, cookie, static):
    async def member_session(request, write=False):
        session = store.get_session(request.cookies.get(cookie + "-verify", ""))
        if not session:
            raise HTTPException(401, "Sign in with Discord to verify")
        if write and (request.headers.get("origin") != cfg.origin or not secrets.compare_digest(
                request.headers.get("x-csrf-token", ""), session["csrf"])):
            raise HTTPException(403, "Invalid request origin or CSRF token")
        member = await api.request("GET", f'/guilds/{cfg.guild_id}/members/{session["user"]}')
        if member.get("user", {}).get("bot"):
            raise Denied("Bots cannot use member verification")
        return session, member

    @app.get("/verify")
    async def page():
        return FileResponse(static / "verify.html")

    @app.get("/api/verification")
    async def status(request: Request):
        session, member = await member_session(request)
        values = store.settings(cfg.guild_id)["values"]
        with store.connection() as db:
            row = db.execute("SELECT state FROM jobs WHERE guild=? AND subject=? AND kind='verify_role' ORDER BY created DESC LIMIT 1",
                             (cfg.guild_id, session["user"])).fetchone()
        return {"member": display_profile(cfg.guild_id, member["user"], member), "csrf": session["csrf"],
                "site_key": os.environ.get("GREYBOT_TURNSTILE_SITE_KEY", ""),
                "enabled": values.get("verification_enabled", False) and configured(),
                "screening_pending": bool(member.get("pending")),
                "verified": bool(values.get("verification_role") and values["verification_role"] in member.get("roles", [])),
                "request_state": row[0] if row else None}

    @app.post("/api/verification")
    async def submit(request: Request):
        session, member = await member_session(request, write=True)
        values = store.settings(cfg.guild_id)["values"]
        if not cfg.enforce or not values.get("verification_enabled"):
            raise Denied("Verification is not enabled")
        if member.get("pending"):
            raise Denied("Accept the server rules in Discord first")
        role = values.get("verification_role", "")
        await validate(cfg, api, [role])
        if role in member.get("roles", []):
            return {"verified": True}
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Invalid request")
        await validate_challenge(body.get("token"), session["user"], cfg.origin)
        # The provider proof is consumed above; neither it nor OAuth credentials enter the journal.
        store.queue("verify-" + secrets.token_hex(16), cfg.guild_id, session["user"], "verify_role", session["user"],
                    {"role": role, "joined_at": member.get("joined_at")})
        return {"queued": True}


async def execute(cfg, store, api, job):
    values = store.settings(cfg.guild_id)["values"]
    body = json.loads(job["body"])
    if (not values.get("verification_enabled") or body["role"] != values.get("verification_role")
            or job["actor"] != job["subject"] or not 0 <= time.time() - job["created"] <= 120):
        raise Denied("Verification request expired or was disabled")
    await validate(cfg, api, [body["role"]])
    member = await api.request("GET", f'/guilds/{cfg.guild_id}/members/{job["subject"]}')
    if member.get("pending") or member.get("user", {}).get("bot") or member.get("joined_at") != body.get("joined_at"):
        raise Denied("Membership changed or server screening is incomplete")
    await api.request("PUT", f'/guilds/{cfg.guild_id}/members/{job["subject"]}/roles/{body["role"]}',
                      reason="greyBot verified this member after server rules acceptance")
