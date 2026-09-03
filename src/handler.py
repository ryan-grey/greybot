"""greyBot — announce first Heroic boss kills and AOTC, and recap raid nights, for
the guild Scrambled.

EventBridge Scheduler -> this Lambda -> Discord webhook. There is no gateway connection
because there is nothing to listen for: the bot announces and never responds.

TWO schedules, one function. The poller runs every fifteen minutes on an empty event; the
recap runs Wednesday and Friday mornings on {"mode": "recap"} and branches here. A second
Lambda would have meant a second copy of the config, the state access and the rate-limit
accounting, all of which would then be free to drift apart from these.

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
import health
import interactions
import notify
import raiderio
import recap as recap_mod
import scorecard
import keys
import store
import team
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

# How far back the recap looks for last night's report. It fires the morning after, and
# Scrambled raids 9pm to midnight Eastern, so eighteen hours from a 10am post reaches back
# to 4pm the previous afternoon -- comfortably before the pull and comfortably after the
# previous raid night. A raid that runs past midnight is still one night to this window,
# which is why the night is keyed on the report's START.
RECAP_LOOKBACK_HOURS = float(os.environ.get("RECAP_LOOKBACK_HOURS", "18"))

# Points that must be LEFT before the recap is allowed to start.
#
# This number has to be bigger than the headroom POINTS_CEILING already reserves, or it
# can never bind and is decoration: at 0.85 of a 3,600-point allowance the generic check
# stops everything with 540 points left, so any recap reserve below that is unreachable
# code. 750 makes the recap yield while the announcer still has room, which is the whole
# point of having two numbers -- the announcer is the higher-priority consumer and must
# never be starved by the recap.
#
# The measured cost of recapping one night is roughly 25 points, so this reserve is thirty
# times what the job needs. That asymmetry is deliberate. A recap that skips a week is
# fine; a first kill that goes unannounced is not.
RECAP_POINT_BUDGET = float(os.environ.get("RECAP_POINT_BUDGET", "750"))
RECAP_MAX_REPORTS = int(os.environ.get("RECAP_MAX_REPORTS", "4"))

# How long a Discord problem may sit before the alert is repeated. One mail per event is
# the rule -- ninety-six polls a day must not be ninety-six emails -- but a single mail
# that lands while Ryan is asleep and gets buried is a silent bot nobody knows about, so
# the state re-announces itself once a day until it clears. Zero disables the repeat.
HEALTH_REMIND_HOURS = float(os.environ.get("HEALTH_REMIND_HOURS", "24"))

# Consecutive polls with the log source visibly empty before that counts as an outage.
# Four is an hour at the fifteen-minute cadence -- long enough that a bad minute at
# Warcraft Logs passes unremarked, short enough that a real outage is a morning's problem
# rather than something found eighteen hours later by asking.
SOURCE_BLIND_POLLS = int(os.environ.get("SOURCE_BLIND_POLLS", "4"))

# Where this function invokes ITSELF to finish a slow /progress. Every other module reads
# AWS_REGION for its own client; this one referenced a REGION that was never defined here,
# so the deferral raised NameError and the command answered "briefly unavailable" instead.
# It went unnoticed because the fast path covered it whenever the snapshot was fresh.
REGION = os.environ.get("AWS_REGION", "us-east-1")

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


def bootstrap(token, gid, scope, cfg, profile, index, now_iso):
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
        created = store.seed_tier(scope, slug, names, baseline, label, now_iso,
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

    store.mark_bootstrapped(scope, now_iso, len(summary),
                            note="seeded from raid_progression + log history")
    log("bootstrap_complete", guild=cfg["guild_name"], realm=cfg["guild_realm"],
        tiers=len(summary), detail=summary, announced=0, points=rate,
        note="SEEDED, did not announce — no messages were posted on this run")


def seed_new_tier(token, gid, scope, cfg, slug, raid_label, profile, index,
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
    created = store.seed_tier(scope, slug, names, baseline, raid_label, now_iso,
                              aotc_already=aotc_already)
    log("seeded_new_tier", slug=slug, raid=raid_label, created=created, silent=silent,
        seededBosses=len(names), historySeen=len(mine), olderThanWindow=len(older),
        baseline=baseline, raiderioKilled=killed, total=total, aotcPreset=aotc_already,
        points=rate)
    return silent


def record_roster(token, cfg, scope, slug, boss_key, kill, now_iso):
    """Who was standing there for a first kill, recorded for the prog roster.

    Called only after an announcement actually went out, and only for a boss that was
    genuinely claimed -- so this runs a handful of times a tier, not once a poll. That is
    what makes an extra Warcraft Logs query affordable here and unaffordable in the poll
    itself, where the same data would mean pulling a 225KB actor list every fifteen
    minutes forever.

    Every failure is swallowed. The announcement has already been posted by this point and
    nothing about the roster is allowed to reach back and affect it; a missing first kill
    costs the roster one sample, which the frequency rule is built to tolerate.
    """
    if not token or not kill.get("reportCode") or not kill.get("encounterID"):
        return False
    try:
        people, rate = wcl.kill_participants(token, kill["reportCode"],
                                             kill["encounterID"])
        players = {team.player_key(p["name"], p.get("server")) for p in people}
        wrote = store.record_first_kill(scope, slug, boss_key, players,
                                        kill["killedAtMs"], kill["reportCode"], now_iso)
        log("first_kill_roster_recorded", slug=slug, boss=kill["name"], players=len(players),
            written=wrote, report=kill["reportCode"], points=rate)
        return wrote
    except Exception as exc:                                   # noqa: BLE001
        log("first_kill_roster_failed", slug=slug, boss=kill.get("name"), error=repr(exc),
            note="the announcement was already posted; the roster loses one sample")
        return False


def announce_kill(cfg, scope, slug, raid_label, kill, state, profile, thumb=None,
                  now_iso=None, token=None):
    """Claim, then post. In that order -- see store.claim_boss."""
    key = boss_key(kill["name"])

    # The subtitle guard, checked BEFORE the claim. Raider.IO's encounter list says
    # "Dimensius" where the logs say "Dimensius, the All-Devouring", so a tier seeded from
    # Raider.IO -- which is what happens when the log history is unavailable, the case
    # seeding exists for -- records a key the next kill will not match. Without this the
    # bot announces a first kill for a boss it was explicitly told was already dead.
    #
    # It only ever suppresses. The claim below stays an exact conditional write, so this
    # cannot authorise an announcement, only decline one, and a decline recovers on the
    # next poll while a duplicate never does.
    prior = raiderio.alias_match(state.get("announced") or (), key)
    if prior and prior != key:
        log("skip_rekill_alias", slug=slug, boss=kill["name"], key=key, matchedSeed=prior,
            note="already recorded under a shorter or longer form of the same name")
        return False

    if not store.claim_boss(scope, slug, key):
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
                                       cfg["guild_name"]),
        world_boss=raiderio.is_world_boss(profile, slug))
    try:
        discord.post_to(destination(cfg), payload)
    except discord.DiscordError as exc:
        # Hand the boss back so the next poll retries it. A missed announcement recovers
        # in fifteen minutes; a duplicate one never recovers at all.
        store.release_boss(scope, slug, key)
        state["announced"].discard(key)
        log("announce_failed", slug=slug, boss=kill["name"], error=str(exc))
        return False

    record_roster(token, cfg, scope, slug, key, kill,
                  now_iso or _iso(datetime.now(timezone.utc)))
    log("announced_kill", slug=slug, boss=kill["name"], key=key,
        encounterID=kill.get("encounterID"), count=count, total=total, realmRank=rank,
        raiderioKilled=r_killed,
        raiderioStale=bool(r_killed is not None and r_killed < count),
        killedAt=_iso(killed_at), report=kill.get("reportCode"))
    return True


def announce_aotc(cfg, scope, slug, raid_label, state, when, thumb=None, profile=None):
    if not store.claim_aotc(scope, slug):
        log("skip_aotc_already", slug=slug)
        return False
    payload = discord.aotc_payload(
        cfg["guild_name"], raid_label, _when_text(when), cfg["role_id"],
        iso_ts=_iso(when), thumbnail_url=thumb, repo_url=REPO_URL,
        guild_label=raiderio.guild_display(profile, cfg["guild_name"], cfg["guild_realm"]),
        guild_url=raiderio.profile_url(profile, cfg["guild_region"], cfg["guild_realm"],
                                       cfg["guild_name"]))
    try:
        discord.post_to(destination(cfg), payload)
    except discord.DiscordError as exc:
        store.release_aotc(scope, slug)
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

    discord.post_to(destination(cfg), payload)
    log("preview_posted", **info, stateWritten=False,
        note="PREVIEW — posted to Discord, no dedupe state was written")
    return {"ok": True, "preview": True, "posted": True, **info}


def _snapshot_age(snapshot, now):
    try:
        stamp = datetime.strptime(snapshot["updatedAt"], "%Y-%m-%dT%H:%M:%SZ")
        return (now - stamp.replace(tzinfo=timezone.utc)).total_seconds()
    except (KeyError, ValueError, TypeError):
        return None


def handle_progress(body, cfg, scope, now):
    """Answer /progress inside the three-second window if at all possible.

    The snapshot the poller leaves behind makes that a single GetItem. When it is missing
    or stale the answer needs a live Raider.IO call, which is exactly the kind of thing
    that turns a cold start into a failed interaction -- so that path defers and hands the
    work to a second, asynchronous invocation instead.
    """
    snapshot = None
    try:
        snapshot = store.get_snapshot(scope)
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
    # guild_id travels with the spec so the deferred half can answer as the server
    # that asked. Without it the follow-up would resolve its scope from the
    # poller's SSM config and reply to server B with server A's progress.
    spec = {"application_id": body.get("application_id"), "token": body.get("token"),
            "guild_id": body.get("guild_id")}
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


def handle_followup(spec, cfg, scope, now):
    """The deferred half: fetch live and PATCH the placeholder into the real answer."""
    if (spec or {}).get("kind") == "setup":
        result = handle_setup(spec.get("body") or {}, _iso(now))
        interactions.edit_followup(spec.get("application_id"), spec.get("token"),
                                   result["embed"])
        log("setup_followup_sent", ok=result["ok"])
        return {"ok": result["ok"], "followup": True, "setup": True}

    # Resolve the asking server again here rather than trusting the scope this
    # invocation was constructed with — the follow-up runs as a fresh Lambda
    # invocation, so its `scope` is the poller's, not the caller's.
    if (spec or {}).get("guild_id"):
        caller_scope, caller_cfg = interaction_scope(spec, cfg)
        if caller_scope is not None:
            scope, cfg = caller_scope, caller_cfg

    profile = raiderio.guild_profile(cfg["guild_region"], cfg["guild_realm"],
                                     cfg["guild_name"])
    index, _exp = raiderio.build_index(profile, EXPANSION_HINT)
    snapshot = None
    try:
        snapshot = store.get_snapshot(scope)
    except Exception:                                          # noqa: BLE001
        pass
    embed = progress_embed_live(cfg, profile, index, snapshot)
    if not embed:
        embed = {"description": "No raid progress is available yet.",
                 "color": discord.BRAND_ACCENT}
    interactions.edit_followup(spec.get("application_id"), spec.get("token"), embed)
    log("progress_followup_sent", applicationId=spec.get("application_id"))
    return {"ok": True, "followup": True}


def handle_setup(body, now_iso):
    """`/setup` — point this server at a WoW guild and a channel.

    Defers first. Validating the guild means a Raider.IO round trip, and spending
    a three-second budget on somebody else's API is how an interaction visibly
    fails in the channel. Type 5 now, the real answer via follow-up.

    NOTHING IS SAVED UNTIL THE GUILD RESOLVES. A typo'd realm that wrote a config
    row would leave the server subscribed to a guild that does not exist, and the
    only symptom would be a bot that never posts -- indistinguishable from a quiet
    week. Failing here, loudly, is the whole reason validation is in this path.
    """
    tenant = keys.tenant_from_interaction(body)
    opts = interactions.command_options(body)
    region = str(opts.get("region") or "").strip().lower()
    realm = str(opts.get("realm") or "").strip()
    guild = str(opts.get("guild") or "").strip()
    channel = str(opts.get("channel") or "").strip()
    role = str(opts.get("role") or "").strip()
    who = ((body.get("member") or {}).get("user") or {}).get("id") or ""

    try:
        profile = raiderio.guild_profile(region, realm, guild)
    except Exception as exc:                                   # noqa: BLE001
        log("setup_guild_lookup_failed", tenant=tenant, region=region,
            realm=realm, guild=guild, error=repr(exc))
        return {"ok": False, "embed": {
            "title": "Could not find that guild",
            "description": (f"Raider.IO has no **{guild}** on **{realm}** ({region.upper()}).\n\n"
                            "Check the spelling of the realm and guild, and note that "
                            "the guild needs at least one logged Heroic kill before "
                            "Raider.IO knows about it."),
            "color": discord.BRAND_ACCENT}}

    store.put_config(tenant, region, realm, guild, channel, now_iso,
                     prog_role_id=role, configured_by=who)
    # Registry AFTER the config, deliberately. The poller reads the registry and
    # then each config; a tenant listed before its config exists is one the very
    # next poll would pick up and find nothing for.
    store.register_tenant(tenant)
    log("setup_saved", tenant=tenant, region=region, realm=realm,
        guild=guild, channel=channel, hasRole=bool(role))

    return {"ok": True, "embed": {
        "title": "greyBot is set up",
        "description": (f"Tracking **{profile.get('name') or guild}** on "
                        f"**{profile.get('realm') or realm}** ({region.upper()}).\n"
                        f"Announcements go to <#{channel}>."
                        + (f"\nMentioning <@&{role}> on a first kill." if role else "")
                        + "\n\nThe next poll seeds what is already dead and announces "
                          "nothing. Kills after that get posted."),
        "color": discord.BRAND_ACCENT}}


def interaction_scope(body, cfg):
    """The Scope and config for the server an interaction came from.

    THIS IS THE CROSS-TENANT BOUNDARY. Answering an interaction from the scope
    the poller happens to hold would mean a command run in server B replying with
    server A's data — the exact leak the key layout exists to prevent, arriving
    through the one code path that takes input from strangers.

    The tenant comes off the verified payload; the guild it maps to comes from
    that tenant's own CONFIG row. Neither is taken from the request.

    Returns (scope, merged cfg). On failure returns (None, reason) where reason
    is "dm" or "unconfigured" — both are real answers to give a user, and neither
    should reach the caller as an exception. An unhandled raise here would return
    a 500 to Discord, which looks to the person who typed the command like the
    bot is broken rather than like they need to do something.
    """
    try:
        tenant = keys.tenant_from_interaction(body)
    except ValueError:
        return None, "dm"
    row = store.get_config(tenant)
    if not row:
        # Fall back to the SSM install ONLY when it is literally this server, so
        # the original single-tenant deployment keeps answering /progress before
        # its CONFIG row exists. Any other server gets told to run /setup.
        if cfg.get("discord_guild_id") and tenant == keys.tenant_pk(cfg["discord_guild_id"]):
            return keys.Scope.build(cfg["guild_region"], cfg["guild_realm"],
                                    cfg["guild_name"], cfg["discord_guild_id"]), cfg
        return None, "unconfigured"
    merged = dict(cfg)
    merged.update({k: v for k, v in row.items() if v})
    return store.scope_for(tenant, row), merged


def handle_interaction(event, cfg, scope, now):
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
            # Answer as the server that asked, not as whichever install the
            # poller's config happens to name.
            caller_scope, caller_cfg = interaction_scope(body, cfg)
            if caller_scope is None:
                why = ("`/progress` only works inside a server."
                       if caller_cfg == "dm" else
                       "This server has not been set up yet — run `/setup` first.")
                return interactions.http(200, interactions.message(
                    {"description": why, "color": discord.BRAND_ACCENT}, ephemeral=True))
            return interactions.http(200,
                                     handle_progress(body, caller_cfg, caller_scope, now))
        if name == "setup":
            # Belt and braces on top of default_member_permissions. That field is
            # a default a server admin can override in Discord's UI, so it is not
            # by itself an authorisation check. 0x20 is MANAGE_GUILD.
            perms = int(str(body.get("member", {}).get("permissions") or "0") or 0)
            if not perms & 0x20:
                log("setup_denied", reason="missing_manage_guild")
                return interactions.http(200, interactions.message(
                    {"description": "You need **Manage Server** to run `/setup`.",
                     "color": discord.BRAND_ACCENT}, ephemeral=True))
            # Defer and hand the Raider.IO lookup to a second invocation, the
            # same shape /progress uses for its cold path. Validating inside the
            # three-second window would gamble the interaction on someone else's
            # API being quick.
            spec = {"kind": "setup", "body": body,
                    "application_id": body.get("application_id"),
                    "token": body.get("token")}
            try:
                boto3.client("lambda", region_name=REGION).invoke(
                    FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME",
                                                "ryangrey-greybot"),
                    InvocationType="Event",
                    Payload=json.dumps({"followup": spec}).encode("utf-8"))
                log("setup_deferred")
                return interactions.http(200, interactions.deferred(ephemeral=True))
            except Exception as exc:                           # noqa: BLE001
                log("setup_defer_failed", error=repr(exc))
                return interactions.http(200, interactions.message(
                    {"description": "Setup is briefly unavailable — try again in a minute.",
                     "color": discord.BRAND_ACCENT}, ephemeral=True))
        return interactions.http(200, interactions.message(
            {"description": f"Unknown command `{name}`.",
             "color": discord.BRAND_ACCENT}, ephemeral=True))

    log("interaction_ignored", type=kind)
    return interactions.http(200, {"type": interactions.PONG})


def _reminder_due(prev, now):
    """Has a still-broken state gone un-mentioned for long enough to say it again?

    An unparseable or missing notifiedAt reads as due. The stored timestamp is only ever
    written by this code, so a bad one means something is already wrong, and the failure
    that costs nothing is one extra email.
    """
    if HEALTH_REMIND_HOURS <= 0:
        return False
    raw = (prev or {}).get("notifiedAt") or ""
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - last) >= timedelta(hours=HEALTH_REMIND_HOURS)


def refresh_snapshot(scope, cfg, profile, index, now_iso):
    """Write the /progress snapshot from the Raider.IO profile this poll already holds.

    Deliberately weaker than the snapshot the announcer writes, and it never overwrites a
    better answer with a worse one in the way that matters: the announcer derives its tier
    from a kill it actually saw, this derives it from display_tier's heuristic. Both write
    the same counts, because both read them from the same profile.

    Best effort in the strongest sense -- a snapshot is a convenience for a slash command
    and losing it must never fail a poll.
    """
    slug = display_tier(profile, None)
    if not slug:
        return
    killed, total = raiderio.progress_for(profile, slug)
    meta = index.raids.get(slug) if index else None
    try:
        store.put_snapshot(scope, slug, (meta or {}).get("name") or slug,
                           killed or 0, total or 0,
                           raiderio.realm_rank(profile, slug, "heroic"), now_iso)
    except Exception as exc:                                   # noqa: BLE001
        log("snapshot_write_failed", error=repr(exc), where="idle")


def _alert_kind(prev, status, now, forced=False):
    """Which email a transition warrants, if any. Pure, and shared.

    Two independent signals use this rule -- Discord standing and whether the log source
    can be read -- and they must not drift apart. A fix to the reminder cadence or to the
    first-run guard has to land in one place, not in two functions that were the same
    shape on the day they were written.

    The first-run guard is the subtle one: a brand new state with nothing recorded before
    is not a recovery, so a fresh deploy does not mail to say nothing is wrong. A first run
    that finds a REAL problem still alerts, because that is a fact worth having on the day
    it is true rather than the day after.
    """
    prev_status = (prev or {}).get("status") or ""
    changed = status != prev_status
    first_run = not prev_status
    if status != health.OK and (changed or forced):
        return health.ALERT
    if status != health.OK and _reminder_due(prev, now):
        return health.REMINDER
    if status == health.OK and changed and not first_run:
        return health.RECOVERY
    if forced:
        return health.TEST
    return None


def run_source_check(cfg, scope, now, now_iso, blind, detail=None):
    """Can greyBot still SEE the raid logs it exists to read?

    A different axis from health.py entirely, and the one that was missing. On 2026-08-31
    Warcraft Logs began answering every report query with an empty list -- for every guild,
    not just this one -- and greyBot went eighteen hours unable to detect a kill while every
    Discord probe reported ok, because Discord was fine. The bot was perfectly healthy and
    completely blind, and the only reason anybody found out was that Ryan asked.

    BLINDNESS IS NOT QUIETNESS, and telling them apart is the whole difficulty. A guild that
    has not raided in three days legitimately has no kills in the window. The distinction is
    made on REPORTS, not kills: reports visible and no new kills is a quiet week; zero
    reports visible while Raider.IO still shows Heroic progress is a source that has stopped
    answering.

    Consecutive polls, not one. A single empty answer is a bad minute at Warcraft Logs, and
    the same rule holds here as for Discord -- only a state that persists is an event.
    """
    prev = store.get_source(scope) or {}
    streak = int(prev.get("blindPolls") or 0)
    streak = streak + 1 if blind else 0

    # Below the threshold the state is recorded and nothing is said. That is what makes the
    # streak survive a cold start: the count lives in DynamoDB, not in this container.
    status = health.SOURCE_BLIND if streak >= SOURCE_BLIND_POLLS else health.OK
    prev_status = prev.get("status") or ""
    changed = status != prev_status
    since = now_iso if changed else (prev.get("since") or now_iso)
    kind = _alert_kind(prev, status, now)

    sent = False
    if kind:
        result = health.source_result(status, streak, SOURCE_BLIND_POLLS, detail or {})
        try:
            sent = notify.publish(
                cfg.get("alert_topic_arn"),
                health.subject(kind, status, cfg.get("guild_name") or ""),
                health.body(kind, result, cfg, now_iso, since=since))
        except notify.NotifyError as exc:
            log("source_alert_undeliverable", status=status, kind=kind, error=str(exc))

    if changed or sent or streak != int(prev.get("blindPolls") or 0):
        store.put_source(scope, status, streak, since,
                         now_iso if sent else (prev.get("notifiedAt") or ""))

    log("source_checked", status=status, blind=blind, blindPolls=streak,
        threshold=SOURCE_BLIND_POLLS, previous=prev_status or None,
        notified=kind if sent else None, **(detail or {}))
    return status


def run_health_check(cfg, scope, now, now_iso, forced=False):
    """Probe Discord, and mail on the TRANSITION rather than on the state.

    The whole design lives in one rule: a definite answer that differs from the last
    definite answer is an event, and nothing else is. Ninety-six polls a day against a
    bot that was kicked on Tuesday must produce one email on Tuesday, a reminder each day
    after, and one more when it is fixed.

    An indefinite answer -- a timeout, a 502, a rate limit -- is not written down at all.
    That is the important half. If Discord being unreachable were recorded as a state, the
    next successful poll would read as a change and mail an all-clear for an outage that
    never happened, and worse, an unreachable Discord during a real kick would clear the
    alert.

    Nothing in here may take the poll down with it, which is why the caller wraps it: an
    announcement that failed to go out because the health check could not reach SNS would
    be this feature causing exactly the outage it exists to report.
    """
    result = health.check(cfg, now)
    prev = store.get_health(scope) or {}
    prev_status = prev.get("status") or ""
    status = result["status"]

    if not result["definite"]:
        log("health_unknown", previous=prev_status or None, probes=result["probes"])
        return result

    # Losing a bot member is a REGRESSION, not a state, and only the stored answer knows
    # whether there was ever a member to lose. greyBot was authorised with
    # `applications.commands` and never with `bot`, so it has no member and never has --
    # reading that absence as a kick is what mailed a false alarm on the first live run.
    # health.py reports; the comparison lives here because the comparison needs history.
    member = result.get("member")
    if prev.get("member") is True and member is False and status == health.OK:
        status = health.NOT_A_MEMBER
        result = dict(result, status=status,
                      cause={"probe": "membership", "verdict": status,
                             "note": "a bot member existed on the last check"})

    changed = status != prev_status
    since = now_iso if changed else (prev.get("since") or now_iso)
    kind = _alert_kind(prev, status, now, forced=forced)

    sent = False
    if kind:
        try:
            sent = notify.publish(
                cfg.get("alert_topic_arn"),
                health.subject(kind, status, cfg.get("guild_name") or ""),
                health.body(kind, result, cfg, now_iso, since=since))
        except notify.NotifyError as exc:
            # Logged and swallowed. The state is still written below, so a topic that comes
            # back tomorrow sends the reminder rather than pretending the day was fine.
            log("health_alert_undeliverable", status=status, kind=kind, error=str(exc))

    # The member flag has to be able to move the write on its own. Gaining or losing a seat
    # while the status stays "ok" is not an event and mails nothing -- but it IS the fact
    # the regression rule reads next time, so skipping the write leaves that rule comparing
    # against a stale answer. That is not hypothetical: greyBot was authorised with the bot
    # scope, the next check saw member=true, status stayed ok, nothing was written, and the
    # stored flag sat at false. A kick of the member would then have compared false against
    # false and said nothing.
    seat_moved = member is not None and member != prev.get("member")
    if changed or sent or seat_moved:
        store.put_health(scope, status, json.dumps(result["cause"], sort_keys=True)
                         if result["cause"] else "", since,
                         now_iso if sent else (prev.get("notifiedAt") or ""),
                         member=member if member is not None else prev.get("member"))

    log("health_checked", status=status, previous=prev_status or None, changed=changed,
        since=since, notified=kind if sent else None, botMember=member,
        alertsConfigured=bool(cfg.get("alert_topic_arn")), probes=result["probes"])
    return result


def handle_admin(event, cfg, scope, now, now_iso):
    action = event.get("admin")
    if action == "health":
        # The manual path, and the only one that mails while everything is fine -- which
        # is how the SNS grant and the SES forwarder get proved end to end without waiting
        # for something to actually go wrong.
        return {"ok": True, "health": run_health_check(cfg, scope, now, now_iso,
                                                       forced=bool(event.get("notify")))}
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


def destination(cfg):
    """Where this install posts.

    A configured tenant posts to its channel with the bot token; the legacy
    single-tenant install posts through the webhook it has always used. Resolved
    in one place so no announce path has to know which kind of install it is
    serving.
    """
    if cfg.get("channel_id") and cfg.get("bot_token"):
        return {"bot_token": cfg["bot_token"], "channel": cfg["channel_id"]}
    return {"webhook": cfg.get("webhook")}


def tenant_configs(cfg):
    """Every install this poll should run for, as (scope, merged cfg) pairs.

    Secrets stay central and per-install settings come from the CONFIG row: WCL
    and Blizzard credentials, the bot token and the alert topic are the operator's
    and identical for everyone, while the guild and channel belong to the tenant.

    An EMPTY REGISTRY FALLS BACK TO THE SSM CONFIG, and that is the migration
    bridge rather than an oversight. Prod is running from SSM right now; this has
    to keep it running until its rows are migrated and it is registered as tenant
    #1. Once the registry has entries the fallback never fires again.

    A registered tenant with no CONFIG row is skipped and logged rather than
    raising. One broken install must not stop the others polling -- that is the
    per-tenant isolation the brief asks for, and the cheapest place to honour it
    is here.
    """
    pairs, missing = [], []
    for tenant in store.list_tenants():
        row = store.get_config(tenant)
        if not row:
            missing.append(tenant)
            continue
        merged = dict(cfg)
        merged.update({k: v for k, v in row.items() if v})
        pairs.append((store.scope_for(tenant, row), merged))
    if missing:
        log("tenants_missing_config", tenants=missing,
            note="registered but never configured — skipped, not fatal")
    if pairs:
        return pairs

    log("no_registered_tenants", note="falling back to the SSM single-tenant config")
    legacy = keys.Scope.build(cfg["guild_region"], cfg["guild_realm"],
                              cfg["guild_name"], cfg["discord_guild_id"])
    return [(legacy, cfg)]


def handler(event, context):
    started = time.time()
    cfg = config.load()
    log("config_loaded", **config.redacted(cfg))

    now = datetime.now(timezone.utc)
    now_iso = _iso(now)
    # One Scope for the whole invocation: the shared WoW partition and this
    # install's tenant partition. The Discord guild id comes from configuration
    # written by a verified interaction, never from a request body -- see
    # keys.tenant_pk.
    scope = keys.Scope.build(cfg["guild_region"], cfg["guild_realm"],
                             cfg["guild_name"], cfg["discord_guild_id"])

    # Interactions first, and before any Warcraft Logs work: this path has three seconds
    # including cold start, and an OAuth round trip it does not need would spend them.
    if isinstance(event, dict):
        if event.get("requestContext", {}).get("http") or "x-signature-ed25519" in {
                str(k).lower() for k in (event.get("headers") or {})}:
            return handle_interaction(event, cfg, scope, now)
        if event.get("followup"):
            return handle_followup(event["followup"], cfg, scope, now)
        if event.get("admin"):
            return handle_admin(event, cfg, scope, now, now_iso)

    # Fan out. One poll per registered install, each on its own Scope and its own
    # merged config. A failure in one tenant is caught and logged rather than
    # raised: the poller is a single Lambda serving every install, so an
    # exception escaping here would stop every OTHER tenant being polled too --
    # one server's revoked channel must not silence the rest.
    results, failed = [], []
    for tenant_scope, tenant_cfg in tenant_configs(cfg):
        try:
            results.append(poll_one(event, tenant_cfg, tenant_scope, now, now_iso, started))
        except Exception as exc:                                   # noqa: BLE001
            log("tenant_poll_failed", tenant=tenant_scope.tenant, error=repr(exc))
            failed.append(tenant_scope.tenant)

    # One tenant keeps the old single-install response shape, so nothing that
    # reads this return value -- the tests, a manual invoke -- had to change.
    if len(results) == 1 and not failed:
        return results[0]
    return {"ok": not failed, "tenants": len(results) + len(failed),
            "failed": failed, "results": results}


def poll_one(event, cfg, scope, now, now_iso, started):
    """One install's poll. Everything below here is per-tenant.

    Extracted from handler() so the poll can run once per registered install.
    It was already written against a single `cfg` and `scope`; the fan-out is a
    loop around it rather than a rewrite of it, which is why the announce, seed
    and claim logic is untouched.
    """
    # Standing in the Discord, checked BEFORE any Warcraft Logs work. Every other branch
    # below this point can return early -- rate-limit backoff, an idle poll, a recap that
    # yields its budget -- and a check placed after any of them would go dark exactly when
    # the bot went quiet, which is the moment it is for.
    #
    # Wrapped because it is an observer. Nothing it can do, including SNS being unreachable
    # or Discord returning something this code has never seen, is allowed to stop an
    # announcement going out.
    try:
        run_health_check(cfg, scope, now, now_iso)
    except Exception as exc:                                       # noqa: BLE001
        log("health_check_error", error=repr(exc))

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

    # The second schedule. Same function, same package, one branch -- not a parallel stack.
    # It sits above the bootstrap branch rather than below it because a recap before the
    # guild has ever been seeded has no announced set to reason about, and would classify
    # every boss as fresh progression.
    if isinstance(event, dict) and str(event.get("mode") or "").lower() == "recap":
        if not store.is_bootstrapped(scope):
            log("recap_before_bootstrap",
                note="the guild has not been seeded yet — nothing to recap against")
            return {"ok": True, "skipped": "not_bootstrapped"}
        return recap_night(token, cfg, scope, now, now_iso, gid, profile, index, started,
                           dry=bool(event.get("dry")), hours=event.get("hours"),
                           manual=bool(event.get("manual")))

    # The first-run branch. Nothing below this line can run until a bootstrap has been
    # recorded, so there is no ordering in which run one announces anything.
    if not store.is_bootstrapped(scope):
        bootstrap(token, gid, scope, cfg, profile, index, now_iso)
        return {"ok": True, "bootstrapped": True, "announced": 0,
                "ms": int((time.time() - started) * 1000)}

    window_start_ms = int((now - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)
    kills, rate = wcl.heroic_kills_since(token, gid, window_start_ms, limit=REPORT_LIMIT)

    if not kills:
        # No kills is two completely different situations wearing the same face, and the
        # bot spent eighteen hours unable to tell them apart. A guild that has not raided
        # in three days has no kills in the window and is perfectly fine. A source that has
        # stopped answering has no kills either, and is an outage.
        #
        # REPORTS are what separates them, so ask -- but only here, in the ambiguous case.
        # reports_in_window carries no fights subquery, so it costs a fraction of the paged
        # announcer query, and a normal poll that found kills never pays for it at all.
        progression = profile.get("raid_progression") or {}
        heroic = sum(int((v or {}).get("heroic_bosses_killed") or 0)
                     for v in progression.values())
        seen, seen_rate = wcl.reports_in_window(
            token, gid, window_start_ms, int(now.timestamp() * 1000), limit=5)
        rate = seen_rate or rate
        blind = heroic > 0 and not seen

        if blind:
            # Deliberately NOT "the guild's logs are private any more". That hint was a
            # guess baked into a log line, and on 2026-08-31 it sent the whole
            # investigation the wrong way for an hour -- the real cause was Warcraft Logs
            # returning empty for every guild in the game, not this one's settings. State
            # the observation; leave the diagnosis to whoever reads it.
            log("no_reports_visible", guild=cfg["guild_name"], realm=cfg["guild_realm"],
                heroicKillsPerRaiderIO=heroic, reportsVisible=0,
                hint="Warcraft Logs returned no reports while Raider.IO still shows Heroic "
                     "progress. Could be private logs, could be the API — check whether "
                     "ANOTHER guild returns reports before concluding. "
                     "See docs/wcl-reportdata-blind.md.")

        run_source_check(cfg, scope, now, now_iso, blind,
                         detail={"heroicKills": heroic, "reportsVisible": len(seen)})

        # Refresh the /progress snapshot even though nothing was announced. It used to be
        # written only on the kills path, so a guild that had not killed anything in an
        # hour -- which is most hours -- left a stale snapshot, and /progress fell through
        # to the deferred live fetch every time. That is a three-second budget spent on a
        # cold start and a Raider.IO round trip for an answer already sitting in the
        # profile this poll ALREADY fetched. Free here, and it keeps the slow path genuinely
        # exceptional rather than routine.
        refresh_snapshot(scope, cfg, profile, index, now_iso)
        log("poll_idle", lookbackDays=LOOKBACK_DAYS, points=rate, reportsVisible=len(seen),
            ms=int((time.time() - started) * 1000))
        return {"ok": True, "kills": 0}

    # Kills came back, so the source is answering. Nothing to ask and nothing to pay for.
    run_source_check(cfg, scope, now, now_iso, False)

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

        state = store.load_tier(scope, slug)
        if state is None:
            if seed_new_tier(token, gid, scope, cfg, slug, raid_label, profile, index,
                             window_start_ms, now_iso):
                continue
            state = store.load_tier(scope, slug) or {"announced": set(), "seedSize": 0,
                                                  "baseline": 0, "aotcAnnounced": False}

        last_announced_ms = None
        for kill in bundle["kills"]:
            if announce_kill(cfg, scope, slug, raid_label, kill, state, profile, thumb,
                             now_iso, token=token):
                announced += 1
                last_announced_ms = kill["killedAtMs"]

        r_killed, total = raiderio.progress_for(profile, slug)
        count = store.progress_count(state, r_killed, total)
        if total and count >= total and not state.get("aotcAnnounced"):
            # Date it by the kill that finished the tier. Falling back to the newest kill
            # in the window would date AOTC by a re-kill on a later farm night, in the case
            # where Raider.IO only caught up after the real clear.
            when_ms = last_announced_ms or bundle["kills"][-1]["killedAtMs"]
            announce_aotc(cfg, scope, slug, raid_label, state, _at(when_ms))

        store.touch(scope, slug, now_iso, raid_name=raid_label)
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
            store.put_snapshot(scope, newest_seen["slug"], newest_seen["raid"],
                               newest_seen["killed"], newest_seen["total"],
                               newest_seen["rank"], now_iso)
        except Exception as exc:                               # noqa: BLE001
            # A snapshot is a convenience for /progress. Losing it must not fail a poll.
            log("snapshot_write_failed", error=repr(exc))

    log("poll_done", kills=len(kills), announced=announced, tiers=len(grouped),
        points=rate, ms=int((time.time() - started) * 1000))
    return {"ok": True, "kills": len(kills), "announced": announced}


# ---------------------------------------------------------------- the scorecard page

_s3 = None


def publish_scorecard(cfg, night_key, page_html):
    """Put one night's page into the site bucket.

    `<night>/index.html`, not `<night>.html`, so the URL a reader sees ends in a slash and
    the CloudFront viewer-request function that already maps directory URIs to index.html
    on ryangrey.dev needs no second rule for this subdomain.

    No ACL and no public-read: the bucket is private and CloudFront reads it through OAC,
    exactly as the main site does. A page put here is readable BECAUSE the distribution can
    read the bucket, never because the object is public.

    Cached for an hour. The page is immutable once written -- a raid night does not change
    after the fact -- so the only thing a longer TTL would buy is a slower fix for a bug in
    the renderer, and the only thing a shorter one buys is requests to S3.
    """
    global _s3                                                 # noqa: PLW0603
    bucket = cfg.get("scorecard_bucket")
    if not bucket:
        raise RuntimeError("scorecard/bucket is not set")
    if _s3 is None:
        _s3 = boto3.client("s3")
    _s3.put_object(
        Bucket=bucket, Key=f"{night_key}/index.html",
        Body=page_html.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
        CacheControl="public, max-age=3600")


# ---------------------------------------------------------------- the weekly recap


def previous_slug(profile, slug):
    """The tier before this one, from Raider.IO's own ordering.

    Used only to seed a new tier's prog roster. Deriving it from raid_progression rather
    than tracking "the last tier we saw" in DynamoDB means there is no extra item to keep
    correct, and no way for that item to disagree with reality after a rollover.
    """
    order = list((profile or {}).get("raid_progression") or {})
    if slug not in order:
        return None
    i = order.index(slug)
    return order[i - 1] if i > 0 else None


def killed_before(scope, slug, announced, report_start_ms):
    """Bosses that were already dead when this report started.

    Not the same thing as the announced set, and the difference is the whole point. The
    announcer polls every fifteen minutes, so a boss killed at 9pm is in `announced` long
    before the recap runs the next morning -- comparing against that set would erase
    exactly the progression evidence that identifies the night as A team's.

    A boss in `announced` with no first-kill record was SEEDED: killed before the bot was
    watching, or absorbed by a rollover seed. Those are long dead by definition.
    """
    dead = set()
    for key in announced or ():
        try:
            rec = store.get_first_kill(scope, slug, key)
        except Exception as exc:                               # noqa: BLE001
            log("first_kill_read_failed", slug=slug, boss=key, error=repr(exc))
            rec = None
        if rec is None or int(rec.get("killedAtMs") or 0) < int(report_start_ms):
            dead.add(key)
    return dead


def prog_roster(scope, slug, announced, profile, cfg, now_iso, persist=True):
    """The roster to classify against, and an honest account of where it came from.

    Three states, in preference order. A roster derived from enough of this tier's own
    first kills is used. One derived from too few is not trusted to reject anybody, so a
    seed carried from the previous tier is preferred while it exists. With neither, the
    roster is empty and signal A abstains -- which is the correct answer in week one of a
    fresh tier, not a failure.
    """
    try:
        records = store.first_kills(scope, slug, announced)
    except Exception as exc:                                   # noqa: BLE001
        log("roster_read_failed", slug=slug, error=repr(exc))
        records = {}

    derived = team.derive_roster([sorted(r["players"]) for r in records.values()],
                                 cfg.get("roster_min_pct", 50))
    if persist:
        try:
            store.save_derived_roster(scope, slug, derived["roster"], derived["sample"],
                                      derived["provisional"], now_iso)
        except Exception as exc:                               # noqa: BLE001
            log("roster_write_failed", slug=slug, error=repr(exc))

    state = None
    try:
        state = store.load_roster(scope, slug)
    except Exception as exc:                                   # noqa: BLE001
        log("roster_state_read_failed", slug=slug, error=repr(exc))

    seed = set((state or {}).get("seed") or ())
    if not seed and derived["provisional"]:
        # First recap of a new tier. Carry the previous tier's roster forward so week one
        # works at all, then let real first kills replace it as they accumulate.
        prev = previous_slug(profile, slug) if persist else None
        if prev:
            try:
                prev_state = store.load_roster(scope, prev) or {}
                candidate = prev_state.get("derived") or set()
                if candidate and store.seed_roster(scope, slug, candidate, prev, now_iso):
                    seed = candidate
                    log("roster_seeded_from_previous_tier", slug=slug, fromSlug=prev,
                        players=len(candidate))
            except Exception as exc:                           # noqa: BLE001
                log("roster_seed_failed", slug=slug, fromSlug=prev, error=repr(exc))

    if derived["roster"] and not derived["provisional"]:
        return derived["roster"], False, {"source": "derived", "sample": derived["sample"]}
    if seed:
        # Logged loudly every time. A verdict reached against a carried-forward roster is
        # a verdict about last tier's raid team, and if the roster changed over the break
        # this is where a wrong answer comes from.
        log("roster_is_seeded", slug=slug, players=len(seed), sample=derived["sample"],
            note="classifying against the PREVIOUS tier's roster until enough first kills "
                 "accumulate in this one")
        return seed, True, {"source": "seed", "sample": derived["sample"]}
    return derived["roster"], False, {"source": "derived-provisional",
                                      "sample": derived["sample"]}


def report_tier(detail, raid_scope, base, cfg, profile, index):
    """Which raid this report was, and the fights that belong to it.

    A report is not one tier any more than it is one activity. The captured Scrambled
    report opens with a Heroic Nymrissa Wavecaller kill -- which is The Tidebound Grotto --
    and then spends the rest of the night in The Venomous Abyss. Taking the tier from the
    earliest fight, the obvious reading, would label that night with a raid it visited for
    two pulls, look up the wrong boss list, and compare the wipes against the wrong
    killed-boss set.

    So the tier is the one holding the MOST of the night's Heroic fights, and the raid_scope is
    narrowed to that tier's fights alone. The warm-up kill in another raid then falls out
    of the leaderboards too, which is correct: it was not part of this raid.
    """
    tally = {}
    for f in raid_scope["fights"]:
        slug, rmeta, how = raiderio.resolve_raid(
            profile, f.get("name"), (detail.get("zone") or {}).get("name"),
            _at(base + (f.get("startTime") or 0)), cfg["guild_region"], index)
        if not slug:
            continue
        rec = tally.setdefault(slug, {"meta": rmeta, "how": how, "fights": []})
        rec["fights"].append(f)
    if not tally:
        return None, None, None, raid_scope, {}
    # The tier is the one holding most of the night. A WORLD BOSS is not competing for
    # that title -- it is one encounter that takes ten minutes -- so it is picked out
    # separately rather than being dropped with the rest of the other-raid fights. A guild
    # that killed the world boss on Heroic did something worth saying, and the old
    # behaviour said nothing at all.
    tiers = {s: v for s, v in tally.items() if not raiderio.is_world_boss(profile, s)}
    worlds = {s: v for s, v in tally.items() if raiderio.is_world_boss(profile, s)}
    if not tiers:
        # A night that was ONLY a world boss. There is no tier to label the card with, so
        # it is not a raid night and the recap says nothing -- same rule as always.
        return None, None, None, raid_scope, worlds

    slug = max(tiers, key=lambda s: len(tiers[s]["fights"]))
    rec = tiers[slug]
    if len(tiers) > 1:
        log("recap_report_spans_tiers", chosen=slug,
            counts={s: len(v["fights"]) for s, v in tiers.items()},
            note="fights from other raids are excluded from the night's card")
    if worlds:
        log("recap_world_boss_seen",
            bosses={s: [f.get("name") for f in v["fights"] if f.get("kill")]
                    for s, v in worlds.items()},
            note="kept out of the tier's boss numbering, reported separately")
    return slug, rec["meta"], rec["how"], recap_mod.raid_scope(rec["fights"], wcl.HEROIC), worlds


def recap_night(token, cfg, scope, now, now_iso, gid, profile, index, started, dry=False,
                hours=None, manual=False):
    """Post one raid night's recap, or post nothing and say why.

    Every exit that posts nothing is a log line, never a message. "No raid this week" in
    a guild channel is noise the guild did not ask for, and an UNKNOWN verdict is the bot
    admitting it cannot tell the two teams apart -- which is precisely the moment not to
    publish a leaderboard naming individual people.

    `manual` posts one night by hand: it permits the window override and ignores
    /greybot/recap/enabled, because a person invoking this deliberately has already made
    that call for this one night. It still claims the night, so a manual post and the
    schedule cannot both publish the same evening.

    `dry` renders the card from real data and returns it without posting and without
    claiming the night. It deliberately ignores /greybot/recap/enabled, because the whole
    point is to look at a real card BEFORE turning the feature on -- a preview gated behind
    the switch it exists to inform is not a preview.

    It must not claim, for the same reason the kill preview must not: a dry run that took
    the ordinary path would mark the night posted, and the real recap would then be
    correctly, silently and permanently skipped. It writes NOTHING at all -- not the
    claim, not the derived roster, not a rollover seed -- so it is safe to run repeatedly
    against production while deciding whether to switch the feature on.
    """
    if dry:
        log("recap_dry_run_start",
            note="rendering from real data — will not post and will not claim the night")
    elif manual:
        # `enabled` governs whether the SCHEDULE may post. A human invoking this by hand
        # with an explicit flag has already made that decision for one night, so the switch
        # does not gate it -- which is what makes it possible to show the guild a real card
        # before committing to a recurring one. The schedule sends {"mode": "recap"} and
        # nothing else, so it can never set this.
        log("recap_manual_post", enabled=bool(cfg.get("recap_enabled")),
            note="posting one night by hand; this does not enable the schedule")
    elif not cfg.get("recap_enabled"):
        log("recap_disabled", note="/greybot/recap/enabled is not true — nothing posted")
        return {"ok": True, "skipped": "disabled"}

    # The budget check comes BEFORE any expensive call, which is the only place it is worth
    # anything. Checking afterwards would report the damage rather than prevent it.
    rate = wcl.rate_limit(wcl.query(token, wcl.RATE_ONLY_Q))
    if rate:
        remaining = rate["limit"] - rate["spent"]
        if remaining < RECAP_POINT_BUDGET:
            log("recap_rate_abort", **rate, remaining=remaining,
                needed=RECAP_POINT_BUDGET,
                note="skipped to protect the kill announcer's share of the hourly budget")
            return {"ok": True, "skipped": "rate_limit", "points": rate}

    # The window is fixed in normal operation -- it is derived from the raid schedule and
    # nothing in an event payload should be able to widen it, or a stray field could make
    # the bot recap a night it was never meant to see. A DRY run is the exception, because
    # the useful moment to preview a card is rarely the morning after a raid.
    lookback = RECAP_LOOKBACK_HOURS
    if (dry or manual) and hours:
        lookback = float(hours)
        log("recap_window_override", hours=lookback,
            note="dry run or explicit manual post only")
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(hours=lookback)).timestamp() * 1000)
    reports, rate = wcl.reports_in_window(token, gid, start_ms, end_ms,
                                          limit=RECAP_MAX_REPORTS)
    if not reports:
        log("recap_no_report", lookbackHours=lookback, points=rate,
            note="no raid last night — nothing posted, by design")
        return {"ok": True, "reports": 0}

    chosen, skipped, tier = [], [], None
    for meta in reports:
        detail, rate = wcl.report_detail(token, meta["code"])
        raid_scope = recap_mod.raid_scope(detail.get("fights"), wcl.HEROIC)
        if not raid_scope["fightIDs"]:
            skipped.append({"report": meta["code"], "why": "no Heroic raid fights",
                            "title": meta.get("title")})
            continue

        base = int(detail.get("startTime") or meta.get("startTime") or 0)
        slug, rmeta, how, raid_scope, worlds = report_tier(detail, raid_scope, base,
                                                           cfg, profile, index)
        if not slug:
            skipped.append({"report": meta["code"], "why": "could not resolve the raid"})
            continue
        if tier is None:
            tier = {"slug": slug, "meta": rmeta,
                    "label": (detail.get("zone") or {}).get("name")
                             or (rmeta or {}).get("name") or slug,
                    "roster": None, "seeded": False, "rosterInfo": None,
                    "worldBosses": []}
        elif slug != tier["slug"]:
            skipped.append({"report": meta["code"], "why": "different tier to the night's"})
            continue

        # Heroic world-boss kills from the same night, named and credited separately.
        for wslug, wrec in (worlds or {}).items():
            for f in wrec["fights"]:
                if f.get("kill") and f.get("name") not in [
                        w["name"] for w in tier["worldBosses"]]:
                    tier["worldBosses"].append(
                        {"name": f.get("name"), "slug": wslug,
                         "raid": (wrec.get("meta") or {}).get("name") or wslug,
                         "difficulty": "Heroic"})

        state = store.load_tier(scope, slug) or {"announced": set()}
        dead = killed_before(scope, slug, state.get("announced") or set(), base)
        roster, seeded, roster_info = prog_roster(scope, slug, state.get("announced") or set(),
                                                  profile, cfg, now_iso, persist=not dry)
        # Kept for the eligibility filter below rather than derived a second time. Both
        # reports of one night are the same tier by the time they get here, so the last
        # answer is the only answer.
        tier.update({"roster": roster, "seeded": seeded, "rosterInfo": roster_info})
        # Which bosses were ALREADY dead when this report opened. Kept on the tier so the
        # card can separate "killed again" from "killed for the first time" -- the second
        # is the only one that is news, and the two are indistinguishable from the kill
        # list alone.
        tier.setdefault("dead", set()).update(dead)

        actors = recap_mod.actor_index(detail.get("masterData"))
        raiders = {team.player_key(actors[i]["name"], actors[i]["server"])
                   for i in raid_scope["raiderIDs"] if i in actors}

        # The tag is authoritative when it exists. Scrambled tags nothing today, so this
        # is always None and the two signals do the work -- but the day somebody tags the
        # B team's reports, the guess stops being a guess with no code change.
        tag = (detail.get("guildTag") or {}).get("name")
        if tag and cfg.get("prog_tag"):
            verdict = team.PROG if tag == cfg["prog_tag"] else team.OTHER
            why = {"verdict": verdict, "why": "decided by the report's guild tag",
                   "tag": tag}
        else:
            if tag:
                # A tagged report with no configured prog tag is new information, not a
                # verdict. Guessing which tag means "A team" from its name is exactly the
                # kind of inference this bot does not make.
                log("recap_tag_seen_but_unconfigured", report=meta["code"], tag=tag,
                    note="set /greybot/team/prog_tag to use tags as the authority")
            verdict, why = team.resolve_team(raiders, raid_scope["fights"], roster, dead,
                                             cfg.get("overlap_high", 70),
                                             cfg.get("overlap_low", 35),
                                             difficulty=wcl.HEROIC, roster_seeded=seeded)

        log("recap_report_classified", report=meta["code"], title=meta.get("title"),
            slug=slug, resolvedBy=how, rosterSource=roster_info["source"],
            rosterSample=roster_info["sample"], **why)

        if verdict != team.PROG:
            skipped.append({"report": meta["code"], "why": f"classified {verdict}"})
            continue
        chosen.append({"meta": meta, "detail": detail, "raidScope": raid_scope, "actors": actors,
                       "base": base, "code": meta["code"], "start": base,
                       "end": int(detail.get("endTime") or meta.get("endTime") or base),
                       "heroicFights": len(raid_scope["fightIDs"])})

    # Two people in the guild both log, so one night routinely produces two reports of the
    # same pulls. Summing those doubles every number on the card.
    chosen, duplicates = recap_mod.drop_duplicate_logs(chosen)
    if duplicates:
        log("recap_duplicate_logs", dropped=duplicates, kept=[c["code"] for c in chosen],
            note="same night logged more than once; the most complete log is used")
        skipped.extend(duplicates)

    if not chosen:
        # The important silence. UNKNOWN posts nothing and never guesses -- the same rule
        # that took the raid-resolution fallback out of the announcer.
        log("recap_nothing_to_post", reports=len(reports), skipped=skipped,
            note="no report was confidently the prog team — posting nothing")
        return {"ok": True, "reports": len(reports), "posted": False, "skipped": skipped}

    # Idempotency is keyed on the local DATE THE NIGHT STARTED. A raid that runs past
    # midnight is one night, and a retry, an overlapping schedule or a manual invocation
    # must all land on the same key.
    earliest = min(chosen, key=lambda c: c["base"])
    night = _local(_at(earliest["base"]))
    night_key = night.strftime("%Y-%m-%d")
    if not dry and not store.claim_recap(scope, night_key):
        log("recap_already_posted", night=night_key, note="claimed by an earlier run")
        return {"ok": True, "night": night_key, "posted": False, "duplicate": True}

    # One source per REPORT, kept separate on purpose. Actor ids and fight ids are scoped
    # to the report that issued them, so merging two reports' blobs into one pile and
    # aggregating by id splits a raider in half and fuses two strangers together. That is
    # not hypothetical: the first live dry run of this code, on a night Scrambled logged
    # twice, reported Thaydan at 27 deaths and again at 14, and named the wrong person as
    # the night's most deaths.
    sources = []
    for c in chosen:
        tables, rate = wcl.report_tables(token, c["meta"]["code"], c["raidScope"]["fightIDs"])
        span = int(c["detail"].get("endTime") or 0) - c["base"]
        pages, rate, calls = wcl.deaths_pages(
            token, c["meta"]["code"], c["raidScope"]["fightIDs"], max(span, 1),
            is_truncated=recap_mod.page_is_truncated,
            cursor_of=recap_mod.last_timestamp)
        if calls > 1:
            log("recap_deaths_paged", report=c["meta"]["code"], pages=calls,
                note="the Deaths table caps at 200 rows and does not say so")
        sources.append({"report": c["meta"]["code"], "actors": c["actors"],
                        "eligible": set(c["raidScope"]["raiderIDs"]),
                        "fightIDs": list(c["raidScope"]["fightIDs"]),
                        "damage": tables.get("damage"),
                        "healing": tables.get("healing"),
                        "damageTaken": tables.get("damageTaken"),
                        "playerDetails": tables.get("playerDetails"), "deaths": pages,
                        "rankings": tables.get("rankings")})

    # The explicit pug rule. Eligibility is "took part in a Heroic raid fight tonight",
    # intersected with the prog roster when there is a usable one. The intersection is what
    # keeps a pug or a trial off the guild's leaderboard; without a usable roster it is
    # dropped rather than guessed, because excluding real raiders is worse than including
    # an occasional guest.
    roster = tier.get("roster") or set()
    seeded, roster_info = tier.get("seeded"), tier.get("rosterInfo") or {"source": "none"}
    excluded = recap_mod.restrict_to_roster(sources, roster)

    combined = recap_mod.raid_scope(
        [f for c in chosen for f in c["raidScope"]["fights"]], wcl.HEROIC)
    summary = recap_mod.summarise(combined, sources,
                                  show_worst_parse=bool(cfg.get("recap_worst_parse")),
                                  encounters=(tier.get("meta") or {}).get("encounters"),
                                  dead=tier.get("dead") or set())
    summary["worldBosses"] = tier.get("worldBosses") or []

    # The full scorecard. Built unconditionally because it is pure computation over blobs
    # already in memory and it is what the dry run has to show; PUBLISHING it is what the
    # base URL gates.
    eligible_names = set(recap_mod.raider_keys(sources))
    rows = recap_mod.scorecard(sources, eligible_names)
    night_text = night.strftime("%A, %B %-d")
    sources_meta = [{"code": c["meta"]["code"], "title": c["meta"].get("title"),
                     "url": report_url(c["meta"]["code"]),
                     "owner": (c["meta"].get("owner") or {}).get("name"),
                     "ownerUrl": scorecard.guild_reports_url(gid),
                     "when": _iso(_at(c["base"]))} for c in chosen]
    page_url = f"{cfg['scorecard_base_url']}/{night_key}/" if cfg.get("scorecard_base_url") else None
    page_html = scorecard.render(
        cfg["guild_name"], tier["label"], night_text,
        summary.get("bossLabels") or summary.get("bosses"), rows, sources_meta,
        raiders=summary.get("raiders"), canonical=page_url,
        region=cfg.get("guild_region"),
        world_bosses=summary.get("worldBosses"))

    # Published BEFORE the card is posted, and the link is dropped if the put fails. A card
    # in the channel saying "full scorecard here" that 404s is worse than a card with no
    # link at all -- the reader cannot tell a broken deploy from a bot that lies.
    if page_url and not dry:
        try:
            publish_scorecard(cfg, night_key, page_html)
            log("scorecard_published", night=night_key, url=page_url, bytes=len(page_html),
                raiders=len(rows))
        except Exception as exc:                               # noqa: BLE001
            log("scorecard_publish_failed", night=night_key, error=repr(exc),
                note="the card will be posted without a scorecard link")
            page_url = None

    payload = discord.recap_embed(
        cfg["guild_name"], tier["label"], night_text, summary,
        report_url=report_url(earliest["meta"]["code"]), iso_ts=_iso(_at(earliest["base"])),
        thumbnail_url=raiderio.icon_url(tier.get("meta")),
        guild_label=raiderio.guild_display(profile, cfg["guild_name"], cfg["guild_realm"]),
        guild_url=raiderio.profile_url(profile, cfg["guild_region"], cfg["guild_realm"],
                                       cfg["guild_name"]),
        scorecard_url=page_url)
    if dry:
        log("recap_dry_run", night=night_key, slug=tier["slug"],
            bosses=summary.get("bosses"), prog=(summary.get("prog") or {}).get("name"),
            raiders=summary.get("raiders"),
        eligible=sum(len(src["eligible"]) for src in sources),
            missingSections=summary["missing"], posted=False, stateWritten=False,
            note="DRY RUN — nothing posted, no night claimed")
        return {"ok": True, "night": night_key, "posted": False, "dry": True,
                "payload": payload, "summary": summary, "scorecard": rows,
                "scorecardHtml": page_html, "scorecardUrl": page_url,
                "sources": sources_meta}

    try:
        discord.post_to(destination(cfg), payload)
    except discord.DiscordError as exc:
        # Hand the night back so the next run retries it, exactly as a failed kill
        # announcement hands the boss back.
        store.release_recap(scope, night_key)
        log("recap_failed", night=night_key, error=str(exc))
        return {"ok": False, "night": night_key, "posted": False}

    log("recap_posted", night=night_key, manual=bool(manual),
        slug=tier["slug"], raid=tier["label"],
        reports=[c["meta"]["code"] for c in chosen], skipped=skipped,
        bosses=summary.get("bosses"), prog=(summary.get("prog") or {}).get("name"),
        raiders=summary.get("raiders"),
        eligible=sum(len(src["eligible"]) for src in sources),
        excludedByRoster=excluded, rosterSource=roster_info["source"], rosterSeeded=seeded,
        worstParse=bool(cfg.get("recap_worst_parse")), missingSections=summary["missing"],
        points=rate, ms=int((time.time() - started) * 1000))
    return {"ok": True, "night": night_key, "posted": True,
            "reports": len(chosen), "missing": summary["missing"]}
