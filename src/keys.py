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

def announced_sk(slug):
    """Where the dedupe set lives. One row per tier per install."""
    return f"ANNOUNCED#{slug}"


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

    __slots__ = ("wow", "tenant")

    def __init__(self, wow, tenant):
        self.wow = wow
        self.tenant = tenant

    @classmethod
    def build(cls, region, realm, name, discord_guild_id):
        return cls(wow_pk(region, realm, name), tenant_pk(discord_guild_id))

    def __repr__(self):
        return f"Scope(wow={self.wow!r}, tenant={self.tenant!r})"

    def __eq__(self, other):
        return (isinstance(other, Scope)
                and self.wow == other.wow and self.tenant == other.tenant)

    # Defining __eq__ removes the inherited __hash__, which would make a Scope
    # unusable as a dict key or set member — and Phase 3's caching layer will
    # want exactly that, to fan one upstream fetch out across the tenants
    # sharing it. Cheaper to keep them hashable now than to debug it later.
    def __hash__(self):
        return hash((self.wow, self.tenant))
