"""Runtime configuration, read from SSM Parameter Store.

Cached with a TTL rather than forever. Caching for the life of the container looks
harmless -- the values rarely change -- but it means a rotated secret does not take
effect until that container happens to recycle, with no signal that anything is stale.
Rotating a credential and watching the bot keep using the old one, with a correct-looking
config_loaded line in the log, is a genuinely hard thing to debug. A few minutes of TTL
costs one GetParameters call per container per interval and removes the whole class of
problem.

Everything that identifies the guild or grants access to something lives here, not in the
Lambda's environment. Two reasons. The webhook URL and the Warcraft Logs secret are
credentials -- a webhook URL is a post-anything-to-#bots capability, and environment
variables are readable by anything that can call GetFunctionConfiguration. And the guild
identity sits alongside them so there is exactly one place to look when the realm slug is
wrong, rather than two that can disagree.

Fetched once per container and cached. The seven required names arrive in a single
GetParameters call; the optional ones follow in chunks of ten, which is that call's limit.
"""

import json
import os
import time

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
PREFIX = os.environ.get("SSM_PREFIX", "/greybot")
TTL_SECONDS = float(os.environ.get("CONFIG_TTL_SECONDS", "300"))

WCL_CLIENT_ID = f"{PREFIX}/wcl/client_id"
WCL_CLIENT_SECRET = f"{PREFIX}/wcl/client_secret"
DISCORD_WEBHOOK = f"{PREFIX}/discord/webhook_url"
DISCORD_ROLE_ID = f"{PREFIX}/discord/prog_role_id"
GUILD_NAME = f"{PREFIX}/guild/name"
GUILD_REALM = f"{PREFIX}/guild/realm"
GUILD_REGION = f"{PREFIX}/guild/region"

NAMES = [WCL_CLIENT_ID, WCL_CLIENT_SECRET, DISCORD_WEBHOOK, DISCORD_ROLE_ID,
         GUILD_NAME, GUILD_REALM, GUILD_REGION]

# Optional. Boss art is decoration, so the bot must run perfectly well without these --
# missing Blizzard credentials cost the card its portrait and nothing else. Making them
# required would let an unset parameter take the announcements down.
BLIZZARD_CLIENT_ID = f"{PREFIX}/blizzard/client_id"
BLIZZARD_CLIENT_SECRET = f"{PREFIX}/blizzard/client_secret"

# Also optional, and for the same reason: slash commands are an addition to the bot, not a
# prerequisite for it. A missing public key disables the interactions endpoint; it must
# never stop the announcer from announcing.
DISCORD_BOT_TOKEN = f"{PREFIX}/discord/bot_token"
DISCORD_PUBLIC_KEY = f"{PREFIX}/discord/public_key"
DISCORD_GUILD_ID = f"{PREFIX}/discord/guild_id"

# The weekly recap and its team classification. Optional for the same reason, and for
# one more: every one of these is unreadable until the execution role is widened to
# include them, and the bot must survive that gap rather than stop announcing kills
# because a feature nobody has enabled yet cannot read its own settings.
#
# RECAP_ENABLED defaults to FALSE when absent, which makes deploying this code a no-op.
# The recap posts a card naming individual raiders into a live guild channel; the one
# thing it must not do is start doing that because a parameter was missing.
RECAP_ENABLED = f"{PREFIX}/recap/enabled"
RECAP_WORST_PARSE = f"{PREFIX}/recap/show_worst_parse"
RECAP_SCHEDULE = f"{PREFIX}/recap/schedule"
TEAM_ROSTER_MIN_PCT = f"{PREFIX}/team/roster_min_first_kill_pct"
TEAM_OVERLAP_HIGH = f"{PREFIX}/team/prog_overlap_high"
TEAM_OVERLAP_LOW = f"{PREFIX}/team/prog_overlap_low"
# Empty today, because Scrambled tags no reports. Introspection confirmed the API
# supports it (reports(guildTagID:), Report.guildTag, Guild.tags), so if the guild
# ever starts tagging its two teams, setting this name turns a statistical guess
# into an authoritative answer with no code change.
TEAM_PROG_TAG = f"{PREFIX}/team/prog_tag"

# Where a health alert is published. Optional, and unset is how the alerts are switched
# off: the probes still run and still log, they simply have nowhere to mail. That is the
# right default for a name that has to exist before the role can be granted it -- the
# parameter can be created in one step and pointed at the topic in another, with no window
# where a half-wired alert either crashes the poll or sends mail nobody expected.
ALERT_TOPIC_ARN = f"{PREFIX}/alerts/sns_topic_arn"

OPTIONAL_NAMES = [BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET,
                  DISCORD_BOT_TOKEN, DISCORD_PUBLIC_KEY, DISCORD_GUILD_ID,
                  RECAP_ENABLED, RECAP_WORST_PARSE, RECAP_SCHEDULE,
                  TEAM_ROSTER_MIN_PCT, TEAM_OVERLAP_HIGH, TEAM_OVERLAP_LOW,
                  TEAM_PROG_TAG, ALERT_TOPIC_ARN]

# Defaults for everything the recap reads. A missing parameter is a configured default,
# not a failure -- the thresholds especially, because they are tuning knobs that only
# become interesting once the first few weeks show how much the two teams overlap.
#
# 70 and 35 are a MARGIN, not a majority, which is the whole point. Several people raid
# on both teams: a B-team report carrying four of them out of twenty raiders sits near
# 20% overlap, an A-team night with a couple of pugs sits near 90%, and the 35-70 gap in
# between is where the bot admits it cannot tell and says nothing.
DEFAULTS = {
    RECAP_ENABLED: "false",
    RECAP_WORST_PARSE: "false",     # parse-shaming starts arguments; opt in, never out
    RECAP_SCHEDULE: "",
    TEAM_ROSTER_MIN_PCT: "50",
    TEAM_OVERLAP_HIGH: "70",
    TEAM_OVERLAP_LOW: "35",
    TEAM_PROG_TAG: "",
    ALERT_TOPIC_ARN: "",
}


def _flag(raw, default=False):
    v = (raw or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _number(raw, default):
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return float(default)

_cfg = Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=10)
ssm = boto3.client("ssm", region_name=REGION, config=_cfg)

_cache = {}
_fetched_at = {"t": 0.0}


def load(now=None):
    """All seven parameters, or a failure naming exactly which are missing.

    SSM answers a request for a parameter that does not exist by simply omitting it from
    Parameters and listing it under InvalidParameters, with a 200. Not checking that turns
    a missing realm slug into a KeyError somewhere much further along.
    """
    now = now if now is not None else time.time()
    if _cache and (now - _fetched_at["t"]) < TTL_SECONDS:
        return _cache

    res = ssm.get_parameters(Names=NAMES, WithDecryption=True)
    got = {p["Name"]: p["Value"] for p in res.get("Parameters", [])}
    missing = [n for n in NAMES if n not in got or not got[n].strip()]
    if missing:
        raise RuntimeError("missing or empty SSM parameters: " + ", ".join(missing))

    # Optional parameters are fetched SEPARATELY, and failure here is survivable.
    # GetParameters denies the entire call if the caller lacks permission on any single
    # name in it, so asking for optional names alongside required ones means one
    # un-granted parameter takes down the whole bot rather than disabling one feature.
    # That is exactly what happened when the Discord interaction parameters were added
    # before the role was widened: the announcer stopped, for want of a slash command.
    #
    # Chunked in tens because that is GetParameters' hard limit and the optional list has
    # outgrown it. Each chunk is caught separately, so a grant that covers the Blizzard
    # keys but not the recap ones disables the recap and keeps the boss art, rather than
    # losing both to one AccessDenied.
    for i in range(0, len(OPTIONAL_NAMES), 10):
        chunk = OPTIONAL_NAMES[i:i + 10]
        try:
            opt = ssm.get_parameters(Names=chunk, WithDecryption=True)
            got.update({p["Name"]: p["Value"] for p in opt.get("Parameters", [])})
        except Exception as exc:                               # noqa: BLE001
            print(json.dumps({"event": "optional_config_unavailable", "error": repr(exc),
                              "names": chunk,
                              "note": "those optional features disabled; required config "
                                      "is fine"}))

    _cache.clear()
    _fetched_at["t"] = now
    _cache.update({
        "wcl_client_id": got[WCL_CLIENT_ID].strip(),
        "wcl_client_secret": got[WCL_CLIENT_SECRET].strip(),
        "webhook": got[DISCORD_WEBHOOK].strip(),
        "role_id": got[DISCORD_ROLE_ID].strip(),
        "guild_name": got[GUILD_NAME].strip(),
        # Raider.IO and Warcraft Logs both want the realm SLUG, lowercase and hyphenated.
        # A display name ("Proudmoore", or worse "Aerie Peak") 404s the profile call, so
        # normalise here rather than depending on how the parameter was typed.
        "guild_realm": got[GUILD_REALM].strip().lower().replace(" ", "-"),
        "guild_region": got[GUILD_REGION].strip().lower(),
        "blizzard_client_id": got.get(BLIZZARD_CLIENT_ID, "").strip(),
        "blizzard_client_secret": got.get(BLIZZARD_CLIENT_SECRET, "").strip(),
        "bot_token": got.get(DISCORD_BOT_TOKEN, "").strip(),
        "public_key": got.get(DISCORD_PUBLIC_KEY, "").strip(),
        "discord_guild_id": got.get(DISCORD_GUILD_ID, "").strip(),
        "recap_enabled": _flag(got.get(RECAP_ENABLED, DEFAULTS[RECAP_ENABLED])),
        "recap_worst_parse": _flag(got.get(RECAP_WORST_PARSE,
                                           DEFAULTS[RECAP_WORST_PARSE])),
        "recap_schedule": got.get(RECAP_SCHEDULE, DEFAULTS[RECAP_SCHEDULE]).strip(),
        "roster_min_pct": _number(got.get(TEAM_ROSTER_MIN_PCT),
                                  DEFAULTS[TEAM_ROSTER_MIN_PCT]),
        "overlap_high": _number(got.get(TEAM_OVERLAP_HIGH), DEFAULTS[TEAM_OVERLAP_HIGH]),
        "overlap_low": _number(got.get(TEAM_OVERLAP_LOW), DEFAULTS[TEAM_OVERLAP_LOW]),
        "prog_tag": got.get(TEAM_PROG_TAG, DEFAULTS[TEAM_PROG_TAG]).strip(),
        "alert_topic_arn": got.get(ALERT_TOPIC_ARN,
                                   DEFAULTS[ALERT_TOPIC_ARN]).strip(),
    })
    return _cache


def redacted(cfg):
    """A form of the config that is safe to log."""
    return {"guild": cfg["guild_name"], "realm": cfg["guild_realm"],
            "region": cfg["guild_region"], "roleId": cfg["role_id"],
            "wclClientId": cfg["wcl_client_id"], "webhookSet": bool(cfg["webhook"]),
            "bossArtEnabled": bool(cfg.get("blizzard_client_id")
                                   and cfg.get("blizzard_client_secret")),
            "interactionsEnabled": bool(cfg.get("public_key")),
            "botTokenSet": bool(cfg.get("bot_token")),
            "recapEnabled": bool(cfg.get("recap_enabled")),
            "recapWorstParse": bool(cfg.get("recap_worst_parse")),
            "rosterMinPct": cfg.get("roster_min_pct"),
            "overlapHigh": cfg.get("overlap_high"),
            "overlapLow": cfg.get("overlap_low"),
            "progTag": cfg.get("prog_tag") or None,
            "alertsEnabled": bool(cfg.get("alert_topic_arn"))}
