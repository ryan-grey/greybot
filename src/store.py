"""Dedupe state. This is the bot, really -- the rest is formatting.

Everything else here can fail and be retried. An announcement cannot: Discord has no
undo, and a duplicate "Scrambled just killed X" in #bots is the exact failure this bot
exists to avoid. So the write that records a boss as announced is also the LOCK that
grants permission to announce it, and it is a conditional update, not a read-then-write.

    UpdateExpression: ADD announced :boss
    ConditionExpression: attribute_not_exists(announced) OR NOT contains(announced, :name)

If two invocations overlap -- a slow poll still running when the schedule fires again, a
retry after a timeout that did not actually fail -- exactly one of them wins the condition
and posts. The loser gets ConditionalCheckFailed, which is the correct answer, not an
error. Read-then-write would let both read an empty set and both post.

The claim is taken BEFORE the webhook call, so the failure mode is a dropped announcement
rather than a duplicate one. `release` puts the boss back if the webhook genuinely fails,
so the next poll retries it. That is a deliberate trade: silence is recoverable on the
next run, a double post is not recoverable at all.

Single table, composite key, Query-only -- same shape as the study engine, and the
execution role again grants no Scan and no DeleteItem. Removing a set member is an
UpdateItem with DELETE, so the rollback path stays inside that grant.

    item              pk                             sk
    tier baseline     WOW#<region>#<realm>#<name>    TIER#<raid-slug>
    bootstrap marker  TENANT#<discord_guild_id>      BOOTSTRAP
    ANNOUNCED SET     TENANT#<discord_guild_id>      ANNOUNCED#<raid-slug>

The announced set is under TENANT#, not WOW#, and that placement is the dedupe
guarantee rather than a filing decision. Two Discord servers can track the same WoW
guild -- a raid guild's main server and its social server -- and they post to two
different channels. Sharing one announced set between them would mean whichever polled
second found every boss already claimed and posted nothing: no error, no log line, a bot
that looks healthy and never speaks. Facts about the guild are shared because they are
the same facts; records of what was posted are not, because they are not.

Every function here takes a `Scope` rather than a pk, so no call site has to remember
which partition a row lives in. See `docs/multi-tenant-keys.md`.

One item per tier is also what makes tier rollover free: a new slug is a new sk, so the
announced set starts empty on its own and nothing has to detect the rollover or clean up.

The members of `announced` are NORMALISED BOSS NAMES, not Warcraft Logs encounter ids.
An encounter id is the more stable identifier and would be the obvious choice, but it
exists only in Warcraft Logs -- and the one situation where cold-start seeding matters
most is precisely the one where Warcraft Logs history is unavailable (private logs, or a
tier cleared longer ago than the lookback reaches). The boss NAME is the only identifier
both APIs share, so keying on it is what lets a tier be seeded from Raider.IO's ordered
encounter list alone. It also makes the stored state readable, which matters at 1am when
the question is "why did it post that". Names are compared through raiderio.normalize, so
punctuation drift between the two APIs cannot split one boss into two members.
"""

import json
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = os.environ.get("STATE_TABLE", "ryangrey-greybot")

_cfg = Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=10)
ddb = boto3.client("dynamodb", region_name=REGION, config=_cfg)


def _s(v):
    return {"S": str(v)}


def _n(v):
    return {"N": str(v)}


# Key construction lives in keys.py, and every function below takes a `Scope`
# rather than a bare pk. That is deliberate: no call site should have to remember
# whether a given row is a shared fact about the WoW guild or a record of what
# one install did. Ask `scope.wow` or `scope.tenant` here, once, against the table
# in `docs/multi-tenant-keys.md`.
from keys import (Scope, wow_pk, tenant_pk, tier_sk, announced_sk,  # noqa: F401
                  ART_PK, CONFIG_SK, REGISTRY_PK, TENANTS_SK)


def _tier_key(scope, slug):
    """SHARED. Tier baseline, raid name, kill counts — facts about the guild."""
    return {"pk": _s(scope.wow), "sk": _s(tier_sk(slug))}


def _ann_key(scope, slug):
    """PER-TENANT. The announced set and the AOTC flag — what THIS install posted.

    Every claim and release below points here rather than at the tier row. That
    is the correctness half of Phase 2: two Discord servers tracking one WoW
    guild post to two channels, so they must not share a dedupe set.
    """
    return {"pk": _s(scope.tenant), "sk": _s(announced_sk(slug))}


def _bootstrap_key(scope):
    return {"pk": _s(scope.tenant), "sk": _s("BOOTSTRAP")}


def is_bootstrapped(scope):
    """Has THIS INSTALL ever been seeded? One marker item, checked before anything can be
    announced, so 'first run announces nothing' is a branch rather than an emergent
    property of several other rules agreeing with each other.

    Per-tenant, not per-guild, and the distinction matters the moment a second
    Discord server tracks an already-tracked guild: that install has itself never
    announced anything, so it must seed independently or its first poll replays
    the whole tier into a brand-new channel.
    """
    res = ddb.get_item(TableName=TABLE, Key=_bootstrap_key(scope), ConsistentRead=True)
    return bool(res.get("Item"))


def mark_bootstrapped(scope, now_iso, tiers, note=""):
    """Record that seeding happened. Conditional, so two invocations racing the first run
    cannot both seed."""
    try:
        ddb.put_item(TableName=TABLE,
                     Item={**_bootstrap_key(scope), "bootstrappedAt": _s(now_iso),
                           "tiers": _n(tiers), "note": _s(note or "")},
                     ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def load_tier(scope, slug):
    """Current state for one tier, assembled from both partitions.

    Two reads, because the row was split: the shared tier row carries the
    baseline and raid name, and this install's announced row carries what IT has
    posted. The merged dict keeps the shape callers already expect.

    A missing ANNOUNCED row is the signal to seed, not a missing TIER row. The
    tier row may well exist already because another tenant tracks the same guild,
    and treating that as 'already seeded' would silently skip seeding for this
    install and let its first poll announce a tier's worth of old kills.
    """
    ann = ddb.get_item(TableName=TABLE, Key=_ann_key(scope, slug),
                       ConsistentRead=True).get("Item")
    if not ann:
        return None
    tier = ddb.get_item(TableName=TABLE, Key=_tier_key(scope, slug),
                        ConsistentRead=True).get("Item") or {}
    return {
        # per-tenant
        "announced": set((ann.get("announced") or {}).get("SS") or []),
        "aotcAnnounced": bool((ann.get("aotcAnnounced") or {}).get("BOOL") or False),
        "seededAt": (ann.get("seededAt") or {}).get("S") or "",
        "seedSize": int((ann.get("seedSize") or {}).get("N") or 0),
        # shared
        "baseline": int((tier.get("baseline") or {}).get("N") or 0),
        "raidName": (tier.get("raidName") or {}).get("S") or "",
    }


def seed_tier(scope, slug, already_killed, baseline, raid_name, now_iso, aotc_already=False):
    """Record what was ALREADY dead the first time this tier is seen, announcing nothing.

    Without this, the first run after a mid-tier deploy posts a kill announcement for
    every boss the guild killed weeks ago, which is both wrong and unrecoverable. The
    condition makes seeding safe to race: a second invocation that also finds no item
    loses here and then reads the winner's state.

    `baseline` is the kill count this tier starts from. It is the larger of what
    Raider.IO reports and what the log history shows, because those two disagree
    constantly and the count in a message must never go backwards.

    `aotc_already` closes the retroactive-AOTC hole. If the tier is already fully cleared
    when the bot first sees it, the flag is set at seed time, so the bot never celebrates
    an achievement the guild earned before it was watching.
    """
    names = sorted({str(x) for x in already_killed if str(x)})

    # The shared tier row. Another tenant tracking this guild may have written it
    # already, and that is fine -- these are the same facts either way. Written
    # first and unconditionally-tolerant, so the tenant row below is the only
    # thing that decides whether THIS install seeds.
    try:
        ddb.put_item(TableName=TABLE,
                     Item={**_tier_key(scope, slug),
                           "baseline": _n(max(int(baseline or 0), len(names))),
                           "raidName": _s(raid_name or slug),
                           "seededAt": _s(now_iso)},
                     ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Already seeded by another install. Keep the existing row: its baseline
        # came from the same upstream and rewriting it would only risk moving a
        # count backwards.

    # This install's announced row. THIS is the seed guard -- conditional, so two
    # invocations racing the first run cannot both seed, and a second tenant on an
    # already-tracked guild still seeds for itself.
    item = {**_ann_key(scope, slug),
            "seedSize": _n(len(names)),
            "aotcAnnounced": {"BOOL": bool(aotc_already)},
            "seededAt": _s(now_iso)}
    if names:                      # DynamoDB has no empty string set
        item["announced"] = {"SS": names}
    try:
        ddb.put_item(TableName=TABLE, Item=item,
                     ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def claim_boss(scope, slug, boss_key):
    """Atomically claim the right to announce one boss. True means announce; False means
    someone already did."""
    try:
        ddb.update_item(
            TableName=TABLE, Key=_ann_key(scope, slug),
            UpdateExpression="ADD announced :b",
            ConditionExpression=("attribute_exists(pk) AND "
                                 "(attribute_not_exists(announced) OR NOT contains(announced, :k))"),
            ExpressionAttributeValues={":b": {"SS": [boss_key]}, ":k": _s(boss_key)})
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_boss(scope, slug, boss_key):
    """Undo a claim whose announcement never made it to Discord, so the next poll retries.
    DELETE on a set, which is UpdateItem -- the role has no DeleteItem and does not need
    one."""
    ddb.update_item(TableName=TABLE, Key=_ann_key(scope, slug),
                    UpdateExpression="DELETE announced :b",
                    ExpressionAttributeValues={":b": {"SS": [boss_key]}})


def claim_aotc(scope, slug):
    """Claim the one-and-only AOTC announcement for this tier. The guard is the whole
    point: the final boss gets re-killed every week for the rest of the tier, and every
    one of those re-kills satisfies 'heroic kills == total bosses'."""
    try:
        ddb.update_item(
            TableName=TABLE, Key=_ann_key(scope, slug),
            UpdateExpression="SET aotcAnnounced = :t",
            ConditionExpression=("attribute_exists(pk) AND "
                                 "(attribute_not_exists(aotcAnnounced) OR aotcAnnounced = :f)"),
            ExpressionAttributeValues={":t": {"BOOL": True}, ":f": {"BOOL": False}})
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_aotc(scope, slug):
    ddb.update_item(TableName=TABLE, Key=_ann_key(scope, slug),
                    UpdateExpression="SET aotcAnnounced = :f",
                    ExpressionAttributeValues={":f": {"BOOL": False}})


def touch(scope, slug, now_iso, raid_name=None):
    expr, vals = ["updatedAt = :u"], {":u": _s(now_iso)}
    if raid_name:
        expr.append("raidName = :r")
        vals[":r"] = _s(raid_name)
    ddb.update_item(TableName=TABLE, Key=_tier_key(scope, slug),
                    UpdateExpression="SET " + ", ".join(expr),
                    ExpressionAttributeValues=vals)


def put_snapshot(scope, slug, raid_name, killed, total, realm_rank, now_iso):
    """What /progress answers from.

    The slash command has a hard three-second budget including cold start, and a
    Raider.IO round trip inside that is a gamble. The poller already makes that call
    every fifteen minutes, so it leaves the display values behind and the command reads
    one item. This is a cache of something already fetched, not a second source of truth --
    the dedupe state remains the authority on what has been announced.
    """
    ddb.put_item(TableName=TABLE, Item={
        "pk": _s(scope.wow), "sk": _s("PROGRESS"),
        "slug": _s(slug), "raidName": _s(raid_name or slug),
        "killed": _n(int(killed or 0)), "total": _n(int(total or 0)),
        "realmRank": _n(int(realm_rank or 0)), "updatedAt": _s(now_iso)})


def get_snapshot(scope):
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(scope.wow), "sk": _s("PROGRESS")})
    item = res.get("Item")
    if not item:
        return None
    def num(field):
        return int((item.get(field) or {}).get("N") or 0)
    return {"slug": (item.get("slug") or {}).get("S") or "",
            "raidName": (item.get("raidName") or {}).get("S") or "",
            "killed": num("killed"), "total": num("total"),
            # Raider.IO writes 0 for "not ranked yet"; keep that as None, not zero.
            "realmRank": num("realmRank") or None,
            "updatedAt": (item.get("updatedAt") or {}).get("S") or ""}


ART_PK = "ART#GLOBAL"


def get_art(boss_key):
    """Cached art URL for a boss, or None if never resolved.

    Returns a dict so a resolved-but-absent answer is distinguishable from an unresolved
    one: {"url": ""} means Blizzard was asked and had nothing, and must not be asked again
    on every future kill. Boss art is the same for everyone, so this partition is shared
    across guilds rather than kept per guild.
    """
    res = ddb.get_item(TableName=TABLE,
                       Key={"pk": _s(ART_PK), "sk": _s(f"BOSS#{boss_key}")})
    item = res.get("Item")
    if not item:
        return None
    return {"url": (item.get("url") or {}).get("S") or "",
            "displayId": int((item.get("displayId") or {}).get("N") or 0)}


def put_art(boss_key, display_id, url, now_iso):
    ddb.put_item(TableName=TABLE, Item={
        "pk": _s(ART_PK), "sk": _s(f"BOSS#{boss_key}"),
        "displayId": _n(int(display_id or 0)), "url": _s(url or ""),
        "resolvedAt": _s(now_iso)})


def progress_count(state, raider_killed, total):
    """The "n" in "n of total".

    Raider.IO lags Warcraft Logs, sometimes by hours, so at the moment of announcing a
    kill it frequently still reports the PRE-kill count. Ryan's call is to announce
    immediately and accept a stale rank, but a stale COUNT is different: "they are now
    5 of 8" posted underneath "just killed the 6th boss" is visibly wrong.

    So the count is the larger of what Raider.IO says and what this bot knows from its
    own claims. The bot's own figure is the seed baseline plus every boss claimed since,
    which stays correct through a mid-tier deploy -- the seed absorbs the back catalogue
    into the baseline instead of counting from zero.
    """
    claimed_since_seed = max(0, len(state.get("announced") or ()) - int(state.get("seedSize") or 0))
    own = int(state.get("baseline") or 0) + claimed_since_seed
    n = max(int(raider_killed or 0), own)
    if total:
        n = min(n, int(total))
    return n


# ---------------------------------------------------------------- prog roster
#
# Three more item shapes, and the constraint that chose all three: the execution role
# grants GetItem, PutItem and UpdateItem, and nothing else. No Query. So none of this can
# be a collection the code discovers by scanning a key range -- every read has to be a
# GetItem against a key the code already knows.
#
# That turns out fine, because the code does already know. The tier item's `announced`
# set IS the list of this tier's first kills, so the participant records are addressable
# one GetItem at a time from a list the bot maintains anyway. Nine reads once a week is
# not a cost worth designing around, and one item per kill stays readable in the console,
# which is the same reason the announced set holds boss names rather than encounter ids.
#
#   item              pk                             sk
#   first-kill roster WOW#<region>#<realm>#<name>    KILL#<slug>#<boss-key>
#   derived roster    WOW#<region>#<realm>#<name>    ROSTER#<slug>
#   recap claims      TENANT#<discord_guild_id>      RECAPS
#
# Kills and rosters are shared: who killed a boss first is a fact about the guild,
# identical for every install watching it. Recap claims are not -- two installs
# post two recaps to two channels, and each has to claim its own night.
#
# A boss in `announced` with no KILL# item is a boss that was SEEDED -- killed before the
# bot was watching, or absorbed by a rollover seed. Those have no participants to record
# and never will, and the recap treats them as "long dead", which is exactly right.


def kill_sk(slug, boss_key):
    return f"KILL#{slug}#{boss_key}"


def roster_sk(slug):
    return f"ROSTER#{slug}"


def record_first_kill(scope, slug, boss_key, players, killed_at_ms, report_code, now_iso):
    """Who was standing there for one first Heroic kill.

    Written after the claim succeeds, so it records kills that were actually announced
    rather than kills that were merely seen. Unconditional on purpose: if a webhook
    failure released the boss and the next poll re-announced it, re-recording the same
    participants over the same key is the correct outcome, not a conflict.

    Returns False without writing when there are no participants. DynamoDB has no empty
    string set, and a first kill whose roster could not be read is better recorded as
    absent than as a boss nobody killed -- absent means "no evidence", which is true.
    """
    names = sorted({str(p) for p in (players or ()) if str(p).strip()})
    if not names:
        return False
    ddb.put_item(TableName=TABLE, Item={
        "pk": _s(scope.wow), "sk": _s(kill_sk(slug, boss_key)),
        "players": {"SS": names}, "killedAtMs": _n(int(killed_at_ms or 0)),
        "reportCode": _s(report_code or ""), "recordedAt": _s(now_iso)})
    return True


def get_first_kill(scope, slug, boss_key):
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(scope.wow), "sk": _s(kill_sk(slug, boss_key))})
    item = res.get("Item")
    if not item:
        return None
    return {"players": set((item.get("players") or {}).get("SS") or []),
            "killedAtMs": int((item.get("killedAtMs") or {}).get("N") or 0),
            "reportCode": (item.get("reportCode") or {}).get("S") or ""}


def first_kills(scope, slug, boss_keys):
    """Every recorded first kill for this tier, keyed by boss.

    `boss_keys` comes from the tier's announced set. Bosses with no record are simply
    absent from the result; the caller needs to tell "seeded, no participants known"
    apart from "nobody was there", and an omission says the first thing.
    """
    out = {}
    for key in sorted(boss_keys or ()):
        rec = get_first_kill(scope, slug, key)
        if rec and rec["players"]:
            out[key] = rec
    return out


def load_roster(scope, slug):
    """The persisted roster for one tier: what was derived, and what it was seeded from."""
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(scope.wow), "sk": _s(roster_sk(slug))},
                       ConsistentRead=True)
    item = res.get("Item")
    if not item:
        return None
    return {"derived": set((item.get("derived") or {}).get("SS") or []),
            "seed": set((item.get("seed") or {}).get("SS") or []),
            "seedFrom": (item.get("seedFrom") or {}).get("S") or "",
            "sample": int((item.get("sample") or {}).get("N") or 0),
            "provisional": bool((item.get("provisional") or {}).get("BOOL") or False),
            "derivedAt": (item.get("derivedAt") or {}).get("S") or ""}


def seed_roster(scope, slug, players, from_slug, now_iso):
    """Carry the previous tier's roster into a new one, once.

    An UpdateItem guarded on `seed` rather than a conditional PutItem, because the tier's
    roster item may already exist -- the first kill of the new tier can easily land before
    the first recap runs. Guarding on the attribute rather than the item means seeding is
    still exactly-once without depending on which of the two got there first.

    Returns True if this call is the one that seeded.
    """
    names = sorted({str(p) for p in (players or ()) if str(p).strip()})
    if not names:
        return False
    try:
        ddb.update_item(
            TableName=TABLE, Key={"pk": _s(scope.wow), "sk": _s(roster_sk(slug))},
            UpdateExpression="SET seed = :p, seedFrom = :f, seededAt = :t",
            ConditionExpression="attribute_not_exists(seed)",
            ExpressionAttributeValues={":p": {"SS": names}, ":f": _s(from_slug or ""),
                                       ":t": _s(now_iso)})
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def save_derived_roster(scope, slug, players, sample, provisional, now_iso):
    """Persist the roster the bot actually used.

    It is recomputed from the first-kill records on every recap, so this copy is not the
    authority -- it is the record of what the authority said, which is what makes "why did
    it call that report B team" answerable a week later from the console alone. It is also
    what a later tier seeds from, so a rollover does not have to replay the evidence.

    EVERY attribute goes through ExpressionAttributeNames, including the three that do not
    need to. `sample` is a DynamoDB reserved word, so the unaliased version of this call
    raised ValidationException on every recap -- and because the caller swallows the failure
    to keep a roster problem away from the announcement, it did so silently for as long as
    it existed. Aliasing the whole expression rather than the one known-bad name means the
    next attribute added here cannot reintroduce that.
    """
    expr = ["#sample = :n", "#provisional = :p", "#derivedAt = :t"]
    attr = {"#sample": "sample", "#provisional": "provisional", "#derivedAt": "derivedAt"}
    vals = {":n": _n(int(sample or 0)), ":p": {"BOOL": bool(provisional)},
            ":t": _s(now_iso)}
    names = sorted({str(x) for x in (players or ()) if str(x).strip()})
    if names:
        expr.append("#derived = :d")
        attr["#derived"] = "derived"
        vals[":d"] = {"SS": names}
    ddb.update_item(TableName=TABLE, Key={"pk": _s(scope.wow), "sk": _s(roster_sk(slug))},
                    UpdateExpression="SET " + ", ".join(expr),
                    ExpressionAttributeNames=attr,
                    ExpressionAttributeValues=vals)


POSTS_SK = "POSTS"

# How many recent messages are kept and re-checked. Deliberately small. The question is
# "has one of our posts been removed", and a moderator clearing the channel shows up in
# the newest handful as readily as in a hundred; keeping a long tail would mean the bot
# alarms about a post from six weeks ago that somebody tidied on purpose.
POSTS_TRACKED = 8


def record_post(scope, message_id, channel_id, kind, now_iso, keep=POSTS_TRACKED):
    """Remember one message the bot just posted, so its removal can be noticed.

    Stored as a LIST, newest last, trimmed to `keep`. A DynamoDB string set would lose the
    order, and order is the whole value here: the newest post is the one whose deletion
    means something is happening right now.

    Failures are the caller's to swallow. This runs immediately after an announcement has
    already gone out, and nothing about bookkeeping is allowed to reach back and affect a
    post that has landed.
    """
    if not message_id:
        return False
    item = ddb.get_item(TableName=TABLE,
                        Key={"pk": _s(scope.tenant), "sk": _s(POSTS_SK)},
                        ConsistentRead=True).get("Item") or {}
    posts = json.loads((item.get("posts") or {}).get("S") or "[]")
    posts = [p for p in posts if p.get("id") != str(message_id)]
    posts.append({"id": str(message_id), "channel": str(channel_id or ""),
                  "kind": kind, "at": now_iso})
    posts = posts[-int(keep):]
    ddb.put_item(TableName=TABLE, Item={
        "pk": _s(scope.tenant), "sk": _s(POSTS_SK),
        "posts": _s(json.dumps(posts)), "updatedAt": _s(now_iso)})
    return True


def recent_posts(scope):
    """The messages the bot has posted lately, oldest first."""
    item = ddb.get_item(TableName=TABLE,
                        Key={"pk": _s(scope.tenant), "sk": _s(POSTS_SK)}).get("Item")
    if not item:
        return []
    try:
        return json.loads((item.get("posts") or {}).get("S") or "[]")
    except ValueError:
        return []


def forget_post(scope, message_id, now_iso):
    """Drop one message from the tracked list.

    Called after a deletion has been ALERTED on. Without it the same missing post would
    re-alert on the next poll and every poll after that, which trains people to ignore the
    alert -- the one outcome a health notification cannot survive.
    """
    posts = [p for p in recent_posts(scope) if p.get("id") != str(message_id)]
    ddb.put_item(TableName=TABLE, Item={
        "pk": _s(scope.tenant), "sk": _s(POSTS_SK),
        "posts": _s(json.dumps(posts)), "updatedAt": _s(now_iso)})
    return True


RECAPS_SK = "RECAPS"


def claim_recap(scope, night_key):
    """Atomically claim the right to post one night's recap. Same discipline as
    claim_boss, and for the same reason -- Discord has no undo.

    Unlike claim_boss this does not require the item to exist first: there is no seeding
    step for recaps, so the very first claim has to be able to create the item. The
    exactly-once property comes from the set membership check, not from the item.
    """
    try:
        ddb.update_item(
            TableName=TABLE, Key={"pk": _s(scope.tenant), "sk": _s(RECAPS_SK)},
            UpdateExpression="ADD posted :b",
            ConditionExpression="attribute_not_exists(posted) OR NOT contains(posted, :k)",
            ExpressionAttributeValues={":b": {"SS": [night_key]}, ":k": _s(night_key)})
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_recap(scope, night_key):
    """Hand the night back after a permanent failure, so the next run retries it."""
    ddb.update_item(TableName=TABLE, Key={"pk": _s(scope.tenant), "sk": _s(RECAPS_SK)},
                    UpdateExpression="DELETE posted :b",
                    ExpressionAttributeValues={":b": {"SS": [night_key]}})


HEALTH_SK = "HEALTH"


def get_health(scope):
    """The last Discord standing this bot recorded, or None if it has never checked.

    None is not "healthy". It is the first run, and handler.py treats it as a state to
    record silently rather than a recovery to celebrate -- otherwise every fresh deploy
    would mail to say nothing is wrong.
    """
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(scope.tenant), "sk": _s(HEALTH_SK)},
                       ConsistentRead=True)
    item = res.get("Item")
    if not item:
        return None
    return {"status": (item.get("status") or {}).get("S") or "",
            "detail": (item.get("detail") or {}).get("S") or "",
            "since": (item.get("since") or {}).get("S") or "",
            "notifiedAt": (item.get("notifiedAt") or {}).get("S") or "",
            # Three-valued on purpose: True, False, or absent because the probe could not
            # say. Absent must not read as False, or an unreachable Discord would look
            # like a bot that just lost its seat.
            "member": (item.get("member") or {}).get("BOOL")}


def put_health(scope, status, detail, since, notified_at="", member=None):
    """Overwrite the standing wholesale.

    A plain PutItem, deliberately -- unlike everything else in this table there is no
    claim to win here. Two overlapping polls that both observe the same state write the
    same item, and the one thing that must not happen (two emails about one kick) is
    prevented by the transition test in handler.py, not by a conditional write. Making it
    conditional would only add a way for the second poll to fail.
    """
    item = {"pk": _s(scope.tenant), "sk": _s(HEALTH_SK),
            "status": _s(status), "detail": _s(detail or ""),
            "since": _s(since), "notifiedAt": _s(notified_at or "")}
    # Written only when the probe actually answered, so "could not tell" stays absent
    # rather than being recorded as a definite False.
    if member is not None:
        item["member"] = {"BOOL": bool(member)}
    ddb.put_item(TableName=TABLE, Item=item)


SOURCE_SK = "SOURCE"


def get_source(scope):
    """Whether the log source was answering last time, and for how many polls it has not.

    The streak lives here rather than in the Lambda because a Lambda container is not a
    place to keep a count -- it is recycled on a whim, and a counter that resets whenever
    AWS feels like it can never reach a threshold of four.
    """
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(scope.wow), "sk": _s(SOURCE_SK)},
                       ConsistentRead=True)
    item = res.get("Item")
    if not item:
        return None
    return {"status": (item.get("status") or {}).get("S") or "",
            "blindPolls": int((item.get("blindPolls") or {}).get("N") or 0),
            "since": (item.get("since") or {}).get("S") or "",
            "notifiedAt": (item.get("notifiedAt") or {}).get("S") or ""}


def put_source(scope, status, blind_polls, since, notified_at=""):
    """Overwrite wholesale, for the same reason put_health does: there is no claim to win
    here, and the one-email-per-event rule is enforced by the transition test rather than
    by a conditional write."""
    ddb.put_item(TableName=TABLE, Item={
        "pk": _s(scope.wow), "sk": _s(SOURCE_SK),
        "status": _s(status), "blindPolls": _n(int(blind_polls or 0)),
        "since": _s(since), "notifiedAt": _s(notified_at or "")})


# --- tenant configuration -------------------------------------------------
#
# The CONFIG row is the install itself: which WoW guild this server tracks, and
# where to post. Written by /setup, read at the top of every poll.
#
# These two take a TENANT PARTITION STRING rather than a Scope, and that is the
# one deliberate exception to the rule above. A Scope needs the WoW guild, and
# the whole point of reading this row is to find out what the WoW guild is --
# there is nothing to build a Scope from until after it returns.

def get_config(tenant):
    """This install's configuration, or None if /setup has never run here.

    None is a real answer, not an error. A server that has added the bot but not
    configured it should be told to run /setup, not shown a stack trace.
    """
    res = ddb.get_item(TableName=TABLE,
                       Key={"pk": _s(tenant), "sk": _s(CONFIG_SK)},
                       ConsistentRead=True)
    item = res.get("Item")
    if not item:
        return None
    return {
        "guild_region": (item.get("guildRegion") or {}).get("S") or "",
        "guild_realm": (item.get("guildRealm") or {}).get("S") or "",
        "guild_name": (item.get("guildName") or {}).get("S") or "",
        "channel_id": (item.get("channelId") or {}).get("S") or "",
        "prog_role_id": (item.get("progRoleId") or {}).get("S") or "",
        "configuredAt": (item.get("configuredAt") or {}).get("S") or "",
        "configuredBy": (item.get("configuredBy") or {}).get("S") or "",
    }


def put_config(tenant, region, realm, name, channel_id, now_iso,
               prog_role_id="", configured_by=""):
    """Write or replace this install's configuration.

    Overwrite rather than conditional-create: re-running /setup to correct a
    typo'd realm has to work, and there is no claim to win here the way there is
    with an announcement.

    Re-pointing a server at a DIFFERENT WoW guild deliberately leaves the old
    ANNOUNCED# rows in place. They are keyed by tier slug, not by guild, so the
    new guild's tiers get their own rows and seed normally; deleting the old ones
    would only risk re-announcing kills if the server ever pointed back.
    """
    ddb.put_item(TableName=TABLE, Item={
        "pk": _s(tenant), "sk": _s(CONFIG_SK),
        "guildRegion": _s(region.lower()), "guildRealm": _s(realm.lower()),
        "guildName": _s(name), "channelId": _s(channel_id),
        "progRoleId": _s(prog_role_id or ""),
        "configuredAt": _s(now_iso), "configuredBy": _s(configured_by or "")})


def scope_for(tenant, cfg):
    """Build the Scope for an install from its CONFIG row."""
    return Scope(wow_pk(cfg["guild_region"], cfg["guild_realm"], cfg["guild_name"]),
                 tenant)


# --- the tenant registry --------------------------------------------------

def register_tenant(tenant):
    """Add an install to the registry. Idempotent -- ADD on a set, so re-running
    /setup does not duplicate anything and does not need a read first."""
    ddb.update_item(
        TableName=TABLE,
        Key={"pk": _s(REGISTRY_PK), "sk": _s(TENANTS_SK)},
        UpdateExpression="ADD tenants :t",
        ExpressionAttributeValues={":t": {"SS": [tenant]}})


def unregister_tenant(tenant):
    """Remove an install, for eject. DELETE on a set is an UpdateItem, so this
    stays inside a grant that deliberately has no DeleteItem."""
    ddb.update_item(
        TableName=TABLE,
        Key={"pk": _s(REGISTRY_PK), "sk": _s(TENANTS_SK)},
        UpdateExpression="DELETE tenants :t",
        ExpressionAttributeValues={":t": {"SS": [tenant]}})


def list_tenants():
    """Every configured install, oldest-order-agnostic.

    A GetItem on a known key, not a Scan. Returns [] when nothing has been set
    up, which the poller treats as 'fall back to the single-tenant path' during
    the migration rather than as 'there is no work'.
    """
    res = ddb.get_item(TableName=TABLE,
                       Key={"pk": _s(REGISTRY_PK), "sk": _s(TENANTS_SK)},
                       ConsistentRead=True)
    item = res.get("Item")
    if not item:
        return []
    return sorted((item.get("tenants") or {}).get("SS") or [])
