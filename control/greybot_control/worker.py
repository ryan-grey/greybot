"""Single-host Gateway collector and audited moderation executor."""

import asyncio
import fcntl
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import discord

from .archive import Archive
from .local_archive import LocalArchive
from .config import Config
from .discord_api import Denied, DiscordAPI, Unavailable
from .events import Collector
from .store import Store
from .mutes import Mutes
from .automod import Detector, execute as execute_automod
from .feed_dispatch import Feed
from .role_panels import execute as execute_roles
from .verification import execute as execute_verification

log = logging.getLogger("greybot.control")


async def execute_one(cfg, store, api, archive):
    if not cfg.enforce:
        return
    if not archive:
        raise RuntimeError("Actions require a verified audit archive")
    await asyncio.to_thread(archive.flush, store)
    if store.pending(1):
        return  # Drain the archive backlog before claiming an action.
    job = store.claim_job()
    if not job:
        return
    try:
        if job["kind"] == "raid":
            from .raid_discord import execute
            await execute(cfg, store, api, job)
            store.finish_job(job, "completed")
            return
        if job["kind"] == "channel_visibility":
            from .channel_choices import execute
            await execute(cfg, store, api, job)
            store.finish_job(job, "completed")
            return
        if job["kind"] == "admin_role":
            from .admin_roles import execute
            await execute(cfg, store, api, job)
            store.finish_job(job, "completed")
            return
        if job["kind"] == "verify_role":
            await execute_verification(cfg, store, api, job)
            store.finish_job(job, "completed")
            return
        if job["kind"] in {"self_role", "role_panel"}:
            await execute_roles(cfg, store, api, job)
            store.finish_job(job, "completed")
            return
        if job["kind"] == "unmute":
            await api.require_admin(job["actor"])
        else:
            await api.authorize_moderation(job["actor"], job["subject"], job["kind"])
        body = json.loads(job["body"])
        if body.get("automatic") and not store.settings(cfg.guild_id)["values"].get("moderation_enabled"):
            raise Denied("Automatic moderation is disabled")
        if body.get("automatic"):
            if not 0 <= time.time() - body["occurred"] <= 120:
                raise Denied("Expired automatic action")
            with store.connection() as db:
                released = db.execute("SELECT 1 FROM events WHERE guild=? AND subject=? AND kind='MUTE_RELEASED' AND seq>? LIMIT 1",
                                      (cfg.guild_id, job["subject"], body["trigger_seq"])).fetchone()
            if released:
                raise Denied("An administrator revoked this mute after the triggering infraction")
        reason = f"Dashboard actor {job['actor']}: {body['reason']}"
        path = f"/guilds/{cfg.guild_id}"
        if job["kind"] == "automod":
            await execute_automod(cfg, store, api, job)
        elif job["kind"] == "mute":
            await Mutes(cfg, store, api, archive).apply(job["actor"], job["subject"], body["reason"])
        elif job["kind"] == "unmute":
            await Mutes(cfg, store, api, archive).release(job["actor"], job["subject"], body["reason"])
        elif job["kind"] == "timeout":
            until = datetime.now(timezone.utc) + timedelta(minutes=body["minutes"])
            await api.request("PATCH", path + f"/members/{job['subject']}",
                              body={"communication_disabled_until": until.isoformat()}, reason=reason)
        elif job["kind"] == "kick":
            await api.request("DELETE", path + f"/members/{job['subject']}", reason=reason)
        elif job["kind"] == "ban":
            await api.request("PUT", path + f"/bans/{job['subject']}",
                              body={"delete_message_seconds": 0}, reason=reason)
        else:
            raise Denied("Unsupported action")
        result = "completed"
    except Denied as exc:
        if job["kind"] == "raid":
            store.append("raid-denied:" + job["id"], cfg.guild_id, "RAID_REQUEST_REJECTED", job["actor"],
                         {"actor": job["actor"], "request": job["id"], "reason": str(exc)})
        result = "denied"
    except Exception:
        # Never retry an ambiguous write: Discord may have applied it. An
        # interrupted process likewise leaves an executing job for review.
        result = "unknown"
    store.finish_job(job, result)


async def run():
    cfg = Config.from_env()
    if not cfg.bot_token:
        raise RuntimeError("Bot token is not configured")
    store = Store(cfg.state_dir / "control.sqlite3")
    from . import raids
    raids.install(store)
    from . import anniversaries
    anniversaries.install(store)
    lock = open(cfg.state_dir / "worker.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with store.connection() as db:
        db.execute("UPDATE raid_events SET delivery='pending' WHERE delivery='sending' AND message!=''")
    api = DiscordAPI(cfg)
    me = await api.request("GET", "/users/@me")
    if me["id"] != cfg.client_id:
        raise RuntimeError("Configured application and bot identities differ")
    archive = None
    if cfg.archive_dir:
        archive = LocalArchive(cfg.archive_dir)
        await asyncio.to_thread(archive.check)
    if cfg.archive_bucket:
        import boto3
        archive = Archive(boto3.client("s3"), cfg.archive_bucket, cfg.retention_days)
        await asyncio.to_thread(archive.check)
    collector = Collector(cfg, store)
    detector = Detector(cfg, store)
    feed = Feed(cfg, store, api)
    intents = discord.Intents.none()
    intents.guilds = intents.members = intents.moderation = True
    intents.guild_messages = intents.guild_reactions = True
    intents.invites = intents.voice_states = intents.emojis_and_stickers = True
    intents.auto_moderation_execution = True
    intents.message_content = cfg.capture_content

    class Client(discord.Client):
        async def on_resumed(self):
            import secrets
            store.append("resume:" + secrets.token_hex(16), cfg.guild_id, "COLLECTOR_RESUMED", "", {})

        async def on_disconnect(self):
            import secrets
            collector.voice.clear()
            store.append("disconnect:" + secrets.token_hex(16), cfg.guild_id, "COLLECTOR_DISCONNECTED", "", {})

        async def on_socket_raw_receive(self, msg):
            try:
                collector.raw(msg)
                packet = json.loads(msg)
                if isinstance(packet, dict):
                    detector.receive(packet)
            except Exception:
                # Do not let the library log raw event payloads on exceptions.
                log.error("Event persistence failed; collector stopping")
                await self.close()

        async def on_error(self, event, *args, **kwargs):
            log.error("Gateway event handler failed")

    client = Client(intents=intents, enable_debug_events=True,
                    member_cache_flags=discord.MemberCacheFlags.none(), max_messages=None)

    async def maintenance():
        next_mute_check = 0
        next_health_check = 0
        next_anniversary_check = 0
        while not client.is_closed():
            try:
                if time.monotonic() >= next_health_check:
                    from .insights import health_tick
                    health_tick(store, cfg.guild_id)
                    next_health_check = time.monotonic() + 30
                if cfg.enforce:
                    await execute_one(cfg, store, api, archive)
                    from .raid_discord import enabled as raids_enabled, deliver_one
                    if raids_enabled():
                        for row in raids.list_events(store, cfg.guild_id):
                            if row["body"]["state"] == "open" and row["body"]["closingTime"] <= time.time():
                                raids.mutate(store, cfg.guild_id, cfg.client_id,
                                    f"deadline:{row['id']}:{row['body']['closingTime']}", row["id"],
                                    row["revision"], "close", "")
                        await deliver_one(cfg, store, api, archive)
                    if time.monotonic() >= next_mute_check:
                        await Mutes(cfg, store, api, archive).reconcile()
                        next_mute_check = time.monotonic() + 60
                elif archive:
                    await asyncio.to_thread(archive.flush, store)
                await feed.tick()
                if time.monotonic() >= next_anniversary_check:
                    next_anniversary_check = time.monotonic() + 300
                    try:
                        await anniversaries.tick(cfg, store, api)
                    except Exception:
                        log.error("Membership anniversary check failed; inspect anniversary_delivery for uncertain posts")
            except Exception:
                log.error("Archive or action processing unavailable; no new action dispatched")
            await asyncio.sleep(5)

    async def maintain_when_ready():
        await client.wait_until_ready()
        await maintenance()

    # discord.py initializes its ready event during login, not construction.
    await client.login(cfg.bot_token)
    maintenance_task = asyncio.create_task(maintain_when_ready())
    try:
        await client.connect(reconnect=True)
    finally:
        maintenance_task.cancel()
        await asyncio.gather(maintenance_task, return_exceptions=True)
        await client.close()
        await api.close()
        lock.close()


if __name__ == "__main__":
    asyncio.run(run())
