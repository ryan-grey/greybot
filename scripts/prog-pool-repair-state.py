#!/usr/bin/env python3
"""One-off (2026-09-21): give the Prog Raid install the Venomous Abyss Heroic kills it
already has, BEFORE its report source is switched to the pooled guild logs.

Why: the 9/20 migration bootstrapped TENANT#<guild>#prog-raid through the old title+uploader
filter, which saw only one stray report, so its announced set holds 2 bosses (baseline 2).
The real history lives on the old guild partition: 7 bosses. Switch the source first and
the next Prog night re-announces five old kills as new, and the "n of 8" count goes wrong.

What it does:
  1. reads the old guild row and the prog-raid row (ANNOUNCED#the-venomous-abyss);
  2. refuses unless the guild row holds exactly the 7 expected bosses, the prog-raid row
     exists, holds nothing outside them and has not announced a clear;
  3. with --apply: writes a JSON backup of both rows, then ADDs the 7 bosses to the
     prog-raid set and sets baseline = seedSize = 7, so progress_count() reads 7 of 8;
  4. reads the row back and fails loudly unless it now says exactly that.

Dry by default: without --apply it only reads and prints the plan.

    AWS_PROFILE=infra .venv/bin/python scripts/prog-pool-repair-state.py            # plan
    AWS_PROFILE=infra .venv/bin/python scripts/prog-pool-repair-state.py --apply    # write
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

TABLE = "ryangrey-greybot"
GUILD = "946663011991556117"
SLUG = "the-venomous-abyss"
SRC = {"pk": {"S": f"TENANT#{GUILD}"}, "sk": {"S": f"ANNOUNCED#{SLUG}"}}
DST = {"pk": {"S": f"TENANT#{GUILD}#prog-raid"}, "sk": {"S": f"ANNOUNCED#{SLUG}"}}
EXPECTED = {"entombed sentinels", "nekzali the soulcoiler", "sszorak", "the coiled altar",
            "the lost explorers", "the twin fangs", "vashnik the malignant"}


def bosses(item):
    return set(((item or {}).get("announced") or {}).get("SS") or [])


def number(item, name):
    return int(((item or {}).get(name) or {}).get("N") or 0)


def run(ddb, table=TABLE, apply=False, backup_dir=None, now=None):
    now = now or datetime.now(timezone.utc)
    src = ddb.get_item(TableName=table, Key=SRC, ConsistentRead=True).get("Item")
    dst = ddb.get_item(TableName=table, Key=DST, ConsistentRead=True).get("Item")
    if bosses(src) != EXPECTED:
        sys.exit(f"STOP: the guild row does not hold the 7 expected bosses: {sorted(bosses(src))}")
    if not dst:
        sys.exit("STOP: the prog-raid row does not exist; has the partition been bootstrapped?")
    extra = bosses(dst) - EXPECTED
    if extra:
        sys.exit(f"STOP: the prog-raid row already holds bosses outside the 7: {sorted(extra)}")
    if ((dst.get("aotcAnnounced") or {}).get("BOOL")):
        sys.exit("STOP: the prog-raid row has already announced a clear; look before changing it")
    print(f"prog-raid now : {sorted(bosses(dst))} baseline={number(dst, 'baseline')} "
          f"seedSize={number(dst, 'seedSize')}")
    print(f"will become   : {sorted(EXPECTED)} baseline=7 seedSize=7")
    if not apply:
        print("dry run: nothing written (pass --apply to write)")
        return 0

    backup_dir = Path(backup_dir or Path.home() / ".local/share/greybot-state-backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"prog-raid-announced-{now:%Y%m%d-%H%M%S}.json"
    backup.write_text(json.dumps({"table": table, "at": now.isoformat(), "guildRow": src,
                                  "progRaidRow": dst}, indent=1, ensure_ascii=False))
    print(f"backup        : {backup}")

    ddb.update_item(
        TableName=table, Key=DST,
        UpdateExpression="SET baseline = :n, seedSize = :n, repairedAt = :t ADD announced :b",
        ConditionExpression=("attribute_exists(pk) AND "
                             "(attribute_not_exists(aotcAnnounced) OR aotcAnnounced = :f)"),
        ExpressionAttributeValues={":n": {"N": "7"}, ":t": {"S": now.isoformat()},
                                   ":b": {"SS": sorted(EXPECTED)}, ":f": {"BOOL": False}})

    back = ddb.get_item(TableName=table, Key=DST, ConsistentRead=True).get("Item")
    ok = (bosses(back) == EXPECTED and number(back, "baseline") == 7
          and number(back, "seedSize") == 7)
    print(f"read back     : {sorted(bosses(back))} baseline={number(back, 'baseline')} "
          f"seedSize={number(back, 'seedSize')} -> {'OK' if ok else 'MISMATCH'}")
    return 0 if ok else 1


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
