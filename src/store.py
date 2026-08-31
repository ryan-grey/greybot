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


def put_snapshot(pk, slug, raid_name, killed, total, realm_rank, now_iso):
    """What /progress answers from.

    The slash command has a hard three-second budget including cold start, and a
    Raider.IO round trip inside that is a gamble. The poller already makes that call
    every fifteen minutes, so it leaves the display values behind and the command reads
    one item. This is a cache of something already fetched, not a second source of truth --
    the dedupe state remains the authority on what has been announced.
    """
    ddb.put_item(TableName=TABLE, Item={
        "pk": _s(pk), "sk": _s("PROGRESS"),
        "slug": _s(slug), "raidName": _s(raid_name or slug),
        "killed": _n(int(killed or 0)), "total": _n(int(total or 0)),
        "realmRank": _n(int(realm_rank or 0)), "updatedAt": _s(now_iso)})


def get_snapshot(pk):
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(pk), "sk": _s("PROGRESS")})
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
#   first-kill roster GUILD#<region>#<realm>#<name>  KILL#<slug>#<boss-key>
#   derived roster    GUILD#<region>#<realm>#<name>  ROSTER#<slug>
#   recap claims      GUILD#<region>#<realm>#<name>  RECAPS
#
# A boss in `announced` with no KILL# item is a boss that was SEEDED -- killed before the
# bot was watching, or absorbed by a rollover seed. Those have no participants to record
# and never will, and the recap treats them as "long dead", which is exactly right.


def kill_sk(slug, boss_key):
    return f"KILL#{slug}#{boss_key}"


def roster_sk(slug):
    return f"ROSTER#{slug}"


def record_first_kill(pk, slug, boss_key, players, killed_at_ms, report_code, now_iso):
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
        "pk": _s(pk), "sk": _s(kill_sk(slug, boss_key)),
        "players": {"SS": names}, "killedAtMs": _n(int(killed_at_ms or 0)),
        "reportCode": _s(report_code or ""), "recordedAt": _s(now_iso)})
    return True


def get_first_kill(pk, slug, boss_key):
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(pk), "sk": _s(kill_sk(slug, boss_key))})
    item = res.get("Item")
    if not item:
        return None
    return {"players": set((item.get("players") or {}).get("SS") or []),
            "killedAtMs": int((item.get("killedAtMs") or {}).get("N") or 0),
            "reportCode": (item.get("reportCode") or {}).get("S") or ""}


def first_kills(pk, slug, boss_keys):
    """Every recorded first kill for this tier, keyed by boss.

    `boss_keys` comes from the tier's announced set. Bosses with no record are simply
    absent from the result; the caller needs to tell "seeded, no participants known"
    apart from "nobody was there", and an omission says the first thing.
    """
    out = {}
    for key in sorted(boss_keys or ()):
        rec = get_first_kill(pk, slug, key)
        if rec and rec["players"]:
            out[key] = rec
    return out


def load_roster(pk, slug):
    """The persisted roster for one tier: what was derived, and what it was seeded from."""
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(pk), "sk": _s(roster_sk(slug))},
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


def seed_roster(pk, slug, players, from_slug, now_iso):
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
            TableName=TABLE, Key={"pk": _s(pk), "sk": _s(roster_sk(slug))},
            UpdateExpression="SET seed = :p, seedFrom = :f, seededAt = :t",
            ConditionExpression="attribute_not_exists(seed)",
            ExpressionAttributeValues={":p": {"SS": names}, ":f": _s(from_slug or ""),
                                       ":t": _s(now_iso)})
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def save_derived_roster(pk, slug, players, sample, provisional, now_iso):
    """Persist the roster the bot actually used.

    It is recomputed from the first-kill records on every recap, so this copy is not the
    authority -- it is the record of what the authority said, which is what makes "why did
    it call that report B team" answerable a week later from the console alone. It is also
    what a later tier seeds from, so a rollover does not have to replay the evidence.
    """
    expr = ["sample = :n", "provisional = :p", "derivedAt = :t"]
    vals = {":n": _n(int(sample or 0)), ":p": {"BOOL": bool(provisional)},
            ":t": _s(now_iso)}
    names = sorted({str(x) for x in (players or ()) if str(x).strip()})
    if names:
        expr.append("derived = :d")
        vals[":d"] = {"SS": names}
    ddb.update_item(TableName=TABLE, Key={"pk": _s(pk), "sk": _s(roster_sk(slug))},
                    UpdateExpression="SET " + ", ".join(expr),
                    ExpressionAttributeValues=vals)


RECAPS_SK = "RECAPS"


def claim_recap(pk, night_key):
    """Atomically claim the right to post one night's recap. Same discipline as
    claim_boss, and for the same reason -- Discord has no undo.

    Unlike claim_boss this does not require the item to exist first: there is no seeding
    step for recaps, so the very first claim has to be able to create the item. The
    exactly-once property comes from the set membership check, not from the item.
    """
    try:
        ddb.update_item(
            TableName=TABLE, Key={"pk": _s(pk), "sk": _s(RECAPS_SK)},
            UpdateExpression="ADD posted :b",
            ConditionExpression="attribute_not_exists(posted) OR NOT contains(posted, :k)",
            ExpressionAttributeValues={":b": {"SS": [night_key]}, ":k": _s(night_key)})
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_recap(pk, night_key):
    """Hand the night back after a permanent failure, so the next run retries it."""
    ddb.update_item(TableName=TABLE, Key={"pk": _s(pk), "sk": _s(RECAPS_SK)},
                    UpdateExpression="DELETE posted :b",
                    ExpressionAttributeValues={":b": {"SS": [night_key]}})


HEALTH_SK = "HEALTH"


def get_health(pk):
    """The last Discord standing this bot recorded, or None if it has never checked.

    None is not "healthy". It is the first run, and handler.py treats it as a state to
    record silently rather than a recovery to celebrate -- otherwise every fresh deploy
    would mail to say nothing is wrong.
    """
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(pk), "sk": _s(HEALTH_SK)},
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


def put_health(pk, status, detail, since, notified_at="", member=None):
    """Overwrite the standing wholesale.

    A plain PutItem, deliberately -- unlike everything else in this table there is no
    claim to win here. Two overlapping polls that both observe the same state write the
    same item, and the one thing that must not happen (two emails about one kick) is
    prevented by the transition test in handler.py, not by a conditional write. Making it
    conditional would only add a way for the second poll to fail.
    """
    item = {"pk": _s(pk), "sk": _s(HEALTH_SK),
            "status": _s(status), "detail": _s(detail or ""),
            "since": _s(since), "notifiedAt": _s(notified_at or "")}
    # Written only when the probe actually answered, so "could not tell" stays absent
    # rather than being recorded as a definite False.
    if member is not None:
        item["member"] = {"BOOL": bool(member)}
    ddb.put_item(TableName=TABLE, Item=item)


SOURCE_SK = "SOURCE"


def get_source(pk):
    """Whether the log source was answering last time, and for how many polls it has not.

    The streak lives here rather than in the Lambda because a Lambda container is not a
    place to keep a count -- it is recycled on a whim, and a counter that resets whenever
    AWS feels like it can never reach a threshold of four.
    """
    res = ddb.get_item(TableName=TABLE, Key={"pk": _s(pk), "sk": _s(SOURCE_SK)},
                       ConsistentRead=True)
    item = res.get("Item")
    if not item:
        return None
    return {"status": (item.get("status") or {}).get("S") or "",
            "blindPolls": int((item.get("blindPolls") or {}).get("N") or 0),
            "since": (item.get("since") or {}).get("S") or "",
            "notifiedAt": (item.get("notifiedAt") or {}).get("S") or ""}


def put_source(pk, status, blind_polls, since, notified_at=""):
    """Overwrite wholesale, for the same reason put_health does: there is no claim to win
    here, and the one-email-per-event rule is enforced by the transition test rather than
    by a conditional write."""
    ddb.put_item(TableName=TABLE, Item={
        "pk": _s(pk), "sk": _s(SOURCE_SK),
        "status": _s(status), "blindPolls": _n(int(blind_polls or 0)),
        "since": _s(since), "notifiedAt": _s(notified_at or "")})
