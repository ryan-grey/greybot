"""Admin-only web interface. Every private request rechecks guild membership."""

import secrets
import os
import json
import time
from .profiles import enrich
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Config
from .discord_api import Denied, DiscordAPI, Unavailable
from .store import Store
from .audit_feed import AUDIT_EVENTS, DEFAULT_AUDIT_EVENTS, event_catalog
from .directory import Directory
from .mutes import Mutes
from .feed_dispatch import preflight as feed_preflight
from .role_panels import validate as validate_roles, receive as receive_roles
from .verification import install as install_verification, configured as verification_configured, receive as receive_verification


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    welcome_channel: str = Field(default="", pattern=r"^\d*$")
    welcome_enabled: bool = False
    verification_enabled: bool = False
    verification_role: str = Field(default="", pattern=r"^\d{0,22}$")
    goodbye_enabled: bool = False
    goodbye_channel: str = Field(default="", pattern=r"^\d{0,22}$")
    self_roles: list[str] = Field(default_factory=list, max_length=3)
    moderation_enabled: bool = False
    audit_feed_enabled: bool = False
    audit_channel: str = Field(default="", pattern=r"^\d{0,22}$")
    audit_events: list[str] = Field(default_factory=lambda: list(DEFAULT_AUDIT_EVENTS), max_length=24)
    audit_ignore_bots: bool = False
    audit_show_avatars: bool = True
    audit_ignored_channels: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("audit_events")
    @classmethod
    def validate_events(cls, values):
        if len(set(values)) != len(values) or any(value not in AUDIT_EVENTS for value in values):
            raise ValueError("Choose supported, unique Discord feed events")
        return values

    @field_validator("audit_ignored_channels")
    @classmethod
    def validate_channels(cls, values):
        if len(set(values)) != len(values) or any(not value.isascii() or not value.isdecimal() or len(value) > 22 for value in values):
            raise ValueError("Enter unique channel IDs")
        return values


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9-]{16,64}$")
    action: str = Field(pattern=r"^(timeout|kick|ban|mute|unmute)$")
    user: str = Field(pattern=r"^\d{1,22}$")
    reason: str = Field(min_length=1, max_length=400)
    minutes: int = Field(default=10, ge=1, le=40320)


class RolesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9-]{16,64}$")
    members: list[str] = Field(min_length=1, max_length=1000)
    role: str = Field(pattern=r"^\d{1,22}$")
    operation: str = Field(pattern=r"^(add|remove)$")

    @field_validator("members")
    @classmethod
    def valid_members(cls, values):
        if len(set(values)) != len(values) or any(not v.isascii() or not v.isdecimal() or len(v) > 22 for v in values):
            raise ValueError("Choose distinct members")
        return values


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str = Field(pattern=r"^[a-f0-9]{64}$")
    checked: bool


def create_app(cfg=None, store=None, discord=None):
    cfg = cfg or Config.from_env()
    store = store or Store(cfg.state_dir / "control.sqlite3")
    discord = discord or DiscordAPI(cfg)
    directory = Directory(cfg, store, discord)
    from . import raids
    raids.install(store)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await discord.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(cfg.origin).hostname])
    app.state.store = store
    cookie = "__Host-greybot" if cfg.secure else "greybot-local"
    state_cookie = cookie + "-login"
    static = Path(__file__).with_name("static")
    install_verification(app, cfg, store, discord, cookie, static)
    from .channel_choices import install as install_choices
    install_choices(app, cfg, store, discord, cookie, static)
    from .raid_web import install as install_raids
    install_raids(app, cfg, store, discord, cookie, static, directory)

    @app.middleware("http")
    async def security_headers(request, call_next):
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid request length"}, status_code=400)
        if length > 16384 or length < 0:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.method in {"POST", "PUT"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' https://cdn.discordapp.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if cfg.secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path == "/verify":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' https://challenges.cloudflare.com; style-src 'self'; "
                "img-src 'self' https://cdn.discordapp.com; connect-src 'self' https://challenges.cloudflare.com; "
                "frame-src https://challenges.cloudflare.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        return response

    @app.exception_handler(Denied)
    async def denied(request, exc):
        if request.method == "GET" and request.url.path in {"/", "/auth/callback"}:
            response = FileResponse(static / "access-denied.html", status_code=403)
            response.delete_cookie(state_cookie, path="/")
            response.delete_cookie(cookie, path="/")
            return response
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(Unavailable)
    async def unavailable(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    async def admin(request, write=False):
        session = store.get_session(request.cookies.get(cookie, ""))
        if not session:
            raise HTTPException(401, "Sign in with Discord")
        await discord.require_admin(session["user"])
        if write and (request.headers.get("origin") != cfg.origin or
                not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"])):
            raise HTTPException(403, "Invalid request origin or CSRF token")
        return session

    @app.get("/")
    async def home(request: Request):
        if not store.get_session(request.cookies.get(cookie, "")):
            return FileResponse(static / "login.html")
        await admin(request)
        return FileResponse(static / "index.html")

    @app.get("/about")
    async def about():
        return FileResponse(static / "about.html")

    @app.post("/discord/roles")
    async def role_interaction(request: Request):
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
        raw = await request.body()
        timestamp = request.headers.get("x-signature-timestamp", "")
        try:
            if abs(time.time() - int(timestamp)) > 300:
                raise ValueError("Expired signature")
            VerifyKey(bytes.fromhex(os.environ.get("GREYBOT_DISCORD_PUBLIC_KEY", ""))).verify(
                timestamp.encode() + raw, bytes.fromhex(request.headers.get("x-signature-ed25519", "")))
        except (BadSignatureError, ValueError, TypeError):
            raise HTTPException(401, "Invalid Discord signature") from None
        if not cfg.enforce or not (cfg.archive_dir or cfg.archive_bucket):
            raise HTTPException(503, "Role worker is unavailable")
        try:
            packet = json.loads(raw)
            if not isinstance(packet, dict):
                raise ValueError()
            from .raid_discord import receive as receive_raid, reply as raid_reply, COMMAND_NAMES, PREFIX
            if (packet.get("type") == 2 and packet.get("data", {}).get("name") in COMMAND_NAMES
                    or str(packet.get("data", {}).get("custom_id", "")).startswith(PREFIX)):
                try:
                    return receive_raid(cfg, store, packet)
                except Denied as exc:
                    return raid_reply(str(exc))
            if packet.get("data", {}).get("custom_id") == "greybot:verify":
                return receive_verification(cfg, store, packet)
            if packet.get("data", {}).get("custom_id") == "greybot:verification_help":
                from .verification import receive_help
                return receive_help(cfg, store, packet)
            return receive_roles(cfg, store, packet)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Invalid interaction") from None

    @app.post("/api/role-panel")
    async def publish_role_panel(request: Request):
        session = await admin(request, write=True)
        body = await request.json()
        channel = str(body.get("channel", ""))
        request_id = str(body.get("request_id", ""))
        if not request_id.isalnum() or not 16 <= len(request_id) <= 64:
            raise HTTPException(400, "Invalid request ID")
        values = store.settings(cfg.guild_id)["values"]
        if not cfg.enforce:
            raise HTTPException(409, "The audited worker is not enabled")
        if not os.environ.get("GREYBOT_DISCORD_PUBLIC_KEY"):
            raise HTTPException(409, "Install the role worker and interaction relay first")
        await validate_roles(cfg, discord, values["self_roles"])
        await feed_preflight(cfg, discord, channel)
        store.queue(request_id, cfg.guild_id, session["user"], "role_panel", "", {"channel": channel})
        return {"queued": True}

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name == "avatar.png":
            return FileResponse(Path(__file__).resolve().parents[2] / "assets" / "greyBot-avatar-v4.png", headers={"Cache-Control": "no-cache"})
        if name not in {"app.js", "style.css", "verify.js", "channels.js", "event-labels.js", "raids.js"}:
            raise HTTPException(404)
        return FileResponse(static / name)

    @app.get("/auth/login")
    async def login(destination: str = "admin"):
        if not cfg.client_secret or not cfg.bot_token:
            raise HTTPException(503, "Dashboard login is not configured")
        if destination not in {"admin", "verify", "channels", "raids"}:
            raise HTTPException(400, "Invalid login destination")
        if destination == "verify" and not (verification_configured() and store.settings(cfg.guild_id)["values"].get("verification_enabled")):
            raise HTTPException(503, "Member verification is not enabled yet")
        state = store.oauth_state(destination)
        response = RedirectResponse("https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": cfg.client_id, "redirect_uri": cfg.callback, "response_type": "code",
            "scope": "identify", "state": state}), status_code=303)
        response.set_cookie(state_cookie, state, max_age=600, secure=cfg.secure,
                            httponly=True, samesite="lax", path="/")
        return response

    @app.get("/auth/callback")
    async def callback(request: Request, code: str = "", state: str = ""):
        expected = request.cookies.get(state_cookie, "")
        if not state or not expected or not secrets.compare_digest(state, expected) or not store.consume_state(state):
            raise HTTPException(403, "Invalid or expired login")
        if not code or len(code) > 1024:
            raise HTTPException(400, "Missing login code")
        user = await discord.identity(code)
        if state.startswith(("verify.", "channels.", "raids.")):
            destination = state.split(".", 1)[0]
            member = await discord.request("GET", f"/guilds/{cfg.guild_id}/members/{user}")
            if member.get("user", {}).get("bot"):
                raise HTTPException(403, "Member verification is for people")
            token = store.session(user, ttl=1800)
            response = RedirectResponse("/" + destination, status_code=303)
            response.delete_cookie(state_cookie, path="/")
            response.set_cookie(cookie + "-" + destination, token, max_age=1800, secure=cfg.secure,
                                httponly=True, samesite="lax", path="/")
            return response
        await discord.require_admin(user)
        token = store.session(user)
        store.append("login:" + secrets.token_hex(16), cfg.guild_id, "ADMIN_LOGIN", user, {"actor": user})
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(state_cookie, path="/")
        response.set_cookie(cookie, token, max_age=28800, secure=cfg.secure,
                            httponly=True, samesite="lax", path="/")
        return response

    @app.post("/auth/logout")
    async def logout(request: Request):
        await admin(request, write=True)
        store.logout(request.cookies.get(cookie, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie, path="/")
        return response

    @app.get("/api/status")
    async def status(request: Request):
        session = await admin(request)
        with store.connection() as db:
            feed_issues = db.execute("SELECT COUNT(*) FROM feed_delivery WHERE guild=? AND state IN ('sending','unknown')", (cfg.guild_id,)).fetchone()[0]
        return {"user": session["user"], "csrf": session["csrf"], "enforcing": cfg.enforce,
                "feed_issues": feed_issues,
                "content_indexing": cfg.capture_content, "archive_configured": bool(cfg.archive_bucket or cfg.archive_dir),
                "archive_mode": "nas" if cfg.archive_dir else "locked" if cfg.archive_bucket else "none",
                "archive_pending": len(store.pending(10001)), "journal_valid": store.verify(),
                "retention_days": cfg.retention_days}

    @app.get("/api/events")
    async def events(request: Request, kind: str = "", subject: str = "", before: int = 0):
        await admin(request)
        rows = store.events(cfg.guild_id, kind=kind[:80], subject=subject[:30], before=before)
        rows = [{**row, "details": store.details(row)} for row in rows]
        return await enrich(rows, cfg, store, discord)

    @app.get("/api/messages")
    async def messages(request: Request, q: str = "", channel: str = "", author: str = ""):
        await admin(request)
        return await enrich(store.search(cfg.guild_id, q[:200], channel[:30], author[:30]), cfg, store, discord, "author")

    @app.get("/api/directory")
    async def names(request: Request):
        await admin(request)
        return await directory.get()

    @app.get("/api/dashboard")
    async def dashboard(request: Request):
        session = await admin(request)
        from .profiles import display_profile
        from .insights import health_status
        member = await discord.request("GET", f'/guilds/{cfg.guild_id}/members/{session["user"]}')
        return {"member": display_profile(cfg.guild_id, member["user"], member), "health": health_status(store, cfg.guild_id)}

    @app.get("/api/activity")
    async def activity(request: Request):
        await admin(request)
        from .insights import scoreboard
        return scoreboard(store, cfg.guild_id, await directory.get())

    @app.get("/api/review")
    async def review(request: Request):
        await admin(request)
        from .insights import server_snapshot, review_items
        items = review_items(*(await server_snapshot(cfg, discord)))
        with store.connection() as db:
            rows = db.execute("SELECT payload FROM events WHERE guild=? AND kind='REVIEW_CHECKED' ORDER BY seq", (cfg.guild_id,)).fetchall()
        checks = {p["item"]: p for r in rows if (p := json.loads(r[0]))}
        return {"items": [{**item, "checked": checks.get(item["id"], {}).get("checked", False)} for item in items]}

    @app.post("/api/review")
    async def check_review(request: Request, body: ReviewBody):
        session = await admin(request, write=True)
        store.append("review:" + secrets.token_hex(16), cfg.guild_id, "REVIEW_CHECKED", session["user"],
                     {**body.model_dump(), "actor": session["user"]})
        return {"ok": True}

    @app.post("/api/member-roles")
    async def member_roles(request: Request, body: RolesBody):
        session = await admin(request, write=True)
        if not cfg.enforce or not (cfg.archive_dir or cfg.archive_bucket):
            raise HTTPException(409, "Audited role worker is disabled")
        from .admin_roles import authorize_selection
        # Validate the explicit selection before queuing any member; the worker
        # checks again immediately before each write, preserving other roles.
        await authorize_selection(cfg, discord, session["user"], body.members, body.role)
        from .store import digest
        return {"jobs": [store.queue(digest(body.request_id + ":" + user), cfg.guild_id, session["user"],
                "admin_role", user, {"role": body.role, "operation": body.operation}) for user in body.members]}

    @app.get("/api/settings")
    async def settings(request: Request):
        await admin(request)
        return {**store.settings(cfg.guild_id), "audit_event_catalog": event_catalog(),
                "audit_default_events": list(DEFAULT_AUDIT_EVENTS)}

    @app.put("/api/settings")
    async def save_settings(request: Request, body: SettingsBody):
        session = await admin(request, write=True)
        if body.verification_enabled:
            if not cfg.enforce or not verification_configured():
                raise HTTPException(409, "Configure the verification provider and audited worker first")
            if not body.welcome_enabled or not os.environ.get("GREYBOT_DISCORD_PUBLIC_KEY"):
                raise HTTPException(409, "Enable welcome messages and configure signed verification buttons first")
            if body.verification_role in body.self_roles:
                raise HTTPException(409, "The verified member role cannot be a self-service role")
            await validate_roles(cfg, discord, [body.verification_role])
        if body.audit_feed_enabled:
            if not (cfg.archive_bucket or cfg.archive_dir):
                raise HTTPException(409, "Configure the audit archive before Discord posting")
            await feed_preflight(cfg, discord, body.audit_channel)
        if body.goodbye_enabled:
            if not (cfg.archive_bucket or cfg.archive_dir):
                raise HTTPException(409, "Configure the audit archive before departure posting")
            await feed_preflight(cfg, discord, body.goodbye_channel)
        if body.welcome_enabled:
            if not (cfg.archive_bucket or cfg.archive_dir):
                raise HTTPException(409, "Configure the audit archive before arrival posting")
            await feed_preflight(cfg, discord, body.welcome_channel)
        if body.self_roles:
            if not cfg.enforce:
                raise HTTPException(409, "Enable the audited worker before role panels")
            await validate_roles(cfg, discord, body.self_roles)
        if body.moderation_enabled:
            if not cfg.enforce or not cfg.capture_content:
                raise HTTPException(409, "Enable the audited worker and message-content access before automatic moderation")
            await Mutes(cfg, store, discord, None).channels()
        values = body.model_dump(exclude={"revision"})
        try:
            store.save_settings(cfg.guild_id, session["user"], body.revision, values)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return store.settings(cfg.guild_id)

    @app.get("/api/actions")
    async def actions(request: Request):
        await admin(request)
        return await enrich(store.jobs(cfg.guild_id), cfg, store, discord)

    @app.get("/api/mutes")
    async def mutes(request: Request):
        await admin(request)
        rows = [{key: value for key, value in row.items() if key not in {"plan", "id"}}
                for row in Mutes(cfg, store, discord, None).records()]
        return await enrich(rows, cfg, store, discord)

    @app.post("/api/actions")
    async def action(request: Request, body: ActionBody):
        session = await admin(request, write=True)
        if not cfg.enforce:
            raise HTTPException(409, "Production actions are disabled")
        if body.action == "unmute":
            await discord.require_admin(session["user"])
        else:
            await discord.authorize_moderation(session["user"], body.user, body.action)
        if body.action == "mute":
            await Mutes(cfg, store, discord, None).channels()
        try:
            return store.queue(body.request_id, cfg.guild_id, session["user"], body.action, body.user,
                               {"reason": body.reason, "minutes": body.minutes})
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    return app
