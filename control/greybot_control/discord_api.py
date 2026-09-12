"""Discord OAuth and REST access with live administrative authorization."""

import asyncio
import json
from urllib.parse import quote

import httpx

API = "https://discord.com/api/v10"
ADMINISTRATOR = 1 << 3


class Denied(Exception):
    pass


class Unavailable(Exception):
    pass


class DiscordAPI:
    def __init__(self, cfg):
        self.cfg = cfg
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, *, body=None, reason="", files=None):
        headers = {"Authorization": "Bot " + self.cfg.bot_token,
                   "User-Agent": "greyBot/2.0"}
        if reason:
            headers["X-Audit-Log-Reason"] = quote(reason[:400], safe="")
        # Only rate-limit rejections are safe to retry here. A 5xx after a
        # moderation write is ambiguous; leave the job for manual reconciliation.
        for attempt in range(3):
            try:
                payload = {'data': {'payload_json': json.dumps(body)}, 'files': files} if files else {'json': body}
                response = await self.http.request(method, API + path, headers=headers, **payload)
            except httpx.HTTPError:
                raise Unavailable("Discord request did not complete") from None
            if response.status_code == 429 and attempt < 2:
                delay = float(response.json().get("retry_after", 1))
                if delay > 10:
                    raise Unavailable("Discord rate limit; retry later")
                await asyncio.sleep(max(delay, 0.1))
                continue
            if response.status_code in {401, 403, 404}:
                raise Denied("Discord access unavailable")
            if response.status_code >= 400:
                raise Unavailable("Discord rejected the request")
            return response.json() if response.content else None
        raise Unavailable("Discord rate limit; retry later")

    async def identity(self, code):
        if not self.cfg.client_secret:
            raise Unavailable("Dashboard OAuth is not configured")
        try:
            response = await self.http.post(API + "/oauth2/token", data={
                "client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret,
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.cfg.callback})
            if response.status_code != 200:
                raise Denied("Discord login failed")
            token = response.json()["access_token"]
            result = await self.http.get(API + "/users/@me", headers={"Authorization": "Bearer " + token})
            if result.status_code != 200:
                raise Denied("Discord identity unavailable")
            # OAuth tokens are never stored in sessions, disk, logs, or cookies.
            return result.json()["id"]
        except (httpx.HTTPError, KeyError, ValueError):
            raise Unavailable("Discord login unavailable") from None

    async def member_context(self, user):
        gid = self.cfg.guild_id
        guild = await self.request("GET", f"/guilds/{gid}")
        member = await self.request("GET", f"/guilds/{gid}/members/{user}")
        roles = await self.request("GET", f"/guilds/{gid}/roles")
        held = [r for r in roles if r["id"] == gid or r["id"] in member.get("roles", [])]
        permissions = 0
        for role in held:
            permissions |= int(role["permissions"])
        return {"owner": guild["owner_id"] == user, "permissions": permissions,
                "position": max((r["position"] for r in held), default=0), "member": member}

    async def require_admin(self, user):
        context = await self.member_context(user)
        if not context["owner"] and not context["permissions"] & ADMINISTRATOR:
            raise Denied("Current server administrator access is required")
        return context

    async def authorize_moderation(self, actor, target, action):
        admin = await self.require_admin(actor)
        other = await self.member_context(target)
        bot = await self.member_context(self.cfg.client_id)
        permission = {"timeout": 1 << 40, "kick": 1 << 1, "ban": 1 << 2, "mute": 1 << 28, "unmute": 1 << 28, "automod": 1 << 13}[action]
        if actor == target or target == self.cfg.client_id or other["owner"]:
            raise Denied("This member cannot be moderated")
        if not admin["owner"] and admin["position"] <= other["position"]:
            raise Denied("Target is at or above your highest role")
        if bot["position"] <= other["position"]:
            raise Denied("Target is at or above the bot's highest role")
        if not bot["permissions"] & (ADMINISTRATOR | permission):
            raise Denied("Bot lacks the required permission")
        if action in {"timeout", "mute", "unmute", "automod"} and other["permissions"] & ADMINISTRATOR:
            raise Denied("Administrators cannot be muted or timed out")
