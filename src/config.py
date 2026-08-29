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

Fetched once per container and cached. get_parameters takes up to ten names, so all seven
arrive in a single call rather than seven.
"""

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
OPTIONAL_NAMES = [BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET]

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

    res = ssm.get_parameters(Names=NAMES + OPTIONAL_NAMES, WithDecryption=True)
    got = {p["Name"]: p["Value"] for p in res.get("Parameters", [])}
    missing = [n for n in NAMES if n not in got or not got[n].strip()]
    if missing:
        raise RuntimeError("missing or empty SSM parameters: " + ", ".join(missing))

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
    })
    return _cache


def redacted(cfg):
    """A form of the config that is safe to log."""
    return {"guild": cfg["guild_name"], "realm": cfg["guild_realm"],
            "region": cfg["guild_region"], "roleId": cfg["role_id"],
            "wclClientId": cfg["wcl_client_id"], "webhookSet": bool(cfg["webhook"]),
            "bossArtEnabled": bool(cfg.get("blizzard_client_id")
                                   and cfg.get("blizzard_client_secret"))}
