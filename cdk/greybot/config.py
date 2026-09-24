"""Per-stage configuration.

Phase 1 is a port with zero behaviour change, so every value under `prod` is
transcribed from `docs/parity-baseline/` — the snapshot of what the hand-rolled
`scripts/deploy.sh` actually built. Do not "tidy" these. A nicer memory size or a
tighter timeout is a behaviour change wearing a cleanup's clothes, and Phase 1's
whole claim is that nothing moved.

`dev` is a separate everything: its own function, table, schedule, API and SSM
prefix, pointed at a throwaway Discord app and test server. It exists so parity
work can post for real without posting into the live raid channel.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StageConfig:
    stage: str
    function_name: str
    table_name: str
    schedule_name: str
    role_name: str
    scheduler_role_name: str
    api_name: str
    ssm_prefix: str

    # Straight from lambda-configuration.json.
    runtime: str = "python3.12"
    handler: str = "handler.handler"
    memory_mb: int = 512
    timeout_seconds: int = 60
    architecture: str = "arm64"

    # Straight from scheduler.json.
    schedule_expression: str = "rate(15 minutes)"
    schedule_timezone: str = "UTC"

    announce_tz: str = "America/New_York"
    repo_url: str = "https://github.com/ryan-grey/greybot"

    # The SNS topic the alarm path already publishes to. Shared with the site's
    # alerting, deliberately: one topic, one delivery pipeline.
    alerts_topic_name: str = "ryangrey-dev-alerts"

    # The bucket behind raids.ryangrey.dev. Prod writes the published recap page here;
    # dev has no subdomain and writes nowhere, so its grant is empty rather than
    # pointed at prod's bucket -- a dev deploy must not be able to overwrite a real
    # night's published page.
    recap_page_bucket: str = ""

    # KMS key that encrypts the SecureString parameters. Same key both stages —
    # it is an account-level key, not a per-environment one.
    kms_key_id: str = "8e811ee5-0cfa-456f-905f-7b664255201e"

    # SSM leaf paths the runtime reads. PATHS ONLY. No value belonging to any of
    # these may ever appear in this repo, in a doc, or in a log line.
    ssm_leaves: tuple = field(default=(
        "wcl/client_id",
        "wcl/client_secret",
        "discord/webhook_url",
        "discord/prog_role_id",
        "discord/bot_token",
        "discord/public_key",
        "discord/guild_id",
        "guild/name",
        "guild/realm",
        "guild/region",
        "blizzard/client_id",
        "blizzard/client_secret",
        "recap/enabled",
        "recap/show_worst_parse",
        "recap/schedule",
        "team/roster_min_first_kill_pct",
        "team/prog_overlap_high",
        "team/prog_overlap_low",
        # Granted but NOT currently created in SSM, and that is correct rather
        # than stale. `src/config.py` reads it through DEFAULTS, so the bot runs
        # fine without it — and the grant being already in place is what lets the
        # parameter be created later without touching IAM. Deleting it would
        # turn "set prog_tag" into an AccessDenied at the next poll.
        "team/prog_tag",
        # Same situation as team/prog_tag: granted, not yet created. Empty is OFF for both
        # of these, so the bot runs fine without them, and having the grant already in
        # place is what lets the subdomain be switched on with an SSM write rather than an
        # IAM change on a live role.
        "recap/page_url",
        "recap/page_bucket",
        "alerts/sns_topic_arn",
        # Signs the roll call's one question to the NAS service. Empty is OFF.
        "rollcall/secret",
        # Who privately gets the night's grey parses, and what counts as grey. Empty is OFF.
        "recap/low_parse_dm",
        "recap/low_parse_max",
        # The Tuesday Great Vault check's channel. Granted, not created: empty is OFF.
        "vault/channel_id",
    ))

    # The schedules beside the poller: same function, same scheduler role, one input each.
    # Created by the infra/ scripts first and adopted by `cdk import`, so every value is
    # the live one, retry policy and Input string byte for byte.
    extra_schedules: tuple = ()

    # Environment beyond the three keys every stage carries. The retired configure-mplus.py
    # set these on the live function; declared here, a template change can no longer drop them.
    extra_env: tuple = ()

    @property
    def is_prod(self) -> bool:
        return self.stage == "prod"

    @property
    def mplus(self) -> bool:
        return any(k == "MPLUS_ENABLED" and v == "1" for k, v in self.extra_env)


@dataclass(frozen=True)
class ExtraSchedule:
    logical_id: str
    suffix: str                 # appended to the function name
    expression: str
    timezone: str
    input: str
    max_event_age: int
    max_retries: int
    description: str = ""


_ET = "America/New_York"
# Days, not minutes: a raid-night post that misses its slot still belongs to that night.
_RAID_DAY = 86400
# M+ jobs run every minute or weekly off a live snapshot; a stale retry is worse than none.
_MPLUS_AGE = 300

PROD_SCHEDULES = (
    ExtraSchedule("RecapSchedule", "recap", "cron(15 0 ? * WED,FRI *)", _ET,
                  '{"mode":"recap"}', _RAID_DAY, 2),
    ExtraSchedule("SaturdayRecapSchedule", "recap-saturday-raid", "cron(15 23 ? * SAT *)", _ET,
                  '{"mode":"recap","team":"saturday-raid"}', _RAID_DAY, 2,
                  "Saturday Raid recap, 1:00 AM Eastern Sunday (the raid runs past "
                  "midnight), team-only"),
    ExtraSchedule("VaultSchedule", "vault", "cron(30 11 ? * TUE *)", _ET,
                  '{"mode":"vault"}', _RAID_DAY, 1),
    ExtraSchedule("MplusCollectSchedule", "mplus-collect", "rate(1 minute)", "UTC",
                  '{"mode": "mplus_collect"}', _MPLUS_AGE, 0),
    ExtraSchedule("MplusRecordsSchedule", "mplus-records", "rate(1 minute)", "UTC",
                  '{"mode": "mplus_records"}', _MPLUS_AGE, 0),
    ExtraSchedule("MplusRecapSchedule", "mplus-recap", "cron(0 10 ? * TUE *)", _ET,
                  '{"mode": "mplus_recap"}', _MPLUS_AGE, 0),
)


PROD = StageConfig(
    stage="prod",
    function_name="ryangrey-greybot",
    table_name="ryangrey-greybot",
    schedule_name="ryangrey-greybot-poll",
    role_name="ryangrey-greybot-role",
    scheduler_role_name="ryangrey-greybot-scheduler-role",
    api_name="greybot-interactions",
    ssm_prefix="/greybot",
    recap_page_bucket="raids.ryangrey.dev",
    extra_schedules=PROD_SCHEDULES,
    extra_env=(
        ("MPLUS_ENABLED", "1"),
        ("MPLUS_CHANNEL_ID", "1548100008719679538"),
        ("MPLUS_SCORE_POLICY", "overall_for_participants"),
        ("MPLUS_RECORD_BOARD_ENABLED", "1"),
    ),
)

# NOTE ON THE DEV SSM PREFIX
#
# `docs/dev-discord-ids.md` suggested `/greybot/dev/...`. This uses
# `/greybot-dev/...` instead, deliberately: the dev tree must not be a SUBTREE of
# the prod tree.
#
# Today it would not matter, because the prod role grants each parameter by full
# literal ARN and none of them match a dev path. But the first time anyone
# reaches for the obvious shortcut — `parameter/greybot/*`, which is exactly what
# the multi-tenant phase will want when the parameter list stops being
# enumerable — that wildcard silently swallows every dev parameter too, and prod
# gains read access to the dev bot's token. Keeping the trees siblings means that
# shortcut stays safe to take.
#
# Leaf structure matches prod (`discord/bot_token`, not `bot_token`) so the two
# stages are diffable.
DEV = StageConfig(
    stage="dev",
    function_name="ryangrey-greybot-dev",
    table_name="ryangrey-greybot-dev",
    schedule_name="ryangrey-greybot-poll-dev",
    role_name="ryangrey-greybot-role-dev",
    scheduler_role_name="ryangrey-greybot-scheduler-role-dev",
    api_name="greybot-interactions-dev",
    ssm_prefix="/greybot-dev",
)

STAGES = {"prod": PROD, "dev": DEV}
