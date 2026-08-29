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

import os
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
            if "NOT contains(announced, :k)" in ConditionExpression:
                have = set((item.get("announced") or {}).get("SS") or [])
                if vals[":k"]["S"] in have:
                    raise _conditional_failure()
            if "aotcAnnounced = :f" in ConditionExpression:
                if bool((item.get("aotcAnnounced") or {}).get("BOOL")):
                    raise _conditional_failure()

        if item is None:
            item = dict(Key)
            self.items[k] = item

        if UpdateExpression.startswith("ADD announced"):
            have = set((item.get("announced") or {}).get("SS") or [])
            have |= set(vals[":b"]["SS"])
            item["announced"] = {"SS": sorted(have)}
        elif UpdateExpression.startswith("DELETE announced"):
            have = set((item.get("announced") or {}).get("SS") or [])
            have -= set(vals[":b"]["SS"])
            if have:
                item["announced"] = {"SS": sorted(have)}
            else:
                item.pop("announced", None)      # DynamoDB drops an emptied set
        elif UpdateExpression.startswith("SET"):
            for assign in UpdateExpression[4:].split(", "):
                field, placeholder = [p.strip() for p in assign.split("=")]
                item[field] = vals[placeholder]
        return {}


FAKE_DDB = FakeDynamo()
FAKE_SSM = types.SimpleNamespace(get_parameters=lambda **kw: {"Parameters": []})

boto3 = types.ModuleType("boto3")
boto3.client = lambda service, **kw: FAKE_DDB if service == "dynamodb" else FAKE_SSM
botocore = types.ModuleType("botocore")
botocore.config = types.ModuleType("botocore.config")
botocore.config.Config = lambda **kw: None
botocore.exceptions = types.ModuleType("botocore.exceptions")
botocore.exceptions.ClientError = ClientError
sys.modules.update({"boto3": boto3, "botocore": botocore,
                    "botocore.config": botocore.config,
                    "botocore.exceptions": botocore.exceptions})

import discord            # noqa: E402
import raiderio           # noqa: E402
import store              # noqa: E402
import wcl                # noqa: E402

# ---------------------------------------------------------------- fixtures
# Captured live from raider.io on 2026-08-28.

RAIDS = [
    {"slug": "tier-mn-1", "name": "MN Tier 1 (VS / DR / MQD)", "short_name": "VS/DR/MQD",
     "starts": {"us": "2026-03-17T15:00:00Z"}, "ends": {"us": "2026-08-18T15:00:00Z"},
     "encounters": [{"name": "Imperator Averzian"}, {"name": "Vorasius"},
                    {"name": "Fallen-King Salhadaar"}, {"name": "Vaelgor & Ezzorak"},
                    {"name": "Lightblinded Vanguard"}, {"name": "Crown of the Cosmos"},
                    {"name": "Chimaerus the Undreamt God"},
                    {"name": "Belo'ren, Child of Al'ar"}, {"name": "Midnight Falls"}]},
    {"slug": "the-tidebound-grotto", "name": "The Tidebound Grotto",
     "starts": {"us": "2026-08-18T15:00:00Z"}, "ends": {"us": "2030-01-01T00:00:00Z"},
     "encounters": [{"name": "Tidewarden Kelsara"}]},
    {"slug": "the-venomous-abyss", "name": "The Venomous Abyss",
     "starts": {"us": "2026-08-18T15:00:00Z"}, "ends": {"us": "2030-01-01T00:00:00Z"},
     "encounters": [{"name": "Sirensong"}, {"name": "Broodmother Vysska"},
                    {"name": "The Drowned Court"}, {"name": "Grimscale Tidecaller"},
                    {"name": "Abyssal Maw"}, {"name": "Venomlord Xar'guul"},
                    {"name": "The Sunken Throne"}, {"name": "Nazj'vora the Envenomed"}]},
]

PROFILE = {
    "name": "Scrambled", "region": "us", "realm": "Area 52",
    "raid_progression": {
        "tier-mn-1": {"summary": "9/9 H", "total_bosses": 9, "normal_bosses_killed": 9,
                      "heroic_bosses_killed": 9, "mythic_bosses_killed": 0},
        "the-tidebound-grotto": {"summary": "1/1 H", "total_bosses": 1,
                                 "normal_bosses_killed": 1, "heroic_bosses_killed": 1,
                                 "mythic_bosses_killed": 0},
        "the-venomous-abyss": {"summary": "5/8 H", "total_bosses": 8,
                               "normal_bosses_killed": 8, "heroic_bosses_killed": 5,
                               "mythic_bosses_killed": 0},
    },
    "raid_rankings": {
        "tier-mn-1": {"heroic": {"world": 4210, "region": 1880, "realm": 74}},
        "the-tidebound-grotto": {"heroic": {"world": 0, "region": 0, "realm": 0}},
        "the-venomous-abyss": {"heroic": {"world": 6600, "region": 2400, "realm": 118}},
    },
}

INDEX = raiderio.RaidIndex(RAIDS)

# Prime the static-data cache so build_index resolves from the fixture instead of calling
# Raider.IO. Without this the offline guard fires, which is exactly what it is for.
raiderio._static_cache.update({10: [], 11: RAIDS, 12: [], 13: []})


def dt(s):
    from datetime import datetime
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------- tests

def test_name_normalisation():
    print("\nBoss-name normalisation")
    check("'&' and 'and' fold together",
          raiderio.normalize("Vaelgor & Ezzorak") == raiderio.normalize("Vaelgor and Ezzorak"))
    check("apostrophes and commas are ignored",
          raiderio.normalize("Belo'ren, Child of Al'ar") == raiderio.normalize("Beloren Child of Alar"))
    check("a typographic apostrophe matches a straight one",
          raiderio.normalize("Belo\u2019ren") == raiderio.normalize("Belo'ren"))
    check("case is ignored",
          raiderio.normalize("SIRENSONG") == raiderio.normalize("Sirensong"))


def test_slug_resolution():
    print("\nRaid-slug resolution")
    # The case the whole design turns on: slugifying the zone name gives
    # "vault-of-shadows"-ish nonsense, never "tier-mn-1".
    slug, meta, how = raiderio.resolve_raid(PROFILE, "Midnight Falls", "MN Tier 1 Raid",
                                            dt("2026-07-01T02:00:00+00:00"), "us", INDEX)
    check("boss name resolves tier-mn-1, which no slugify can reach",
          (slug, how) == ("tier-mn-1", "encounter-name"), f"got {slug!r} via {how!r}")
    check("slugify really would have missed it",
          raiderio.slugify("MN Tier 1 Raid") not in PROFILE["raid_progression"])

    slug, meta, how = raiderio.resolve_raid(PROFILE, "Nazj'vora the Envenomed",
                                            "The Venomous Abyss",
                                            dt("2026-08-28T02:00:00+00:00"), "us", INDEX)
    check("current-tier boss resolves by name",
          (slug, how) == ("the-venomous-abyss", "encounter-name"), f"got {slug!r}/{how!r}")

    # An unknown boss (a tier newer than the static data) still resolves via the zone name.
    slug, meta, how = raiderio.resolve_raid(PROFILE, "Some Brand New Boss",
                                            "The Venomous Abyss",
                                            dt("2026-08-28T02:00:00+00:00"), "us", INDEX)
    check("unknown boss falls back to the zone slug",
          (slug, how) == ("the-venomous-abyss", "zone-slug"), f"got {slug!r}/{how!r}")

    # Neither name known: the live window is the next rung. Only one 8-boss raid and one
    # 1-boss raid are live, so this is ambiguous by design -- assert it does NOT guess.
    slug, meta, how = raiderio.resolve_raid(PROFILE, "Unknown", "Unknown Zone",
                                            dt("2026-08-28T02:00:00+00:00"), "us", INDEX)
    check("two live raids means no live-window guess; falls to last key",
          how == "last-key-fallback", f"got {how!r}")

    # With only one raid live, the window rung should fire.
    solo = raiderio.RaidIndex([RAIDS[0], RAIDS[2]])
    slug, meta, how = raiderio.resolve_raid(PROFILE, "Unknown", "Unknown Zone",
                                            dt("2026-05-01T02:00:00+00:00"), "us", solo)
    check("a single live raid resolves by window",
          (slug, how) == ("tier-mn-1", "live-window"), f"got {slug!r}/{how!r}")

    check("expansion search anchors on the profile's own slugs",
          raiderio.build_index(PROFILE, 11)[0] is not None)


def test_progress_count():
    print("\n'n of total' derivation")
    # Bot seeded at 5, has since announced the 6th. Raider.IO still reports 5.
    state = {"announced": {"1", "2", "3", "4", "5", "6"}, "seedSize": 5, "baseline": 5}
    check("stale Raider.IO does not produce a stale count",
          store.progress_count(state, 5, 8) == 6, store.progress_count(state, 5, 8))
    check("Raider.IO wins when it is ahead of us",
          store.progress_count(state, 7, 8) == 7)

    # Mid-tier deploy: log history only reached 3 of the 6 already dead, Raider.IO knew 6.
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

    # Tier rollover: the announced set starts empty and every kill is earned by a claim,
    # so the baseline is 0. A baseline carried over from Raider.IO would double-count and
    # announce the first boss of a new tier as "2 of 8".
    fresh = {"announced": {"1"}, "seedSize": 0, "baseline": 0}
    check("first kill of a new tier is 1 of 8, not 2",
          store.progress_count(fresh, 1, 8) == 1, store.progress_count(fresh, 1, 8))
    check("...and still 1 when Raider.IO has not caught up at all",
          store.progress_count(fresh, 0, 8) == 1)


def test_dedupe():
    print("\nDedupe and the announce-once claim")
    FAKE_DDB.items.clear()
    pk = store.guild_pk("us", "area-52", "Scrambled")
    slug = "the-venomous-abyss"

    check("seeding creates the tier", store.seed_tier(pk, slug, {"1", "2"}, 5,
                                                      "The Venomous Abyss", "now"))
    check("seeding twice does not clobber",
          store.seed_tier(pk, slug, {"9"}, 99, "x", "now") is False)
    state = store.load_tier(pk, slug)
    check("seeded set is preserved", state["announced"] == {"1", "2"}, state["announced"])
    check("baseline is max(Raider.IO, history)", state["baseline"] == 5, state["baseline"])

    check("a new boss can be claimed", store.claim_boss(pk, slug, "6"))
    check("the same boss cannot be claimed twice", store.claim_boss(pk, slug, "6") is False)
    check("a seeded boss is never announced", store.claim_boss(pk, slug, "1") is False)

    store.release_boss(pk, slug, "6")
    check("a released boss is retried on the next poll", store.claim_boss(pk, slug, "6"))

    check("a tier that was never seeded refuses claims",
          store.claim_boss(pk, "some-future-tier", "1") is False)


def test_aotc_guard():
    print("\nAOTC fires once")
    FAKE_DDB.items.clear()
    pk = store.guild_pk("us", "area-52", "Scrambled")
    slug = "the-venomous-abyss"
    store.seed_tier(pk, slug, {"1"}, 1, "The Venomous Abyss", "now")

    check("AOTC can be claimed once", store.claim_aotc(pk, slug))
    check("a re-kill of the final boss does not re-fire it",
          store.claim_aotc(pk, slug) is False)
    check("the flag survives a reload", store.load_tier(pk, slug)["aotcAnnounced"])

    store.release_aotc(pk, slug)
    check("a failed webhook lets AOTC retry", store.claim_aotc(pk, slug))

    # A tier already cleared before the bot existed must never be celebrated.
    FAKE_DDB.items.clear()
    store.seed_tier(pk, "tier-mn-1", {"1"}, 9, "MN Tier 1", "now", aotc_already=True)
    check("seeding a finished tier pre-sets the AOTC flag",
          store.load_tier(pk, "tier-mn-1")["aotcAnnounced"])
    check("so no retroactive AOTC is possible",
          store.claim_aotc(pk, "tier-mn-1") is False)


def test_discord_payloads():
    print("\nDiscord payloads")
    p = discord.kill_embed("Scrambled", "Abyssal Maw", 6, 8, "The Venomous Abyss", 118,
                           report_url="https://www.warcraftlogs.com/reports/abc")
    body = p["embeds"][0]
    check("title matches the spec line",
          body["title"] == "Scrambled just killed Abyssal Maw", body["title"])
    check("count line matches the spec",
          "They are now **6** of **8** in Heroic The Venomous Abyss" in body["description"])
    check("rank line matches the spec", "Ranked server **#118**" in body["description"])
    check("a kill card mentions nobody", p["allowed_mentions"] == {"parse": []})

    unranked = discord.kill_embed("Scrambled", "Sirensong", 1, 8, "The Venomous Abyss", None)
    check("an unranked guild is not 'Ranked server #0'",
          "Ranked server" not in unranked["embeds"][0]["description"])
    check("rank 0 from Raider.IO reads as unranked",
          raiderio.realm_rank(PROFILE, "the-tidebound-grotto") is None)
    check("a real rank is returned", raiderio.realm_rank(PROFILE, "the-venomous-abyss") == 118)

    a = discord.aotc_payload("Scrambled", "The Venomous Abyss",
                             "August 28, 2026 at 11:14 PM EDT", "12345")
    check("the role is mentioned in content", a["content"] == "<@&12345>")
    check("the role is allow-listed so the ping actually fires",
          a["allowed_mentions"]["roles"] == ["12345"])
    check("nothing else can be mentioned", a["allowed_mentions"]["parse"] == [])
    check("AOTC title matches the spec",
          a["embeds"][0]["title"] == "Scrambled just got AOTC on August 28, 2026 at 11:14 PM EDT")
    check("AOTC body matches the spec",
          a["embeds"][0]["description"] == "Congratulations to the team!")

    noping = discord.aotc_payload("Scrambled", "R", "when", "")
    check("an unset role id posts without a broken mention", "content" not in noping)


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
                {"id": 9, "encounterID": 3010, "name": "Abyssal Maw", "kill": True,
                 "difficulty": 4, "startTime": 300_000, "endTime": 600_000},
                {"id": 4, "encounterID": 3009, "name": "Grimscale Tidecaller", "kill": True,
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

    check("only Heroic boss kills survive", [k["name"] for k in kills] ==
          ["Grimscale Tidecaller", "Abyssal Maw"], [k["name"] for k in kills])
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


# ---------------------------------------------------------------- end to end

ABYSS = [e["name"] for e in RAIDS[2]["encounters"]]


def test_end_to_end():
    """The two guarantees worth wiring the whole thing together for: a mid-tier deploy
    announces nothing, and a re-kill announces nothing."""
    print("\nEnd to end")
    import copy
    from datetime import datetime, timedelta, timezone

    os.environ.update({"GUILD_NAME": "Scrambled", "GUILD_REALM": "area-52",
                       "GUILD_REGION": "us", "PROG_RAIDER_ROLE_ID": "12345",
                       "ANNOUNCE_TZ": "America/New_York"})
    import handler
    handler._secrets.update({"client_id": "id", "client_secret": "sec",
                             "webhook": "https://discord.test/hook"})

    now = datetime.now(timezone.utc)
    ms = lambda days_ago: int((now - timedelta(days=days_ago)).timestamp() * 1000)

    posts = []
    profile = copy.deepcopy(PROFILE)

    def kill(idx, days_ago):
        return {"encounterID": 3000 + idx, "name": ABYSS[idx], "zoneID": 44,
                "zoneName": "The Venomous Abyss", "reportCode": "aBcD",
                "killedAtMs": ms(days_ago)}

    window = []          # kills inside the routine poll window
    history = []         # everything the deep seed pass can see

    def fake_kills(token, gid, since_ms, limit=12, difficulty=4):
        deep = since_ms < ms(handler.LOOKBACK_DAYS + 1)
        src = history if deep else window
        return sorted([k for k in src if k["killedAtMs"] >= since_ms],
                      key=lambda k: k["killedAtMs"]), {"limit": 3600, "spent": 10.0,
                                                       "resetsIn": 60, "fraction": 0.003}

    handler.wcl.get_token = lambda *a, **kw: "tok"
    handler.wcl.find_guild = lambda *a, **kw: ({"id": 777, "name": "Scrambled"},
                                               {"limit": 3600, "spent": 10.0,
                                                "resetsIn": 60, "fraction": 0.003})
    handler.wcl.heroic_kills_since = fake_kills
    # A warm container skips the guild lookup, so the rate check falls through to a bare
    # rateLimitData query. Left unstubbed it is a live call, which the offline guard above
    # catches -- but the point of the test is the flow, so give it an answer.
    handler.wcl.query = lambda token, doc, variables=None: {
        "rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 10.0,
                          "pointsResetIn": 60}}
    handler.raiderio.guild_profile = lambda *a, **kw: profile
    handler.raiderio.static_raids = lambda exp: RAIDS if exp == 11 else []
    handler.discord.post = lambda hook, payload, **kw: posts.append(payload) or 204

    # --- cold start, five bosses already dead, a sixth killed an hour ago -----
    FAKE_DDB.items.clear()
    handler._guild_id["value"] = None
    history = [kill(i, 30 - i) for i in range(5)] + [kill(5, 0.04)]
    window = [kill(5, 0.04)]
    profile["raid_progression"]["the-venomous-abyss"]["heroic_bosses_killed"] = 6
    handler.handler({}, None)
    check("a mid-tier deploy announces nothing at all", posts == [], f"{len(posts)} posts")
    seeded = store.load_tier(store.guild_pk("us", "area-52", "Scrambled"),
                             "the-venomous-abyss")
    check("...but it remembers every boss already dead",
          len(seeded["announced"]) == 6, seeded["announced"])

    # --- the seventh boss dies -----------------------------------------------
    posts.clear()
    window = [kill(5, 0.04), kill(6, 0.01)]
    history = history + [kill(6, 0.01)]
    profile["raid_progression"]["the-venomous-abyss"]["heroic_bosses_killed"] = 6   # lagging
    handler.handler({}, None)
    check("a genuinely new kill is announced exactly once", len(posts) == 1, len(posts))
    check("only the new boss is announced, not the six already known",
          posts and posts[0]["embeds"][0]["title"] == f"Scrambled just killed {ABYSS[6]}")
    check("the count comes from the bot, not from lagging Raider.IO",
          posts and "**7** of **8**" in posts[0]["embeds"][0]["description"],
          posts[0]["embeds"][0]["description"] if posts else "")

    # --- the same poll runs again --------------------------------------------
    posts.clear()
    handler.handler({}, None)
    check("polling again announces nothing", posts == [], f"{len(posts)} posts")

    # --- the final boss dies: kill card, then AOTC ----------------------------
    posts.clear()
    window = [kill(6, 0.01), kill(7, 0.005)]
    history = history + [kill(7, 0.005)]
    handler.handler({}, None)
    check("the final boss gets its own kill card", len(posts) == 2, len(posts))
    check("and it reads 8 of 8",
          posts and "**8** of **8**" in posts[0]["embeds"][0]["description"])
    check("AOTC follows it", posts and "just got AOTC on" in posts[1]["embeds"][0]["title"])
    check("AOTC pings Prog Raiders and nothing else",
          posts and posts[1].get("content") == "<@&12345>"
          and posts[1]["allowed_mentions"] == {"parse": [], "roles": ["12345"]})

    # --- farm night: the final boss dies again -------------------------------
    posts.clear()
    window = [kill(7, 0.001)]
    handler.handler({}, None)
    check("a re-kill of the final boss announces nothing, and no second AOTC",
          posts == [], f"{len(posts)} posts")

    # --- new tier next week ---------------------------------------------------
    posts.clear()
    profile["raid_progression"]["a-brand-new-raid"] = {
        "total_bosses": 8, "heroic_bosses_killed": 0, "normal_bosses_killed": 1,
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


def main():
    print("scrambled-raid-bot self-test")
    for fn in (test_name_normalisation, test_slug_resolution, test_progress_count,
               test_dedupe, test_aotc_guard, test_discord_payloads, test_wcl_parsing,
               test_end_to_end):
        fn()
    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
