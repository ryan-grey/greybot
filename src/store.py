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
    bootstrap marker  GUILD#<region>#<realm>#<name>  BOOTSTRAP
    tier state        GUILD#<region>#<realm>#<name>  TIER#<raid-slug>

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


def guild_pk(region, realm, name):
    return f"GUILD#{region.lower()}#{realm.lower()}#{name.lower()}"


def tier_sk(slug):
    return f"TIER#{slug}"


def _key(pk, slug):
    return {"pk": _s(pk), "sk": _s(tier_sk(slug))}


def _bootstrap_key(pk):
    return {"pk": _s(pk), "sk": _s("BOOTSTRAP")}


def is_bootstrapped(pk):
    """Has this guild ever been seeded? One marker item, checked before anything can be
    announced, so 'first run announces nothing' is a branch rather than an emergent
    property of several other rules agreeing with each other."""
    res = ddb.get_item(TableName=TABLE, Key=_bootstrap_key(pk), ConsistentRead=True)
    return bool(res.get("Item"))


def mark_bootstrapped(pk, now_iso, tiers, note=""):
    """Record that seeding happened. Conditional, so two invocations racing the first run
    cannot both seed."""
    try:
        ddb.put_item(TableName=TABLE,
                     Item={**_bootstrap_key(pk), "bootstrappedAt": _s(now_iso),
                           "tiers": _n(tiers), "note": _s(note or "")},
                     ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def load_tier(pk, slug):
    """Current state for one tier. A missing item means this tier has never been seen,
    which is the signal to seed rather than to announce."""
    res = ddb.get_item(TableName=TABLE, Key=_key(pk, slug), ConsistentRead=True)
    item = res.get("Item")
    if not item:
        return None
    return {
        "announced": set((item.get("announced") or {}).get("SS") or []),
        "seedSize": int((item.get("seedSize") or {}).get("N") or 0),
        "baseline": int((item.get("baseline") or {}).get("N") or 0),
        "aotcAnnounced": bool((item.get("aotcAnnounced") or {}).get("BOOL") or False),
        "raidName": (item.get("raidName") or {}).get("S") or "",
        "seededAt": (item.get("seededAt") or {}).get("S") or "",
    }


def seed_tier(pk, slug, already_killed, baseline, raid_name, now_iso, aotc_already=False):
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
    item = {**_key(pk, slug),
            "seedSize": _n(len(names)),
            "baseline": _n(max(int(baseline or 0), len(names))),
            "aotcAnnounced": {"BOOL": bool(aotc_already)},
            "raidName": _s(raid_name or slug),
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


def claim_boss(pk, slug, boss_key):
    """Atomically claim the right to announce one boss. True means announce; False means
    someone already did."""
    try:
        ddb.update_item(
            TableName=TABLE, Key=_key(pk, slug),
            UpdateExpression="ADD announced :b",
            ConditionExpression=("attribute_exists(pk) AND "
                                 "(attribute_not_exists(announced) OR NOT contains(announced, :k))"),
            ExpressionAttributeValues={":b": {"SS": [boss_key]}, ":k": _s(boss_key)})
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_boss(pk, slug, boss_key):
    """Undo a claim whose announcement never made it to Discord, so the next poll retries.
    DELETE on a set, which is UpdateItem -- the role has no DeleteItem and does not need
    one."""
    ddb.update_item(TableName=TABLE, Key=_key(pk, slug),
                    UpdateExpression="DELETE announced :b",
                    ExpressionAttributeValues={":b": {"SS": [boss_key]}})


def claim_aotc(pk, slug):
    """Claim the one-and-only AOTC announcement for this tier. The guard is the whole
    point: the final boss gets re-killed every week for the rest of the tier, and every
    one of those re-kills satisfies 'heroic kills == total bosses'."""
    try:
        ddb.update_item(
            TableName=TABLE, Key=_key(pk, slug),
            UpdateExpression="SET aotcAnnounced = :t",
            ConditionExpression=("attribute_exists(pk) AND "
                                 "(attribute_not_exists(aotcAnnounced) OR aotcAnnounced = :f)"),
            ExpressionAttributeValues={":t": {"BOOL": True}, ":f": {"BOOL": False}})
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_aotc(pk, slug):
    ddb.update_item(TableName=TABLE, Key=_key(pk, slug),
                    UpdateExpression="SET aotcAnnounced = :f",
                    ExpressionAttributeValues={":f": {"BOOL": False}})


def touch(pk, slug, now_iso, raid_name=None):
    expr, vals = ["updatedAt = :u"], {":u": _s(now_iso)}
    if raid_name:
        expr.append("raidName = :r")
        vals[":r"] = _s(raid_name)
    ddb.update_item(TableName=TABLE, Key=_key(pk, slug),
                    UpdateExpression="SET " + ", ".join(expr),
                    ExpressionAttributeValues=vals)


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
