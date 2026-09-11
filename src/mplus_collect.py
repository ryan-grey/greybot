"""Bounded, resumable Raider.IO collection; frequent polls retain observed run history."""
from datetime import timedelta
import re
import time

import mplus
import raiderio

API = "https://raider.io/api/v1/"


def fetch(path, **params):
    return raiderio._get(API + path, params, timeout=8)


def collect(repo, cfg, now, budget=35, source=fetch):
    owner = repo.lease()
    if not owner:
        return {"skipped":"collector_busy"}
    started = time.monotonic()
    try:
        roster = repo.get("ROSTER")
        if not roster or (now - mplus.stamp(roster["at"])).total_seconds() > 3600:
            data = source("guilds/profile", region=cfg["guild_region"], realm=cfg["guild_realm"],
                          name=cfg["guild_name"], fields="members")
            members = sorted((mplus.person(x["character"]) for x in data.get("members",[])), key=lambda p:p["key"])
            if not members:
                raise ValueError("Guild roster unavailable; do not classify runs")
            roster = {"at":now.isoformat(), "members":members}
            repo.put("ROSTER", roster)
        meta = repo.get("COLLECTOR") or {"cursor":0,"first_observed":now.isoformat()}
        members = roster["members"]
        guild_keys = {p["key"] for p in members}
        period = mplus.week_window(now)[1].astimezone(mplus.EASTERN) + timedelta(days=7)
        count, runs, errors = 0, 0, 0
        cursor = int(meta.get("cursor",0)) % len(members)
        while count < min(24, len(members)) and time.monotonic() - started < budget:
            char = members[cursor]
            region, realm, name = char["key"].split("/",2)
            try:
                profile = source("characters/profile", region=region, realm=realm, name=name,
                    fields="mythic_plus_scores_by_season:current,mythic_plus_recent_runs,mythic_plus_weekly_highest_level_runs,mythic_plus_best_runs")
                scores = profile.get("mythic_plus_scores_by_season",[])
                if scores:
                    repo.put(f'SCORE#{period.date()}#{char["key"]}', {"key":char["key"],"at":now.isoformat(),
                        "season":scores[0]["season"],"score":scores[0]["scores"]["all"],
                        "source_at":profile.get("last_crawled_at")})
                refs = {}
                for field in ("mythic_plus_recent_runs","mythic_plus_weekly_highest_level_runs","mythic_plus_best_runs"):
                    for run in profile.get(field,[]):
                        match = re.search(r"/mythic-plus-runs/(season-[a-z0-9-]+)/(\d+)", run.get("url", ""))
                        if match and mplus.stamp(run["completed_at"]) >= now - timedelta(days=15):
                            refs["/".join(match.groups())] = match.groups()
                complete = True
                for identity, (season, run_id) in refs.items():
                    if repo.get("SEEN#"+identity):
                        continue
                    if time.monotonic() - started >= budget:
                        complete = False
                        break
                    raw = source("mythic-plus/run-details", season=season, id=run_id)
                    run = mplus.normalize_run(raw, guild_keys, now)
                    # Include even ineligible runs in the seen ledger, never in totals.
                    if len(run["guild_members"]) >= 2:
                        repo.put("RUN#"+run["completed"][:10]+"#"+identity, run, once=True)
                        runs += 1
                    repo.put("SEEN#"+identity,{"id":identity,"at":now.isoformat()}, once=True)
                if not complete:
                    break  # Resume this character rather than losing its pending runs.
            except (raiderio.RaiderIOError, TimeoutError, ValueError, KeyError, TypeError):
                errors += 1
                # Record an observable coverage issue without credentials or raw payloads.
                repo.put("ERROR#"+now.date().isoformat()+"#"+char["key"], {"key":char["key"],"at":now.isoformat()})
            cursor = (cursor + 1) % len(members)
            count += 1
        repo.put("COLLECTOR", {**meta,"cursor":cursor,"at":now.isoformat(),"profiles":count,"errors":errors})
        return {"profiles":count,"new_runs":runs,"errors":errors,"roster_size":len(members)}
    finally:
        repo.release(owner)


def weekly_data(repo, now):
    start, end = mplus.week_window(now)
    # Query only date prefixes in the requested UTC interval, never scan the table.
    runs, day = [], start.date()
    while day <= end.date():
        runs.extend(repo.prefix("RUN#"+day.isoformat()+"#"))
        day += timedelta(days=1)
    first = {r["key"]:r for r in repo.prefix(f"SCORE#{start.date()}#")}
    last = {r["key"]:r for r in repo.prefix(f"SCORE#{end.date()}#")}
    snapshots = {}
    for key in first.keys() & last.keys():
        a,b=first[key],last[key]
        # A missing or stale boundary sample is unknown, not a zero score.
        if not all(0 <= (boundary-mplus.stamp(row["at"])).total_seconds() <= 3600
                   for boundary,row in ((start,a),(end,b))):
            continue
        snapshots[key]={"start":a["score"],"end":b["score"],"season_start":a["season"],"season_end":b["season"]}
    result = mplus.summarize(runs,snapshots,start,end)
    meta=repo.get("COLLECTOR") or {}
    result["coverage"] += " Collection began " + str(meta.get("first_observed","not yet")) + "."
    result["coverage"] += " Scores are the latest API observations before each boundary, within one hour; upstream updates can lag."
    result["collector_at"] = meta.get("at")
    return result
