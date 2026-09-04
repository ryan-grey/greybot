#!/usr/bin/env python3
"""Register a RAID TEAM as its own install of greyBot.

    ./scripts/register-team.py --table ryangrey-greybot \
        --guild us/proudmoore/Scrambled --discord-guild 946663011991556117 \
        --team meers-raid --name "Meer's Raid" --channel 1545486229116817509 \
        --wcl-user 40245 --raid-days tue,thu --difficulties normal,heroic \
        --role 1460717131376365608
    ./scripts/register-team.py ... --apply

Dry run by default. Nothing is written without --apply.

A team is a second install inside a Discord server the bot already serves: its own
channel, its own first kills at its own difficulties, its own recap, fed by one raider's
personal Warcraft Logs uploads on the team's raid days. The layout is in
`docs/multi-tenant-keys.md` under "Raid teams".

What happens after --apply: the very next poll finds the new registry entry, reads this
CONFIG row, and BOOTSTRAPS the team -- it seeds every tier from the uploader's history at
each difficulty and posts nothing. Kills after that are announced. There is no window in
which an old kill can be announced, because the poll cannot reach the announce path
until the bootstrap marker exists.

Why a script and not /setup: /setup is for a server picking a guild. A team needs a
Warcraft Logs user id and a raid-day filter, neither of which a server admin should be
guessing at in a slash command, and the whole thing is one row. Operator task, run with
AWS_PROFILE=infra -- the Lambda role has no need to write CONFIG rows for teams.

Re-running with the same --team OVERWRITES the CONFIG row (that is how a channel or a raid
day gets corrected) and leaves every other row alone. It never touches the announced sets
or the bootstrap marker, so a re-run cannot make the team re-seed or re-announce.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import keys  # noqa: E402

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_guild(text):
    parts = [p for p in str(text).split("/") if p]
    if len(parts) != 3:
        raise SystemExit(f"--guild must be region/realm/Name, got {text!r}")
    region, realm, name = parts
    return region.lower(), realm.lower(), name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True)
    ap.add_argument("--guild", required=True, help="region/realm/Name")
    ap.add_argument("--discord-guild", required=True, help="Discord server (guild) id")
    ap.add_argument("--team", required=True, help="slug, e.g. meers-raid")
    ap.add_argument("--name", required=True, help="display name, e.g. \"Meer's Raid\"")
    ap.add_argument("--channel", required=True, help="Discord channel id to post in")
    ap.add_argument("--wcl-user", required=True, type=int,
                    help="Warcraft Logs user id whose uploads are the team's logs")
    ap.add_argument("--raid-days", default="",
                    help="comma-separated weekdays (tue,thu); empty means every day")
    ap.add_argument("--difficulties", default="heroic",
                    help="comma-separated, in announce order: normal,heroic")
    ap.add_argument("--role", default="", help="role id the clear cards ping; empty = none")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    region, realm, name = parse_guild(args.guild)
    slug = keys.team_slug(args.team)
    tenant = keys.team_pk(args.discord_guild, slug)
    for d in [x.strip().lower() for x in args.raid_days.split(",") if x.strip()]:
        if d[:3] not in DAYS:
            raise SystemExit(f"unknown weekday {d!r} in --raid-days")
    diffs = [x.strip().lower() for x in args.difficulties.split(",") if x.strip()]
    for d in diffs:
        if d not in keys.DIFFICULTIES:
            raise SystemExit(f"unknown difficulty {d!r}; choose from {keys.DIFFICULTIES}")
    if not diffs:
        raise SystemExit("--difficulties must name at least one difficulty")
    for label, value in (("--channel", args.channel), ("--role", args.role)):
        if value and not str(value).isdigit():
            raise SystemExit(f"{label} must be a numeric Discord snowflake")

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item = {
        "pk": {"S": tenant}, "sk": {"S": keys.CONFIG_SK},
        "guildRegion": {"S": region}, "guildRealm": {"S": realm}, "guildName": {"S": name},
        "channelId": {"S": str(args.channel)}, "progRoleId": {"S": str(args.role or "")},
        "configuredAt": {"S": now_iso}, "configuredBy": {"S": "register-team"},
        "teamSlug": {"S": slug}, "teamName": {"S": args.name},
        "wclUserId": {"S": str(args.wcl_user)},
        "raidDays": {"S": ",".join(x.strip().lower() for x in args.raid_days.split(",")
                                   if x.strip())},
        "difficulties": {"S": ",".join(diffs)},
    }

    scope = keys.Scope.build(region, realm, name, args.discord_guild, team=slug)
    print(f"tenant   {tenant}")
    print(f"facts    {scope.wow}")
    print(f"config   " + ", ".join(f"{k}={v['S']}" for k, v in item.items()
                                   if k not in ("pk", "sk")))
    print(f"registry {keys.REGISTRY_PK}/{keys.TENANTS_SK}  ADD {tenant}")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply.")
        return 0

    ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    ddb.put_item(TableName=args.table, Item=item)
    # Registry AFTER the config, same order as /setup: a tenant listed before its config
    # exists is one the very next poll would pick up and find nothing for.
    ddb.update_item(TableName=args.table,
                    Key={"pk": {"S": keys.REGISTRY_PK}, "sk": {"S": keys.TENANTS_SK}},
                    UpdateExpression="ADD tenants :t",
                    ExpressionAttributeValues={":t": {"SS": [tenant]}})
    print("\nwritten. The next poll bootstraps the team silently; kills after that are "
          "announced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
