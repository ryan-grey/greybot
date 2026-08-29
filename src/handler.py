"""greyBot — announce first Heroic boss kills, and AOTC, for the guild Scrambled.

EventBridge Scheduler -> this Lambda -> Discord webhook. There is no gateway connection
because there is nothing to listen for: the bot announces and never responds.

The #logs channel is deliberately not a source. Scraping posted links only catches the
kills somebody remembered to paste, breaks whenever the link format shifts, and -- the
part that actually matters -- cannot tell a first kill from the ninth re-kill of the same
boss, which is the single distinction this bot exists to make. Warcraft Logs says what
died and when; Raider.IO says how many and what rank; DynamoDB says whether it has already
been announced. All three are needed and none of them is the #logs channel.

Two seeding paths, and they are deliberately different:

  bootstrap()      the first run for a guild, ever. Seeds EVERY tier in raid_progression
                   from Raider.IO's counts and the log history, sets the AOTC flag on the
                   tiers already cleared, and returns before a single webhook call can
                   happen. Scrambled arrives with three cleared tiers and 2/8 in a fourth;
                   without this, run one is a dozen retroactive kill cards and a false
                   AOTC for a tier finished months ago, in a live guild channel.

  seed_new_tier()  a raid slug that shows up LATER, which is a tier rollover. Here
                   Raider.IO's count must NOT be used to seed, because at rollover that
                   count is describing the very kills about to be announced. It seeds only
                   from history older than the poll window, so genuinely fresh kills are
                   announced rather than swallowed.

Ordering note: Raider.IO lags Warcraft Logs, sometimes by hours. Ryan's call is to
announce immediately and live with a stale RANK rather than sit on the news. The COUNT
gets no such licence -- see store.progress_count.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import boto3

import blizzard
import config
import discord
import interactions
import raiderio
import store
import wcl

# Tuning knobs, not provisioned configuration. Everything that identifies the guild or
# grants access to something comes from SSM via config.load().
ANNOUNCE_TZ = os.environ.get("ANNOUNCE_TZ", "America/New_York")
# Credited on the AOTC card only. Empty means no credit line at all, which is what should
# happen until the repository is actually public rather than a link that 404s.
REPO_URL = os.environ.get("REPO_URL", "")
# /progress answers from the poller's snapshot when it is fresher than this, and defers to
# a live fetch when it is not. An hour is generous: the poller refreshes every fifteen
# minutes, so falling through means something has already gone quiet.
SNAPSHOT_MAX_AGE = float(os.environ.get("SNAPSHOT_MAX_AGE", "3600"))
EPHEMERAL_REPLIES = os.environ.get("EPHEMERAL_REPLIES", "1").lower() not in ("0", "false", "no")
EXPANSION_HINT = int(os.environ.get("EXPANSION_HINT", "11"))

# How far back a routine poll looks. Generous relative to the schedule so a few missed
# runs -- a deploy, an outage, a throttle -- catch up instead of losing kills.
LOOKBACK_DAYS = float(os.environ.get("LOOKBACK_DAYS", "3"))
REPORT_LIMIT = int(os.environ.get("REPORT_LIMIT", "12"))

# Seeding reaches back across whole tiers instead, and runs once.
#
# The page size is a COMPLEXITY budget, not a preference. Warcraft Logs prices a query
# from its shape, and asking for fights inside each report costs roughly 707 per report
# against a 50,000 ceiling -- a single page of 100 was rejected at 70,705 without
# returning anything. 25 lands near 17,700, leaving room for the weights to change, and
# the depth comes from walking pages instead.
SEED_LOOKBACK_DAYS = float(os.environ.get("SEED_LOOKBACK_DAYS", "240"))
SEED_REPORT_LIMIT = int(os.environ.get("SEED_REPORT_LIMIT", "25"))
SEED_MAX_PAGES = int(os.environ.get("SEED_MAX_PAGES", "6"))

# Warcraft Logs bills points per hour, not requests. Above this fraction of the hourly
# allowance the run stops before the expensive reports query. Announcements resume when
# the window rolls; the alternative is spending the last points and getting nothing.
POINTS_CEILING = float(os.environ.get("POINTS_CEILING", "0.85"))

_guild_id = {"value": None}


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, default=str))


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


def _at(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def report_url(code):
    return f"https://www.warcraftlogs.com/reports/{code}" if code else None


def boss_key(name):
    """The dedupe key. A normalised boss name -- the only identifier Warcraft Logs and
    Raider.IO share. See the store module docstring for why not the encounter id."""
    return raiderio.normalize(name)


def display_tier(profile, snapshot):
    """Which tier /progress should describe.

    Note this uses the "one tier is partly cleared" heuristic that resolve_raid refuses.
    The difference is what a wrong answer costs. Attributing a KILL to the wrong raid
    corrupts the dedupe state and can manufacture a false AOTC; naming the wrong raid in a
    read-only progress card is visible, harmless and self-correcting. The snapshot written
    by the poller -- which derives its tier from an actual kill -- is still preferred, and
    this only runs when there is none.
    """
    progression = profile.get("raid_progression") or {}
    if snapshot and snapshot.get("slug") in progression:
        return snapshot["slug"]
    partial = [slug for slug, v in progression.items()
               if 0 < int(v.get("heroic_bosses_killed") or 0) < int(v.get("total_bosses") or 0)]
    if len(partial) == 1:
        return partial[0]
    return list(progression)[-1] if progression else None


def progress_embed_live(cfg, profile, index, snapshot, as_of=None):
    slug = display_tier(profile, snapshot)
    if not slug:
        return None
    meta = index.raids.get(slug) if index else None
    killed, total = raiderio.progress_for(profile, slug)
    return discord.progress_embed(
        cfg["guild_name"], (meta or {}).get("name") or slug, killed or 0, total or "?",
        raiderio.realm_rank(profile, slug, "heroic"),
        thumbnail_url=raiderio.icon_url(meta),
        guild_label=raiderio.guild_display(profile, cfg["guild_name"], cfg["guild_realm"]),
        guild_url=raiderio.profile_url(profile, cfg["guild_region"], cfg["guild_realm"],
                                       cfg["guild_name"]),
        as_of=as_of)


def boss_art(cfg, boss_name, now_iso, fallback=None):
    """Per-boss art URL, falling back to the raid icon.

    Cached in DynamoDB permanently: a boss's creature display id never changes, so this is
    one Blizzard lookup per boss ever and none at all in steady state. Failures are cached
    too -- a boss Blizzard cannot resolve would otherwise be looked up again on every
    single announcement, spending the API budget to be told "no" repeatedly.

    Every failure path here returns the fallback. Art is decoration, and nothing about it
    is allowed to cost an announcement.
    """
    key = boss_key(boss_name)
    try:
        cached = store.get_art(key)
    except Exception as exc:                                   # noqa: BLE001
        log("art_cache_read_failed", boss=boss_name, error=repr(exc))
        return fallback
    if cached is not None:
        return cached["url"] or fallback

    if not (cfg.get("blizzard_client_id") and cfg.get("blizzard_client_secret")):
        return fallback

    try:
        token = blizzard.get_token(cfg["blizzard_client_id"], cfg["blizzard_client_secret"])
        display, url = blizzard.resolve(token, boss_name, raiderio.normalize)
    except blizzard.BlizzardError as exc:
        # Not cached: a transient Blizzard failure should be retried next time, unlike a
        # definitive "no such encounter", which is.
        log("art_lookup_failed", boss=boss_name, error=str(exc))
        return fallback

    try:
        store.put_art(key, display, url or "", now_iso)
    except Exception as exc:                                   # noqa: BLE001
        log("art_cache_write_failed", boss=boss_name, error=repr(exc))
    log("art_resolved", boss=boss_name, displayId=display, url=url or None,
        found=bool(url))
    return url or fallback


def guild_id(token, cfg):
    if _guild_id["value"]:
        return _guild_id["value"], None
    guild, rate = wcl.find_guild(token, cfg["guild_name"], cfg["guild_realm"],
                                 cfg["guild_region"])
    if not guild or not guild.get("id"):
        raise RuntimeError(
            f"Warcraft Logs has no guild '{cfg['guild_name']}' on "
            f"{cfg['guild_realm']}-{cfg['guild_region']}. Check the realm slug in SSM "
            f"({config.GUILD_REALM}) — it must be lowercase and hyphenated.")
    _guild_id["value"] = int(guild["id"])
    return _guild_id["value"], rate


def history_by_slug(token, gid, cfg, profile, index, days, limit, max_pages=1):
    """Every Heroic kill the log history can still see, grouped by raid slug."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    kills, rate = wcl.heroic_kills_since(token, gid, since, limit=limit,
                                         max_pages=max_pages)
    grouped, skipped = {}, {}
    for k in kills:
        slug, _meta, how = raiderio.resolve_raid(profile, k["name"], k.get("zoneName"),
                                                 _at(k["killedAtMs"]), cfg["guild_region"],
                                                 index)
        if slug:
            grouped.setdefault(slug, []).append(k)
        else:
            skipped.setdefault(how, set()).add(k["name"])
    for how, names in skipped.items():
        # Worth a line rather than a silent drop: "known-raid-not-tracked" is the healthy
        # case (an old expansion's tier being farmed), "unresolved" means a boss nothing
        # could identify, which is the one to look at if announcements go missing.
        log("kills_not_attributed", reason=how, bosses=sorted(names), count=len(names))
    return grouped, rate


def bootstrap(token, gid, pk, cfg, profile, index, now_iso):
    """First run for this guild: seed every tier, announce nothing.

    This function never calls discord.post, and handler() returns immediately after it.
    'Announces nothing' is therefore structural rather than a property that happens to
    fall out of the dedupe rules agreeing with each other.
    """
    grouped, rate = history_by_slug(token, gid, cfg, profile, index,
                                    SEED_LOOKBACK_DAYS, SEED_REPORT_LIMIT,
                                    max_pages=SEED_MAX_PAGES)
    progression = profile.get("raid_progression") or {}
    if not progression:
        raise RuntimeError(
            "Raider.IO returned no raid_progression for "
            f"{cfg['guild_name']}-{cfg['guild_realm']}. Refusing to bootstrap from an "
            "empty profile — an empty seed would announce the whole back catalogue.")

    summary = []
    for slug in progression:
        killed, total = raiderio.progress_for(profile, slug)
        meta = index.raids.get(slug) if index else None
        seen = [k["name"] for k in grouped.get(slug, [])]
        names, basis = raiderio.seed_names(meta, killed, total, seen)
        aotc_already = bool(total and killed is not None and killed >= total)
        label = (meta or {}).get("name") or slug
        # A baseline above the boss total is arithmetically impossible and is exactly what
        # manufactures a premature "8 of 8". Clamp it rather than trusting the inputs.
        baseline = min(killed or 0, total) if total else (killed or 0)
        created = store.seed_tier(pk, slug, names, baseline, label, now_iso,
                                  aotc_already=aotc_already)

        log("seeded_tier", slug=slug, raid=label, created=created, seededBosses=len(names),
            fromLogHistory=len(seen), basis=basis, raiderioKilled=killed, total=total,
            baseline=baseline, aotcPreset=aotc_already, announced=0)
        if basis == "assumed-kill-order":
            # Recorded rather than silent: the log history could not account for every
            # kill Raider.IO knows about, so the published boss order stood in for it.
            log("seed_assumption", slug=slug, raid=label, raiderioKilled=killed,
                fromLogHistory=len(seen),
                note="log history was short; seeded the first N bosses in published order")
        summary.append({"slug": slug, "bosses": len(names), "aotc": aotc_already})

    store.mark_bootstrapped(pk, now_iso, len(summary),
                            note="seeded from raid_progression + log history")
    log("bootstrap_complete", guild=cfg["guild_name"], realm=cfg["guild_realm"],
        tiers=len(summary), detail=summary, announced=0, points=rate,
        note="SEEDED, did not announce — no messages were posted on this run")


def seed_new_tier(token, gid, pk, cfg, slug, raid_label, profile, index,
                  window_start_ms, now_iso):
    """A raid slug seen for the first time AFTER bootstrap: a tier rollover.

    Returns True if the tier was seeded silently and its kills must not be announced.

    Raider.IO's count is deliberately not used to seed here. At rollover it is describing
    exactly the kills about to be announced, so seeding from it swallows the first boss of
    every new tier. What separates a rollover from a gap in coverage is whether any of the
    tier's history predates the poll window: a rollover has only fresh kills.
    """
    grouped, rate = history_by_slug(token, gid, cfg, profile, index,
                                    SEED_LOOKBACK_DAYS, SEED_REPORT_LIMIT,
                                    max_pages=SEED_MAX_PAGES)
    mine = grouped.get(slug, [])
    older = [k for k in mine if k["killedAtMs"] < window_start_ms]
    killed, total = raiderio.progress_for(profile, slug)
    silent = bool(older)

    names = {boss_key(k["name"]) for k in (mine if silent else [])}
    # A rollover seed starts empty and earns every kill through a claim, so its baseline
    # must be zero -- otherwise the first boss of a new tier announces as "2 of 8".
    baseline = (killed or 0) if silent else 0
    aotc_already = bool(total and killed is not None and killed >= total)
    created = store.seed_tier(pk, slug, names, baseline, raid_label, now_iso,
                              aotc_already=aotc_already)
    log("seeded_new_tier", slug=slug, raid=raid_label, created=created, silent=silent,
        seededBosses=len(names), historySeen=len(mine), olderThanWindow=len(older),
        baseline=baseline, raiderioKilled=killed, total=total, aotcPreset=aotc_already,
        points=rate)
    return silent


def announce_kill(cfg, pk, slug, raid_label, kill, state, profile, thumb=None,
                  now_iso=None):
    """Claim, then post. In that order -- see store.claim_boss."""
    key = boss_key(kill["name"])
    if not store.claim_boss(pk, slug, key):
        log("skip_rekill", slug=slug, boss=kill["name"], key=key)
        return False

    state["announced"].add(key)
    thumb = boss_art(cfg, kill["name"], now_iso or _iso(datetime.now(timezone.utc)),
                     fallback=thumb)
    r_killed, total = raiderio.progress_for(profile, slug)
    count = store.progress_count(state, r_killed, total)
    rank = raiderio.realm_rank(profile, slug, "heroic")
    killed_at = _at(kill["killedAtMs"])

    payload = discord.kill_embed(
        cfg["guild_name"], kill["name"], count, total or "?", raid_label, rank,
        report_url=report_url(kill.get("reportCode")), iso_ts=_iso(killed_at),
        thumbnail_url=thumb,
        guild_label=raiderio.guild_display(profile, cfg["guild_name"], cfg["guild_realm"]),
        guild_url=raiderio.profile_url(profile, cfg["guild_region"], cfg["guild_realm"],
                                       cfg["guild_name"]))
    try:
        discord.post(cfg["webhook"], payload)
    except discord.DiscordError as exc:
        # Hand the boss back so the next poll retries it. A missed announcement recovers
        # in fifteen minutes; a duplicate one never recovers at all.
        store.release_boss(pk, slug, key)
        state["announced"].discard(key)
        log("announce_failed", slug=slug, boss=kill["name"], error=str(exc))
        return False

    log("announced_kill", slug=slug, boss=kill["name"], key=key,
        encounterID=kill.get("encounterID"), count=count, total=total, realmRank=rank,
        raiderioKilled=r_killed,
        raiderioStale=bool(r_killed is not None and r_killed < count),
        killedAt=_iso(killed_at), report=kill.get("reportCode"))
    return True


def announce_aotc(cfg, pk, slug, raid_label, state, when, thumb=None, profile=None):
    if not store.claim_aotc(pk, slug):
        log("skip_aotc_already", slug=slug)
        return False
    payload = discord.aotc_payload(
        cfg["guild_name"], raid_label, _when_text(when), cfg["role_id"],
        iso_ts=_iso(when), thumbnail_url=thumb, repo_url=REPO_URL,
        guild_label=raiderio.guild_display(profile, cfg["guild_name"], cfg["guild_realm"]),
        guild_url=raiderio.profile_url(profile, cfg["guild_region"], cfg["guild_realm"],
                                       cfg["guild_name"]))
    try:
        discord.post(cfg["webhook"], payload)
    except discord.DiscordError as exc:
        store.release_aotc(pk, slug)
        log("aotc_failed", slug=slug, error=str(exc))
        return False
    state["aotcAnnounced"] = True
    log("announced_aotc", slug=slug, raid=raid_label, when=_iso(when),
        rolePinged=bool(cfg["role_id"]))
    return True


def preview(spec, cfg, token, gid, profile, index):
    """Render a kill card from REAL data without recording anything.

    Invoke with {"preview": {}} to see what an announcement will look like before a raid
    night produces one. It reads the guild's actual first Heroic kill of the current tier
    and dates the card with the real timestamp of that kill.

    It touches no state, by construction: nothing in this function calls store, so it
    cannot claim a boss. That matters more than it might appear -- a preview that took the
    ordinary path would mark the boss announced, and the guild's real first kill would then
    be correctly, silently, and permanently skipped. The whole point of the bot, defeated
    by the demo of it.

    {"preview": {"dry": true}} returns the payload instead of posting it.
    """
    kills, _rate = wcl.heroic_kills_since(
        token, gid, (datetime.now(timezone.utc)
                     - timedelta(days=SEED_LOOKBACK_DAYS)).timestamp() * 1000,
        limit=SEED_REPORT_LIMIT, max_pages=SEED_MAX_PAGES)

    attributed = []
    for k in kills:
        slug, meta, how = raiderio.resolve_raid(profile, k["name"], k.get("zoneName"),
                                                _at(k["killedAtMs"]), cfg["guild_region"],
                                                index)
        if slug:
            attributed.append((slug, meta, k))
    if not attributed:
        raise RuntimeError("No attributable Heroic kills found to preview.")

    # "The current tier" is the tier of the most recent kill -- derived from the logs, the
    # same way the announcer decides, rather than guessed from Raider.IO's progression.
    slug = spec.get("slug") or attributed[-1][0]
    in_tier = [(m, k) for s, m, k in attributed if s == slug]
    if spec.get("boss"):
        want = raiderio.normalize(spec["boss"])
        in_tier = [(m, k) for m, k in in_tier
                   if raiderio.normalize(k["name"]) == want] or in_tier

    # Earliest kill in that tier: the guild's actual first Heroic boss there.
    meta, kill = min(in_tier, key=lambda mk: mk[1]["killedAtMs"])
    _killed, total = raiderio.progress_for(profile, slug)
    rank = raiderio.realm_rank(profile, slug, "heroic")
    raid_label = kill.get("zoneName") or (meta or {}).get("name") or slug
    killed_at = _at(kill["killedAtMs"])

    payload = discord.kill_embed(
        cfg["guild_name"], kill["name"], int(spec.get("count", 1)), total or "?",
        raid_label, rank, report_url=report_url(kill.get("reportCode")),
        iso_ts=_iso(killed_at),
        thumbnail_url=boss_art(cfg, kill["name"], _iso(datetime.now(timezone.utc)),
                               fallback=raiderio.icon_url(meta)),
        guild_label=raiderio.guild_display(profile, cfg["guild_name"], cfg["guild_realm"]),
        guild_url=raiderio.profile_url(profile, cfg["guild_region"], cfg["guild_realm"],
                                       cfg["guild_name"]))

    info = {"boss": kill["name"], "raid": raid_label, "slug": slug,
            "killedAt": _iso(killed_at), "localTime": _when_text(killed_at),
            "count": int(spec.get("count", 1)), "total": total, "realmRank": rank,
            "report": kill.get("reportCode")}

    if spec.get("dry"):
        log("preview_dry_run", **info, stateWritten=False)
        return {"ok": True, "preview": True, "posted": False, "payload": payload, **info}

    discord.post(cfg["webhook"], payload)
    log("preview_posted", **info, stateWritten=False,
        note="PREVIEW — posted to Discord, no dedupe state was written")
    return {"ok": True, "preview": True, "posted": True, **info}


def _snapshot_age(snapshot, now):
    try:
        stamp = datetime.strptime(snapshot["updatedAt"], "%Y-%m-%dT%H:%M:%SZ")
        return (now - stamp.replace(tzinfo=timezone.utc)).total_seconds()
    except (KeyError, ValueError, TypeError):
        return None


def handle_progress(body, cfg, pk, now):
    """Answer /progress inside the three-second window if at all possible.

    The snapshot the poller leaves behind makes that a single GetItem. When it is missing
    or stale the answer needs a live Raider.IO call, which is exactly the kind of thing
    that turns a cold start into a failed interaction -- so that path defers and hands the
    work to a second, asynchronous invocation instead.
    """
    snapshot = None
    try:
        snapshot = store.get_snapshot(pk)
    except Exception as exc:                                   # noqa: BLE001
        log("snapshot_read_failed", error=repr(exc))

    age = _snapshot_age(snapshot, now) if snapshot else None
    if snapshot and age is not None and age <= SNAPSHOT_MAX_AGE:
        embed = discord.progress_embed(
            cfg["guild_name"], snapshot["raidName"], snapshot["killed"],
            snapshot["total"] or "?", snapshot["realmRank"],
            thumbnail_url=None,
            guild_label=f"{cfg['guild_name']} \u00b7 {cfg['guild_realm'].title()}",
            guild_url=raiderio.profile_url({}, cfg["guild_region"], cfg["guild_realm"],
                                           cfg["guild_name"]),
            as_of=snapshot["updatedAt"])
        log("progress_from_snapshot", ageSeconds=int(age), slug=snapshot["slug"])
        return interactions.message(embed, ephemeral=EPHEMERAL_REPLIES)

    # Slow path. Defer first -- a Lambda cannot answer later without having answered now.
    spec = {"application_id": body.get("application_id"), "token": body.get("token")}
    try:
        boto3.client("lambda", region_name=REGION).invoke(
            FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "ryangrey-greybot"),
            InvocationType="Event",
            Payload=json.dumps({"followup": spec}).encode("utf-8"))
        log("progress_deferred", snapshotAgeSeconds=age)
        return interactions.deferred(ephemeral=EPHEMERAL_REPLIES)
    except Exception as exc:                                   # noqa: BLE001
        # Could not defer. Say something rather than let the interaction time out with a
        # red "application did not respond" in the channel.
        log("progress_defer_failed", error=repr(exc))
        return interactions.message(
            {"description": "Progress is briefly unavailable — try again in a minute.",
             "color": discord.BRAND_ACCENT}, ephemeral=EPHEMERAL_REPLIES)


def handle_followup(spec, cfg, pk, now):
    """The deferred half: fetch live and PATCH the placeholder into the real answer."""
    profile = raiderio.guild_profile(cfg["guild_region"], cfg["guild_realm"],
                                     cfg["guild_name"])
    index, _exp = raiderio.build_index(profile, EXPANSION_HINT)
    snapshot = None
    try:
        snapshot = store.get_snapshot(pk)
    except Exception:                                          # noqa: BLE001
        pass
    embed = progress_embed_live(cfg, profile, index, snapshot)
    if not embed:
        embed = {"description": "No raid progress is available yet.",
                 "color": discord.BRAND_ACCENT}
    interactions.edit_followup(spec.get("application_id"), spec.get("token"), embed)
    log("progress_followup_sent", applicationId=spec.get("application_id"))
    return {"ok": True, "followup": True}


def handle_interaction(event, cfg, pk, now):
    """Verify, then dispatch. Verification is not optional and not conditional."""
    headers = interactions.lower_headers(event)
    body_bytes = interactions.raw_body(event)
    ok = interactions.verify(cfg.get("public_key"),
                             headers.get("x-signature-ed25519"),
                             headers.get("x-signature-timestamp"),
                             body_bytes)
    if not ok:
        # Discord probes this endpoint with deliberately invalid signatures. Answering
        # 200 to one of those costs the interactions URL.
        log("interaction_rejected", reason="bad_signature",
            hasKey=bool(cfg.get("public_key")))
        return interactions.unauthorized()

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except ValueError:
        return interactions.unauthorized()

    kind = body.get("type")
    if kind == interactions.PING:
        log("interaction_ping")
        return interactions.http(200, {"type": interactions.PONG})

    if kind == interactions.APPLICATION_COMMAND:
        name = interactions.command_name(body)
        log("interaction_command", command=name)
        if name == "progress":
            return interactions.http(200, handle_progress(body, cfg, pk, now))
        return interactions.http(200, interactions.message(
            {"description": f"Unknown command `{name}`.",
             "color": discord.BRAND_ACCENT}, ephemeral=True))

    log("interaction_ignored", type=kind)
    return interactions.http(200, {"type": interactions.PONG})


def handle_admin(action, cfg):
    if action != "register_commands":
        raise RuntimeError(f"unknown admin action: {action}")
    if not cfg.get("bot_token"):
        raise RuntimeError("no bot token in SSM — set /greybot/discord/bot_token")
    if not cfg.get("discord_guild_id"):
        raise RuntimeError("no guild id in SSM — set /greybot/discord/guild_id")
    app_id = interactions.application_id(cfg["bot_token"])
    result = interactions.register_guild_commands(
        cfg["bot_token"], app_id, cfg["discord_guild_id"], interactions.COMMANDS)
    names = [c.get("name") for c in (result or [])]
    log("commands_registered", applicationId=app_id, guildId=cfg["discord_guild_id"],
        commands=names)
    return {"ok": True, "applicationId": app_id, "commands": names}


def handler(event, context):
    started = time.time()
    cfg = config.load()
    log("config_loaded", **config.redacted(cfg))

    now = datetime.now(timezone.utc)
    now_iso = _iso(now)
    pk = store.guild_pk(cfg["guild_region"], cfg["guild_realm"], cfg["guild_name"])

    # Interactions first, and before any Warcraft Logs work: this path has three seconds
    # including cold start, and an OAuth round trip it does not need would spend them.
    if isinstance(event, dict):
        if event.get("requestContext", {}).get("http") or "x-signature-ed25519" in {
                str(k).lower() for k in (event.get("headers") or {})}:
            return handle_interaction(event, cfg, pk, now)
        if event.get("followup"):
            return handle_followup(event["followup"], cfg, pk, now)
        if event.get("admin"):
            return handle_admin(event["admin"], cfg)

    token = wcl.get_token(cfg["wcl_client_id"], cfg["wcl_client_secret"])

    gid, rate = guild_id(token, cfg)

    # The preview branch returns before any of the announcing machinery, so it cannot
    # reach a claim even by accident.
    if isinstance(event, dict) and event.get("preview") is not None:
        profile = raiderio.guild_profile(cfg["guild_region"], cfg["guild_realm"],
                                         cfg["guild_name"])
        index, _exp = raiderio.build_index(profile, EXPANSION_HINT)
        return preview(event["preview"] or {}, cfg, token, gid, profile, index)

    if rate is None:
        rate = wcl.rate_limit(wcl.query(token, wcl.RATE_ONLY_Q))
    if rate and rate["fraction"] >= POINTS_CEILING:
        log("rate_limit_backoff", **rate, ceiling=POINTS_CEILING)
        return {"ok": True, "skipped": "rate_limit", "points": rate}

    profile = raiderio.guild_profile(cfg["guild_region"], cfg["guild_realm"],
                                     cfg["guild_name"])
    index, expansions = raiderio.build_index(profile, EXPANSION_HINT)

    # The first-run branch. Nothing below this line can run until a bootstrap has been
    # recorded, so there is no ordering in which run one announces anything.
    if not store.is_bootstrapped(pk):
        bootstrap(token, gid, pk, cfg, profile, index, now_iso)
        return {"ok": True, "bootstrapped": True, "announced": 0,
                "ms": int((time.time() - started) * 1000)}

    window_start_ms = int((now - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)
    kills, rate = wcl.heroic_kills_since(token, gid, window_start_ms, limit=REPORT_LIMIT)

    if not kills:
        progression = profile.get("raid_progression") or {}
        # A public guild with progress but zero visible reports is the private-logs case:
        # a client-credentials token can only read public reports, so the event source is
        # blind and no amount of retrying will fix it. Say so plainly in the logs.
        if progression and any((v or {}).get("heroic_bosses_killed")
                               for v in progression.values()):
            log("no_reports_visible", guild=cfg["guild_name"], realm=cfg["guild_realm"],
                hint="Raider.IO shows Heroic kills but Warcraft Logs returned no reports — "
                     "the guild's logs are probably private to this OAuth client.")
        log("poll_idle", lookbackDays=LOOKBACK_DAYS, points=rate,
            ms=int((time.time() - started) * 1000))
        return {"ok": True, "kills": 0}

    # Resolve every kill to a raid from the boss that died, rather than deciding on one
    # "current tier" up front. A raid night that clears the new tier and then farms an old
    # one for mounts is two tiers in one report, and a single current-tier answer gets the
    # second half wrong.
    grouped = {}
    for k in kills:
        slug, meta, how = raiderio.resolve_raid(profile, k["name"], k.get("zoneName"),
                                                _at(k["killedAtMs"]), cfg["guild_region"],
                                                index)
        if not slug:
            log("unresolved_raid", boss=k["name"], zone=k.get("zoneName"))
            continue
        k["_how"] = how
        label = (meta or {}).get("name") or k.get("zoneName") or slug
        grouped.setdefault(slug, {"label": label, "meta": meta,
                                  "kills": []})["kills"].append(k)

    announced = 0
    newest_seen = {"ms": -1}
    for slug, bundle in grouped.items():
        # Prefer the Warcraft Logs zone name in the message: it is the name raiders use.
        # Raider.IO's is a data label and reads like one ("MN Tier 1 (VS / DR / MQD)").
        raid_label = bundle["kills"][0].get("zoneName") or bundle["label"]
        thumb = raiderio.icon_url(bundle.get("meta"))

        state = store.load_tier(pk, slug)
        if state is None:
            if seed_new_tier(token, gid, pk, cfg, slug, raid_label, profile, index,
                             window_start_ms, now_iso):
                continue
            state = store.load_tier(pk, slug) or {"announced": set(), "seedSize": 0,
                                                  "baseline": 0, "aotcAnnounced": False}

        last_announced_ms = None
        for kill in bundle["kills"]:
            if announce_kill(cfg, pk, slug, raid_label, kill, state, profile, thumb,
                             now_iso):
                announced += 1
                last_announced_ms = kill["killedAtMs"]

        r_killed, total = raiderio.progress_for(profile, slug)
        count = store.progress_count(state, r_killed, total)
        if total and count >= total and not state.get("aotcAnnounced"):
            # Date it by the kill that finished the tier. Falling back to the newest kill
            # in the window would date AOTC by a re-kill on a later farm night, in the case
            # where Raider.IO only caught up after the real clear.
            when_ms = last_announced_ms or bundle["kills"][-1]["killedAtMs"]
            announce_aotc(cfg, pk, slug, raid_label, state, _at(when_ms))

        store.touch(pk, slug, now_iso, raid_name=raid_label)
        # Leave the display values behind for /progress. Written for the tier of the most
        # recent kill, so the snapshot's notion of "current" comes from an actual kill
        # rather than from a heuristic over Raider.IO's progression.
        newest = max(bundle["kills"], key=lambda k: k["killedAtMs"])["killedAtMs"]
        if newest >= newest_seen["ms"]:
            newest_seen.update({"ms": newest, "slug": slug, "raid": raid_label,
                                "killed": count, "total": total,
                                "rank": raiderio.realm_rank(profile, slug, "heroic")})
        log("tier_summary", slug=slug, raid=raid_label,
            resolvedBy=bundle["kills"][0].get("_how"), killsSeen=len(bundle["kills"]),
            count=count, total=total, expansions=expansions)

    if newest_seen["ms"] >= 0:
        try:
            store.put_snapshot(pk, newest_seen["slug"], newest_seen["raid"],
                               newest_seen["killed"], newest_seen["total"],
                               newest_seen["rank"], now_iso)
        except Exception as exc:                               # noqa: BLE001
            # A snapshot is a convenience for /progress. Losing it must not fail a poll.
            log("snapshot_write_failed", error=repr(exc))

    log("poll_done", kills=len(kills), announced=announced, tiers=len(grouped),
        points=rate, ms=int((time.time() - started) * 1000))
    return {"ok": True, "kills": len(kills), "announced": announced}
