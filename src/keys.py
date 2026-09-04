"""Partition keys for the multi-tenant table.

Three namespaces, and which one a row belongs in is decided by one question: is
this a fact about the WoW guild, or a record of what this install did?

    WOW#<region>#<realm>#<name>   upstream facts. SHARED by every tenant.
    TENANT#<discord_guild_id>     what this install did. NEVER shared.
    ART#GLOBAL                    boss artwork. Global.

Full reasoning in `docs/multi-tenant-keys.md`. The two things worth repeating
here, because both are load-bearing:

**Shared upstream is what makes the WCL budget survive.** The points budget is
account-wide, so N tenants tracking one guild must cost one guild's worth of
polling, not N.

**Dedupe must NOT be shared.** The `announced` set lives under TENANT#, because
two Discord servers tracking one guild post to two different channels. Sharing it
would mean whichever polled second found every boss already claimed and silently
posted nothing — no error, no log line, just a bot that looks fine and never
speaks.
"""


def wow_pk(region, realm, name):
    """Partition for upstream facts about a WoW guild.

    `WOW#`, not the old `GUILD#`. The rename is deliberate rather than cosmetic:
    "guild" now means two different things (a WoW guild and a Discord guild), and
    a key prefix that could mean either is a key prefix somebody will eventually
    build wrong. The migration rewrites the old rows.
    """
    return f"WOW#{region.lower()}#{realm.lower()}#{name.lower()}"


def tenant_pk(discord_guild_id):
    """Partition for one install.

    `discord_guild_id` MUST come from the verified interaction payload or from
    configuration written by a verified interaction — never from a field in a
    request body. This is GreyScale's "no endpoint accepts a member id" rule
    transplanted: if no caller can name a tenant, no caller can reach another
    tenant's rows.

    Rejects anything that is not a bare Discord snowflake. A tenant id carrying a
    `#` could otherwise forge a sort key boundary, and one carrying whitespace or
    an empty string would collide every such tenant into a single partition.
    """
    raw = str(discord_guild_id or "").strip()
    if not raw or not raw.isdigit():
        raise ValueError(f"discord guild id must be a numeric snowflake, got {raw!r}")
    return f"TENANT#{raw}"


import re

_TEAM_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def team_slug(raw):
    """A raid team's slug, validated to the same standard as a tenant id.

    Lowercase letters, digits and hyphens only, so a slug can never carry a `#` and forge
    a key boundary. Set by the operator's registration script, never by a request.
    """
    slug = str(raw or "").strip().lower()
    if not _TEAM_SLUG.match(slug):
        raise ValueError(f"team slug must match [a-z0-9-], got {raw!r}")
    return slug


def team_pk(discord_guild_id, slug):
    """Partition for a RAID TEAM's install inside a Discord server.

    Scrambled runs more than one raid team in one Discord, and each wants its own
    channel and its own first kills. A team is therefore its own install: its own
    announced sets, its own bootstrap, its own recap claims -- everything the tenant
    partition holds -- under a key that extends the server's tenant key rather than
    replacing it. `TENANT#<guild>#<team>` sorts beside `TENANT#<guild>` in the registry
    and can never be confused with a bare server, whose id is all digits.
    """
    return f"{tenant_pk(discord_guild_id)}#{team_slug(slug)}"


def team_wow_pk(region, realm, name, slug):
    """Partition for a raid TEAM's facts, kept apart from the guild's.

    The shared `WOW#` partition holds facts every install agrees on: which bosses the
    guild has killed, who was there. A team's first kills are not the guild's first
    kills -- Meer's Raid killing a boss on Normal weeks after the prog team cleared it on
    Heroic is a first for the team and nothing for the guild -- and a team's first-kill
    rosters must never feed the prog team's derived roster. So a team gets its own copy
    of the whole facts partition, and shares nothing with the guild install.
    """
    return f"{wow_pk(region, realm, name)}#{team_slug(slug)}"


def tenant_from_interaction(body):
    """The tenant an interaction came from, taken off the VERIFIED payload.

    This is the only way a request-driven code path may learn which tenant it is
    acting as. `body` must be the JSON of an interaction whose Ed25519 signature
    has already been checked — Discord populates `guild_id` itself, so a caller
    cannot claim to be a server they are not in.

    Nothing reads a guild id from a command option or a request field. That is
    GreyScale's "no endpoint accepts a member id" rule: if no caller can name a
    tenant, no caller can reach another tenant's rows.

    Raises for a DM (no `guild_id`), which is correct — every one of this bot's
    commands is about a server's configuration, and there is no sensible tenant
    to answer as outside one.
    """
    gid = (body or {}).get("guild_id")
    if not gid:
        raise ValueError("interaction has no guild_id — commands only work in a server")
    return tenant_pk(gid)


ART_PK = "ART#GLOBAL"


# --- sort keys ------------------------------------------------------------
#
# Under WOW#

def tier_sk(slug):
    return f"TIER#{slug}"


def kill_sk(slug, boss_key):
    return f"KILL#{slug}#{boss_key}"


def roster_sk(slug):
    return f"ROSTER#{slug}"


PROGRESS_SK = "PROGRESS"
SOURCE_SK = "SOURCE"


# Under TENANT#

HEROIC = "heroic"
NORMAL = "normal"
DIFFICULTIES = (NORMAL, HEROIC)


def announced_sk(slug, difficulty=HEROIC):
    """Where the dedupe set lives. One row per tier per difficulty per install.

    Heroic keeps the bare `ANNOUNCED#<slug>` it has always had, so every row already in
    the table stays exactly where it is. Any other difficulty gets its own row under a
    suffix: a boss's first Normal kill and its first Heroic kill are two different
    events, claimed separately, and one set holding both would make the second one
    silent.
    """
    if difficulty == HEROIC:
        return f"ANNOUNCED#{slug}"
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty {difficulty!r}")
    return f"ANNOUNCED#{slug}#{difficulty}"


def recap_sk(night_key):
    return f"RECAP#{night_key}"


CONFIG_SK = "CONFIG"
BOOTSTRAP_SK = "BOOTSTRAP"
HEALTH_SK = "HEALTH"


class Scope:
    """The two partitions one poll operates across.

    Passed around instead of a bare `pk` so that no call site has to remember
    which namespace a given row lives in — asking `scope.wow` or `scope.tenant`
    is a decision the reader can check against the table in the design doc.
    """

    __slots__ = ("wow", "tenant", "team")

    def __init__(self, wow, tenant, team=None):
        self.wow = wow
        self.tenant = tenant
        # The team slug, or None for an install that tracks the whole guild. Carried on
        # the Scope so log lines and card keys can say which install acted without every
        # caller re-deriving it from the partition string.
        self.team = team

    @classmethod
    def build(cls, region, realm, name, discord_guild_id, team=None):
        if team:
            slug = team_slug(team)
            return cls(team_wow_pk(region, realm, name, slug),
                       team_pk(discord_guild_id, slug), slug)
        return cls(wow_pk(region, realm, name), tenant_pk(discord_guild_id))

    def __repr__(self):
        return f"Scope(wow={self.wow!r}, tenant={self.tenant!r}, team={self.team!r})"

    def __eq__(self, other):
        return (isinstance(other, Scope)
                and self.wow == other.wow and self.tenant == other.tenant)

    # Defining __eq__ removes the inherited __hash__, which would make a Scope
    # unusable as a dict key or set member — and Phase 3's caching layer will
    # want exactly that, to fan one upstream fetch out across the tenants
    # sharing it. Cheaper to keep them hashable now than to debug it later.
    def __hash__(self):
        return hash((self.wow, self.tenant))


# The registry. One row listing every configured install, so the poller can find
# them with a GetItem on a known pk.
#
# Not a Scan and not a GSI, deliberately. The execution role grants GetItem,
# PutItem, UpdateItem and Query and nothing else -- no Scan, no BatchGetItem --
# and that absence is a security property this project has already paid for
# once. A registry row keeps enumeration inside the existing grant.
#
# The ceiling this implies is worth stating: a DynamoDB item caps at 400 KB, so a
# string set of snowflakes tops out somewhere in the low tens of thousands of
# tenants. The WCL points budget caps installs far below that, so the registry is
# not the binding constraint -- but if it ever becomes one, the answer is a
# sharded registry (REGISTRY#0..N), not a Scan.
REGISTRY_PK = "REGISTRY"
TENANTS_SK = "TENANTS"
