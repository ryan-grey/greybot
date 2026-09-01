#!/usr/bin/env python3
"""Migrate a single-tenant table to the Phase 2 layout.

    GUILD#<region>#<realm>#<name>   ->   WOW#<region>#<realm>#<name>   (shared)
                                    +    TENANT#<discord_guild_id>     (this install)

Dry run by default. Nothing is written without --apply.

    ./scripts/migrate-to-tenants.py --table ryangrey-greybot-dev \
        --guild us/proudmoore/Scrambled --tenant 1544178250518102016 --channel <id>
    ./scripts/migrate-to-tenants.py ... --apply

---------------------------------------------------------------------------
Three things this deliberately does NOT do
---------------------------------------------------------------------------

**It does not delete the old rows.** Copy, verify, and leave the originals in
place. The whole risk of this migration is the announced set: a lost member is a
boss kill re-announced into a live channel, and there is no undo for that. The
old rows cost pennies and are the rollback. Delete them in a separate pass, once
a poll has run clean on the new layout -- `--delete-old` does that and nothing
else.

**It does not run as the Lambda role.** That role has no Query and no
DeleteItem, on purpose. Migration is an operator task; run it with
AWS_PROFILE=infra.

**It is not clever about conflicts.** Every write is conditional on the target
not already existing, so a re-run reports "already there" rather than
overwriting. Re-running after a partial failure is safe; re-running to "fix" a
row is not, and will tell you so.
"""

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError


def parse_guild(text):
    parts = [p for p in str(text).split("/") if p]
    if len(parts) != 3:
        raise SystemExit(f"--guild must be region/realm/Name, got {text!r}")
    region, realm, name = parts
    return region.lower(), realm.lower(), name


def classify(sk):
    """Where one old row goes. Returns (namespace, new_sk) or None to split.

    'shared' means a fact about the WoW guild that every install tracking it sees
    identically. 'tenant' means a record of what this install did. The split
    entry is TIER#, which is both.
    """
    if sk.startswith("TIER#"):
        return ("split", sk)
    if sk.startswith("KILL#") or sk.startswith("ROSTER#"):
        return ("shared", sk)
    return {
        "PROGRESS": ("shared", "PROGRESS"),
        "SOURCE": ("shared", "SOURCE"),
        "BOOTSTRAP": ("tenant", "BOOTSTRAP"),
        "HEALTH": ("tenant", "HEALTH"),
        "RECAPS": ("tenant", "RECAPS"),
    }.get(sk)


# Which attributes of a TIER# row belong to which side. Everything here is
# explicit rather than "whatever is left", because a new attribute appearing on
# that row later must fail loudly rather than land silently on the wrong side.
TIER_SHARED = {"baseline", "raidName", "seededAt", "updatedAt"}
TIER_TENANT = {"announced", "aotcAnnounced", "seedSize", "seededAt"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True)
    ap.add_argument("--guild", required=True, help="region/realm/Name")
    ap.add_argument("--tenant", required=True, help="Discord guild id (snowflake)")
    ap.add_argument("--channel", default="", help="channel id for the CONFIG row")
    ap.add_argument("--role", default="", help="optional mention role id")
    ap.add_argument("--region-aws", default="us-east-1")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--delete-old", action="store_true",
                    help="second pass: remove the migrated GUILD# rows")
    ap.add_argument("--keep-webhook", action="store_true",
                    help=("migrate with no channel, so this install keeps posting "
                          "through the operator webhook in SSM. This is what the "
                          "original single-tenant install wants: handler.destination() "
                          "falls back to the webhook when a CONFIG row has no channel, "
                          "so migrating does not change where anything posts."))
    args = ap.parse_args()

    region, realm, name = parse_guild(args.guild)
    if not str(args.tenant).isdigit():
        raise SystemExit(f"--tenant must be a snowflake, got {args.tenant!r}")

    old_pk = f"GUILD#{region}#{realm}#{name.lower()}"
    wow_pk = f"WOW#{region}#{realm}#{name.lower()}"
    tenant_pk = f"TENANT#{args.tenant}"

    ddb = boto3.client("dynamodb", region_name=args.region_aws)

    print(f"table   {args.table}")
    print(f"from    {old_pk}")
    print(f"to      {wow_pk}")
    print(f"        {tenant_pk}")
    print(f"mode    {'APPLY' if args.apply else 'dry run'}\n")

    rows = ddb.query(TableName=args.table,
                     KeyConditionExpression="pk = :p",
                     ExpressionAttributeValues={":p": {"S": old_pk}},
                     ConsistentRead=True).get("Items", [])
    if not rows:
        print(f"No rows under {old_pk}. Nothing to migrate.")
        return 0
    print(f"{len(rows)} row(s) to migrate\n")

    planned, unknown = [], []
    for item in rows:
        sk = item["sk"]["S"]
        where = classify(sk)
        if where is None:
            unknown.append(sk)
            continue
        kind, new_sk = where
        attrs = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        if kind == "split":
            shared = {k: v for k, v in attrs.items() if k in TIER_SHARED}
            tenant = {k: v for k, v in attrs.items() if k in TIER_TENANT}
            stray = set(attrs) - TIER_SHARED - TIER_TENANT
            if stray:
                unknown.append(f"{sk} has unclassified attributes: {sorted(stray)}")
                continue
            planned.append((wow_pk, new_sk, shared))
            planned.append((tenant_pk, "ANNOUNCED#" + sk[len("TIER#"):], tenant))
        else:
            planned.append((wow_pk if kind == "shared" else tenant_pk, new_sk, attrs))

    if unknown:
        # Refuse rather than guess. A row whose home is unknown is exactly the
        # row that would be silently dropped or silently shared.
        print("REFUSING TO MIGRATE — unclassified rows or attributes:")
        for u in unknown:
            print(f"  {u}")
        print("\nAdd them to classify()/TIER_* above, with a reason, and re-run.")
        return 1

    for pk, sk, attrs in planned:
        ann = ""
        if sk.startswith("ANNOUNCED#"):
            n = len((attrs.get("announced") or {}).get("SS") or [])
            ann = f"   [{n} announced boss(es)]"
        print(f"  {pk:<36} {sk}{ann}")

    cfg_item = {
        "pk": {"S": tenant_pk}, "sk": {"S": "CONFIG"},
        "guildRegion": {"S": region}, "guildRealm": {"S": realm},
        "guildName": {"S": name}, "channelId": {"S": args.channel},
        "progRoleId": {"S": args.role},
        "configuredAt": {"S": "migrated"}, "configuredBy": {"S": "migration"}}
    print(f"\n  {tenant_pk:<36} CONFIG   (guild={name}, channel={args.channel or '(none)'})")
    print(f"  REGISTRY                             TENANTS  += {tenant_pk}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    if not args.channel and not args.keep_webhook:
        raise SystemExit("\n--channel is required with --apply: a tenant with no "
                         "channel is an install that cannot post.\n"
                         "Pass --keep-webhook if this install should keep posting "
                         "through the operator webhook in SSM.")

    wrote, existed = 0, 0
    for pk, sk, attrs in planned:
        item = {"pk": {"S": pk}, "sk": {"S": sk}, **attrs}
        try:
            ddb.put_item(TableName=args.table, Item=item,
                         ConditionExpression="attribute_not_exists(pk)")
            wrote += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            existed += 1
            print(f"  already present, left alone: {pk} {sk}")

    ddb.put_item(TableName=args.table, Item=cfg_item)
    ddb.update_item(TableName=args.table,
                    Key={"pk": {"S": "REGISTRY"}, "sk": {"S": "TENANTS"}},
                    UpdateExpression="ADD tenants :t",
                    ExpressionAttributeValues={":t": {"SS": [tenant_pk]}})
    print(f"\nwrote {wrote}, already present {existed}, plus CONFIG and registry")

    # ---- verify, and make the announced sets the thing that is checked ----
    print("\nVerifying...")
    ok = True
    for item in rows:
        sk = item["sk"]["S"]
        if not sk.startswith("TIER#"):
            continue
        slug = sk[len("TIER#"):]
        before = set((item.get("announced") or {}).get("SS") or [])
        got = ddb.get_item(TableName=args.table,
                           Key={"pk": {"S": tenant_pk},
                                "sk": {"S": "ANNOUNCED#" + slug}},
                           ConsistentRead=True).get("Item") or {}
        after = set((got.get("announced") or {}).get("SS") or [])
        same = before == after
        ok = ok and same
        print(f"  {'ok  ' if same else 'FAIL'} {slug}: {len(before)} -> {len(after)} announced")
        if not same:
            print(f"       missing: {sorted(before - after)}")
            print(f"       extra:   {sorted(after - before)}")

    if not ok:
        print("\nVERIFICATION FAILED. The old rows are untouched — do not delete them.")
        return 1
    print("\nEvery announced set matches. Old rows left in place as the rollback.")

    if args.delete_old:
        print("\nDeleting old rows...")
        for item in rows:
            ddb.delete_item(TableName=args.table,
                            Key={"pk": item["pk"], "sk": item["sk"]})
            print(f"  deleted {old_pk} {item['sk']['S']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
