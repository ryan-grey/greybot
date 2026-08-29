"""Runtime configuration, read from SSM Parameter Store.

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

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
PREFIX = os.environ.get("SSM_PREFIX", "/greybot")

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


def load():
    """All seven parameters, or a failure naming exactly which are missing.

    SSM answers a request for a parameter that does not exist by simply omitting it from
    Parameters and listing it under InvalidParameters, with a 200. Not checking that turns
    a missing realm slug into a KeyError somewhere much further along.
    """
    if _cache:
        return _cache

    res = ssm.get_parameters(Names=NAMES + OPTIONAL_NAMES, WithDecryption=True)
    got = {p["Name"]: p["Value"] for p in res.get("Parameters", [])}
    missing = [n for n in NAMES if n not in got or not got[n].strip()]
    if missing:
        raise RuntimeError("missing or empty SSM parameters: " + ", ".join(missing))

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
