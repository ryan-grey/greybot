#!/usr/bin/env python3
"""One-off (2026-09-21): switch the Prog Raid install to the POOLED report source Ryan
approved ("any logs are fine, pool them as resources across all user uploads").

Before: exact title "Prog Raid" + uploader 519077, which matched nothing since 8/27.
After : no title, no uploader (every guild report from any uploader), raidDays "tue,thu"
        (Saturday is not a Prog night), and uploader 40245 (Meerclar, whose Meer's Raid
        logs are his own) excluded by name. Several uploads of one night are expected and
        safe: see handler.on_source_reports.

Run it only AFTER scripts/prog-pool-repair-state.py --apply and AFTER the code that reads
wclExcludeOwnerIds is deployed (scripts/deploy-cdk.sh prod).

What it does:
  1. reads the CONFIG row and refuses unless it is the prog-raid team row;
  2. with --apply: writes a JSON backup of the whole row, then sets the four fields;
  3. reads the row back and fails loudly unless those four changed and nothing else did.

Dry by default: without --apply it only reads and prints the plan.

    AWS_PROFILE=infra .venv/bin/python scripts/prog-pool-config.py            # plan
    AWS_PROFILE=infra .venv/bin/python scripts/prog-pool-config.py --apply    # write
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

TABLE = "ryangrey-greybot"
KEY = {"pk": {"S": "TENANT#946663011991556117"}, "sk": {"S": "CONFIG"}}
WANT = {"wclReportTitle": "", "wclReportOwnerId": "", "raidDays": "tue,thu",
        "wclExcludeOwnerIds": "40245"}
STAMP = "sourceChangedAt"


def text(item, name):
    return ((item or {}).get(name) or {}).get("S", "")


def run(ddb, table=TABLE, apply=False, backup_dir=None, now=None):
    now = now or datetime.now(timezone.utc)
    row = ddb.get_item(TableName=table, Key=KEY, ConsistentRead=True).get("Item")
    if not row:
        sys.exit("STOP: no CONFIG row for the server-wide install")
    if text(row, "teamSlug") != "prog-raid":
        # Pooling without the team scope would read guild-wide logs into the guild partition.
        sys.exit(f"STOP: teamSlug is {text(row, 'teamSlug')!r}, not 'prog-raid'")
    for name, value in WANT.items():
        print(f"{name:<20} {text(row, name)!r:<14} -> {value!r}")
    if not apply:
        print("dry run: nothing written (pass --apply to write)")
        return 0

    backup_dir = Path(backup_dir or Path.home() / ".local/share/greybot-state-backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"prog-raid-config-{now:%Y%m%d-%H%M%S}.json"
    backup.write_text(json.dumps({"table": table, "at": now.isoformat(), "config": row},
                                 indent=1, ensure_ascii=False))
    print(f"backup: {backup}")

    names = {f"#a{i}": n for i, n in enumerate(WANT)}
    values = {f":v{i}": {"S": v} for i, v in enumerate(WANT.values())}
    names["#stamp"], values[":stamp"] = STAMP, {"S": now.isoformat()}
    values[":slug"] = {"S": "prog-raid"}
    ddb.update_item(
        TableName=table, Key=KEY,
        UpdateExpression="SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(WANT)))
                         + ", #stamp = :stamp",
        ConditionExpression="teamSlug = :slug",
        ExpressionAttributeNames=names, ExpressionAttributeValues=values)

    back = ddb.get_item(TableName=table, Key=KEY, ConsistentRead=True).get("Item") or {}
    changed = all(text(back, n) == v for n, v in WANT.items())
    others = ({k: v for k, v in back.items() if k not in WANT and k != STAMP}
              == {k: v for k, v in row.items() if k not in WANT and k != STAMP})
    for name in WANT:
        print(f"read back {name:<20} {text(back, name)!r}")
    print("four fields set:", changed, "| every other attribute unchanged:", others)
    return 0 if changed and others else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write; without it, plan only")
    ap.add_argument("--table", default=TABLE)
    ap.add_argument("--backup-dir", default=None)
    args = ap.parse_args()
    import boto3
    ddb = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "infra"),
                        region_name="us-east-1").client("dynamodb")
    return run(ddb, args.table, args.apply, args.backup_dir)


if __name__ == "__main__":
    sys.exit(main())
