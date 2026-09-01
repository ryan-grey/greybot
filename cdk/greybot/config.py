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
        "alerts/sns_topic_arn",
    ))

    @property
    def is_prod(self) -> bool:
        return self.stage == "prod"


PROD = StageConfig(
    stage="prod",
    function_name="ryangrey-greybot",
    table_name="ryangrey-greybot",
    schedule_name="ryangrey-greybot-poll",
    role_name="ryangrey-greybot-role",
    scheduler_role_name="ryangrey-greybot-scheduler-role",
    api_name="greybot-interactions",
    ssm_prefix="/greybot",
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
