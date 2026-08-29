"""Scrambled raid-progress bot — announce first Heroic boss kills, and AOTC, in Discord.

EventBridge Scheduler -> this Lambda -> Discord webhook. There is no gateway connection
because there is nothing to listen for: the bot announces and never responds.

The #logs channel is deliberately not a source. Scraping posted links only catches the
kills somebody remembered to paste, breaks whenever the link format shifts, and -- the
part that actually matters -- cannot tell a first kill from the ninth re-kill of the same
boss, which is the single distinction this bot exists to make. Warcraft Logs says what
died and when; Raider.IO says how many and what rank; DynamoDB says whether it has already
been announced. All three are needed and none of them is the #logs channel.

Ordering note: Raider.IO lags Warcraft Logs, sometimes by hours. Ryan's call is to
announce immediately and live with a stale RANK rather than sit on the news. The COUNT
gets no such licence -- see store.progress_count.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

import discord
import raiderio
import store
import wcl

REGION = os.environ.get("AWS_REGION", "us-east-1")

GUILD_NAME = os.environ.get("GUILD_NAME", "Scrambled")
GUILD_REALM = os.environ.get("GUILD_REALM", "")
GUILD_REGION = os.environ.get("GUILD_REGION", "us").lower()
ROLE_ID = os.environ.get("PROG_RAIDER_ROLE_ID", "")
ANNOUNCE_TZ = os.environ.get("ANNOUNCE_TZ", "America/New_York")
EXPANSION_HINT = int(os.environ.get("EXPANSION_HINT", "11"))

# How far back a routine poll looks. Generous relative to the schedule so a few missed
# runs -- a deploy, an outage, a throttle -- catch up instead of losing kills.
LOOKBACK_DAYS = float(os.environ.get("LOOKBACK_DAYS", "3"))
REPORT_LIMIT = int(os.environ.get("REPORT_LIMIT", "12"))

# The seed pass reaches back across the whole tier instead. It runs once per tier, and it
# has to see every boss already killed -- a boss missing from the seed set gets announced
# as a "first kill" the next time it dies, weeks late.
SEED_LOOKBACK_DAYS = float(os.environ.get("SEED_LOOKBACK_DAYS", "150"))
SEED_REPORT_LIMIT = int(os.environ.get("SEED_REPORT_LIMIT", "60"))

# Warcraft Logs bills points per hour, not requests. Above this fraction of the hourly
# allowance the run stops before the expensive reports query. Announcements resume when
# the window rolls; the alternative is spending the last points and getting nothing.
POINTS_CEILING = float(os.environ.get("POINTS_CEILING", "0.85"))

SEED_ONLY = os.environ.get("SEED_ONLY", "").lower() in ("1", "true", "yes")

P_CLIENT_ID = os.environ.get("SSM_WCL_CLIENT_ID", "/scrambled/wcl/client_id")
P_CLIENT_SECRET = os.environ.get("SSM_WCL_CLIENT_SECRET", "/scrambled/wcl/client_secret")
P_WEBHOOK = os.environ.get("SSM_DISCORD_WEBHOOK", "/scrambled/discord/webhook_url")

_cfg = Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=10)
ssm = boto3.client("ssm", region_name=REGION, config=_cfg)

_secrets = {}
_guild_id = {"value": None}


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, default=str))


def secrets():
    """Fetch the three secrets once per container.

    They are in Parameter Store rather than in the function's environment because the
    deploy template is in a public repo and Lambda environment variables are readable by
    anything that can call GetFunctionConfiguration -- a Discord webhook URL is a
    post-anything-to-this-channel credential, not a config value.
    """
    if _secrets:
        return _secrets
    names = [P_CLIENT_ID, P_CLIENT_SECRET, P_WEBHOOK]
    res = ssm.get_parameters(Names=names, WithDecryption=True)
    got = {p["Name"]: p["Value"] for p in res.get("Parameters", [])}
    missing = [n for n in names if n not in got]
    if missing:
        raise RuntimeError(f"missing SSM parameters: {', '.join(missing)}")
    _secrets.update({"client_id": got[P_CLIENT_ID],
                     "client_secret": got[P_CLIENT_SECRET],
                     "webhook": got[P_WEBHOOK]})
    return _secrets


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(ANNOUNCE_TZ))
    except Exception:                                          # noqa: BLE001
        # No tzdata in the runtime, or a bad TZ name. A UTC timestamp is a cosmetic
        # downgrade; failing the AOTC announcement over a timezone would not be.
        log("tz_fallback", tz=ANNOUNCE_TZ)
        return dt.astimezone(timezone.utc)


def _when_text(dt):
    local = _local(dt)
    stamp = local.strftime("%B %-d, %Y at %-I:%M %p")
    return f"{stamp} {local.strftime('%Z')}".strip()


def report_url(code):
    return f"https://www.warcraftlogs.com/reports/{code}" if code else None


def guild_id(token):
    if _guild_id["value"]:
        return _guild_id["value"], None
    guild, rate = wcl.find_guild(token, GUILD_NAME, GUILD_REALM, GUILD_REGION)
    if not guild or not guild.get("id"):
        raise RuntimeError(
            f"Warcraft Logs has no guild '{GUILD_NAME}' on {GUILD_REALM}-{GUILD_REGION}. "
            "Check the realm slug (lowercase, hyphenated).")
    _guild_id["value"] = int(guild["id"])
    return _guild_id["value"], rate


def announce_kill(webhook, pk, slug, raid_label, kill, state, profile):
    """Claim, then post. In that order -- see store.claim_boss."""
    boss_key = str(kill["encounterID"])
    if not store.claim_boss(pk, slug, boss_key):
        log("skip_rekill", slug=slug, boss=kill["name"], encounterID=kill["encounterID"])
        return False

    state["announced"].add(boss_key)
    r_killed, total = raiderio.progress_for(profile, slug)
    count = store.progress_count(state, r_killed, total)
    rank = raiderio.realm_rank(profile, slug, "heroic")
    killed_at = datetime.fromtimestamp(kill["killedAtMs"] / 1000, tz=timezone.utc)

    payload = discord.kill_embed(
        GUILD_NAME, kill["name"], count, total or "?", raid_label, rank,
        report_url=report_url(kill.get("reportCode")), iso_ts=_iso(killed_at))
    try:
        discord.post(webhook, payload)
    except discord.DiscordError as exc:
        # Hand the boss back so the next poll retries it. A missed announcement recovers
        # in fifteen minutes; a duplicate one never recovers at all.
        store.release_boss(pk, slug, boss_key)
        state["announced"].discard(boss_key)
        log("announce_failed", slug=slug, boss=kill["name"], error=str(exc))
        return False

    log("announced_kill", slug=slug, boss=kill["name"], encounterID=kill["encounterID"],
        count=count, total=total, realmRank=rank,
        raiderioKilled=r_killed, raiderioStale=bool(r_killed is not None and r_killed < count),
        killedAt=_iso(killed_at), report=kill.get("reportCode"))
    return True


def announce_aotc(webhook, pk, slug, raid_label, state, when):
    if not store.claim_aotc(pk, slug):
        log("skip_aotc_already", slug=slug)
        return False
    payload = discord.aotc_payload(GUILD_NAME, raid_label, _when_text(when), ROLE_ID,
                                   iso_ts=_iso(when))
    try:
        discord.post(webhook, payload)
    except discord.DiscordError as exc:
        store.release_aotc(pk, slug)
        log("aotc_failed", slug=slug, error=str(exc))
        return False
    state["aotcAnnounced"] = True
    log("announced_aotc", slug=slug, raid=raid_label, when=_iso(when),
        rolePinged=bool(ROLE_ID))
    return True


def handler(event, context):
    started = time.time()
    if not GUILD_REALM:
        raise RuntimeError("GUILD_REALM is unset — Raider.IO cannot be queried without it.")

    sec = secrets()
    token = wcl.get_token(sec["client_id"], sec["client_secret"])
    now = datetime.now(timezone.utc)
    now_iso = _iso(now)

    gid, rate = guild_id(token)
    if rate is None:
        rate = wcl.rate_limit(wcl.query(token, wcl.RATE_ONLY_Q))
    if rate and rate["fraction"] >= POINTS_CEILING:
        log("rate_limit_backoff", **rate, ceiling=POINTS_CEILING)
        return {"ok": True, "skipped": "rate_limit", "points": rate}

    window_start_ms = int((now - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)
    kills, rate = wcl.heroic_kills_since(token, gid, window_start_ms, limit=REPORT_LIMIT)

    profile = raiderio.guild_profile(GUILD_REGION, GUILD_REALM, GUILD_NAME)
    index, expansion = raiderio.build_index(profile, EXPANSION_HINT)

    if not kills:
        progression = profile.get("raid_progression") or {}
        # A public guild with progress but zero visible reports is the private-logs case:
        # a client-credentials token can only read public reports, so the event source is
        # blind and no amount of retrying will fix it. Say so plainly in the logs.
        if progression and any((v or {}).get("heroic_bosses_killed") for v in progression.values()):
            log("no_reports_visible", guild=GUILD_NAME, realm=GUILD_REALM,
                hint="Raider.IO shows Heroic kills but Warcraft Logs returned no reports — "
                     "the guild's logs are probably private to this OAuth client.")
        log("poll_idle", lookbackDays=LOOKBACK_DAYS, points=rate,
            ms=int((time.time() - started) * 1000))
        return {"ok": True, "kills": 0}

    pk = store.guild_pk(GUILD_REGION, GUILD_REALM, GUILD_NAME)

    # Resolve every kill to a raid first, so the encounter->raid mapping is available to
    # the seed pass as well as to the announcements.
    grouped = {}
    for k in kills:
        killed_at = datetime.fromtimestamp(k["killedAtMs"] / 1000, tz=timezone.utc)
        slug, meta, how = raiderio.resolve_raid(profile, k["name"], k.get("zoneName"),
                                                killed_at, GUILD_REGION, index)
        if not slug:
            log("unresolved_raid", boss=k["name"], zone=k.get("zoneName"))
            continue
        k["_how"] = how
        label = (meta or {}).get("name") or k.get("zoneName") or slug
        grouped.setdefault(slug, {"label": label, "meta": meta, "kills": []})["kills"].append(k)

    announced = 0
    for slug, bundle in grouped.items():
        label = bundle["label"]
        # Prefer the Warcraft Logs zone name in the message: it is the name raiders use.
        # Raider.IO's is a data label and reads like one ("MN Tier 1 (VS / DR / MQD)").
        raid_label = bundle["kills"][0].get("zoneName") or label

        state = store.load_tier(pk, slug)
        if state is None:
            if _seed_and_should_skip(token, gid, pk, slug, raid_label, profile,
                                     window_start_ms, now_iso, index):
                continue
            state = store.load_tier(pk, slug) or {"announced": set(), "seedSize": 0,
                                                  "baseline": 0, "aotcAnnounced": False}

        last_announced_ms = None
        for kill in bundle["kills"]:
            if announce_kill(sec["webhook"], pk, slug, raid_label, kill, state, profile):
                announced += 1
                last_announced_ms = kill["killedAtMs"]

        r_killed, total = raiderio.progress_for(profile, slug)
        count = store.progress_count(state, r_killed, total)
        if total and count >= total and not state.get("aotcAnnounced"):
            # Date it by the kill that finished the tier. Falling back to the newest kill
            # in the window would date AOTC by a re-kill on a later farm night, in the case
            # where Raider.IO only caught up after the real clear.
            when_ms = last_announced_ms or bundle["kills"][-1]["killedAtMs"]
            announce_aotc(sec["webhook"], pk, slug, raid_label, state,
                          datetime.fromtimestamp(when_ms / 1000, tz=timezone.utc))

        store.touch(pk, slug, now_iso, raid_name=raid_label)
        log("tier_summary", slug=slug, raid=raid_label,
            resolvedBy=bundle["kills"][0].get("_how"), killsSeen=len(bundle["kills"]),
            count=count, total=total, expansion=expansion)

    log("poll_done", kills=len(kills), announced=announced, tiers=len(grouped),
        points=rate, ms=int((time.time() - started) * 1000))
    return {"ok": True, "kills": len(kills), "announced": announced}


def _seed_and_should_skip(token, gid, pk, slug, raid_label, profile, window_start_ms,
                          now_iso, index):
    """Seed one tier. Split out so the encounter-name filter has the raid index to hand."""
    since = (datetime.now(timezone.utc)
             - timedelta(days=SEED_LOOKBACK_DAYS)).timestamp() * 1000
    history, rate = wcl.heroic_kills_since(token, gid, since, limit=SEED_REPORT_LIMIT)

    mine = []
    for k in history:
        killed_at = datetime.fromtimestamp(k["killedAtMs"] / 1000, tz=timezone.utc)
        k_slug, _meta, _how = raiderio.resolve_raid(profile, k["name"], k.get("zoneName"),
                                                    killed_at, GUILD_REGION, index)
        if k_slug == slug:
            mine.append(k)

    older = [k for k in mine if k["killedAtMs"] < window_start_ms]
    r_killed, total = raiderio.progress_for(profile, slug)
    silent = bool(older) or SEED_ONLY

    keys = {str(k["encounterID"]) for k in (mine if silent else [])}
    # The baseline is what is already accounted for OUTSIDE the announced set. A silent
    # seed swallows the back catalogue, so the baseline carries it. A rollover seed starts
    # empty and earns every kill through a claim, so its baseline must be zero -- otherwise
    # the first boss of a new tier announces as "2 of 8". progress_count still takes the
    # max against Raider.IO, so a kill the log never saw is not lost either way.
    baseline = (r_killed or 0) if silent else 0
    aotc_already = bool(total and r_killed is not None and r_killed >= total)
    created = store.seed_tier(pk, slug, keys, baseline, raid_label, now_iso,
                              aotc_already=aotc_already)
    log("seeded_tier", slug=slug, raid=raid_label, created=created, silent=silent,
        seededBosses=len(keys), historySeen=len(mine), olderThanWindow=len(older),
        baseline=baseline, raiderioKilled=r_killed, total=total,
        aotcAlready=aotc_already, seedOnly=SEED_ONLY, points=rate)
    return silent
