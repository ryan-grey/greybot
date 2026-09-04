#!/usr/bin/env python3
"""Offline self-test. No AWS, no boto3, no network. scripts/deploy.sh runs it first.

The bot's whole job is "announce exactly once", and the failure it must never have is a
duplicate post -- which is invisible in staging, unrecoverable in production, and lands in
a channel other people read. So the dedupe is tested against a fake DynamoDB that actually
evaluates the ConditionExpressions store.py sends, rather than against a stub that agrees
with whatever the code does. The fake understands only the handful of expressions this
codebase uses; it is a test double for these calls, not a DynamoDB implementation.

Everything else here is a pure function fed a fixture captured from the live APIs on
2026-08-28, including the awkward one: `tier-mn-1`, whose slug no zone name will ever
slugify into.
"""

import json
import os
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("AWS_REGION", "us-east-1")

FAILED = []

import urllib.request                                                       # noqa: E402


def _no_network(*a, **kw):
    raise AssertionError(
        "the self-test tried to make a real HTTP request — it must be fully offline")


urllib.request.urlopen = _no_network


# A fixed Discord snowflake for the tests. Any valid one will do; what
# matters is that it is the SAME one throughout, so dedupe rows written by
# one test are found by the next.
TEST_TENANT = "900000000000000001"


def check(name, cond, detail=""):
    if cond:
        print(f"  [ok] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILED.append(name)


# ---------------------------------------------------------------- fake AWS

class ClientError(Exception):
    def __init__(self, response, op):
        super().__init__(response["Error"]["Code"])
        self.response = response
        self.operation_name = op


def _conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                  "Message": "The conditional request failed"}}, "Op")


# DynamoDB's reserved word list is 573 entries long; this is the subset a bot about raid
# rosters and kill counts can plausibly reach for, which is the only part that can catch a
# mistake in THIS codebase. Add to it when a new attribute name is added, not before.
RESERVED_WORDS = frozenset("""
    count counter current data date default first from group hash health hour index items
    key keys language last max member min minute month name names number order percent
    period position range raw role sample second self sequence server set sets size
    snapshot source space start state status system time timestamp total type update user
    value values view year zone
""".split())


class FakeDynamo:
    """In-memory table that evaluates the exact conditions store.py sends."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _k(key):
        return (key["pk"]["S"], key["sk"]["S"])

    def get_item(self, TableName, Key, ConsistentRead=False):
        item = self.items.get(self._k(Key))
        return {"Item": dict(item)} if item else {}

    def put_item(self, TableName, Item, ConditionExpression=None):
        k = self._k(Item)
        if ConditionExpression == "attribute_not_exists(pk)" and k in self.items:
            raise _conditional_failure()
        self.items[k] = dict(Item)
        return {}

    def update_item(self, TableName, Key, UpdateExpression,
                    ConditionExpression=None, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None):
        k = self._k(Key)
        item = self.items.get(k)
        vals = ExpressionAttributeValues or {}

        if ConditionExpression:
            if "attribute_exists(pk)" in ConditionExpression and item is None:
                raise _conditional_failure()
            # Set-membership guards, over whichever set attribute the caller named:
            # `announced` for boss claims, `posted` for recap claims.
            guard = re.search(r"NOT contains\((\w+), :k\)", ConditionExpression)
            if guard:
                have = set(((item or {}).get(guard.group(1)) or {}).get("SS") or [])
                if vals[":k"]["S"] in have:
                    raise _conditional_failure()
            if "aotcAnnounced = :f" in ConditionExpression:
                if bool((item.get("aotcAnnounced") or {}).get("BOOL")):
                    raise _conditional_failure()
            # A bare attribute guard -- store.seed_roster's "seed this once". Matched in
            # full rather than by substring so it cannot also fire on the OR-form above,
            # where attribute_not_exists is one alternative and not the whole condition.
            once = re.fullmatch(r"attribute_not_exists\((\w+)\)", ConditionExpression.strip())
            if once and item is not None and once.group(1) in item:
                raise _conditional_failure()

        if item is None:
            item = dict(Key)
            self.items[k] = item

        # Any placeholder name, not just :b. Real DynamoDB does not care what the
        # value is called, and hardcoding one here made the fake quietly ignore
        # the tenant registry's `ADD tenants :t` -- an update that silently does
        # nothing is the worst possible fake behaviour.
        setop = re.match(r"(ADD|DELETE) (\w+) (:\w+)$", UpdateExpression.strip())
        if setop:
            verb, attr, ph = setop.group(1), setop.group(2), setop.group(3)
            have = set((item.get(attr) or {}).get("SS") or [])
            have = have | set(vals[ph]["SS"]) if verb == "ADD" else have - set(vals[ph]["SS"])
            if have:
                item[attr] = {"SS": sorted(have)}
            else:
                item.pop(attr, None)             # DynamoDB drops an emptied set
        elif UpdateExpression.startswith("SET"):
            attrs = ExpressionAttributeNames or {}
            # list_append is modelled rather than rejected. store.record_post uses it to
            # append atomically, and a fake that could not express that would have left
            # the only interesting property of that call -- that two overlapping writers
            # both survive -- untested.
            appends = re.findall(
                r"(\w+)\s*=\s*list_append\(\s*(?:if_not_exists\(\s*\w+\s*,\s*(:\w+)\s*\)"
                r"|(\w+))\s*,\s*(:\w+)\s*\)", UpdateExpression)
            for attr, empty_ph, _plain, new_ph in appends:
                have = list((item.get(attr) or {}).get("L")
                            or (vals.get(empty_ph, {}) or {}).get("L") or [])
                item[attr] = {"L": have + list(vals[new_ph]["L"])}
            body = re.sub(r"\w+\s*=\s*list_append\([^)]*\)[^,]*\)", "",
                          UpdateExpression[4:])
            for assign in body.split(", "):
                assign = assign.strip().strip(",").strip()
                if not assign or "=" not in assign:
                    continue
                field, placeholder = [p.strip() for p in assign.split("=", 1)]
                if not placeholder.startswith(":"):
                    continue
                if field.startswith("#"):
                    field = attrs[field]
                elif field.lower() in RESERVED_WORDS:
                    # The fake used to accept this and store the attribute anyway, so
                    # store.save_derived_roster's unaliased `sample = :n` passed here and
                    # raised ValidationException against the real table on every recap.
                    raise ClientError(
                        {"Error": {"Code": "ValidationException",
                                   "Message": "Invalid UpdateExpression: Attribute name "
                                              f"is a reserved keyword; reserved keyword: "
                                              f"{field}"}}, "UpdateItem")
                item[field] = vals[placeholder]
        return {}


FAKE_DDB = FakeDynamo()
# Fixture values, not real ones. The guild name and realm are the public subject of the
# project, but the OAuth client id and the Discord role id are account identifiers with no
# reason to be in a public repo -- the tests never care what they contain, only that they
# are carried through intact.
SSM_VALUES = {
    "/greybot/wcl/client_id": "00000000-0000-0000-0000-000000000000",
    "/greybot/wcl/client_secret": "not-a-real-secret",
    "/greybot/discord/webhook_url": "https://discord.com/api/webhooks/1/tok",
    "/greybot/discord/prog_role_id": "111111111111111111",
    "/greybot/guild/name": "Scrambled",
    "/greybot/guild/realm": "proudmoore",
    "/greybot/guild/region": "us",
    # The tenant. Required since Phase 2: it is the partition every announcement
    # record is written under, so a poll cannot proceed without knowing which
    # install it is acting as.
    "/greybot/discord/guild_id": TEST_TENANT,
}


def _fake_get_parameters(Names=None, WithDecryption=False):
    return {"Parameters": [{"Name": n, "Value": SSM_VALUES[n]}
                           for n in (Names or []) if n in SSM_VALUES],
            "InvalidParameters": [n for n in (Names or []) if n not in SSM_VALUES]}


FAKE_SSM = types.SimpleNamespace(get_parameters=_fake_get_parameters)

# Every SNS publish the code makes, in order. The health alerts are a mail-on-transition
# rule, so what matters in the tests is not that a message can be built -- it is HOW MANY
# arrive across a sequence of polls. This list is the assertion.
SENT = []


def _fake_publish(TopicArn=None, Subject=None, Message=None):
    SENT.append({"topic": TopicArn, "subject": Subject, "body": Message})
    return {"MessageId": "fake"}


FAKE_SNS = types.SimpleNamespace(publish=_fake_publish)

boto3 = types.ModuleType("boto3")
boto3.client = lambda service, **kw: {"dynamodb": FAKE_DDB,
                                      "sns": FAKE_SNS}.get(service, FAKE_SSM)
botocore = types.ModuleType("botocore")
botocore.config = types.ModuleType("botocore.config")
botocore.config.Config = lambda **kw: None
botocore.exceptions = types.ModuleType("botocore.exceptions")
botocore.exceptions.ClientError = ClientError
sys.modules.update({"boto3": boto3, "botocore": botocore,
                    "botocore.config": botocore.config,
                    "botocore.exceptions": botocore.exceptions})

import blizzard           # noqa: E402
import config             # noqa: E402
import interactions       # noqa: E402
import discord            # noqa: E402
import raiderio           # noqa: E402
import health             # noqa: E402
import notify             # noqa: E402
import keys
import store              # noqa: E402
import team               # noqa: E402
import recap              # noqa: E402

# A REAL Warcraft Logs response, trimmed and with character names substituted. Its whole
# reason for existing is that the shapes of `table` and `rankings` are undocumented and
# were not what a careful reading of the brief would have produced. See src/recap.py.
FIXTURE = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "fixtures", "report-recap.json"), encoding="utf-8"))
import wcl                # noqa: E402

# ---------------------------------------------------------------- fixtures
# Captured live from raider.io on 2026-08-28.

RAIDS = [
    {
        "slug": "tier-mn-1",
        "icon": "inv_achievement_raid_darkwell",
        "name": "MN Tier 1 (VS / DR / MQD)",
        "short_name": "VS/DR/MQD",
        "starts": {
            "us": "2026-03-17T15:00:00Z"
        },
        "ends": {
            "us": "2026-08-18T15:00:00Z"
        },
        "encounters": [
            {
                "name": "Imperator Averzian"
            },
            {
                "name": "Vorasius"
            },
            {
                "name": "Fallen-King Salhadaar"
            },
            {
                "name": "Vaelgor & Ezzorak"
            },
            {
                "name": "Lightblinded Vanguard"
            },
            {
                "name": "Crown of the Cosmos"
            },
            {
                "name": "Chimaerus the Undreamt God"
            },
            {
                "name": "Belo'ren, Child of Al'ar"
            },
            {
                "name": "Midnight Falls"
            }
        ]
    },
    {
        "slug": "sporefall",
        "icon": "inv_1207_achievement_raid_fungariangiant_fungalgiant",
        "name": "Sporefall",
        "short_name": "SF",
        "starts": {
            "us": "2026-06-16T15:00:00Z"
        },
        "ends": {
            "us": "2026-08-18T15:00:00Z"
        },
        "encounters": [
            {
                "name": "Rotmire"
            }
        ]
    },
    {
        "slug": "the-tidebound-grotto",
        "icon": "achievement_boss_elitenagamale",
        "name": "The Tidebound Grotto",
        "short_name": "TG",
        "starts": {
            "us": "2026-08-18T15:00:00Z"
        },
        "ends": {
            "us": "2030-01-01T00:00:00Z"
        },
        "encounters": [
            {
                "name": "Nymrissa Wavecaller"
            }
        ]
    },
    {
        "slug": "the-venomous-abyss",
        "icon": "8039569",
        "name": "The Venomous Abyss",
        "short_name": "VA",
        "starts": {
            "us": "2026-08-18T15:00:00Z"
        },
        "ends": {
            "us": "2030-01-01T00:00:00Z"
        },
        "encounters": [
            {
                "name": "Nek'zali the Soulcoiler"
            },
            {
                "name": "Entombed Sentinels"
            },
            {
                "name": "The Lost Explorers"
            },
            {
                "name": "Vashnik the Malignant"
            },
            {
                "name": "Sszorak"
            },
            {
                "name": "The Twin Fangs"
            },
            {
                "name": "The Coiled Altar"
            },
            {
                "name": "Ula'tek"
            }
        ]
    }
]

PROFILE = {
    "name": "Scrambled", "region": "us", "realm": "Proudmoore", "faction": "alliance",
    "raid_progression": {
        "tier-mn-1": {"summary": "9/9 H", "total_bosses": 9, "normal_bosses_killed": 9,
                      "heroic_bosses_killed": 9, "mythic_bosses_killed": 0},
        "sporefall": {"summary": "1/1 H", "total_bosses": 1, "normal_bosses_killed": 1,
                      "heroic_bosses_killed": 1, "mythic_bosses_killed": 0},
        "the-tidebound-grotto": {"summary": "1/1 H", "total_bosses": 1,
                                 "normal_bosses_killed": 1, "heroic_bosses_killed": 1,
                                 "mythic_bosses_killed": 0},
        "the-venomous-abyss": {"summary": "2/8 H", "total_bosses": 8,
                               "normal_bosses_killed": 8, "heroic_bosses_killed": 2,
                               "mythic_bosses_killed": 0},
    },
    "raid_rankings": {
        "tier-mn-1": {"heroic": {"world": 3094, "region": 1107, "realm": 34}},
        "sporefall": {"heroic": {"world": 8480, "region": 3523, "realm": 120}},
        "the-tidebound-grotto": {"heroic": {"world": 5307, "region": 1702, "realm": 64}},
        "the-venomous-abyss": {"heroic": {"world": 5776, "region": 1879, "realm": 66}},
    },
}

ABYSS = [e["name"] for e in RAIDS[3]["encounters"]]
MN1 = [e["name"] for e in RAIDS[0]["encounters"]]

INDEX = raiderio.RaidIndex(RAIDS)

# Prime the static-data cache so build_index resolves from the fixture instead of calling
# Raider.IO. Without this the offline guard fires, which is exactly what it is for.
# Expansion 10 is loaded on purpose. Scrambled's logs still hold Heroic Manaforge Omega
# kills from the previous expansion, and a boss the index has never heard of cannot be
# recognised as out of scope -- it has to be identifiable in order to be rejected.
PREV_RAIDS = [
    {
        "slug": "manaforge-omega",
        "icon": "inv_112_achievement_raid_manaforgeomega",
        "name": "Manaforge Omega",
        "starts": {
            "us": "2025-08-12T15:00:00Z"
        },
        "ends": {
            "us": "2026-03-02T22:00:00Z"
        },
        "encounters": [
            {
                "name": "Plexus Sentinel"
            },
            {
                "name": "Loom'ithar"
            },
            {
                "name": "Soulbinder Naazindhri"
            },
            {
                "name": "Forgeweaver Araz"
            },
            {
                "name": "The Soul Hunters"
            },
            {
                "name": "Fractillus"
            },
            {
                "name": "Nexus-King Salhadaar"
            },
            {
                "name": "Dimensius"
            }
        ]
    }
]

MANAFORGE = [e["name"] for e in PREV_RAIDS[0]["encounters"]]

raiderio._static_cache.update({9: [], 10: PREV_RAIDS, 11: RAIDS, 12: [], 13: []})


def dt(s):
    from datetime import datetime
    return datetime.fromisoformat(s)


# Prime the static-data cache so build_index resolves from the fixture instead of calling
# Raider.IO. Without this the offline guard fires, which is exactly what it is for.
# Expansion 10 is loaded on purpose. Scrambled's logs still hold Heroic Manaforge Omega
# kills from the previous expansion, and a boss the index has never heard of cannot be
# recognised as out of scope -- it has to be identifiable in order to be rejected.
PREV_RAIDS = [
    {
        "slug": "manaforge-omega",
        "name": "Manaforge Omega",
        "starts": {
            "us": "2025-08-12T15:00:00Z"
        },
        "ends": {
            "us": "2026-03-02T22:00:00Z"
        },
        "encounters": [
            {
                "name": "Plexus Sentinel"
            },
            {
                "name": "Loom'ithar"
            },
            {
                "name": "Soulbinder Naazindhri"
            },
            {
                "name": "Forgeweaver Araz"
            },
            {
                "name": "The Soul Hunters"
            },
            {
                "name": "Fractillus"
            },
            {
                "name": "Nexus-King Salhadaar"
            },
            {
                "name": "Dimensius"
            }
        ]
    }
]

MANAFORGE = [e["name"] for e in PREV_RAIDS[0]["encounters"]]

raiderio._static_cache.update({9: [], 10: PREV_RAIDS, 11: RAIDS, 12: [], 13: []})
INDEX = raiderio.RaidIndex(RAIDS)


# ---------------------------------------------------------------- tests

def test_config():
    print("\nConfig from SSM")
    config._cache.clear()
    cfg = config.load()
    check("the seven required parameters are read in one call",
          {"wcl_client_id", "wcl_client_secret", "webhook", "role_id",
           "guild_name", "guild_realm", "guild_region"} <= set(cfg), sorted(cfg))
    check("Blizzard credentials are optional, and absent means art is simply off",
          cfg["blizzard_client_id"] == ""
          and config.redacted(cfg)["bossArtEnabled"] is False)
    check("guild identity comes from SSM, not the environment",
          (cfg["guild_name"], cfg["guild_realm"], cfg["guild_region"])
          == ("Scrambled", "proudmoore", "us"))
    check("the prog role id is carried through",
          cfg["role_id"] == "111111111111111111")
    check("redacted config never carries the secret or the webhook",
          "not-a-real-secret" not in json.dumps(config.redacted(cfg))
          and "discord.com/api/webhooks" not in json.dumps(config.redacted(cfg)))

    # A realm typed as a display name is the single most likely misconfiguration, and it
    # fails as a 404 from Raider.IO rather than as anything that names the cause.
    config._cache.clear()
    SSM_VALUES["/greybot/guild/realm"] = "Aerie Peak"
    check("a display-name realm is normalised to a slug",
          config.load()["guild_realm"] == "aerie-peak")
    SSM_VALUES["/greybot/guild/realm"] = "proudmoore"

    # A rotated secret must actually take effect. Caching config for the life of the
    # container means a rotation silently does nothing until that container recycles,
    # while config_loaded keeps logging a perfectly correct-looking line.
    config._cache.clear()
    config.load(now=1000.0)
    SSM_VALUES["/greybot/wcl/client_secret"] = "rotated-value"
    check("a rotation is NOT picked up within the TTL",
          config.load(now=1000.0 + config.TTL_SECONDS - 1)["wcl_client_secret"]
          != "rotated-value")
    check("a rotation IS picked up once the TTL expires",
          config.load(now=1000.0 + config.TTL_SECONDS + 1)["wcl_client_secret"]
          == "rotated-value")
    SSM_VALUES["/greybot/wcl/client_secret"] = "not-a-real-secret"

    # An optional parameter the role cannot read must disable one feature, not the bot.
    # GetParameters denies the whole call on a single un-granted name, so the optional set
    # is fetched separately and its failure is survivable.
    config._cache.clear()
    real = config.ssm.get_parameters
    def deny_optional(Names=None, WithDecryption=False):
        if set(Names or []) & set(config.OPTIONAL_NAMES):
            raise RuntimeError("AccessDeniedException: not authorized")
        return real(Names=Names, WithDecryption=WithDecryption)
    config.ssm.get_parameters = deny_optional
    try:
        degraded = config.load(now=5000.0)
        check("an unreadable OPTIONAL parameter does not take the bot down",
              degraded["guild_name"] == "Scrambled")
        check("...it just turns that feature off",
              degraded["public_key"] == "" and degraded["bot_token"] == "")
    except Exception as exc:                                   # noqa: BLE001
        check("an unreadable OPTIONAL parameter does not take the bot down", False, repr(exc))
    finally:
        config.ssm.get_parameters = real
        config._cache.clear()

    config._cache.clear()
    saved = SSM_VALUES.pop("/greybot/guild/realm")
    try:
        config.load()
        check("a missing parameter fails loudly", False, "no error raised")
    except RuntimeError as exc:
        check("a missing parameter fails loudly and names itself",
              "/greybot/guild/realm" in str(exc), str(exc))
    SSM_VALUES["/greybot/guild/realm"] = saved
    config._cache.clear()
    config.load()


def test_boss_art():
    """Art is decoration. Every failure path has to end in a card, not an exception."""
    print("\nBoss art")
    import handler
    FAKE_DDB.items.clear()
    config._cache.clear()

    calls = []
    handler.blizzard.get_token = lambda *a, **kw: "tok"

    def resolver(result):
        def fake(token, name, normalize):
            calls.append(name)
            return result
        return fake

    # No credentials: the raid icon stands in, and Blizzard is never called.
    cfg = dict(config.load())
    handler.blizzard.resolve = resolver((1, "https://never.test/x.jpg"))
    calls.clear()
    got = handler.boss_art(cfg, "Sszorak", "now", fallback="RAID_ICON")
    check("without credentials the raid icon stands in", got == "RAID_ICON", got)
    check("...and Blizzard is not called at all", calls == [], calls)

    # With credentials: resolved once, then served from cache forever.
    cfg["blizzard_client_id"] = "id"
    cfg["blizzard_client_secret"] = "secret"
    handler.blizzard.resolve = resolver((4242, "https://render.test/4242.jpg"))
    calls.clear()
    got = handler.boss_art(cfg, "Sszorak", "now", fallback="RAID_ICON")
    check("a resolved boss gets its own art", got == "https://render.test/4242.jpg", got)
    got = handler.boss_art(cfg, "Sszorak", "now", fallback="RAID_ICON")
    check("the second call is served from cache", len(calls) == 1, calls)
    check("cache survives a reload",
          store.get_art(handler.boss_key("Sszorak"))["displayId"] == 4242)

    # A boss Blizzard has no answer for is cached as a miss, not re-asked forever.
    handler.blizzard.resolve = resolver((None, None))
    calls.clear()
    got = handler.boss_art(cfg, "The Coiled Altar", "now", fallback="RAID_ICON")
    check("an unresolvable boss falls back to the raid icon", got == "RAID_ICON", got)
    handler.boss_art(cfg, "The Coiled Altar", "now", fallback="RAID_ICON")
    check("...and is not looked up again on every future kill", len(calls) == 1, calls)

    # A transient Blizzard failure must NOT be cached -- it should retry next time.
    def boom(token, name, normalize):
        calls.append(name)
        raise handler.blizzard.BlizzardError("HTTP 503")
    handler.blizzard.resolve = boom
    calls.clear()
    got = handler.boss_art(cfg, "Ula'tek", "now", fallback="RAID_ICON")
    check("a Blizzard outage still produces a card", got == "RAID_ICON", got)
    handler.boss_art(cfg, "Ula'tek", "now", fallback="RAID_ICON")
    check("...and is retried rather than cached as a miss", len(calls) == 2, calls)

    check("art URLs are built from the display id",
          blizzard.art_url(4242)
          == "https://render.worldofwarcraft.com/us/npcs/zoom/creature-display-4242.jpg")
    check("no display id means no URL", blizzard.art_url(None) is None)
    FAKE_DDB.items.clear()
    config._cache.clear()


def test_name_normalisation():
    print("\nBoss-name normalisation")
    check("'&' and 'and' fold together",
          raiderio.normalize("Vaelgor & Ezzorak") == raiderio.normalize("Vaelgor and Ezzorak"))
    check("apostrophes and commas are ignored",
          raiderio.normalize("Belo'ren, Child of Al'ar")
          == raiderio.normalize("Beloren Child of Alar"))
    check("a typographic apostrophe matches a straight one",
          raiderio.normalize("Belo\u2019ren") == raiderio.normalize("Belo'ren"))
    check("case is ignored",
          raiderio.normalize("NEK'ZALI THE SOULCOILER")
          == raiderio.normalize("Nek'zali the Soulcoiler"))
    check("every live boss name normalises to something non-empty and unique",
          len({raiderio.normalize(n) for r in RAIDS for e in r["encounters"]
               for n in [e["name"]]}) == sum(len(r["encounters"]) for r in RAIDS))


def test_slug_resolution():
    print("\nRaid-slug resolution")
    # The case the whole design turns on: no slugification of any zone name produces
    # "tier-mn-1", so a zone-name approach misattributes those kills silently.
    slug, meta, how = raiderio.resolve_raid(PROFILE, "Midnight Falls", "MN Tier 1 Raid",
                                            dt("2026-07-01T02:00:00+00:00"), "us", INDEX)
    check("boss name resolves tier-mn-1, which no slugify can reach",
          (slug, how) == ("tier-mn-1", "encounter-name"), f"got {slug!r} via {how!r}")
    check("slugify really would have missed it",
          raiderio.slugify("MN Tier 1 Raid") not in PROFILE["raid_progression"])

    slug, meta, how = raiderio.resolve_raid(PROFILE, "Ula'tek", "The Venomous Abyss",
                                            dt("2026-08-28T02:00:00+00:00"), "us", INDEX)
    check("current-tier boss resolves by name",
          (slug, how) == ("the-venomous-abyss", "encounter-name"), f"got {slug!r}/{how!r}")

    check("every live boss resolves to its own raid by name",
          all(raiderio.resolve_raid(PROFILE, e["name"], "", None, "us", INDEX)[0]
              == r["slug"] for r in RAIDS for e in r["encounters"]))

    slug, meta, how = raiderio.resolve_raid(PROFILE, "Some Brand New Boss",
                                            "The Venomous Abyss",
                                            dt("2026-08-28T02:00:00+00:00"), "us", INDEX)
    check("an unknown boss falls back to the zone slug",
          (slug, how) == ("the-venomous-abyss", "zone-slug"), f"got {slug!r}/{how!r}")

    solo = raiderio.RaidIndex([RAIDS[0], RAIDS[3]])
    slug, meta, how = raiderio.resolve_raid(PROFILE, "Unknown", "Unknown Zone",
                                            dt("2026-05-01T02:00:00+00:00"), "us", solo)
    check("a single live raid resolves by window",
          (slug, how) == ("tier-mn-1", "live-window"), f"got {slug!r}/{how!r}")

    # THE regression. Scrambled farms Manaforge Omega, last expansion's tier. Those bosses
    # are not in raid_progression, and the version that always returned something dumped
    # all eight into the-venomous-abyss -- an 8-boss raid -- so the next real kill there
    # would have read "8 of 8" and fired AOTC with a role ping.
    full = raiderio.build_index(PROFILE, 11)[0]
    for boss in MANAFORGE:
        slug, meta, how = raiderio.resolve_raid(PROFILE, boss, "Manaforge Omega",
                                                dt("2026-08-27T02:00:00+00:00"), "us", full)
        if slug is not None:
            check(f"last expansion's boss {boss!r} must not resolve to a tracked tier",
                  False, f"resolved to {slug!r} via {how!r}")
            break
    else:
        check("every Manaforge Omega boss is recognised and declined", True)
    check("...and declined for the RIGHT reason, not merely unrecognised",
          raiderio.resolve_raid(PROFILE, MANAFORGE[0], "Manaforge Omega",
                                dt("2026-08-27T02:00:00+00:00"), "us", full)[2]
          == "known-raid-not-tracked")
    check("the index spans the previous expansion so those bosses are identifiable",
          "manaforge-omega" in full.raids)

    slug, meta, how = raiderio.resolve_raid(PROFILE, "Totally Unknown Boss", "Nowhere",
                                            dt("2026-08-28T02:00:00+00:00"), "us", full)
    check("an unattributable kill is skipped, never guessed into the newest tier",
          slug is None and how == "unresolved", f"got {slug!r}/{how!r}")

    # The heuristic this design deliberately does NOT use: "the tier that is neither
    # cleared nor untouched". It works today and stops working at the worst moment.
    neither = [k for k, v in PROFILE["raid_progression"].items()
               if 0 < v["heroic_bosses_killed"] < v["total_bosses"]]
    check("the 'neither cleared nor untouched' heuristic happens to work today",
          neither == ["the-venomous-abyss"], neither)
    cleared = {k: dict(v) for k, v in PROFILE["raid_progression"].items()}
    cleared["the-venomous-abyss"]["heroic_bosses_killed"] = 8
    check("...and identifies NO tier the moment the guild clears it, which is AOTC night",
          [k for k, v in cleared.items()
           if 0 < v["heroic_bosses_killed"] < v["total_bosses"]] == [])


def test_seed_names():
    print("\nSeeding a tier from Raider.IO when the logs cannot help")
    mn1 = INDEX.raids["tier-mn-1"]
    abyss = INDEX.raids["the-venomous-abyss"]

    names, basis = raiderio.seed_names(mn1, 9, 9, [])
    check("a cleared tier seeds every boss even with no log history at all",
          len(names) == 9 and basis == "cleared-tier", f"{len(names)} / {basis}")
    check("...which is what stops a transmog run announcing nine ancient first kills",
          raiderio.normalize("Midnight Falls") in names)

    names, basis = raiderio.seed_names(abyss, 2, 8, [])
    check("a partly cleared tier with no history seeds the first N in published order",
          names == {raiderio.normalize(n) for n in ABYSS[:2]} and basis == "assumed-kill-order",
          f"{sorted(names)} / {basis}")

    names, basis = raiderio.seed_names(abyss, 2, 8, [ABYSS[0], ABYSS[1]])
    check("history that already accounts for the count is used as-is",
          len(names) == 2 and basis == "history-only", basis)

    names, basis = raiderio.seed_names(abyss, 2, 8, [ABYSS[4]])
    check("history and the count are unioned, never subtracted",
          raiderio.normalize(ABYSS[4]) in names and len(names) == 3, sorted(names))

    names, basis = raiderio.seed_names(None, 2, 8, [ABYSS[0]])
    check("no static data still seeds whatever history saw",
          names == {raiderio.normalize(ABYSS[0])} and basis == "history-only")

    # Belt and braces: even if resolution went wrong again, a tier can only be seeded with
    # bosses it actually contains. This is the assertion that makes a repeat impossible.
    names, basis = raiderio.seed_names(abyss, 2, 8, MANAFORGE + [ABYSS[0], ABYSS[1]])
    check("foreign bosses can never inflate a tier's seed",
          names == {raiderio.normalize(n) for n in ABYSS[:2]},
          sorted(names))
    check("a seed can never exceed the raid's own boss count",
          len(raiderio.seed_names(abyss, 8, 8, MANAFORGE)[0]) <= 8)


def test_progress_count():
    print("\n'n of total' derivation")
    state = {"announced": {"a", "b", "c", "d", "e", "f"}, "seedSize": 5, "baseline": 5}
    check("stale Raider.IO does not produce a stale count",
          store.progress_count(state, 5, 8) == 6, store.progress_count(state, 5, 8))
    check("Raider.IO wins when it is ahead of us",
          store.progress_count(state, 7, 8) == 7)

    seeded = {"announced": {"a", "b", "c"}, "seedSize": 3, "baseline": 6}
    check("baseline absorbs history the log window missed",
          store.progress_count(seeded, 6, 8) == 6)
    seeded["announced"].add("d")
    check("the next kill counts up from the baseline, not from zero",
          store.progress_count(seeded, 6, 8) == 7)

    check("count is clamped to the boss total",
          store.progress_count({"announced": set("abcdefghijkl"), "seedSize": 0,
                                "baseline": 8}, 8, 8) == 8)
    check("no Raider.IO at all still yields our own count",
          store.progress_count(seeded, None, 8) == 7)

    fresh = {"announced": {"a"}, "seedSize": 0, "baseline": 0}
    check("first kill of a new tier is 1 of 8, not 2",
          store.progress_count(fresh, 1, 8) == 1, store.progress_count(fresh, 1, 8))
    check("...and still 1 when Raider.IO has not caught up at all",
          store.progress_count(fresh, 0, 8) == 1)


def test_tenant_keys():
    print("\nMulti-tenant key layout")

    # --- namespaces are distinct and derived, not passed in ---------------
    wow = keys.wow_pk("US", "Proudmoore", "Scrambled")
    check("WoW partition is case-folded", wow == "WOW#us#proudmoore#scrambled", wow)
    check("WoW partition is not the old GUILD# prefix",
          not wow.startswith("GUILD#"), wow)

    t1 = keys.tenant_pk("1544178250518102016")
    t2 = keys.tenant_pk(1544178250518102016)          # int and str agree
    check("tenant partition from snowflake", t1 == "TENANT#1544178250518102016", t1)
    check("int and str snowflakes agree", t1 == t2)

    # --- a tenant id must be a bare snowflake -----------------------------
    #
    # Not decoration. A tenant id carrying '#' could forge a sort-key boundary,
    # and an empty or blank one would collide every such tenant into a single
    # partition -- which is a cross-tenant data leak wearing a typo's clothes.
    for bad in ("", "   ", None, "123#456", "TENANT#1", "abc", "12 34", "-1", "1.0"):
        try:
            keys.tenant_pk(bad)
            check(f"rejects tenant id {bad!r}", False, "was accepted")
        except ValueError:
            check(f"rejects tenant id {bad!r}", True)

    # --- the two namespaces can never collide ----------------------------
    check("WoW and tenant partitions cannot collide",
          not wow.startswith("TENANT#") and not t1.startswith("WOW#"))

    # --- dedupe lives under the TENANT, which is the whole point ----------
    check("announced set is a tenant sort key",
          keys.announced_sk("abyss") == "ANNOUNCED#abyss")
    check("tier metadata is a WoW sort key",
          keys.tier_sk("abyss") == "TIER#abyss")

    # Two Discord servers tracking the SAME WoW guild: shared upstream,
    # separate dedupe. This is the pair of properties Phase 2 exists to create.
    a = keys.Scope.build("us", "proudmoore", "Scrambled", "111111111111111111")
    b = keys.Scope.build("us", "proudmoore", "Scrambled", "222222222222222222")
    check("same guild, two installs SHARE upstream", a.wow == b.wow, a.wow)
    check("same guild, two installs SPLIT dedupe", a.tenant != b.tenant,
          f"{a.tenant} vs {b.tenant}")

    # And the converse: one install tracking a different guild must not reuse
    # another guild's upstream rows.
    c = keys.Scope.build("eu", "draenor", "Scrambled", "111111111111111111")
    check("different guild, same install SPLITS upstream", a.wow != c.wow)
    check("different guild, same install SHARES tenant", a.tenant == c.tenant)

    # Phase 3 fans one upstream fetch out across the tenants sharing it, which
    # means Scopes end up as dict keys. Defining __eq__ drops the inherited
    # __hash__, so this would fail silently until that phase.
    a2 = keys.Scope.build("us", "proudmoore", "Scrambled", "111111111111111111")
    check("equal scopes compare equal", a == a2)
    check("scopes are hashable and dedupe by value", len({a, a2, b}) == 2)


def test_setup_command():
    print("\n/setup: tenant config")
    import handler
    FAKE_DDB.items.clear()

    OTHER = "900000000000000002"

    def body(gid=TEST_TENANT, perms="32", **opts):
        base = {"region": "us", "realm": "Proudmoore", "guild": "Scrambled",
                "channel": "555000000000000001"}
        base.update(opts)
        return {"guild_id": gid,
                "member": {"permissions": perms, "user": {"id": "777"}},
                "application_id": "1", "token": "tok",
                "data": {"name": "setup",
                         "options": [{"name": k, "value": v}
                                     for k, v in base.items() if v is not None]}}

    # --- the tenant comes from the payload, never from an option -----------
    check("tenant is derived from the verified guild_id",
          keys.tenant_from_interaction(body()) == f"TENANT#{TEST_TENANT}")
    try:
        keys.tenant_from_interaction({"data": {}})
        check("a DM has no tenant to act as", False, "accepted a DM")
    except ValueError:
        check("a DM has no tenant to act as", True)
    # An option called guild_id must be ignored entirely: this is the whole
    # no-endpoint-accepts-a-member-id rule, and it is worth an explicit test.
    forged = body()
    forged["data"]["options"].append({"name": "guild_id", "value": OTHER})
    check("an option cannot forge the tenant",
          keys.tenant_from_interaction(forged) == f"TENANT#{TEST_TENANT}")

    # --- a guild that does not resolve saves NOTHING -----------------------
    def boom(*a, **kw):
        raise RuntimeError("404 not found")
    real_profile, raiderio.guild_profile = raiderio.guild_profile, boom
    try:
        res = handler.handle_setup(body(realm="Nosuchrealm"), "2026-09-01T00:00:00Z")
    finally:
        raiderio.guild_profile = real_profile
    check("an unresolvable guild reports failure", res["ok"] is False)
    check("...and says which realm and guild it tried",
          "Nosuchrealm" in res["embed"]["description"])
    check("...and writes NO config row",
          store.get_config(f"TENANT#{TEST_TENANT}") is None,
          "a typo'd realm would leave a server silently subscribed to nothing")

    # --- the happy path ---------------------------------------------------
    # The suite is fully offline, so the lookup is stubbed. It echoes back the
    # realm Raider.IO would return, which is how the success embed can differ
    # from what was typed.
    def resolved(region, realm, name):
        return {"name": name, "realm": realm.title(), "region": region}
    raiderio.guild_profile = resolved

    res = handler.handle_setup(body(), "2026-09-01T00:00:00Z")
    check("a resolvable guild succeeds", res["ok"] is True)
    cfg = store.get_config(f"TENANT#{TEST_TENANT}")
    check("...writes the config row", cfg is not None)
    check("...case-folds region and realm",
          (cfg["guild_region"], cfg["guild_realm"]) == ("us", "proudmoore"), cfg)
    check("...keeps the guild name as typed", cfg["guild_name"] == "Scrambled")
    check("...records the channel", cfg["channel_id"] == "555000000000000001")
    check("...records who ran it", cfg["configuredBy"] == "777")
    check("...leaves the mention role empty when not given", cfg["prog_role_id"] == "")

    # --- re-running corrects a mistake ------------------------------------
    handler.handle_setup(body(realm="Tichondrius", role="888"),
                         "2026-09-02T00:00:00Z")
    cfg = store.get_config(f"TENANT#{TEST_TENANT}")
    check("re-running /setup replaces the config", cfg["guild_realm"] == "tichondrius")
    check("...and can add a mention role", cfg["prog_role_id"] == "888")

    # --- two servers do not see each other's config -----------------------
    handler.handle_setup(body(gid=OTHER, guild="Elsewhere"),
                         "2026-09-01T00:00:00Z")
    a = store.get_config(f"TENANT#{TEST_TENANT}")
    b = store.get_config(f"TENANT#{OTHER}")
    check("a second server gets its own config", b["guild_name"] == "Elsewhere")
    check("...and does not disturb the first", a["guild_name"] == "Scrambled")
    check("an unconfigured server reads as None",
          store.get_config("TENANT#900000000000000009") is None)

    # --- THE CROSS-TENANT BOUNDARY ----------------------------------------
    #
    # An interaction must be answered as the server that sent it, never as
    # whichever install the poller's config happens to name. This is the one
    # code path that takes input from strangers, so it is where the key layout's
    # isolation is either real or decorative.
    A_scope, A_cfg = handler.interaction_scope({"guild_id": TEST_TENANT}, {})
    B_scope, B_cfg = handler.interaction_scope({"guild_id": OTHER}, {})
    check("server A resolves to its own tenant", A_scope.tenant == f"TENANT#{TEST_TENANT}")
    check("server B resolves to its own tenant", B_scope.tenant == f"TENANT#{OTHER}")
    check("...and to its own WoW guild, not A's",
          B_scope.wow != A_scope.wow, f"{B_scope.wow} vs {A_scope.wow}")
    check("...with its own guild name in the config",
          B_cfg["guild_name"] == "Elsewhere")

    # A server that has never run /setup gets told so, rather than being handed
    # somebody else's data.
    s, why = handler.interaction_scope({"guild_id": "900000000000000009"}, {})
    check("an unconfigured server gets no scope", s is None)
    check("...and is told to run /setup", why == "unconfigured", why)

    # A DM has no server to answer as. This must not raise: an unhandled
    # exception here is a 500 to Discord, which reads as a broken bot.
    s, why = handler.interaction_scope({}, {})
    check("a DM gets no scope and does not raise", s is None)
    check("...and is told commands need a server", why == "dm", why)

    raiderio.guild_profile = real_profile
    FAKE_DDB.items.clear()


def test_tenant_fanout():
    print("\nTenant registry and poll fan-out")
    import handler
    FAKE_DDB.items.clear()

    SSM_CFG = {"guild_region": "us", "guild_realm": "proudmoore",
               "guild_name": "Scrambled", "discord_guild_id": TEST_TENANT,
               "webhook": "https://discord.com/api/webhooks/1/legacy",
               "bot_token": "bot-tok", "wcl_client_id": "id"}

    # --- the migration bridge --------------------------------------------
    # Prod runs from SSM today. An empty registry MUST keep it polling, or
    # migrating would mean an outage.
    check("an empty registry lists no tenants", store.list_tenants() == [])
    pairs = handler.tenant_configs(SSM_CFG)
    check("...so the poll falls back to the SSM config", len(pairs) == 1)
    check("...with the legacy scope", pairs[0][0].tenant == f"TENANT#{TEST_TENANT}")
    check("...and the legacy webhook destination",
          handler.destination(pairs[0][1]) == {"webhook": SSM_CFG["webhook"]})

    # --- registration -----------------------------------------------------
    A, B = f"TENANT#{TEST_TENANT}", "TENANT#900000000000000002"
    store.put_config(A, "us", "Proudmoore", "Scrambled", "555000000000000001", "now")
    store.register_tenant(A)
    check("a registered tenant appears", store.list_tenants() == [A])
    store.register_tenant(A)
    check("...registering twice does not duplicate", store.list_tenants() == [A])

    store.put_config(B, "eu", "Draenor", "Elsewhere", "555000000000000002", "now")
    store.register_tenant(B)
    check("a second tenant appears", store.list_tenants() == sorted([A, B]))

    # --- fan-out ----------------------------------------------------------
    pairs = handler.tenant_configs(SSM_CFG)
    check("the poll now runs for both tenants", len(pairs) == 2)
    by_tenant = {s.tenant: c for s, c in pairs}
    check("...each on its own WoW partition",
          {s.wow for s, _ in pairs} ==
          {"WOW#us#proudmoore#scrambled", "WOW#eu#draenor#elsewhere"})
    check("...keeping the operator's shared secrets",
          all(c["wcl_client_id"] == "id" for c in by_tenant.values()))
    check("...but each install's own guild",
          by_tenant[B]["guild_name"] == "Elsewhere")

    # A configured tenant posts to its channel with the bot token, NOT through
    # the operator's webhook -- otherwise every tenant's kills land in one server.
    dest = handler.destination(by_tenant[B])
    check("a configured tenant posts to its own channel",
          dest == {"bot_token": "bot-tok", "channel": "555000000000000002"}, dest)
    check("...and never through the shared webhook", "webhook" not in dest)

    # --- one broken install must not stop the others ----------------------
    store.register_tenant("TENANT#900000000000000003")   # registered, never configured
    pairs = handler.tenant_configs(SSM_CFG)
    check("a tenant with no config is skipped, not fatal", len(pairs) == 2)
    check("...and the healthy ones still poll",
          {s.tenant for s, _ in pairs} == {A, B})

    # --- the fan-out is actually WIRED IN, not just defined ---------------
    #
    # This was a real gap: tenant_configs() existed and was tested while
    # handler() still polled one SSM-derived scope, so a second tenant would have
    # been registered, configured, and never polled.
    polled = []
    real_poll = handler.poll_one
    handler.poll_one = lambda ev, c, s, n, ni, st: (polled.append(s.tenant)
                                                    or {"ok": True, "tenant": s.tenant})
    try:
        out = handler.handler({"source": "aws.scheduler"}, None)
    finally:
        handler.poll_one = real_poll
    check("handler polls every registered tenant", sorted(polled) == sorted([A, B]),
          polled)
    check("...and reports both", out.get("tenants") == 2, out)

    # One tenant failing must not stop the others: the poller is one Lambda for
    # every install, so an escaping exception would silence everybody.
    polled.clear()
    def one_explodes(ev, c, s, n, ni, st):
        polled.append(s.tenant)
        if s.tenant == A:
            raise RuntimeError("this tenant's channel was revoked")
        return {"ok": True}
    handler.poll_one = one_explodes
    try:
        out = handler.handler({"source": "aws.scheduler"}, None)
    finally:
        handler.poll_one = real_poll
    check("a failing tenant does not stop the others", sorted(polled) == sorted([A, B]))
    check("...and the failure is reported, not swallowed", out.get("failed") == [A], out)

    # --- eject ------------------------------------------------------------
    store.unregister_tenant(B)
    check("an ejected tenant stops being polled",
          [s.tenant for s, _ in handler.tenant_configs(SSM_CFG)] == [A])
    check("...but its config row survives for a re-install",
          store.get_config(B) is not None)

    FAKE_DDB.items.clear()


def test_dedupe():
    print("\nDedupe and the announce-once claim")
    FAKE_DDB.items.clear()
    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    slug = "the-venomous-abyss"

    check("seeding creates the tier",
          store.seed_tier(pk, slug, {"a", "b"}, 2, "The Venomous Abyss", "now"))
    check("seeding twice does not clobber",
          store.seed_tier(pk, slug, {"z"}, 99, "x", "now") is False)
    state = store.load_tier(pk, slug)
    check("seeded set is preserved", state["announced"] == {"a", "b"}, state["announced"])
    check("baseline is max(Raider.IO, history)", state["baseline"] == 2, state["baseline"])

    check("a new boss can be claimed", store.claim_boss(pk, slug, "c"))
    check("the same boss cannot be claimed twice", store.claim_boss(pk, slug, "c") is False)
    check("a seeded boss is never announced", store.claim_boss(pk, slug, "a") is False)

    store.release_boss(pk, slug, "c")
    check("a released boss is retried on the next poll", store.claim_boss(pk, slug, "c"))

    check("a tier that was never seeded refuses claims",
          store.claim_boss(pk, "some-future-tier", "a") is False)

    check("the bootstrap marker starts absent", store.is_bootstrapped(pk) is False)
    check("marking it works", store.mark_bootstrapped(pk, "now", 4))
    check("it cannot be marked twice", store.mark_bootstrapped(pk, "now", 4) is False)
    check("and it reads back as done", store.is_bootstrapped(pk))


def test_aotc_guard():
    print("\nAOTC fires once")
    FAKE_DDB.items.clear()
    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    slug = "the-venomous-abyss"
    store.seed_tier(pk, slug, {"a"}, 1, "The Venomous Abyss", "now")

    check("AOTC can be claimed once", store.claim_aotc(pk, slug))
    check("a re-kill of the final boss does not re-fire it",
          store.claim_aotc(pk, slug) is False)
    check("the flag survives a reload", store.load_tier(pk, slug)["aotcAnnounced"])

    store.release_aotc(pk, slug)
    check("a failed webhook lets AOTC retry", store.claim_aotc(pk, slug))

    FAKE_DDB.items.clear()
    store.seed_tier(pk, "tier-mn-1", {"a"}, 9, "MN Tier 1", "now", aotc_already=True)
    check("seeding a finished tier pre-sets the AOTC flag",
          store.load_tier(pk, "tier-mn-1")["aotcAnnounced"])
    check("so no retroactive AOTC is possible",
          store.claim_aotc(pk, "tier-mn-1") is False)


def test_discord_payloads():
    print("\nDiscord payloads")
    p = discord.kill_embed("Scrambled", ABYSS[2], 3, 8, "The Venomous Abyss", 66,
                           report_url="https://www.warcraftlogs.com/reports/abc")
    body = p["embeds"][0]
    check("title matches the spec line",
          body["title"] == f"Scrambled just killed {ABYSS[2]}", body["title"])
    check("count line matches the spec",
          "They are now **3** of **8** in Heroic The Venomous Abyss" in body["description"])
    check("rank line matches the spec", "Ranked server **#66**" in body["description"])
    check("a kill card mentions nobody", p["allowed_mentions"] == {"parse": []})

    # Raider.IO's terms require a link back from anything public using their data, and
    # both the count and the rank come from them. A footer cannot carry it -- embed
    # footers are plain text, so a link there is dead characters.
    attributed = discord.kill_embed("Scrambled", ABYSS[2], 3, 8, "The Venomous Abyss", 66,
                                    guild_label="Scrambled \u00b7 Proudmoore",
                                    guild_url="https://raider.io/guilds/us/proudmoore/Scrambled")
    check("the card carries a clickable link back to Raider.IO",
          attributed["embeds"][0]["author"]["url"]
          == "https://raider.io/guilds/us/proudmoore/Scrambled")
    check("and it is labelled with the guild, not an advert",
          attributed["embeds"][0]["author"]["name"] == "Scrambled \u00b7 Proudmoore")
    check("no attribution URL means no empty author block",
          "author" not in discord.kill_embed("S", "B", 1, 8, "R", 1)["embeds"][0])
    check("the profile URL comes from Raider.IO's own response",
          raiderio.profile_url({"profile_url": "https://raider.io/x"}, "us", "p", "S")
          == "https://raider.io/x")
    check("...and is constructed only if that is missing",
          raiderio.profile_url({}, "us", "proudmoore", "Scrambled")
          == "https://raider.io/guilds/us/proudmoore/Scrambled")
    check("the guild label uses Raider.IO's realm spelling, not the slug",
          raiderio.guild_display({"realm": "Proudmoore"}, "Scrambled", "proudmoore")
          == "Scrambled \u00b7 Proudmoore")

    # A kill card is for the raid team. A developer plug on every one of them is noise in
    # a channel shared with other people.
    check("a kill card carries NO developer credit",
          "greyBot](" not in json.dumps(attributed))
    check("a card with no art is still a valid card", "thumbnail" not in body)

    art = discord.kill_embed("Scrambled", ABYSS[2], 3, 8, "The Venomous Abyss", 66,
                             thumbnail_url="https://example.test/i.jpg")
    check("art rides along as a thumbnail when there is one",
          art["embeds"][0]["thumbnail"] == {"url": "https://example.test/i.jpg"})

    # Raider.IO hands out an icon NAME for some raids and a bare FileDataID for others,
    # with no apparent rule. Blizzard's CDN resolves both; Wowhead's resolves only one.
    check("an icon name builds a URL",
          raiderio.icon_url({"icon": "inv_achievement_raid_darkwell"})
          == "https://render.worldofwarcraft.com/us/icons/56/inv_achievement_raid_darkwell.jpg")
    check("a bare FileDataID builds one too",
          raiderio.icon_url({"icon": "8039569"})
          == "https://render.worldofwarcraft.com/us/icons/56/8039569.jpg")
    check("a raid with no icon yields no URL and no crash",
          raiderio.icon_url({}) is None and raiderio.icon_url(None) is None)
    check("a path-like icon value is refused rather than interpolated",
          raiderio.icon_url({"icon": "../../evil"}) is None)
    check("every live raid in the fixture has usable art",
          all(raiderio.icon_url(m) for m in INDEX.raids.values()),
          [k for k, m in INDEX.raids.items() if not raiderio.icon_url(m)])

    unranked = discord.kill_embed("Scrambled", ABYSS[0], 1, 8, "The Venomous Abyss", None)
    check("an unranked guild is not 'Ranked server #0'",
          "Ranked server" not in unranked["embeds"][0]["description"])
    check("a real rank is read from the live shape",
          raiderio.realm_rank(PROFILE, "the-venomous-abyss") == 66)

    a = discord.aotc_payload("Scrambled", "The Venomous Abyss",
                             "August 28, 2026 at 11:14 PM EDT", "111111111111111111")
    check("the role is mentioned in content", a["content"] == "<@&111111111111111111>")
    check("the role is allow-listed so the ping actually fires",
          a["allowed_mentions"]["roles"] == ["111111111111111111"])
    check("nothing else can be mentioned", a["allowed_mentions"]["parse"] == [])
    check("AOTC title matches the spec",
          a["embeds"][0]["title"]
          == "Scrambled just got AOTC on August 28, 2026 at 11:14 PM EDT")
    check("AOTC body matches the spec",
          a["embeds"][0]["description"] == "Congratulations to the team!")

    noping = discord.aotc_payload("Scrambled", "R", "when", "")
    check("an unset role id posts without a broken mention", "content" not in noping)

    credited = discord.aotc_payload("Scrambled", "R", "when", "1",
                                    repo_url="https://github.com/ryan-grey/greybot")
    check("AOTC — once per tier — is where the credit goes",
          "[greyBot](https://github.com/ryan-grey/greybot)"
          in credited["embeds"][0]["description"])
    check("...and it still leads with the line from the spec",
          credited["embeds"][0]["description"].startswith("Congratulations to the team!"))
    check("no repo URL means no credit line, not a dead link",
          discord.aotc_payload("S", "R", "w", "1")["embeds"][0]["description"]
          == "Congratulations to the team!")

    # The drawn AOTC card. The image carries the words, the embed keeps only what a PNG
    # cannot do: the ping, the author link and the clickable credit.
    carded = discord.aotc_payload("Scrambled", "R", "when", "1",
                                  thumbnail_url="https://art/x.jpg",
                                  repo_url="https://github.com/ryan-grey/greybot",
                                  card_url="https://raids/cards/r/aotc.png")["embeds"][0]
    check("a drawn AOTC card becomes the embed image",
          carded.get("image") == {"url": "https://raids/cards/r/aotc.png"})
    check("...and the embed stops repeating the card's words",
          "title" not in carded and "thumbnail" not in carded, sorted(carded))
    check("...but the credit link survives, because a PNG cannot be clicked",
          carded.get("description") == "[greyBot](https://github.com/ryan-grey/greybot)")
    check("...and the footer still names the achievement",
          carded["footer"]["text"] == "Ahead of the Curve — Heroic R")
    check("a card with no repo URL has no empty description either",
          "description" not in discord.aotc_payload("S", "R", "w", "1",
                                                    card_url="https://x/aotc.png")
          ["embeds"][0])
    check("the ping is untouched by the card",
          discord.aotc_payload("S", "R", "w", "1", card_url="https://x")["content"]
          == "<@&1>")


def test_wcl_parsing():
    print("\nWarcraft Logs parsing")
    base = 1_756_000_000_000
    fake = {
        "rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 900.0,
                          "pointsResetIn": 1200},
        "reportData": {"reports": {"data": [{
            "code": "aBcD", "startTime": base, "endTime": base + 9_000_000,
            "zone": {"id": 44, "name": "The Venomous Abyss"},
            "fights": [
                {"id": 9, "encounterID": 3010, "name": ABYSS[2], "kill": True,
                 "difficulty": 4, "startTime": 300_000, "endTime": 600_000},
                {"id": 4, "encounterID": 3009, "name": ABYSS[1], "kill": True,
                 "difficulty": 4, "startTime": 100_000, "endTime": 200_000},
                {"id": 2, "encounterID": 3011, "name": "Mythic Thing", "kill": True,
                 "difficulty": 5, "startTime": 50_000, "endTime": 60_000},
                {"id": 1, "encounterID": 0, "name": "Trash", "kill": True,
                 "difficulty": 4, "startTime": 10_000, "endTime": 20_000},
                {"id": 7, "encounterID": 3012, "name": "A Wipe", "kill": False,
                 "difficulty": 4, "startTime": 700_000, "endTime": 800_000},
            ]}]}},
    }
    saved = wcl.query
    wcl.query = lambda token, doc, variables=None: fake
    try:
        kills, rate = wcl.heroic_kills_since("t", 1, 0)
    finally:
        wcl.query = saved

    check("only Heroic boss kills survive",
          [k["name"] for k in kills] == [ABYSS[1], ABYSS[2]], [k["name"] for k in kills])
    check("kills come back oldest-first",
          kills[0]["killedAtMs"] < kills[1]["killedAtMs"])
    check("fight time is an offset from the report start, not an absolute stamp",
          kills[1]["killedAtMs"] == base + 600_000, kills[1]["killedAtMs"])
    check("zone travels with the kill", kills[0]["zoneName"] == "The Venomous Abyss")
    check("report code travels with the kill", kills[0]["reportCode"] == "aBcD")
    check("rate limit is read as a fraction of the hourly allowance",
          rate["fraction"] == 0.25, rate)
    check("a missing rate block is 'unknown', not a crash",
          wcl.rate_limit({}) is None)


def test_wcl_pagination():
    """Regression: a single 100-report page was rejected outright by Warcraft Logs at
    70,705 complexity against a 50,000 ceiling, and the whole first run died with it."""
    print("\nWarcraft Logs paging")
    base = 1_756_000_000_000
    calls = []

    def report(i):
        return {"code": f"r{i}", "startTime": base + i * 1000,
                "zone": {"id": 44, "name": "The Venomous Abyss"},
                "fights": [{"id": 1, "encounterID": 3000 + i, "name": ABYSS[i % 8],
                            "kill": True, "difficulty": 4, "startTime": 0,
                            "endTime": 1000}]}

    def paged(pages):
        def fake(token, doc, variables=None):
            calls.append(dict(variables or {}))
            page = (variables or {}).get("page", 1)
            data = pages[page - 1] if page - 1 < len(pages) else []
            return {"rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 5.0,
                                      "pointsResetIn": 60},
                    "reportData": {"reports": {"data": data}}}
        return fake

    saved = wcl.query
    try:
        check("the query sends a page variable at all",
              "$page: Int!" in wcl.REPORTS_Q and "page: $page" in wcl.REPORTS_Q)

        # Two full pages then a short one: it should walk all three and stop.
        calls.clear()
        wcl.query = paged([[report(i) for i in range(3)],
                           [report(i) for i in range(3, 6)],
                           [report(6)]])
        kills, rate = wcl.heroic_kills_since("t", 1, 0, limit=3, max_pages=6)
        check("a full page triggers the next one", len(calls) == 3, len(calls))
        check("pages are requested in order",
              [c["page"] for c in calls] == [1, 2, 3], [c["page"] for c in calls])
        check("kills from every page are merged", len(kills) == 7, len(kills))
        check("and returned oldest-first",
              [k["killedAtMs"] for k in kills] == sorted(k["killedAtMs"] for k in kills))

        # A short first page is the last page; do not ask for another.
        calls.clear()
        wcl.query = paged([[report(0)]])
        wcl.heroic_kills_since("t", 1, 0, limit=3, max_pages=6)
        check("a short page stops paging immediately", len(calls) == 1, len(calls))

        # max_pages is a hard cap even when every page comes back full.
        calls.clear()
        wcl.query = paged([[report(i) for i in range(3)]] * 10)
        wcl.heroic_kills_since("t", 1, 0, limit=3, max_pages=2)
        check("max_pages caps the walk", len(calls) == 2, len(calls))

        calls.clear()
        wcl.query = paged([[report(i) for i in range(3)]] * 10)
        wcl.heroic_kills_since("t", 1, 0, limit=3)
        check("a routine poll defaults to a single page", len(calls) == 1, len(calls))

        check("the seed page size stays well under the complexity ceiling",
              25 * 707 < 50000, 25 * 707)
        check("...and the routine poll size too", 12 * 707 < 50000, 12 * 707)
    finally:
        wcl.query = saved


def test_interactions():
    """Signature verification, which is the one place a bug is punished by Discord.

    Discord sends deliberately invalid signatures as a routine probe and removes the
    interactions URL from any app that answers 200 to one. So the rejection path is
    tested here as thoroughly as the acceptance path, against real Ed25519 rather than a
    stub -- a stub would happily agree with an implementation that had the arguments the
    wrong way round.
    """
    print("\nDiscord interactions")
    try:
        from nacl.signing import SigningKey
    except ImportError:
        check("PyNaCl available for real signature tests", False,
              "missing — run scripts/deploy.sh (it builds .venv), or "
              "python3 -m venv .venv && .venv/bin/pip install pynacl, "
              "then .venv/bin/python scripts/selftest.py")
        return

    key = SigningKey.generate()
    public_key = key.verify_key.encode().hex()
    body = json.dumps({"type": 1}).encode()
    ts = "1735689600"
    sig = key.sign(ts.encode() + body).signature.hex()

    check("a genuine signature verifies",
          interactions.verify(public_key, sig, ts, body))
    check("a tampered BODY is rejected",
          not interactions.verify(public_key, sig, ts, body + b" "))
    check("a tampered TIMESTAMP is rejected",
          not interactions.verify(public_key, sig, "1735689601", body))
    check("a signature from a different key is rejected",
          not interactions.verify(SigningKey.generate().verify_key.encode().hex(),
                                  sig, ts, body))
    check("a malformed signature is rejected, not raised",
          not interactions.verify(public_key, "zzzz", ts, body))
    check("a malformed public key is rejected, not raised",
          not interactions.verify("nothex", sig, ts, body))
    check("a missing signature header is rejected",
          not interactions.verify(public_key, None, ts, body))
    check("a missing public key is rejected",
          not interactions.verify("", sig, ts, body))

    # The base64 trap: API Gateway may deliver the body encoded, and verifying the encoded
    # string instead of the decoded bytes fails every signature in a way that looks
    # exactly like a wrong key.
    import base64 as _b64
    encoded = {"body": _b64.b64encode(body).decode(), "isBase64Encoded": True}
    check("a base64 body is decoded to the bytes Discord signed",
          interactions.raw_body(encoded) == body)
    check("a plain body passes through unchanged",
          interactions.raw_body({"body": body.decode()}) == body)
    check("...and the encoded form still verifies end to end",
          interactions.verify(public_key, sig, ts, interactions.raw_body(encoded)))

    # Re-serialising the JSON changes the bytes and breaks the signature. This asserts the
    # trap is real, so nobody "tidies" raw_body into a parse-and-dump later.
    reserialised = json.dumps(json.loads(body.decode())).encode()
    check("re-serialising the JSON would have broken it (hence raw bytes)",
          reserialised == body or not interactions.verify(public_key, sig, ts, reserialised))

    import handler
    config._cache.clear()
    cfg = dict(config.load())
    cfg["public_key"] = public_key
    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    def event(payload, signer=key, timestamp=ts):
        raw = json.dumps(payload).encode()
        return {"headers": {"X-Signature-Ed25519": signer.sign(
                                timestamp.encode() + raw).signature.hex(),
                            "X-Signature-Timestamp": timestamp},
                "body": raw.decode(), "isBase64Encoded": False,
                "requestContext": {"http": {"method": "POST"}}}

    res = handler.handle_interaction(event({"type": 1}), cfg, pk, now)
    check("PING is answered with PONG and a 200",
          res["statusCode"] == 200 and json.loads(res["body"]) == {"type": 1}, res)
    check("...with a JSON content type",
          res["headers"]["content-type"] == "application/json")

    bad = event({"type": 1})
    bad["headers"]["X-Signature-Ed25519"] = "00" * 64
    res = handler.handle_interaction(bad, cfg, pk, now)
    check("an INVALID signature gets 401, never 200 — Discord probes for this",
          res["statusCode"] == 401, res)

    forged = event({"type": 1}, signer=SigningKey.generate())
    check("a signature from the wrong key gets 401",
          handler.handle_interaction(forged, cfg, pk, now)["statusCode"] == 401)

    nokey = dict(cfg); nokey["public_key"] = ""
    check("no configured public key means reject, never wave through",
          handler.handle_interaction(event({"type": 1}), nokey, pk, now)["statusCode"] == 401)

    # /progress answered from the snapshot the poller leaves behind.
    FAKE_DDB.items.clear()
    store.put_snapshot(pk, "the-venomous-abyss", "The Venomous Abyss", 2, 8, 67,
                       now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    res = handler.handle_interaction(
        # guild_id, as Discord always sends for a guild command. Since Phase 2
        # this is what decides WHICH install the answer is about, so an
        # interaction without one is a DM rather than a command in a server.
        event({"type": 2, "data": {"name": "progress"}, "token": "t",
               "guild_id": TEST_TENANT, "application_id": "1"}), cfg, pk, now)
    payload = json.loads(res["body"])
    check("/progress answers immediately from the snapshot",
          payload["type"] == interactions.CHANNEL_MESSAGE_WITH_SOURCE, payload)
    desc = payload["data"]["embeds"][0]["description"]
    check("...with the line from the spec",
          "**2** of **8** in Heroic The Venomous Abyss" in desc
          and "ranked server **#67**" in desc, desc)
    check("...ephemeral by default", payload["data"]["flags"] == interactions.EPHEMERAL)
    check("...and mentioning nobody",
          payload["data"]["allowed_mentions"] == {"parse": []})

    check("an unknown command is answered, not ignored",
          json.loads(handler.handle_interaction(
              event({"type": 2, "data": {"name": "nope"}, "token": "t",
                     "application_id": "1"}), cfg, pk, now)["body"])["type"]
          == interactions.CHANNEL_MESSAGE_WITH_SOURCE)

    check("the registered command set is /progress and /setup",
          [c["name"] for c in interactions.COMMANDS] == ["progress", "setup"])

    setup = interactions.SETUP_COMMAND
    # 0x20 is MANAGE_GUILD. Discord takes this as a STRING bitfield; an int here
    # is silently rejected and the command ends up available to everyone.
    check("/setup is gated on Manage Server",
          setup["default_member_permissions"] == "32")
    check("...as a string, which is what Discord accepts",
          isinstance(setup["default_member_permissions"], str))
    check("/setup refuses DMs", setup["dm_permission"] is False)
    opts = {o["name"]: o for o in setup["options"]}
    check("...asks for region, realm, guild and channel",
          all(k in opts for k in ("region", "realm", "guild", "channel")))
    check("...takes the channel as a real channel picker, not a typed id",
          opts["channel"]["type"] == 7)
    check("...restricts it to channels a webhook can post in",
          opts["channel"]["channel_types"] == [0, 5])
    check("...makes the mention role optional", opts["role"]["required"] is False)

    flat = interactions.command_options(
        {"data": {"options": [{"name": "region", "value": "us"},
                              {"name": "realm", "value": "Proudmoore"}]}})
    check("options flatten to a dict", flat == {"region": "us", "realm": "Proudmoore"})
    check("...and missing options are simply absent",
          interactions.command_options({"data": {}}) == {})
    FAKE_DDB.items.clear()
    config._cache.clear()


# ---------------------------------------------------------------- end to end

def test_end_to_end():
    """Scrambled's real starting position: three cleared tiers, 2 of 8 in the current one.

    The first assertion is the one this whole path exists for. Run one must post nothing --
    not one retroactive kill card, and above all no AOTC for tier-mn-1, which was cleared
    months ago.
    """
    print("\nEnd to end, from Scrambled's real starting position")
    import copy
    from datetime import datetime, timedelta, timezone

    import handler
    config._cache.clear()

    now = datetime.now(timezone.utc)
    ms = lambda days_ago: int((now - timedelta(days=days_ago)).timestamp() * 1000)

    posts = []
    profile = copy.deepcopy(PROFILE)
    window, history = [], []

    def kill(name, days_ago, zone="The Venomous Abyss"):
        return {"encounterID": 3000 + (hash(name) % 900), "name": name, "zoneID": 44,
                "zoneName": zone, "reportCode": "aBcD", "killedAtMs": ms(days_ago)}

    def fake_kills(token, gid, since_ms, limit=12, difficulty=4, max_pages=1):
        deep = since_ms < ms(handler.LOOKBACK_DAYS + 1)
        src = history if deep else window
        return (sorted([k for k in src if k["killedAtMs"] >= since_ms],
                       key=lambda k: k["killedAtMs"]),
                {"limit": 3600, "spent": 10.0, "resetsIn": 60, "fraction": 0.003})

    handler.wcl.get_token = lambda *a, **kw: "tok"
    handler.wcl.find_guild = lambda *a, **kw: ({"id": 777, "name": "Scrambled"},
                                               {"limit": 3600, "spent": 10.0,
                                                "resetsIn": 60, "fraction": 0.003})
    handler.wcl.query = lambda token, doc, variables=None: {
        "rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 10.0,
                          "pointsResetIn": 60}}
    handler.wcl.heroic_kills_since = fake_kills
    handler.raiderio.guild_profile = lambda *a, **kw: profile
    handler.raiderio.static_raids = lambda exp: {11: RAIDS, 10: PREV_RAIDS}.get(exp, [])
    handler.discord.post = lambda hook, payload, **kw: posts.append(payload) or 204

    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)

    # --- run one. Warcraft Logs can only still see the two current-tier kills; the
    # --- tier-mn-1 clear is older than the lookback, exactly as it would be in reality.
    FAKE_DDB.items.clear()
    handler._guild_id["value"] = None
    # History carries last expansion's Manaforge Omega farm alongside the real kills --
    # the exact shape of the live data that broke the first bootstrap.
    history = ([kill(ABYSS[0], 6), kill(ABYSS[1], 5)]
               + [kill(n, 20 + i, zone="Manaforge Omega") for i, n in enumerate(MANAFORGE)])
    window = []
    handler.handler({}, None)

    check("the first run posts NOTHING", posts == [], f"{len(posts)} posts")
    check("it recorded that it bootstrapped", store.is_bootstrapped(pk))
    check("every tier in the profile was seeded",
          all(store.load_tier(pk, s) for s in profile["raid_progression"]),
          [s for s in profile["raid_progression"] if not store.load_tier(pk, s)])

    mn1 = store.load_tier(pk, "tier-mn-1")
    check("tier-mn-1 seeded all nine bosses despite no log history for it",
          len(mn1["announced"]) == 9, len(mn1["announced"]))
    check("tier-mn-1 has its AOTC flag PRE-SET, so no false AOTC is possible",
          mn1["aotcAnnounced"])
    abyss = store.load_tier(pk, "the-venomous-abyss")
    check("the current tier seeded its two kills with AOTC unset",
          len(abyss["announced"]) == 2 and abyss["aotcAnnounced"] is False,
          f"{len(abyss['announced'])} / {abyss['aotcAnnounced']}")
    check("no Manaforge Omega boss leaked into the current tier",
          not (abyss["announced"] & {raiderio.normalize(n) for n in MANAFORGE}),
          sorted(abyss["announced"]))
    check("and its baseline cannot exceed the boss total",
          abyss["baseline"] <= 8, abyss["baseline"])

    # --- run two, unchanged. Must not re-bootstrap and must not announce.
    posts.clear()
    handler.handler({}, None)
    check("the second run neither re-seeds nor announces", posts == [], f"{len(posts)} posts")

    # --- a transmog run through the old, long-cleared tier -------------------
    posts.clear()
    window = [kill(MN1[0], 0.02, zone="MN Tier 1"), kill(MN1[8], 0.01, zone="MN Tier 1")]
    handler.handler({}, None)
    check("farming a cleared tier announces nothing and fires no AOTC",
          posts == [], f"{len(posts)} posts")

    # --- the third boss of the current tier goes down ------------------------
    posts.clear()
    window = [kill(ABYSS[2], 0.01)]
    profile["raid_progression"]["the-venomous-abyss"]["heroic_bosses_killed"] = 2  # lagging
    handler.handler({}, None)
    check("a genuinely new kill is announced exactly once", len(posts) == 1, len(posts))
    check("and it names the right boss",
          posts and posts[0]["embeds"][0]["title"] == f"Scrambled just killed {ABYSS[2]}")
    check("the count comes from the bot, not from lagging Raider.IO",
          posts and "**3** of **8**" in posts[0]["embeds"][0]["description"],
          posts[0]["embeds"][0]["description"] if posts else "")
    check("the realm rank is carried through",
          posts and "Ranked server **#66**" in posts[0]["embeds"][0]["description"])

    posts.clear()
    handler.handler({}, None)
    check("polling again announces nothing", posts == [], f"{len(posts)} posts")

    # --- the rest of the tier falls, ending on the final boss ----------------
    posts.clear()
    window = [kill(n, 0.005) for n in ABYSS[3:]]
    profile["raid_progression"]["the-venomous-abyss"]["heroic_bosses_killed"] = 8
    handler.handler({}, None)
    check("the remaining five bosses each get a card",
          len([p for p in posts if "just killed" in p["embeds"][0]["title"]]) == 5,
          len(posts))
    check("AOTC follows, exactly once",
          len([p for p in posts if "just got AOTC on" in p["embeds"][0]["title"]]) == 1)
    aotc = [p for p in posts if "just got AOTC on" in p["embeds"][0]["title"]][0]
    check("AOTC pings Prog Raiders and nothing else",
          aotc.get("content") == "<@&111111111111111111>"
          and aotc["allowed_mentions"] == {"parse": [],
                                           "roles": ["111111111111111111"]})

    posts.clear()
    window = [kill(ABYSS[7], 0.001)]
    handler.handler({}, None)
    check("re-killing the final boss announces nothing and no second AOTC",
          posts == [], f"{len(posts)} posts")

    # --- next patch: a tier that did not exist at bootstrap ------------------
    posts.clear()
    profile["raid_progression"]["a-brand-new-raid"] = {
        "total_bosses": 8, "heroic_bosses_killed": 1, "normal_bosses_killed": 3,
        "mythic_bosses_killed": 0}
    profile["raid_rankings"]["a-brand-new-raid"] = {"heroic": {"world": 0, "region": 0,
                                                               "realm": 0}}
    window = [{"encounterID": 4001, "name": "A New First Boss", "zoneID": 99,
               "zoneName": "A Brand New Raid", "reportCode": "zZ",
               "killedAtMs": ms(0.001)}]
    history = history + window
    handler.handler({}, None)
    check("a new tier's first kill is announced, not swallowed as history",
          len(posts) == 1, len(posts))
    check("and it reads 1 of 8, not 2 of 8",
          posts and "**1** of **8**" in posts[0]["embeds"][0]["description"],
          posts[0]["embeds"][0]["description"] if posts else "")
    check("an unranked new tier omits the rank line",
          posts and "Ranked server" not in posts[0]["embeds"][0]["description"])

    # --- preview renders a real card and records absolutely nothing ----------
    # A preview that took the ordinary path would claim the boss, and the guild's real
    # first kill would then be correctly, silently and permanently skipped -- the bot's
    # entire purpose defeated by the demo of it.
    posts.clear()
    import copy as _copy
    before = _copy.deepcopy(FAKE_DDB.items)

    res = handler.handler({"preview": {"dry": True, "slug": "the-venomous-abyss"}}, None)
    check("a dry preview posts nothing at all", posts == [], f"{len(posts)} posts")
    check("a dry preview still renders a card",
          "payload" in res and "just killed" in res["payload"]["embeds"][0]["title"])
    check("a dry preview writes no state", FAKE_DDB.items == before)

    res = handler.handler({"preview": {"slug": "the-venomous-abyss"}}, None)
    check("a live preview posts exactly one card", len(posts) == 1, len(posts))
    check("the preview card reads as a first kill",
          posts and "**1** of **8**" in posts[0]["embeds"][0]["description"],
          posts[0]["embeds"][0]["description"] if posts else "")
    check("the preview is dated by the REAL kill, not by now",
          posts and posts[0]["embeds"][0]["timestamp"] == res["killedAt"])
    check("a live preview writes NO dedupe state whatsoever",
          FAKE_DDB.items == before,
          "state changed — a preview would silence the real first kill")

    check("previewing picks the tier's EARLIEST kill",
          res["boss"] == ABYSS[0], res["boss"])
    check("the preview never fires AOTC", not any(
        "AOTC" in p["embeds"][0]["title"] for p in posts))


def test_team_install():
    """A second raid team in the same Discord: Meer's Raid.

    Its own channel, its own first kills on Normal AND Heroic, its own clear cards, fed
    by one raider's personal Warcraft Logs uploads on the team's raid days only -- and
    the guild install carries on exactly as before, never seeing a team query.
    """
    print("\nA raid team as its own install")
    import copy
    from datetime import datetime, timedelta, timezone
    import handler
    config._cache.clear()

    # --- keys -------------------------------------------------------------
    t = keys.team_pk(TEST_TENANT, "meers-raid")
    check("a team key extends the server's tenant key",
          t == f"TENANT#{TEST_TENANT}#meers-raid", t)
    for bad in ("Meer's", "meers raid", "", "a#b", "x" * 33):
        try:
            keys.team_pk(TEST_TENANT, bad)
            check(f"rejects team slug {bad!r}", False, "was accepted")
        except ValueError:
            check(f"rejects team slug {bad!r}", True)
    guild_scope = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    team_scope = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT,
                                  team="meers-raid")
    check("a team shares NOTHING with the guild install: not the facts partition",
          team_scope.wow != guild_scope.wow, team_scope.wow)
    check("...and not the tenant partition", team_scope.tenant != guild_scope.tenant)
    check("Heroic's announced row is unchanged",
          keys.announced_sk("abyss") == "ANNOUNCED#abyss")
    check("Normal gets its own announced row",
          keys.announced_sk("abyss", keys.NORMAL) == "ANNOUNCED#abyss#normal")
    check("guild Heroic cards keep their bare path", handler.card_prefix() == "")
    check("a team's Normal cards get their own directory",
          handler.card_prefix("meers-raid", "normal") == "meers-raid/normal/")

    # --- config -----------------------------------------------------------
    FAKE_DDB.items.clear()
    handler._guild_id["value"] = None
    G, T = f"TENANT#{TEST_TENANT}", t
    TEAM_CHANNEL, TEAM_ROLE = "555000000000000009", "222222222222222222"
    store.put_config(G, "us", "Proudmoore", "Scrambled", "", "now")
    store.register_tenant(G)
    store.put_config(T, "us", "Proudmoore", "Scrambled", TEAM_CHANNEL, "now",
                     prog_role_id=TEAM_ROLE, configured_by="register-team",
                     team={"slug": "meers-raid", "name": "Meer's Raid",
                           "wcl_user_id": 40245, "raid_days": "",
                           "difficulties": "normal,heroic"})
    store.register_tenant(T)
    row = store.get_config(T)
    check("the team's config round-trips", row["team_slug"] == "meers-raid"
          and row["wcl_user_id"] == "40245" and row["team_name"] == "Meer's Raid", row)
    check("...and the guild's row has no team", store.get_config(G)["team_slug"] == "")

    ssm = config.load()
    # Prod holds a bot token; the offline fixture does not. Channel posting needs one.
    ssm["bot_token"] = "bot-tok"
    pairs = {s.tenant: (s, c) for s, c in handler.tenant_configs(ssm)}
    check("both installs are polled", set(pairs) == {G, T}, sorted(pairs))
    tscope, tcfg = pairs[T]
    gscope, gcfg = pairs[G]
    check("the team's scope is team-scoped", tscope.team == "meers-raid" and
          tscope.wow.endswith("#meers-raid"), tscope)
    check("the team announces Normal then Heroic",
          handler.difficulties(tcfg) == ["normal", "heroic"])
    check("the guild announces Heroic only, as ever",
          handler.difficulties(gcfg) == ["heroic"])
    check("the team's clear card pings ITS role, not the operator's",
          tcfg["role_id"] == TEAM_ROLE and gcfg["role_id"] == "111111111111111111")
    check("the team posts to its channel with the bot token",
          handler.destination(tcfg) == {"bot_token": "bot-tok", "channel": TEAM_CHANNEL})
    try:
        handler.destination(dict(tcfg, bot_token=""))
        check("a channel install with no bot token REFUSES rather than using the webhook",
              False, "fell through to the webhook")
    except handler.discord.DiscordError:
        check("a channel install with no bot token REFUSES rather than using the webhook",
              True)
    check("the team credits its own name", handler.display_name(tcfg) == "Meer's Raid"
          and handler.display_name(gcfg) == "Scrambled")

    # --- the raid-day filter ---------------------------------------------
    now = datetime.now(timezone.utc)
    ms = lambda days_ago: int((now - timedelta(days=days_ago)).timestamp() * 1000)
    day_name = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    raid_day = day_name[handler._local(handler._at(ms(0.5))).weekday()]
    off_day = day_name[handler._local(handler._at(ms(1.5))).weekday()]
    check("the fixture's two nights fall on different weekdays", raid_day != off_day)
    tcfg["raid_days"] = raid_day
    store.put_config(T, "us", "Proudmoore", "Scrambled", TEAM_CHANNEL, "now",
                     prog_role_id=TEAM_ROLE,
                     team={"slug": "meers-raid", "name": "Meer's Raid",
                           "wcl_user_id": 40245, "raid_days": raid_day,
                           "difficulties": "normal,heroic"})

    def kill(name, days_ago, report="rEpOrT"):
        return {"encounterID": 3000 + (hash(name) % 900), "name": name, "zoneID": 44,
                "zoneName": "The Venomous Abyss", "reportCode": report,
                "reportStartMs": ms(days_ago), "killedAtMs": ms(days_ago) + 60000}

    on = handler.on_raid_days([kill("a", 0.5), kill("b", 1.5)], tcfg)
    check("a kill from a report filed off the raid day is dropped",
          [k["name"] for k in on] == ["a"], on)
    check("no raid days means no filter",
          len(handler.on_raid_days([kill("a", 0.5), kill("b", 1.5)], gcfg)) == 2)

    # --- the two sources -------------------------------------------------
    # Every seven days lands on the same weekday, so history on 7.5 and 14.5 days ago is
    # on the raid day, and the Saturday-shaped kill on 1.5 days ago is not.
    profile = copy.deepcopy(PROFILE)
    calls = []
    sources = {("guild", 4): [], ("user", 3): [], ("user", 4): []}

    def fake_kills(token, gid, since_ms, limit=12, difficulty=4, max_pages=1,
                   user_id=None):
        calls.append({"user_id": user_id, "difficulty": difficulty})
        src = sources[("user" if user_id else "guild", difficulty)]
        return (sorted([k for k in src if k["killedAtMs"] >= since_ms],
                       key=lambda k: k["killedAtMs"]),
                {"limit": 3600, "spent": 10.0, "resetsIn": 60, "fraction": 0.003})

    posts = []
    real_post_to, real_participants = handler.discord.post_to, handler.wcl.kill_participants
    handler.wcl.get_token = lambda *a, **kw: "tok"
    handler.wcl.find_guild = lambda *a, **kw: ({"id": 777, "name": "Scrambled"}, None)
    handler.wcl.query = lambda token, doc, variables=None: {
        "rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 10.0,
                          "pointsResetIn": 60}}
    handler.wcl.heroic_kills_since = fake_kills
    handler.wcl.reports_in_window = lambda *a, **kw: ([{"code": "x"}], None)
    handler.wcl.kill_participants = lambda *a, **kw: ([{"name": "Meerclar",
                                                        "server": "Proudmoore"}], None)
    handler.raiderio.guild_profile = lambda *a, **kw: profile
    handler.raiderio.static_raids = lambda exp: {11: RAIDS, 10: PREV_RAIDS}.get(exp, [])
    handler.discord.post_to = lambda dest, payload, **kw: posts.append((dest, payload))

    # Run one: bootstrap. The team's Normal history holds three kills on raid days and
    # one on the off day; its Heroic history is empty.
    sources[("guild", 4)] = [kill(ABYSS[0], 7.5), kill(ABYSS[1], 7.5)]
    sources[("user", 3)] = [kill(ABYSS[0], 14.5), kill(ABYSS[1], 14.5), kill(ABYSS[2], 7.5),
                            kill(ABYSS[3], 1.5)]
    handler.handler({}, None)
    check("the first run posts nothing for either install", posts == [], len(posts))
    check("both installs bootstrapped",
          store.is_bootstrapped(gscope) and store.is_bootstrapped(tscope))
    check("the guild's queries never carry a user id",
          all(c["user_id"] is None for c in calls if c["difficulty"] == 4
              and c is calls[0]) and calls[0]["user_id"] is None, calls[:2])
    check("the team's queries ask for its uploader at both difficulties",
          {(c["user_id"], c["difficulty"]) for c in calls if c["user_id"]}
          == {(40245, 3), (40245, 4)}, calls)

    tn = store.load_tier(tscope, "the-venomous-abyss", keys.NORMAL)
    th = store.load_tier(tscope, "the-venomous-abyss", keys.HEROIC)
    gh = store.load_tier(gscope, "the-venomous-abyss")
    check("the team's Normal seed is its raid-day history, NOT Raider.IO's 8/8",
          len(tn["announced"]) == 3 and tn["baseline"] == 3 and not tn["aotcAnnounced"],
          tn)
    check("...the off-day kill was not seeded",
          raiderio.normalize(ABYSS[3]) not in tn["announced"])
    check("the team's Heroic seed is empty, not the guild's 2/8",
          th["announced"] == set() and th["baseline"] == 0, th)
    check("the guild's Heroic seed is untouched by the team",
          len(gh["announced"]) == 2 and gh["baseline"] == 2, gh)
    check("the team's cleared world boss is pre-set, so no clear card fires for it",
          store.load_tier(tscope, "the-tidebound-grotto", keys.NORMAL)["announced"]
          == set())

    # Run two: the team kills its fourth Normal boss on a raid night.
    posts.clear()
    sources[("user", 3)].append(kill(ABYSS[3], 0.5))
    handler.handler({}, None)
    check("the team's new Normal first kill is announced once", len(posts) == 1,
          len(posts))
    dest, payload = posts[0] if posts else ({}, {"embeds": [{}]})
    embed = payload["embeds"][0]
    check("...to the team's channel", dest.get("channel") == TEAM_CHANNEL, dest)
    check("...credited to the team", embed.get("title") ==
          f"Meer's Raid just killed {ABYSS[3]}", embed.get("title"))
    check("...as a Normal kill, counted from the team's own claims",
          "**4** of **8** in Normal The Venomous Abyss" in embed.get("description", ""),
          embed.get("description"))
    check("...with no realm rank, which is the guild's and not the team's",
          "Ranked server" not in embed.get("description", ""))

    posts.clear()
    handler.handler({}, None)
    check("polling again announces nothing", posts == [], len(posts))

    # The guild kills the same boss on Heroic: the guild's card, the guild's channel,
    # and the team's Heroic row does not move.
    posts.clear()
    sources[("guild", 4)].append(kill(ABYSS[3], 0.4))
    handler.handler({}, None)
    check("the guild's Heroic kill goes to the guild's destination only",
          len(posts) == 1 and "webhook" in posts[0][0], [d for d, _ in posts])
    check("...and still reads as Scrambled on Heroic",
          posts and posts[0][1]["embeds"][0]["title"].startswith("Scrambled just killed")
          and "in Heroic" in posts[0][1]["embeds"][0]["description"])
    check("the team's Heroic row is still empty",
          store.load_tier(tscope, "the-venomous-abyss", keys.HEROIC)["announced"] == set())

    # The team's first Heroic kill: a Heroic card, in the team's channel, 1 of 8.
    posts.clear()
    sources[("user", 4)].append(kill(ABYSS[0], 0.3))
    handler.handler({}, None)
    check("the team's first Heroic kill is its own event", len(posts) == 1 and
          posts[0][0].get("channel") == TEAM_CHANNEL and
          "**1** of **8** in Heroic" in posts[0][1]["embeds"][0]["description"],
          [p[1]["embeds"][0].get("description") for p in posts])

    # The team finishes Normal: four more bosses, then the silver clear card.
    posts.clear()
    sources[("user", 3)].extend(kill(n, 0.2) for n in ABYSS[4:])
    handler.handler({}, None)
    kills_posted = [p for d, p in posts if "just killed" in p["embeds"][0].get("title", "")]
    clears = [p for d, p in posts if "just cleared" in p["embeds"][0].get("title", "")]
    check("the remaining four Normal bosses each get a card", len(kills_posted) == 4,
          len(kills_posted))
    check("then ONE Normal-clear card", len(clears) == 1, len(clears))
    clear = clears[0] if clears else {"embeds": [{}]}
    check("...titled as a Normal clear",
          "just cleared Normal The Venomous Abyss on" in clear["embeds"][0].get("title", ""),
          clear["embeds"][0].get("title"))
    check("...in silver, not AOTC gold",
          clear["embeds"][0].get("color") == handler.discord.NORMAL_SILVER)
    check("...pinging the team's role", clear.get("content") == f"<@&{TEAM_ROLE}>"
          and clear["allowed_mentions"]["roles"] == [TEAM_ROLE], clear.get("content"))
    check("no AOTC was fired by a Normal clear",
          not any("AOTC" in p["embeds"][0].get("title", "") for _, p in posts))

    posts.clear()
    sources[("user", 3)].append(kill(ABYSS[7], 0.1))
    handler.handler({}, None)
    check("re-killing the last Normal boss announces nothing and no second clear",
          posts == [], len(posts))

    # A Saturday-shaped night: the same uploader, the off day. Nothing.
    posts.clear()
    sources[("user", 4)].append(kill(ABYSS[1], 1.5))
    handler.handler({}, None)
    check("an off-day kill from the same uploader is not the team's", posts == [],
          len(posts))

    # --- aiming a manual run at one team ----------------------------------
    polled = []
    real_poll = handler.poll_one
    handler.poll_one = lambda ev, c, s, n, ni, st: polled.append(s.team) or {"ok": True}
    try:
        handler.handler({"team": "meers-raid"}, None)
        check("an event naming a team polls that team alone", polled == ["meers-raid"],
              polled)
        polled.clear()
        handler.handler({}, None)
        check("...and a scheduled event still fans out to everyone",
              sorted(polled, key=str) == [None, "meers-raid"], polled)
    finally:
        handler.poll_one = real_poll

    # --- backfilling the seeded firsts ------------------------------------
    # The three Normal bosses the bootstrap seeded get their cards after the fact, oldest
    # first, dated by the real kill, and the announced set does not move.
    posts.clear()
    before = copy.deepcopy(FAKE_DDB.items)
    res = handler.handler({"admin": "backfill", "team": "meers-raid",
                           "difficulty": "normal", "slugs": ["the-venomous-abyss"],
                           "dry": True}, None)
    check("a dry backfill posts nothing", posts == [], len(posts))
    # By now the team's announced set holds all eight; the off-day kill of ABYSS[3] is
    # not its first, the on-day one half a day ago is.
    check("...and plans a card per announced first kill, oldest first",
          [c["boss"] for c in res["cards"]] == list(ABYSS),
          [c["boss"] for c in res.get("cards", [])])
    check("...numbered by their place in the tier",
          [c["count"] for c in res["cards"]] == list(range(1, 9)))
    check("...dating the off-day boss by its RAID-DAY kill, not the Saturday one",
          res["cards"][3]["killedAt"] == handler._iso(handler._at(ms(0.5) + 60000)),
          res["cards"][3]["killedAt"])
    check("...each carrying the kill's own date on the card",
          all(c["localTime"] in c["payload"]["embeds"][0]["description"]
              for c in res["cards"]))
    check("...and dated by the kill, not by now",
          res["cards"][0]["payload"]["embeds"][0]["timestamp"] == res["cards"][0]["killedAt"])
    res = handler.handler({"admin": "backfill", "team": "meers-raid",
                           "difficulty": "normal", "slugs": ["the-venomous-abyss"]}, None)
    check("a live backfill posts the eight cards to the team's channel",
          len(posts) == 8 and all(d.get("channel") == TEAM_CHANNEL for d, _ in posts),
          [d for d, _ in posts])
    check("...reading 1 through 8 of 8 in Normal",
          ["**%d** of **8** in Normal" % i in posts[i - 1][1]["embeds"][0]["description"]
           for i in range(1, 9)] == [True] * 8)
    skip = lambda items: {k: v for k, v in items.items()
                          if k[1] != "POSTS" and not k[1].startswith("KILL#")}
    check("...and touches no announced state", skip(FAKE_DDB.items) == skip(before))
    check("...but records each first kill's roster, as a live announcement would",
          store.get_first_kill(tscope, "the-venomous-abyss", raiderio.normalize(ABYSS[2]))
          ["players"] == {"meerclar@proudmoore"})
    posts.clear()
    handler.handler({"admin": "backfill", "team": "meers-raid", "difficulty": "normal",
                     "slugs": ["the-venomous-abyss"], "post": False}, None)
    check("post:false records rosters and posts nothing", posts == [], len(posts))

    handler.discord.post_to = real_post_to
    handler.wcl.kill_participants = real_participants
    ssm.pop("bot_token", None)
    FAKE_DDB.items.clear()
    handler._guild_id["value"] = None


# ------------------------------------------------- prog team identification
#
# Twenty A-team raiders, sixteen B-team raiders, and four people who genuinely raid on
# both. That overlap is the entire reason the classifier needs a margin rather than a
# majority, so it is in the fixture rather than assumed away.

A_TEAM = [f"a{i:02d}" for i in range(1, 21)]
B_ONLY = [f"b{i:02d}" for i in range(1, 17)]
CROSS = A_TEAM[:4]                       # raid on both teams, legitimately
B_TEAM = B_ONLY + CROSS

# Four first kills. "fill01" stood in for one absent raider on exactly one night, which
# is the case the frequency rule exists to reject.
FIRST_KILLS = [
    list(A_TEAM),
    A_TEAM[:19] + ["fill01"],
    list(A_TEAM),
    list(A_TEAM),
]

ALREADY_DEAD = {raiderio.normalize(n) for n in ABYSS[:2]}


def fights(kills=(), wipes=()):
    out = []
    for i, name in enumerate(kills):
        out.append({"id": i, "name": name, "kill": True, "difficulty": wcl.HEROIC,
                    "encounterID": 3000 + i})
    for i, name in enumerate(wipes):
        out.append({"id": 100 + i, "name": name, "kill": False, "difficulty": wcl.HEROIC,
                    "encounterID": 3000 + ABYSS.index(name)})
    return out


FARM_NIGHT = fights(kills=ABYSS[:2])                 # only bosses that were already dead
PROG_NIGHT = fights(kills=ABYSS[:2], wipes=[ABYSS[2]])   # pushing one that was not


def test_roster_derivation():
    print("\nDeriving the prog roster from first kills")
    r = team.derive_roster(FIRST_KILLS, min_pct=50)
    check("every regular raider is on the roster",
          {team.player_key(n) for n in A_TEAM} <= r["roster"],
          sorted(r["roster"]))
    check("a one-off fill-in is not",
          team.player_key("fill01") not in r["roster"], sorted(r["roster"]))
    check("the sample size is carried for the log line", r["sample"] == 4, r["sample"])
    check("four kills is enough to discriminate", r["provisional"] is False)

    # The arithmetic that MIN_FIRST_KILLS_FOR_ROSTER exists for: at a 50% threshold a
    # one-night fill-in clears the bar on any sample smaller than three.
    thin = team.derive_roster(FIRST_KILLS[:2], min_pct=50)
    check("two kills admits the fill-in", team.player_key("fill01") in thin["roster"])
    check("...and is therefore flagged provisional", thin["provisional"] is True)
    check("no kills at all yields an empty provisional roster",
          team.derive_roster([], 50) == {"roster": set(), "sample": 0, "counts": {},
                                         "provisional": True, "minPct": 50.0})

    named = team.derive_roster([[{"name": "Belo'ren", "server": "Proudmoore"}],
                                [{"name": "Beloren", "server": "proudmoore"}]], 50)
    check("punctuation and case fold to one player", len(named["roster"]) == 1,
          named["roster"])
    same = team.derive_roster([[{"name": "Shadowstep", "server": "Proudmoore"}],
                               [{"name": "Shadowstep", "server": "Illidan"}]], 50)
    check("the same name on two realms is two players", len(same["counts"]) == 2,
          same["counts"])


def test_team_resolution():
    print("\nClassifying a report as prog, other, or neither")
    roster = team.derive_roster(FIRST_KILLS, 50)["roster"]
    HIGH, LOW = 70, 35

    def verdict(raiders, fs, r=roster, dead=ALREADY_DEAD):
        v, why = team.resolve_team({team.player_key(n) for n in raiders}, fs, r, dead,
                                   HIGH, LOW, difficulty=wcl.HEROIC)
        return v, why

    v, why = verdict(A_TEAM[:18] + ["pug01", "pug02"], PROG_NIGHT)
    check("a clean A-team report is PROG", v == team.PROG, why)

    v, why = verdict(B_TEAM, FARM_NIGHT)
    check("a clean B-team report is OTHER", v == team.OTHER, why)
    check("...decided by roster overlap alone", why["signalB"] == "inconclusive", why)

    # The named test case: B team on a farm night wipes on bosses that are already dead,
    # which is not progression and must not read as any.
    v, why = verdict(B_TEAM, fights(kills=[ABYSS[0]], wipes=[ABYSS[1]]))
    check("B team wiping only on already-killed bosses is still OTHER",
          v == team.OTHER, why)

    v, why = verdict(A_TEAM[:10] + B_ONLY[:10], FARM_NIGHT)
    check("a heavily cross-team night is UNKNOWN", v == team.UNKNOWN, why)
    check("...because neither signal was conclusive",
          why["why"] == "neither signal was conclusive", why)

    v, why = verdict(B_TEAM, PROG_NIGHT)
    check("low overlap but real progression is UNKNOWN, not a coin flip",
          v == team.UNKNOWN, why)
    check("...and says so as a disagreement", why["why"] == "signals disagree", why)

    # Cold start: nothing derived yet, nothing to seed from.
    v, why = verdict(A_TEAM, PROG_NIGHT, r=set())
    check("with no roster at all, progression alone decides", v == team.PROG, why)
    check("...explicitly by signal B", why["why"] == "decided by signal B", why)
    v, why = verdict(A_TEAM, FARM_NIGHT, r=set())
    check("with no roster and no progression, nothing is posted", v == team.UNKNOWN, why)

    # An empty roster must not read as zero overlap, which would be a confident OTHER.
    check("an empty roster makes overlap unanswerable, not zero",
          team.roster_overlap({"a01"}, set()) is None)

    v, why = verdict(A_TEAM[:4], PROG_NIGHT)
    check("a four-person log is too small for a percentage",
          why["roster"]["overlap"] is None, why["roster"])

    # A brand-new tier: nothing is dead yet, so every encounter is progression. This is
    # the accepted edge case from the README -- signal B cannot tell the teams apart on
    # opening bosses, and says PROG for whoever raided first.
    v, why = verdict(A_TEAM, fights(kills=[ABYSS[0]]), dead=set())
    check("at a fresh tier the first kill reads as progression", v == team.PROG, why)


def test_kill_card_image():
    print("\nThe first-kill card, drawn")
    import handler
    import kill_card

    real_load, kill_card._load = kill_card._load, lambda url, timeout=8: (_ for _ in ()).throw(
        RuntimeError("offline"))
    try:
        # Art that cannot be fetched must not cost the card. The layout falls back to
        # text from the left margin rather than returning None and losing the drawing.
        png = kill_card.render("Nek'zali the Soulcoiler", "Scrambled just killed",
                               ["They are now 5 of 8"], art_url="https://example/x.jpg")
        check("unreachable art still produces a card", png and png[:8] == b"\x89PNG\r\n\x1a\n",
              type(png))
    finally:
        kill_card._load = real_load

    png = kill_card.render("Boss", "Guild just killed", ["line"], art_url=None)
    check("a card with no art at all is still a card", bool(png))

    # AOTC is the same drawing in gold. Same size, same layout, one colour different --
    # so the two can never drift apart, and the gold is checked on the pixels themselves.
    from PIL import Image as _Image
    import io as _io
    gold = kill_card.render("Ahead of the Curve", "Scrambled just got",
                            ["Heroic The Venomous Abyss", "when"], accent=kill_card.GOLD)
    img = _Image.open(_io.BytesIO(gold)).convert("RGB")
    check("an AOTC card is the same size as a kill card",
          img.size == (kill_card.WIDTH, kill_card.HEIGHT), img.size)
    check("...and its headline is actually painted gold",
          kill_card.GOLD in set(img.getdata()) and kill_card.ACCENT not in set(img.getdata()))
    check("the accent is a parameter, and the default is still the kill-card blue",
          kill_card.ACCENT in set(_Image.open(_io.BytesIO(png)).convert("RGB").getdata()))

    # The boss is already CLAIMED when the card is drawn, so a bug in here loses the
    # announcement outright rather than just the picture. It happened once, live.
    check("a broken card returns None instead of taking the announcement down",
          handler.kill_card_url({"recap_page_url": "https://x", "recap_page_bucket": "b"},
                                "slug", object(), "Boss", "head", ["body"], None) is None)

    # The failure that matters: no Pillow in the package. It must return None so the
    # caller falls back to the ordinary embed, NOT raise and take the announcement down.
    import builtins
    real_import = builtins.__import__

    def no_pil(name, *a, **kw):
        if name.startswith("PIL"):
            raise ImportError("no PIL")
        return real_import(name, *a, **kw)

    builtins.__import__ = no_pil
    try:
        check("without Pillow it returns None rather than raising",
              kill_card.render("Boss", "x", ["y"]) is None)
    finally:
        builtins.__import__ = real_import

    # Long names shrink rather than truncate -- the boss name is the one string on the
    # card a reader is actually looking for.
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    text, font = kill_card._fit("Nek'zali the Soulcoiler", os.path.join(kill_card.FONT_DIR, "DejaVuSans-Bold.ttf"),
                                d, 626, 54, 32)
    check("a long boss name shrinks and keeps every character",
          text == "Nek'zali the Soulcoiler" and font.size < 54, (text, font.size))
    check("...and a short one keeps the full size",
          kill_card._fit("Sszorak", os.path.join(kill_card.FONT_DIR, "DejaVuSans-Bold.ttf"),
                         d, 626, 54, 32)[1].size == 54)


def test_deleted_post_probe():
    print("\nNoticing that one of our posts was deleted")
    calls = []

    def fake_get(url, token=None, timeout=8):
        calls.append(url)
        return FAKE_HTTP.get(url, (200, {"id": "1"}))

    real_get, health._get = health._get, fake_get
    try:
        FAKE_HTTP.clear()
        posts = [{"id": "111", "channel": "C1", "kind": "kill", "at": "t1"},
                 {"id": "222", "channel": "C1", "kind": "recap", "at": "t2"}]

        p = health.posts_probe("tok", posts)
        check("every tracked post still present reads ok", p["verdict"] == health.OK, p)
        check("...and the NEWEST is checked first",
              calls and calls[0].endswith("/messages/222"), calls[:1])

        FAKE_HTTP["https://discord.com/api/v10/channels/C1/messages/222"] = (
            404, {"code": 10008, "message": "Unknown Message"})
        p = health.posts_probe("tok", posts)
        check("a 404 on a tracked post is a deletion",
              p["verdict"] == health.POST_DELETED, p)
        check("...and it says WHICH post, so the alert is actionable",
              p.get("messageId") == "222" and p.get("kind") == "recap", p)

        # 403 is losing read access, which is a different problem with a different fix.
        # Calling it a deletion sends somebody hunting for a message that is still there.
        FAKE_HTTP["https://discord.com/api/v10/channels/C1/messages/222"] = (
            403, {"code": 50001, "message": "Missing Access"})
        p = health.posts_probe("tok", posts)
        check("losing read access is UNKNOWN, not a deletion",
              p["verdict"] == health.UNKNOWN, p)

        # The bug this replaced: a read-then-write lost a record the first time two
        # writers overlapped, and the lost post's deletion could then never be noticed.
        FAKE_DDB.items.clear()
        pk2 = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
        store.record_post(pk2, "A", "C1", "kill", "t1")
        store.record_post(pk2, "B", "C1", "recap", "t2")
        got = [p["id"] for p in store.recent_posts(pk2)]
        check("two posts recorded back to back both survive", got == ["A", "B"], got)
        store.forget_post(pk2, "A", "t3")
        check("...and forgetting one keeps the other",
              [p["id"] for p in store.recent_posts(pk2)] == ["B"])

        check("no tracked posts is not a failure",
              health.posts_probe("tok", [])["verdict"] == health.OK)
        check("a post with no channel id is skipped rather than alarmed about",
              health.posts_probe("tok", [{"id": "9"}])["verdict"] == health.OK)

        # It has to outrank nothing that is worse, and it has to be IN the ordering at
        # all -- a verdict missing from SEVERITY raises when check() picks the worst.
        check("post_deleted is ranked below the outages that stop announcements",
              health.SEVERITY.index(health.POST_DELETED)
              > health.SEVERITY.index(health.WEBHOOK_GONE))
        # The alert has to say the bot will not put it back, because the natural
        # assumption is that a self-healing bot would.
        check("...and the advice says the bot will NOT repost it",
              "not repost" in health.ADVICE[health.POST_DELETED].lower(),
              health.ADVICE[health.POST_DELETED][:80])
        check("...and it has a headline naming the server",
              "{guild}" in health.HEADLINE[health.POST_DELETED],
              health.HEADLINE[health.POST_DELETED])
    finally:
        health._get = real_get


FAKE_HTTP = {}


def test_roster_state():
    print("\nRoster state, seeding and the recap claim")
    FAKE_DDB.items.clear()
    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    slug = "the-venomous-abyss"

    for i, name in enumerate(ABYSS[:4]):
        store.record_first_kill(pk, slug, raiderio.normalize(name),
                                {team.player_key(p) for p in FIRST_KILLS[i]},
                                1_700_000_000_000 + i, f"REPORT{i}", "now")
    boss_keys = {raiderio.normalize(n) for n in ABYSS[:5]}  # the 5th was seeded, not killed
    recorded = store.first_kills(pk, slug, boss_keys)
    check("four first kills read back", len(recorded) == 4, sorted(recorded))
    check("a seeded boss with no participants is simply absent",
          raiderio.normalize(ABYSS[4]) not in recorded)

    derived = team.derive_roster([sorted(v["players"]) for v in recorded.values()], 50)
    check("the roster survives a round trip through DynamoDB",
          derived["roster"] == team.derive_roster(FIRST_KILLS, 50)["roster"])

    # The same round trip WITH SERVERS, which is the only shape production ever writes --
    # handler.record_roster keys on name and server both. The check above passed
    # throughout the bug because its fixture names carry no server, so re-keying them was
    # a no-op; re-keying "a01@proudmoore" is not, and the roster it produced overlapped
    # the live raiders by exactly zero, which is what silenced the 2026-09-02 recap.
    FAKE_DDB.items.clear()
    cross_realm = [[{"name": n, "server": "Proudmoore"} for n in kill]
                   for kill in FIRST_KILLS]
    live = team.player_keys(cross_realm[0])
    for i, kill in enumerate(cross_realm):
        store.record_first_kill(pk, slug, raiderio.normalize(ABYSS[i]),
                                team.player_keys(kill),
                                1_700_000_000_000 + i, f"REPORT{i}", "now")
    read_back = store.first_kills(pk, slug, {raiderio.normalize(n) for n in ABYSS[:4]})
    with_server = team.derive_roster([sorted(v["players"]) for v in read_back.values()],
                                     50)["roster"]
    check("a stored key is not re-keyed on the way back out",
          all("@" in k for k in with_server), sorted(with_server)[:3])
    check("...so the derived roster still matches the raiders it came from",
          with_server == {k for k in live if not k.startswith("fill")},
          sorted(with_server - live))

    FAKE_DDB.items.clear()
    for i, name in enumerate(ABYSS[:4]):
        store.record_first_kill(pk, slug, raiderio.normalize(name),
                                {team.player_key(p) for p in FIRST_KILLS[i]},
                                1_700_000_000_000 + i, f"REPORT{i}", "now")

    check("no participants means no record written",
          store.record_first_kill(pk, slug, "nobody", set(), 1, "R", "now") is False)

    store.save_derived_roster(pk, slug, derived["roster"], derived["sample"],
                              derived["provisional"], "now")
    saved = store.load_roster(pk, slug)
    check("the derived roster persists", saved["derived"] == derived["roster"])
    check("...with the sample it came from", saved["sample"] == 4, saved["sample"])

    # Tier rollover: week one has no first kills, so the previous tier's roster stands in.
    nxt = "the-next-tier"
    check("a new tier seeds from the last one", store.seed_roster(pk, nxt, saved["derived"],
                                                                  slug, "now"))
    check("and only once", store.seed_roster(pk, nxt, {"someone-else"}, slug, "now") is False)
    seeded = store.load_roster(pk, nxt)
    check("the seed reads back intact", seeded["seed"] == saved["derived"])
    check("...labelled with where it came from", seeded["seedFrom"] == slug)
    check("a seeded tier has derived nothing of its own", seeded["derived"] == set())

    # Seeding must still work when the new tier's roster item already exists, which is
    # what happens whenever a first kill lands before the first recap of the tier.
    third = "the-tier-after"
    store.record_first_kill(pk, third, "someboss", {"a01"}, 1, "R", "now")
    store.save_derived_roster(pk, third, {"a01"}, 1, True, "now")
    check("an existing roster item can still be seeded",
          store.seed_roster(pk, third, saved["derived"], slug, "now"))

    check("a night can be claimed once", store.claim_recap(pk, "2026-08-29"))
    check("an overlapping run does not double-post", store.claim_recap(pk, "2026-08-29") is False)
    check("a different night is unaffected", store.claim_recap(pk, "2026-09-05"))
    store.release_recap(pk, "2026-08-29")
    check("a failed webhook lets the night retry", store.claim_recap(pk, "2026-08-29"))



def test_iam_grant_covers_config():
    """Every parameter config.py asks for must be in the execution role's policy.

    This is not tidiness. GetParameters denies the ENTIRE call if the caller lacks
    permission on any single name in it, and config.py fetches the optional names in
    chunks of ten -- so one un-granted parameter silently pins every other name sharing
    its chunk to a default. The symptom is a value you can see in the console that the bot
    is demonstrably not using, which is a genuinely hard thing to debug.

    It has happened once already: the Discord interaction parameters were added before the
    role was widened, and the announcer stopped for want of a slash command.
    """
    print("\nThe role policy covers everything config.py reads")
    import re
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    # Read from the CDK stage config, which is what actually builds the role today.
    # This used to read infra/grant-alerts.sh, and that script has not owned the policy
    # since the Phase 1 cutover -- so the gate was green against an artifact nothing
    # deploys. A check pointed at a stale source is not a weaker check, it is a check
    # that reports success for a policy the account has never seen.
    grant = open(os.path.join(root, "cdk", "greybot", "config.py"),
                 encoding="utf-8").read()
    src = open(os.path.join(root, "src", "config.py"), encoding="utf-8").read()

    wanted = set(re.findall(r'f"\{PREFIX\}(/[a-z_/]+)"', src))
    leaves = grant.split("ssm_leaves")[1] if "ssm_leaves" in grant else ""
    granted = {"/" + m for m in re.findall(r'"([a-z_]+(?:/[a-z_]+)+)",', leaves)}
    check("config.py asks for a plausible number of parameters", len(wanted) >= 19,
          len(wanted))
    check("the role grants every one of them", wanted <= granted,
          f"NOT granted: {sorted(wanted - granted)}")
    check("and grants nothing it does not read", granted <= wanted,
          f"granted but unused: {sorted(granted - wanted)}")

    # The chunk boundary is the reason this matters, so assert it is still a real hazard
    # rather than a theoretical one.
    names = re.search(r"OPTIONAL_NAMES = \[(.*?)\]", src, re.S).group(1)
    optional = [n.strip() for n in names.replace("\n", " ").split(",") if n.strip()]
    check("the optional list still spans more than one GetParameters call",
          len(optional) > 10, len(optional))


# ------------------------------------------------- discord standing / alerts

def test_health():
    """The probes, and the rule that turns ninety-six polls a day into one email.

    Two failure modes are being defended against here, and they pull in opposite
    directions. One is a bot that gets kicked on a Tuesday and nobody finds out until the
    guild asks why the last three kills went unannounced. The other is a bot that mails
    every fifteen minutes because Discord had a bad afternoon. The transition rule is what
    satisfies both, and it only works if an indefinite answer is treated as no answer at
    all -- which is the assertion that matters most in this block.
    """
    print("\nDiscord standing: probes, and one email per event")
    from datetime import datetime, timedelta, timezone

    import handler

    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    WEBHOOK = "https://discord.com/api/webhooks/1/tok"
    GID = "999"
    cfg = {"webhook": WEBHOOK, "bot_token": "t", "discord_guild_id": GID,
           "guild_name": "Scrambled",
           "alert_topic_arn": "arn:aws:sns:us-east-1:0:ryangrey-dev-alerts"}

    real_get = health._get

    def responder(**scripted):
        """Each probe's URL mapped to a scripted (status, body). Everything unmentioned
        answers healthily, so each case states only the thing it is testing."""
        def _get(url, token=None, timeout=10):
            if url.startswith(WEBHOOK):
                return scripted.get("webhook", (200, {"channel_id": "5"}))
            if "/users/@me/guilds" in url:
                return scripted.get("guilds", (200, [{"id": GID}]))
            if url.endswith("/users/@me"):
                return scripted.get("identity", (200, {"id": "42"}))
            if url.endswith("/commands"):
                return scripted.get("installation", (200, [{"name": "progress"}]))
            if "/members/" in url:
                return scripted.get("member", (200, {}))
            raise AssertionError("unexpected probe URL " + url)
        return _get

    def run(**scripted):
        health._get = responder(**scripted)
        return health.check(cfg, now)

    res = run()
    check("everything healthy reads ok", res["status"] == health.OK, res)
    check("...definitely, having asked all five questions",
          res["definite"] and [p["probe"] for p in res["probes"]] ==
          ["webhook", "identity", "installation", "membership", "member"], res["probes"])

    # THE FALSE ALARM THIS BLOCK EXISTS FOR. greyBot is authorised with
    # `applications.commands` and not `bot`, so it has no member and never has. The first
    # live run read that as a kick and mailed about a bot that was working perfectly.
    res = run(guilds=(200, []))
    check("no bot member is NOT a fault — greyBot has never had one",
          res["status"] == health.OK and res["definite"], res)
    check("...and the answer is carried for handler.py to compare against history",
          res["member"] is False, res)

    res = run(guilds=(200, [{"id": GID}]))
    check("a bot member that IS there is reported as present", res["member"] is True, res)

    # Losing the app's authorisation is the real "thrown out" signal for this bot.
    res = run(installation=(403, {"code": 50001}))
    check("the app being removed from the server is an alert",
          res["status"] == health.NOT_INSTALLED and res["definite"], res)
    check("...and says so without claiming to know kick from ban",
          "banned" in health.ADVICE[health.NOT_INSTALLED], "")

    # Removing an app deletes the webhooks that app created. The mail leads with the
    # cause, not with whichever symptom was probed first.
    res = run(installation=(403, {"code": 50001}), webhook=(404, {"code": 10015}))
    check("a removal that also killed the webhook still reports the removal",
          res["status"] == health.NOT_INSTALLED, res)

    res = run(webhook=(404, {"code": 10015}))
    check("a deleted webhook on its own is its own alert",
          res["status"] == health.WEBHOOK_GONE, res)

    res = run(member=(200, {"communication_disabled_until": "2026-09-01T18:00:00+00:00"}))
    check("a timeout still running is a timeout", res["status"] == health.TIMED_OUT, res)

    # communication_disabled_until is left behind after it expires. Reading it as a flag
    # would keep mailing about a punishment that ended.
    res = run(member=(200, {"communication_disabled_until": "2026-08-30T18:00:00+00:00"}))
    check("an EXPIRED timeout is not a timeout", res["status"] == health.OK, res)

    res = run(member=(200, {"mute": True}))
    check("a server mute is reported", res["status"] == health.SERVER_MUTED, res)

    res = run(identity=(401, {"code": 0}))
    check("a rejected token is its own state", res["status"] == health.BAD_TOKEN, res)
    check("...and stops the probes that would need it",
          [p["probe"] for p in res["probes"]] == ["webhook", "identity"], res["probes"])

    # Asking about a timeout on an app with no member returns 404 and would read as a
    # second, invented failure.
    res = run(guilds=(200, []))
    check("with no member, the moderation probe is not even asked",
          "member" not in [p["probe"] for p in res["probes"]], res["probes"])

    # --- the indefinite answers. None of these may ever reach an inbox.
    res = run(webhook=(500, {}))
    check("a 500 is not evidence of anything",
          res["status"] == health.UNKNOWN and not res["definite"], res)

    res = run(guilds=(429, {"retry_after": 1}))
    check("neither is a rate limit", not res["definite"], res)

    res = run(identity=(None, {"error": "timeout"}))
    check("neither is a request that never arrived", not res["definite"], res)

    # 200 is this endpoint's maximum page size, so a full page cannot prove absence.
    res = run(guilds=(200, [{"id": str(i)} for i in range(200)]))
    check("a full page of guilds without ours is unknown, not kicked",
          res["status"] == health.UNKNOWN, res)

    res = run(webhook=(500, {}), installation=(403, {"code": 50001}))
    check("but one unknown probe never masks a definite failure",
          res["status"] == health.NOT_INSTALLED and res["definite"], res)

    # ---------------------------------------------------------------- the mail
    body = health.body(health.ALERT, run(installation=(403, {"code": 50001})), cfg,
                       "2026-09-01T12:00:00Z", since="2026-09-01T12:00:00Z")
    check("the alert names the server", "Scrambled" in body)
    check("...admits a kick and a ban are indistinguishable from in here",
          "banned" in body and "Re-authorise" in body)
    check("...and shows every probe's raw answer",
          "installation" in body and "webhook" in body)

    check("SNS subjects are capped", len(notify.clean_subject("x" * 300)) <= 100)
    check("...and stripped to ASCII, which SNS is fussy about",
          notify.clean_subject("greyBot \u2014 kicked").isascii())

    # ------------------------------------------------- one email per event
    FAKE_DDB.items.clear()
    SENT.clear()
    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    _iso = handler._iso
    real_check = health.check

    def canned(status, definite=True, member=None):
        cause = None if status == health.OK else {"probe": "membership", "verdict": status}
        return lambda c, n=None: {"status": status, "definite": definite, "cause": cause,
                                  "member": member,
                                  "probes": [{"probe": "membership", "verdict": status}]}

    health.check = canned(health.OK)
    handler.run_health_check(cfg, pk, now, _iso(now))
    check("a healthy first run mails nobody", SENT == [], SENT)
    check("...but records the state, so the next change is a change",
          (store.get_health(pk) or {}).get("status") == health.OK)

    health.check = canned(health.NOT_A_MEMBER)
    handler.run_health_check(cfg, pk, now, _iso(now))
    check("being thrown out sends exactly one email", len(SENT) == 1, SENT)

    t = now + timedelta(minutes=15)
    handler.run_health_check(cfg, pk, t, _iso(t))
    check("the next poll, still kicked, sends nothing", len(SENT) == 1, SENT)

    t = now + timedelta(hours=25)
    handler.run_health_check(cfg, pk, t, _iso(t))
    check("a day later it says so once more", len(SENT) == 2, SENT)
    check("...marked a reminder rather than a new event",
          SENT[1]["subject"].startswith("Still:"), SENT[1]["subject"])

    # The one that would be worst to get wrong: Discord going unreachable during a real
    # kick must not read as recovery.
    health.check = canned(health.UNKNOWN, definite=False)
    t = now + timedelta(hours=26)
    handler.run_health_check(cfg, pk, t, _iso(t))
    check("an unreachable Discord mails nothing", len(SENT) == 2, SENT)
    check("...and does not clear a live alert",
          store.get_health(pk)["status"] == health.NOT_A_MEMBER)

    health.check = canned(health.OK)
    t = now + timedelta(hours=27)
    handler.run_health_check(cfg, pk, t, _iso(t))
    check("coming back sends one all-clear", len(SENT) == 3, SENT)
    check("...that reads as a recovery", "back to normal" in SENT[2]["subject"],
          SENT[2]["subject"])
    t = now + timedelta(hours=28)
    handler.run_health_check(cfg, pk, t, _iso(t))
    check("and then it goes quiet again", len(SENT) == 3, SENT)

    # ------------------------------------------------- the regression rule
    #
    # Absence is only an incident when presence was recorded before. A bot that never had
    # a member stays quiet forever; one that HAD a member and lost it gets mailed once.
    FAKE_DDB.items.clear()
    SENT.clear()
    health.check = canned(health.OK, member=False)
    handler.run_health_check(cfg, pk, now, _iso(now))
    t = now + timedelta(minutes=15)
    handler.run_health_check(cfg, pk, t, _iso(t))
    check("a bot that never had a member is never mailed about one", SENT == [], SENT)

    # Gaining a seat is not an event and mails nothing -- but it must still be WRITTEN, or
    # the regression rule compares against a stale answer forever. This is the bug that
    # actually shipped: after the bot scope was added the status stayed ok, nothing was
    # written, and the stored flag sat at false while the live one was true.
    health.check = canned(health.OK, member=True)
    t = now + timedelta(minutes=30)
    handler.run_health_check(cfg, pk, t, _iso(t))
    check("gaining a seat mails nobody", SENT == [], SENT)
    check("...but IS persisted, or the regression rule goes blind",
          store.get_health(pk)["member"] is True, store.get_health(pk))

    FAKE_DDB.items.clear()
    SENT.clear()
    health.check = canned(health.OK, member=True)
    handler.run_health_check(cfg, pk, now, _iso(now))
    check("a healthy run with a member mails nobody", SENT == [], SENT)
    health.check = canned(health.OK, member=False)
    t = now + timedelta(minutes=15)
    handler.run_health_check(cfg, pk, t, _iso(t))
    check("but LOSING a member that was there is one alert", len(SENT) == 1, SENT)
    check("...reported as not_a_member",
          store.get_health(pk)["status"] == health.NOT_A_MEMBER, store.get_health(pk))

    # ------------------------------------------------- degrading, not failing
    FAKE_DDB.items.clear()
    SENT.clear()
    quiet = dict(cfg, alert_topic_arn="")
    health.check = canned(health.NOT_A_MEMBER)
    handler.run_health_check(quiet, pk, now, _iso(now))
    check("with no topic configured nothing is mailed", SENT == [], SENT)
    check("...though the state is still recorded",
          store.get_health(pk)["status"] == health.NOT_A_MEMBER)
    t = now + timedelta(minutes=15)
    handler.run_health_check(cfg, pk, t, _iso(t))
    check("...so wiring the topic up later still gets the alert out", len(SENT) == 1, SENT)

    # A publish that fails must not take the poll down with it, and must not leave the
    # state claiming the mail went.
    FAKE_DDB.items.clear()
    SENT.clear()
    boom = lambda **kw: (_ for _ in ()).throw(RuntimeError("topic gone"))
    notify.sns.publish, health.check = boom, canned(health.OK)
    handler.run_health_check(cfg, pk, now, _iso(now))
    health.check = canned(health.TIMED_OUT)
    handler.run_health_check(cfg, pk, now, _iso(now))
    check("a broken SNS does not raise out of the health check", True)
    check("...and leaves nothing claiming to have been sent",
          store.get_health(pk)["notifiedAt"] == "", store.get_health(pk))
    notify.sns.publish = _fake_publish
    health.check, health._get = real_check, real_get


def test_progress_slow_path():
    """/progress when the snapshot is stale, which is the path that was broken in prod.

    Someone ran /progress and got "Progress is briefly unavailable". The deferral raised
    NameError -- handler.py referenced a REGION it never defined -- so the slow path had
    never once worked. It went unnoticed for as long as it did because the FAST path
    covered for it, and the fast path stopped covering only when the snapshot went stale.

    Which it did constantly: put_snapshot was written on the kills path alone, so a guild
    that had killed nothing in the last hour -- most hours -- left an hour-old snapshot and
    every /progress fell through to the broken deferral. Two bugs that each hid the other.
    """
    print("\n/progress when the snapshot is stale")
    from datetime import datetime, timedelta, timezone

    import handler

    check("handler defines the region it invokes itself in",
          getattr(handler, "REGION", None), getattr(handler, "REGION", None))

    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    FAKE_DDB.items.clear()
    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    cfg = {"guild_name": "Scrambled", "guild_realm": "proudmoore", "guild_region": "us"}

    # A snapshot older than the max age must defer, not apologise.
    stale = handler._iso(now - timedelta(seconds=handler.SNAPSHOT_MAX_AGE + 60))
    store.put_snapshot(pk, "the-venomous-abyss", "The Venomous Abyss", 2, 8, 12, stale)
    invoked = []

    class FakeLambda:
        def invoke(self, **kw):
            invoked.append(kw)
            return {"StatusCode": 202}

    real_client = handler.boto3.client
    handler.boto3.client = lambda svc, **kw: FakeLambda() if svc == "lambda" else real_client(svc, **kw)
    res = handler.handle_progress({"application_id": "1", "token": "t"}, cfg, pk, now)
    handler.boto3.client = real_client
    check("a stale snapshot defers rather than failing",
          res["type"] == interactions.DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, res)
    check("...by asynchronously invoking itself once",
          len(invoked) == 1 and invoked[0]["InvocationType"] == "Event", invoked)

    # And the reason it goes stale in the first place is now fixed: an idle poll refreshes.
    FAKE_DDB.items.clear()
    handler.refresh_snapshot(pk, cfg, PROFILE, INDEX, "2026-09-01T12:00:00Z")
    snap = store.get_snapshot(pk)
    check("an idle poll leaves a fresh snapshot behind",
          snap and snap["updatedAt"] == "2026-09-01T12:00:00Z", snap)
    check("...naming a real tier, not a slug",
          snap and snap["raidName"] and snap["raidName"] != snap["slug"], snap)


def test_subtitle_aliases():
    """Raider.IO says "Dimensius"; the logs say "Dimensius, the All-Devouring".

    This is the one long-standing known bug in the seeding path, and it bites in exactly
    the situation seeding exists for: a tier seeded from Raider.IO's encounter list because
    the log history was unavailable. The seed records one key, the next kill arrives under
    the other, the dedupe sees a boss it has never heard of, and the bot announces a first
    kill for a boss it was explicitly told was already dead -- with a role ping, if it is
    the last one.
    """
    print("\nBoss names the two APIs print differently")
    n = raiderio.normalize

    known = {n("Dimensius"), n("Sszorak"), n("The Coiled Altar")}
    check("a subtitle in the logs matches a bare seed from Raider.IO",
          raiderio.alias_match(known, n("Dimensius, the All-Devouring")) == "dimensius")
    check("...and the bare form matches a seed that carried the subtitle",
          raiderio.alias_match({n("Dimensius, the All-Devouring")}, n("Dimensius"))
          == "dimensius the all devouring")
    check("an exact name still matches itself",
          raiderio.alias_match(known, n("Sszorak")) == "sszorak")
    check("a boss genuinely not in the set does not match",
          raiderio.alias_match(known, n("Ula'tek")) is None)

    # The failure that would matter more than the one being fixed: two different bosses
    # folded into one would SILENCE a real first kill forever.
    check("a shared first word is not a match",
          raiderio.alias_match({n("Dimensius")}, n("Dimensia")) is None)
    check("...nor is a prefix that stops mid-word",
          raiderio.alias_match({n("Sszorak")}, n("Sszorakar")) is None)
    check("an empty key matches nothing", raiderio.alias_match(known, "") is None)

    # And end to end: seed the tier the way Raider.IO would, then kill it the way the logs
    # would name it, and confirm nothing is posted.
    import handler
    FAKE_DDB.items.clear()
    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    store.seed_tier(pk, "t", {n("Dimensius")}, 1, "T", "now")
    state = store.load_tier(pk, "t")
    posts = []
    handler.discord.post = lambda *a, **kw: posts.append(a)
    posted = handler.announce_kill(
        {"guild_name": "Scrambled", "webhook": "w", "role_id": "", "guild_realm": "p",
         "guild_region": "us"},
        pk, "t", "T", {"name": "Dimensius, the All-Devouring", "killedAtMs": 0,
                       "encounterID": 1, "reportCode": "R"},
        state, PROFILE)
    check("a seeded boss killed under its long name announces NOTHING",
          posted is False and posts == [], posts)


def test_source_blind():
    """The other axis: the bot is fine and the source has stopped answering.

    This exists because it actually happened. On 2026-08-31 Warcraft Logs began returning
    an empty report list for every guild, greyBot went eighteen hours unable to detect a
    kill, and every Discord probe said ok the whole time -- because Discord WAS ok. The
    monitor watched the wrong axis.

    The hard part is not detecting emptiness, it is not crying wolf about a guild that
    simply has not raided. That distinction is on REPORTS, not kills, and it is the first
    thing asserted here.
    """
    print("\nThe log source, and telling a quiet week from an outage")
    from datetime import datetime, timedelta, timezone

    import handler

    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    _iso = handler._iso
    cfg = {"guild_name": "Scrambled",
           "alert_topic_arn": "arn:aws:sns:us-east-1:0:ryangrey-dev-alerts"}
    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    FAKE_DDB.items.clear()
    SENT.clear()

    # Four consecutive blind polls before a word is said. A single empty answer is a bad
    # minute at Warcraft Logs, not an outage.
    for i in range(handler.SOURCE_BLIND_POLLS - 1):
        t = now + timedelta(minutes=15 * i)
        handler.run_source_check(cfg, pk, t, _iso(t), True, {"heroicKills": 8})
    check(f"{handler.SOURCE_BLIND_POLLS - 1} blind polls stay quiet", SENT == [], SENT)
    check("...but the streak is persisted, not held in the container",
          store.get_source(pk)["blindPolls"] == handler.SOURCE_BLIND_POLLS - 1,
          store.get_source(pk))

    t = now + timedelta(minutes=15 * (handler.SOURCE_BLIND_POLLS - 1))
    handler.run_source_check(cfg, pk, t, _iso(t), True, {"heroicKills": 8})
    check("crossing the threshold sends exactly one email", len(SENT) == 1, SENT)
    check("...that says Discord is fine and the source is not",
          "Warcraft Logs" in SENT[0]["subject"], SENT[0]["subject"])
    check("...and points at the handoff rather than guessing the cause",
          "wcl-reportdata-blind" in SENT[0]["body"])

    t = now + timedelta(hours=1)
    handler.run_source_check(cfg, pk, t, _iso(t), True, {"heroicKills": 8})
    check("still blind an hour later sends nothing more", len(SENT) == 1, SENT)

    t = now + timedelta(hours=25)
    handler.run_source_check(cfg, pk, t, _iso(t), True, {"heroicKills": 8})
    check("a day later it reminds, once", len(SENT) == 2, SENT)
    check("...marked a reminder", SENT[1]["subject"].startswith("Still:"), SENT[1]["subject"])

    t = now + timedelta(hours=26)
    handler.run_source_check(cfg, pk, t, _iso(t), False)
    check("reports coming back sends one all-clear", len(SENT) == 3, SENT)
    check("...and resets the streak", store.get_source(pk)["blindPolls"] == 0,
          store.get_source(pk))

    # A quiet week must never reach the threshold at all, no matter how long it lasts.
    FAKE_DDB.items.clear()
    SENT.clear()
    for i in range(12):                      # three hours of polls with nothing to find
        t = now + timedelta(minutes=15 * i)
        handler.run_source_check(cfg, pk, t, _iso(t), False)
    check("a guild that simply has not raided is never called an outage", SENT == [], SENT)
    check("...and never accumulates a streak", store.get_source(pk)["blindPolls"] == 0)

    # The two signals share one rule, so a fix to the cadence lands in one place.
    check("the transition rule is shared with the Discord check",
          handler._alert_kind({}, health.OK, now) is None
          and handler._alert_kind({}, health.SOURCE_BLIND, now) == health.ALERT
          and handler._alert_kind({"status": health.SOURCE_BLIND}, health.OK, now)
              == health.RECOVERY)


def _src(report="R1", actors=None, elig=None, fids=None,
         damage=None, deaths=(), rankings=None):
    """One report's worth of blobs, in the shape recap.py aggregates over."""
    return {"report": report, "actors": actors, "eligible": set(elig or ()),
            "fightIDs": list(fids or ()), "damage": damage,
            "deaths": list(deaths), "rankings": rankings}


def test_recap_parsers():
    print("\nReading the untyped table and rankings blobs")
    scope = recap.raid_scope(FIXTURE["fights"], wcl.HEROIC)
    actors = recap.actor_index(FIXTURE["masterData"])
    elig, fids = scope["raiderIDs"], scope["fightIDs"]

    def src(**kw):
        kw.setdefault("actors", actors)
        kw.setdefault("elig", elig)
        kw.setdefault("fids", fids)
        return _src(**kw)

    check("only Heroic raid fights are in scope", len(fids) == 16, len(fids))
    check("the Normal kill and three Mythic+ runs are excluded", scope["excluded"] == 4,
          scope["excluded"])
    check("the raid is eighteen people", len(elig) == 18, len(elig))
    check("actors is indexed but is NOT the roster",
          len(actors) > len(elig), f"{len(actors)} actors, {len(elig)} raiders")

    # FINDING 1. The captured report holds a raid AND three Mythic+ dungeons, so a table
    # over the whole report is not a raid leaderboard. It names different people.
    unscoped = recap.top_damage([src(damage=FIXTURE["damage"])])
    scoped = recap.top_damage([src(damage=FIXTURE["damageScoped"])])
    check("an unscoped table names a different top three",
          [r["name"] for r in unscoped] != [r["name"] for r in scoped],
          f"{[r['name'] for r in unscoped]} vs {[r['name'] for r in scoped]}")
    full = {r["name"]: r["total"]
            for r in recap.top_damage([src(damage=FIXTURE["damageScoped"])], 99)}
    check("...whose leader is not even in the real top three",
          unscoped[0]["name"] not in {r["name"] for r in scoped},
          f"{unscoped[0]['name']} vs {[r['name'] for r in scoped]}")
    check("...because most of that leader's damage was in a dungeon",
          full[unscoped[0]["name"]] < unscoped[0]["total"] / 2,
          f"{unscoped[0]['total']:,} unscoped vs {full[unscoped[0]['name']]:,} in the raid")
    check("a pet never reaches the leaderboard",
          "Lightspawn Lasher" not in full)
    check("dungeon partners are not raiders",
          not ({"Brackwater", "Foxglove", "Hearthstone", "Marrowgate"}
               & {r["name"] for r in recap.top_damage([src(damage=FIXTURE["damage"])], 99)}))

    # FINDING 2. The Deaths table stops at 200 rows and says nothing about it.
    pages = FIXTURE["deathPages"]
    check("a full page is recognised as truncated", recap.page_is_truncated(pages[0]))
    check("a short page is not", recap.page_is_truncated(pages[1]) is False)
    check("the cursor is the last death seen", recap.last_timestamp(pages[0]) == 7859016,
          recap.last_timestamp(pages[0]))
    one = recap.death_counts([src(deaths=[pages[0]])])
    all_ = recap.death_counts([src(deaths=pages)])
    check("one page undercounts the night",
          sum(r["deaths"] for r in one) == 200 and sum(r["deaths"] for r in all_) == 245,
          f"{sum(r['deaths'] for r in one)} vs {sum(r['deaths'] for r in all_)}")
    check("...and names the WRONG person as most deaths",
          one[0]["deaths"] != all_[0]["deaths"], f"{one[0]} vs {all_[0]}")
    check("re-reading an overlapping page cannot inflate a count",
          recap.death_counts([src(deaths=pages + [pages[0]])]) == all_)

    # FINDING 3, and the one a single-report fixture cannot show. Actor ids are scoped to
    # ONE REPORT. A night logged twice renumbers everybody, so aggregating by id splits
    # each raider in half and fuses unrelated people together. This is the shape of the bug
    # the first live dry run produced.
    shifted_actors = {aid + 1000: dict(a) for aid, a in actors.items()}
    shifted_damage = {"data": {"entries": [
        dict(e, id=e["id"] + 1000) for e in FIXTURE["damageScoped"]["data"]["entries"]]}}
    shifted_deaths = [{"data": {"entries": [
        dict(e, id=e["id"] + 1000) for e in p["data"]["entries"]]}} for p in pages]
    two = [src(report="A", damage=FIXTURE["damageScoped"], deaths=pages),
           _src(report="B", actors=shifted_actors, elig={i + 1000 for i in elig},
                fids=fids, damage=shifted_damage, deaths=shifted_deaths)]

    merged = recap.top_damage(two, 99)
    check("the same night logged twice is still eighteen people",
          len(recap.raider_keys(two)) == 18, len(recap.raider_keys(two)))
    check("...and nobody appears twice under a renumbered id",
          len(merged) == len({r["name"] for r in merged}), len(merged))
    check("...with each player's damage summed, not split",
          merged[0]["total"] == 2 * full[merged[0]["name"]],
          f"{merged[0]}")
    dmerged = recap.death_counts(two)
    check("deaths merge on the player too",
          sum(r["deaths"] for r in dmerged) == 490 and
          len(dmerged) == len({r["name"] for r in dmerged}),
          f"{sum(r['deaths'] for r in dmerged)} over {len(dmerged)} people")

    # FINDING 4, also invisible in a one-report fixture: two people in the guild both log,
    # so one night produces two reports of the SAME pulls. Summing those doubles the card.
    HOUR = 3600000
    twice = [{"code": "swibeto", "start": 0, "end": int(5.86 * HOUR), "heroicFights": 16},
             {"code": "elder", "start": -4 * 60000, "end": int(2.79 * HOUR),
              "heroicFights": 16}]
    kept, dropped = recap.drop_duplicate_logs(twice)
    check("the same night logged twice keeps one report", len(kept) == 1, kept)
    check("...and keeps the more complete one", kept[0]["code"] == "swibeto", kept)
    check("...naming what it duplicated",
          dropped[0]["duplicateOf"] == "swibeto", dropped)

    split = [{"code": "before", "start": 0, "end": 2 * HOUR, "heroicFights": 8},
             {"code": "after", "start": 2 * HOUR + 60000, "end": 5 * HOUR,
              "heroicFights": 8}]
    kept2, dropped2 = recap.drop_duplicate_logs(split)
    check("a night logged in two PARTS keeps both", len(kept2) == 2 and not dropped2,
          f"{kept2} / {dropped2}")
    check("...in their original order",
          [r["code"] for r in kept2] == ["before", "after"], kept2)
    nudge = [{"code": "x", "start": 0, "end": 2 * HOUR, "heroicFights": 8},
             {"code": "y", "start": int(1.8 * HOUR), "end": 4 * HOUR, "heroicFights": 8}]
    check("a small tail overlap is still two parts, not a duplicate",
          len(recap.drop_duplicate_logs(nudge)[0]) == 2)

    # rankings covers every ranked kill in the report, at every difficulty.
    par = recap.parses([src(rankings=FIXTURE["rankings"])], None)
    got = {r["difficulty"] for r in FIXTURE["rankings"]["data"]}
    check("the rankings blob really does mix difficulties", got == {3, 4, 10}, got)
    check("only Heroic parses survive",
          par["best"]["boss"] in {"Nymrissa Wavecaller", "Nek'zali the Soulcoiler",
                                  "The Lost Explorers"}, par["best"]["boss"])
    check("rankPercent is used, not bracketPercent", par["best"]["percent"] == 81.0,
          par["best"]["percent"])

    # Difficulty alone is not enough. The report's Heroic kills span two raids, so a card
    # labelled with one of them must not credit a parse earned in the other.
    grotto = {f["id"] for f in FIXTURE["fights"]
              if f["name"] == "Nymrissa Wavecaller" and f["difficulty"] == wcl.HEROIC}
    check("the unscoped best parse comes from the OTHER raid",
          par["best"]["boss"] == "Nymrissa Wavecaller", par["best"]["boss"])
    tiered = recap.parses([src(rankings=FIXTURE["rankings"],
                               fids=[i for i in fids if i not in grotto])], None)
    check("scoping to the night's tier excludes it",
          tiered["best"]["boss"] != "Nymrissa Wavecaller", tiered["best"])
    check("...and still finds a parse from the right raid",
          tiered["best"]["boss"] in {"Nek'zali the Soulcoiler", "The Lost Explorers"},
          tiered["best"]["boss"])

    # The roster intersection: the explicit answer to "who counts".
    drop = {r["id"] for r in FIXTURE["damageScoped"]["data"]["entries"]
            if r["name"] == scoped[0]["name"]}
    thin = [src(damage=FIXTURE["damageScoped"], elig=elig - drop)]
    check("dropping someone from the eligible set drops them from the card",
          scoped[0]["name"] not in {r["name"] for r in recap.top_damage(thin)})
    roster = {team.player_key(a["name"], a["server"]) for i, a in actors.items()
              if i in elig and a["name"] != scoped[0]["name"]}
    keep = [src(damage=FIXTURE["damageScoped"])]
    gone = recap.restrict_to_roster(keep, roster)
    check("restrict_to_roster excludes the off-roster raider", gone == 1, gone)
    check("...and an empty result is refused rather than emptying the card",
          recap.restrict_to_roster([src(damage=None)], {"nobody-at-all"}) == 0)

    # A wipe night. Rankings only exist for kills, so there is nothing to say.
    wipes = [dict(f, kill=False) for f in FIXTURE["fights"]]
    wipe = recap.summarise(recap.raid_scope(wipes, wcl.HEROIC),
                           [src(damage=FIXTURE["damageScoped"], deaths=pages,
                                rankings={"data": []})])
    check("a wipe night killed nothing", wipe["bosses"] == [], wipe["bosses"])
    check("...has a progression boss anyway", wipe["prog"]["name"] == "The Lost Explorers",
          wipe["prog"])
    check("...and simply carries no parse section", wipe["parses"] is None)
    check("...which is reported as a missing section", "parses" in wipe["missing"])

    # Boss numbering, including the two ways it must decline to answer. A position that
    # cannot be established is dropped, never guessed: "1/8" on a boss the list does not
    # contain would claim progression the guild does not have.
    # The two shapes the fixture night does not have. A farm clear must NOT claim a
    # first kill, and a boss left standing is the one worth a pull count.
    night = [src(damage=FIXTURE["damageScoped"], deaths=pages,
                 rankings=FIXTURE["rankings"])]
    # Everything this scope killed, marked as already dead -- so the night is pure farm.
    # Keyed off what was actually killed rather than off ABYSS, because this fixture spans
    # two raids and the warm-up kill is not an Abyss boss.
    farm = recap.summarise(scope, night, encounters=ABYSS,
                           dead={raiderio.normalize(b)
                                 for b in recap.bosses_killed(scope)})
    check("a farm night claims no first kills", farm["firstKills"] == [], farm["firstKills"])
    check("...but still counts what it killed", farm["killed"] == len(farm["bosses"]),
          farm["killed"])
    fresh = recap.summarise(scope, night, encounters=ABYSS, dead=set())
    check("a first kill is a boss that had not died before",
          fresh["firstKills"] == fresh["bosses"], fresh["firstKills"])

    standing = recap.unkilled(recap.raid_scope(
        [dict(f, kill=False) for f in FIXTURE["fights"]], wcl.HEROIC))
    check("a boss that never died is listed with its pull count",
          standing and all(r["pulls"] > 0 for r in standing), standing[:2])
    check("...and a boss that DID die is not listed as standing",
          not [r for r in recap.unkilled(scope) if r["name"] in fresh["bosses"]],
          recap.unkilled(scope))

    check("a boss is numbered by its place in the published order",
          recap.boss_labels(["The Lost Explorers"], ABYSS) == ["3/8 The Lost Explorers"],
          recap.boss_labels(["The Lost Explorers"], ABYSS))
    check("...matched through the same fold as every other boss key",
          recap.boss_labels(["Nekzali the Soulcoiler"], ABYSS)
          == ["1/8 Nekzali the Soulcoiler"])
    check("a boss the tier does not contain keeps its bare name",
          recap.boss_labels(["Nymrissa Wavecaller"], ABYSS) == ["Nymrissa Wavecaller"])
    check("an unreadable encounter list numbers nothing at all",
          recap.boss_labels(ABYSS[:2], []) == ABYSS[:2],
          recap.boss_labels(ABYSS[:2], []))

    # Fields that vanish. Every one has to cost one section, never the card.
    for label, kw in (
            ("damage blob is empty", {"damage": {}}),
            ("damage entries lost 'total'", {"damage": {"data": {"entries": [
                {"id": i, "name": "x"} for i in elig]}}}),
            ("deaths lost 'timestamp'", {"deaths": [{"data": {"entries": [
                {"id": i, "fight": fids[0]} for i in elig]}}]}),
            ("rankings became a bare list", {"rankings": {"data": "nonsense"}}),
            ("rankings lost rankPercent", {"rankings": {"data": [
                {"difficulty": 4, "kill": 1, "encounter": {"name": "X"},
                 "roles": {"dps": {"characters": [{"name": "Ashvale"}]}}}]}}),
    ):
        args = {"damage": FIXTURE["damageScoped"], "deaths": pages,
                "rankings": FIXTURE["rankings"]}
        args.update(kw)
        out = recap.summarise(scope, [src(**args)])
        check(f"survives: {label}", isinstance(out, dict) and "bosses" in out)
    check("a blob that is not even a dict is survivable",
          recap.top_damage([src(damage="nonsense"), src(damage=None), src(damage=7)]) == [])


def test_recap_end_to_end():
    print("\nThe morning-after recap, end to end")
    import copy
    from datetime import datetime, timedelta, timezone
    import handler
    config._cache.clear()

    FAKE_DDB.items.clear()
    handler._guild_id["value"] = None
    now = datetime.now(timezone.utc)
    posts = []
    profile = copy.deepcopy(PROFILE)
    reports = [{"code": "FIXTURECODE0001", "title": "Prog Raid",
                "startTime": int((now - timedelta(hours=13)).timestamp() * 1000),
                "endTime": int((now - timedelta(hours=10)).timestamp() * 1000),
                "guildTag": None, "zone": {"id": 44, "name": "The Venomous Abyss"}}]
    rate = {"limitPerHour": 3600, "pointsSpentThisHour": 10.0, "pointsResetIn": 60}

    detail = dict(FIXTURE)
    detail["startTime"] = reports[0]["startTime"]
    detail["endTime"] = reports[0]["endTime"]

    handler.wcl.get_token = lambda *a, **kw: "tok"
    handler.wcl.find_guild = lambda *a, **kw: ({"id": 777, "name": "Scrambled"}, None)
    handler.wcl.query = lambda token, doc, variables=None: {"rateLimitData": rate}
    handler.wcl.reports_in_window = lambda *a, **kw: (reports, None)
    handler.wcl.report_detail = lambda token, code: (detail, None)
    handler.wcl.report_tables = lambda token, code, fids: (
        {"damage": FIXTURE["damageScoped"], "rankings": FIXTURE["rankings"]}, None)
    handler.wcl.deaths_pages = lambda *a, **kw: (FIXTURE["deathPages"], None, 2)
    handler.raiderio.guild_profile = lambda *a, **kw: profile
    handler.raiderio.static_raids = lambda exp: {11: RAIDS, 10: PREV_RAIDS}.get(exp, [])
    handler.discord.post = lambda hook, payload, **kw: posts.append(payload) or 204

    pk = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT)
    store.mark_bootstrapped(pk, "now", 4)
    store.seed_tier(pk, "the-venomous-abyss", set(), 0, "The Venomous Abyss", "now")
    store.seed_tier(pk, "the-tidebound-grotto", set(), 0, "The Tidebound Grotto", "now")

    cfg = config.load()
    cfg["recap_enabled"] = False
    handler.handler({"mode": "recap"}, None)
    check("a disabled recap posts nothing", posts == [], f"{len(posts)} posts")

    # The preview path, which has to work BEFORE the feature is switched on -- and must
    # not claim the night, or the real recap would be silently skipped forever after.
    before = dict(FAKE_DDB.items)
    res = handler.handler({"mode": "recap", "dry": True}, None)
    check("a dry run works while the recap is still disabled", res.get("dry") is True, res)
    check("...renders a real card", res["payload"]["embeds"][0]["fields"])
    check("...posts nothing", posts == [], f"{len(posts)} posts")
    check("...and claims NO night, so the real recap still fires",
          FAKE_DDB.items == before, "state changed — the dry run would silence the recap")

    # The window override, and the fact that only a dry run gets one.
    old_start = reports[0]["startTime"]
    reports[0]["startTime"] = int((now - timedelta(hours=42)).timestamp() * 1000)
    reports[0]["endTime"] = int((now - timedelta(hours=36)).timestamp() * 1000)
    detail["startTime"], detail["endTime"] = reports[0]["startTime"], reports[0]["endTime"]
    seen = {}

    def spy(token, gid, start, end, limit=10):
        seen["hours"] = (end - start) / 3600000.0
        return reports, None

    handler.wcl.reports_in_window = spy
    handler.handler({"mode": "recap", "dry": True, "hours": 48}, None)
    check("a dry run can widen its own window", round(seen["hours"]) == 48, seen)
    cfg["recap_enabled"] = True
    handler.handler({"mode": "recap", "hours": 999}, None)
    check("a scheduled run cannot", round(seen["hours"]) == 18, seen)
    cfg["recap_enabled"] = False

    # An explicit manual post: ignores the enabled switch, may widen the window, and still
    # claims the night so it cannot be published twice.
    posts.clear()
    # The window test above did a real post, which claimed this night. Release it, or the
    # manual case would be testing the duplicate guard rather than the manual path.
    # Recap claims are per-tenant, so the fake table is keyed by that partition.
    FAKE_DDB.items.pop((pk.tenant, store.RECAPS_SK), None)
    before_manual = dict(FAKE_DDB.items)
    res = handler.handler({"mode": "recap", "manual": True, "hours": 48}, None)
    check("a manual post works while the schedule is still disabled",
          len(posts) == 1 and res.get("posted") is True, f"{len(posts)} posts / {res}")
    check("...and honours its window", round(seen["hours"]) == 48, seen)
    posts.clear()
    res = handler.handler({"mode": "recap", "manual": True, "hours": 48}, None)
    check("...and claims the night, so it cannot post twice",
          posts == [] and res.get("duplicate") is True, f"{len(posts)} posts / {res}")
    FAKE_DDB.items.clear()
    FAKE_DDB.items.update(before_manual)
    posts.clear()
    posts.clear()
    FAKE_DDB.items.clear()
    FAKE_DDB.items.update(before)
    reports[0]["startTime"], reports[0]["endTime"] = old_start, old_start + 3600000
    detail["startTime"], detail["endTime"] = old_start, old_start + 3600000
    handler.wcl.reports_in_window = lambda *a, **kw: (reports, None)

    cfg["recap_enabled"] = True
    rate = {"limitPerHour": 3600, "pointsSpentThisHour": 3500.0, "pointsResetIn": 60}
    res = handler.handler({"mode": "recap"}, None)
    check("an exhausted point budget aborts before the expensive calls",
          res.get("skipped") == "rate_limit", res)
    check("...and posts nothing", posts == [], f"{len(posts)} posts")

    # The band that matters, and the reason the two reserves are different numbers: the
    # announcer's ceiling is not yet reached, so a poll would still run -- and the recap
    # stands down anyway to leave those points for it.
    rate = {"limitPerHour": 3600, "pointsSpentThisHour": 3000.0, "pointsResetIn": 60}
    res = handler.handler({"mode": "recap"}, None)
    check("the recap yields while the announcer still has headroom",
          res.get("skipped") == "rate_limit", res)
    check("...at a point where a poll would NOT have backed off",
          3000.0 / 3600 < handler.POINTS_CEILING,
          f"{3000.0 / 3600:.3f} vs ceiling {handler.POINTS_CEILING}")
    check("...and posts nothing", posts == [], f"{len(posts)} posts")
    rate = {"limitPerHour": 3600, "pointsSpentThisHour": 10.0, "pointsResetIn": 60}

    saved, reports = reports, []
    res = handler.handler({"mode": "recap"}, None)
    check("a night with no report posts nothing, silently", posts == [] and res["reports"] == 0)
    reports = saved

    # Cold start: no roster at all, so signal B alone decides -- and this report is full of
    # wipes on bosses nothing has ever killed.
    posts.clear()
    res = handler.handler({"mode": "recap"}, None)
    check("a prog night posts exactly one card", len(posts) == 1, f"{len(posts)} posts")
    check("...as one embed, not several",
          posts and len(posts[0]["embeds"]) == 1, len(posts[0]["embeds"]) if posts else 0)
    check("...and pings nobody", posts and posts[0]["allowed_mentions"] == {"parse": []})
    check("...with no content line at all", posts and "content" not in posts[0])

    embed = posts[0]["embeds"][0]
    check("the card is labelled with the tier holding most of the night",
          "The Venomous Abyss" in embed["footer"]["text"], embed["footer"]["text"])
    # Nymrissa Wavecaller is a WORLD BOSS -- Raider.IO carries it as a one-encounter raid,
    # the same shape as sporefall. It must not be numbered among the tier's bosses, and it
    # must not inflate the tier's count, but it IS reported: a Heroic world boss kill is
    # news, and the old behaviour dropped it silently.
    # Option A, pinned. The world boss is reported but its fights are NOT in any column.
    # The scope handed to summarise is the tier's, so the one thing that could leak a
    # world-boss number into a leaderboard -- a rankings row for it -- is filtered out by
    # the fightID scoping every aggregate already shares.
    wb = "Nymrissa Wavecaller"
    tier_only = recap.raid_scope(
        [f for f in FIXTURE["fights"]
         if raiderio.normalize(f.get("name")) != raiderio.normalize(wb)], wcl.HEROIC)
    leaked = recap.parse_rows(
        [_src(actors=recap.actor_index(FIXTURE["masterData"]),
              elig=tier_only["raiderIDs"], fids=tier_only["fightIDs"],
              rankings=FIXTURE["rankings"])], None)
    check("the world boss is in the fixture to begin with",
          any(raiderio.normalize(f.get("name")) == raiderio.normalize(wb)
              for f in FIXTURE["fights"]))
    check("...but no world-boss parse reaches the tier's columns",
          not [r for r in leaked if r["boss"] == wb],
          sorted({r["boss"] for r in leaked}))

    check("the world boss is not numbered among the tier's bosses",
          "/8 Nymrissa" not in embed["description"], embed["description"])
    check("...and does not inflate the tier's kill count",
          "killed **2** Heroic bosses" in embed["description"], embed["description"])
    # Counted, not named. The card names first kills and bosses left standing -- things
    # the guild has to go back and do -- and a world boss is neither.
    check("...but is counted in the same sentence, without being named",
          "killed **2** Heroic bosses & the world boss" in embed["description"],
          embed["description"])
    check("...and is not named anywhere on the card",
          "Nymrissa" not in json.dumps(posts[0]), embed["description"])

    # Each kill carries its place in the tier's PUBLISHED order, against the tier's real
    # boss total. The fixture kills the 1st and 3rd bosses of eight, and it kills them in
    # that order -- so numbering by tonight's kill order would produce "2/8 The Lost
    # Explorers" and read as a boss the guild has not reached yet.
    check("the first kill carries its position in the tier",
          "**1/8 Nek'zali the Soulcoiler**" in embed["description"], embed["description"])
    check("...and the numbers follow the published order, not tonight's kill order",
          "**3/8 The Lost Explorers**" in embed["description"], embed["description"])

    # EVERY boss the card names is numbered. A card that numbers the kill list and leaves
    # the pull count and the parse bare reads as three different kinds of boss.
    # The card leads on the count, then on what was NEW. Nothing had been killed before
    # this fixture night, so both kills are first kills.
    check("the card counts the kills rather than listing them",
          "killed **2** Heroic bosses" in embed["description"], embed["description"])
    check("...and names the first kills, numbered",
          "including first kills on **1/8 Nek'zali the Soulcoiler** and "
          "**3/8 The Lost Explorers**" in embed["description"], embed["description"])

    # A parse is about a person. A tier position next to their name reads as part of
    # their score, so the parse fields carry the bare boss name.
    parse_field = [f for f in embed.get("fields", []) if f["name"] == "Best parse"]
    check("the best parse names its boss WITHOUT a tier number",
          parse_field and "/8 " not in parse_field[0]["value"],
          parse_field[0]["value"] if parse_field else None)
    check("the progression boss is the one with the most wipes",
          "The Lost Explorers" in embed["description"], embed["description"])
    fields = {f["name"] for f in embed.get("fields", [])}
    check("top damage is on the card", "Top damage" in fields, fields)
    check("most deaths is on the card", "Most deaths" in fields, fields)
    check("worst parse is OFF by default", "Worst parse" not in fields, fields)

    posts.clear()
    res = handler.handler({"mode": "recap"}, None)
    check("running it again does not double-post", posts == [], f"{len(posts)} posts")
    check("...because the night was already claimed", res.get("duplicate") is True, res)

    # A B-team night: everything it touched was already dead, and there is no roster to
    # tell the teams apart. Neither signal is conclusive.
    FAKE_DDB.items.clear()
    store.mark_bootstrapped(pk, "now", 4)
    dead = {raiderio.normalize(f["name"]) for f in FIXTURE["fights"]
            if f["difficulty"] == wcl.HEROIC}
    store.seed_tier(pk, "the-venomous-abyss", dead, 3, "The Venomous Abyss", "now")
    store.seed_tier(pk, "the-tidebound-grotto", dead, 1, "The Tidebound Grotto", "now")
    posts.clear()
    res = handler.handler({"mode": "recap"}, None)
    check("with nothing new killed and no roster, the verdict is UNKNOWN",
          res.get("posted") is False, res)
    check("...and UNKNOWN posts NOTHING", posts == [], f"{len(posts)} posts")

    # --- the same night, recapped for a TEAM install -------------------------
    # The very report that was UNKNOWN for the guild is the team's by construction: it
    # came from the team's uploader on the team's raid day. No classifier, one card, in
    # the team's channel, under the team's name and the night's difficulty.
    FAKE_DDB.items.clear()
    day_name = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    night_day = day_name[handler._local(handler._at(reports[0]["startTime"])).weekday()]
    T = keys.team_pk(TEST_TENANT, "meers-raid")
    store.put_config(T, "us", "Proudmoore", "Scrambled", "555000000000000009", "now",
                     prog_role_id="222222222222222222",
                     team={"slug": "meers-raid", "name": "Meer's Raid",
                           "wcl_user_id": 40245, "raid_days": night_day,
                           "difficulties": "normal,heroic"})
    store.register_tenant(T)
    tscope = keys.Scope.build("us", "proudmoore", "Scrambled", TEST_TENANT,
                              team="meers-raid")
    store.mark_bootstrapped(tscope, "now", 4)
    store.seed_tier(tscope, "the-venomous-abyss", set(), 0, "The Venomous Abyss", "now")
    cfg["bot_token"] = "bot-tok"
    cfg["recap_enabled"] = True
    asked = {}

    def team_reports(token, gid, start, end, limit=10, user_id=None):
        asked["user_id"] = user_id
        return reports, None

    channel_posts = []
    real_post_to = handler.discord.post_to
    handler.wcl.reports_in_window = team_reports
    handler.discord.post_to = lambda dest, payload, **kw: channel_posts.append((dest, payload))
    posts.clear()
    res = handler.handler({"mode": "recap"}, None)
    handler.discord.post_to = real_post_to
    cfg["recap_enabled"] = False
    cfg.pop("bot_token", None)
    store.unregister_tenant(T)

    check("the team's recap reads its uploader's reports", asked.get("user_id") == 40245,
          asked)
    team_cards = [(d, p) for d, p in channel_posts if d.get("channel") == "555000000000000009"]
    check("the team's night posts exactly one card, to the team's channel",
          len(team_cards) == 1, [d for d, _ in channel_posts])
    if team_cards:
        embed = team_cards[0][1]["embeds"][0]
        check("...under the team's name", embed["title"].startswith("Meer's Raid — "),
              embed["title"])
        check("...at the difficulty the night was spent on",
              embed["footer"]["text"].startswith("Heroic The Venomous Abyss"),
              embed["footer"])
        check("...pinging nobody", "content" not in team_cards[0][1])
    check("the team's night is claimed under the TEAM's tenant",
          not store.claim_recap(tscope, res.get("night") or "") if res.get("night")
          else False, res)


def main():
    print("greyBot self-test")
    for fn in (test_config, test_boss_art, test_name_normalisation, test_slug_resolution,
               test_seed_names, test_progress_count,
               test_tenant_keys, test_setup_command, test_tenant_fanout,
               test_dedupe, test_aotc_guard,
               test_roster_derivation, test_team_resolution, test_roster_state,
               test_deleted_post_probe, test_kill_card_image,
               test_discord_payloads, test_wcl_parsing, test_wcl_pagination,
               test_interactions, test_progress_slow_path,
               test_subtitle_aliases, test_health,
               test_source_blind,
               test_iam_grant_covers_config,
               test_recap_parsers, test_end_to_end,
               test_team_install,
               test_recap_end_to_end):
        fn()
    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
