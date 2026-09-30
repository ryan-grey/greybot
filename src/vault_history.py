"""Private weekly run evidence, separate by character, reset and provider.

Leaderboard collection/persistent observation approach adapted from wowaudit/core,
MIT Copyright (c) 2015 Vincent Wong. See assets/LICENSE-wowaudit.txt.
No counts are ever added across providers; only distinct runs within one provider.
"""
import hashlib
import json
import os
import time
from datetime import timedelta

from botocore.exceptions import ClientError

import store
import vault
import vault_sources as sources

RETENTION_DAYS = 90


def merge_runs(old, new, source, start, end):
    """Monotonic union. Timestamp rounding matches Blizzard's leaderboard/profile APIs.

    WCL upload clocks can differ by a minute. RIO run IDs take precedence across snapshots.
    Ambiguous evidence is conservatively deduplicated, never treated as another completion.
    """
    out = []
    lo, hi = int(start.timestamp()*1000), int(end.timestamp()*1000)
    for run in sorted(old + new, key=lambda r: (r["at"], str(r["dungeon"]))):
        at, level, dungeon = int(run["at"]), int(run["level"]), str(run["dungeon"])
        if not lo <= at < hi or level < 1:
            continue
        ids = set(run.get("ids") or [])
        match = next((r for r in out if
                      (ids and ids.intersection(r.get("ids") or [])) or
                      (r["dungeon"] == dungeon and (
                          (source == "Warcraft Logs" and r["level"] == level
                           and abs(r["at"] - at) <= 60_000) or
                          (source != "Warcraft Logs" and r["at"]//1000 == at//1000)))), None)
        if match is None:
            out.append({"at": at, "level": level, "dungeon": dungeon, "ids": sorted(ids)})
        else:
            match["level"] = max(match["level"], level)
            match["ids"] = sorted(set(match.get("ids") or []) | ids)
    return out


def history_key(scope, profile, region, end):
    ident = [region.lower(), *sources.identity(profile["name"], profile["realm"])]
    digest = hashlib.sha256(json.dumps(ident, ensure_ascii=False).encode()).hexdigest()
    return {"pk": {"S": scope.tenant + "#" + digest},
            "sk": {"S": "WEEK#" + end.date().isoformat()}}


def reconcile(scope, profile, region, start, end, observed, *, persist=False, ddb=None,
              table=None):
    """Optimistic writes prevent overlapping collectors from losing observations.

    Dry runs read history but do not write it. Expired rows are ignored even before TTL
    physically removes them. Missing provider evidence never erases stored evidence.
    """
    ddb = ddb or store.ddb
    table = table or os.environ.get("VAULT_HISTORY_TABLE")
    key = history_key(scope, profile, region, end)
    for attempt in range(5):
        item = (ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
                if table else None)
        old = json.loads(item["evidence"]["S"]) if item else {}
        if item and int(item["expiresAt"]["N"]) <= time.time():
            old = {}
        combined = {}
        for source in sources.SOURCES:
            previous = old.get(source)
            incoming = observed.get(source)
            combined[source] = (merge_runs(previous or [], incoming or [], source, start, end)
                                if previous is not None or incoming is not None else None)
        if not table or not persist:
            return combined
        encoded = json.dumps(combined, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > 300_000:
            raise ValueError("vault history item exceeds safe size; evidence was not truncated")
        version = int(item["version"]["N"]) if item else 0
        values = {**key, "evidence": {"S": encoded}, "version": {"N": str(version + 1)},
                  "expiresAt": {"N": str(int((end + timedelta(days=RETENTION_DAYS)).timestamp()))},
                  "updatedAt": {"N": str(int(time.time()))}}
        try:
            ddb.put_item(TableName=table, Item=values,
                         ConditionExpression="version = :v" if item else "attribute_not_exists(pk)",
                         **({"ExpressionAttributeValues": {":v": {"N": str(version)}}} if item else {}))
            return combined
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise RuntimeError("vault history changed repeatedly; refusing a lost update")


def levels(evidence):
    return {source: sorted((r["level"] for r in runs), reverse=True) if runs is not None else None
            for source, runs in evidence.items()}


def for_profiles(scope, profiles, region, start, end, blizzard, wcl, *, persist=False):
    return {key: levels(reconcile(scope, profile, region, start, end, {
                "Raider.IO": None if profile.get("rio_unavailable") else vault.mplus_runs(profile, start, end),
                "Blizzard": blizzard.get(key), "Warcraft Logs": wcl.get(key)}, persist=persist))
            for key, profile in profiles.items()}
