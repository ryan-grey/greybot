"""Weekly Mythic+ eligibility and leaderboards, independent of API/storage/rendering."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
CATEGORIES = (
    ("highest", "Highest timed key"),
    ("score", "Weekly IO gain"),
    ("timed", "Timed runs"),
    ("ten", "Timed +10 runs"),
    ("guild_highest", "All-guild highest"),
    ("guild_timed", "All-guild runs"),
)


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A source timestamp must include its timezone")
    return result.astimezone(timezone.utc)


def week_window(now):
    """Last completed Tuesday 10am-to-Tuesday 10am Eastern reporting week."""
    if now.tzinfo is None:
        raise ValueError("An aware current time is required")
    local = now.astimezone(EASTERN)
    end = (local - timedelta(days=(local.weekday() - 1) % 7)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    if local < end:
        end -= timedelta(days=7)
    return (end - timedelta(days=7)).astimezone(timezone.utc), end.astimezone(timezone.utc)


def character_key(character):
    def value(field):
        raw = character.get(field, "")
        return str(raw.get("slug") or raw.get("name") or "") if isinstance(raw, dict) else str(raw)
    region, realm, name = value("region"), value("realm"), value("name")
    if not all((region, realm, name)):
        raise ValueError("Incomplete character identity")
    return "/".join((region.casefold(), realm.casefold().replace(" ", "-").replace("'", ""), name.casefold()))


def person(character):
    key = character_key(character)
    cls = character.get("class", "")
    realm = character.get("realm", "")
    return {"key": key, "name": str(character["name"]),
            "class": cls.get("name", "") if isinstance(cls, dict) else cls,
            "server": realm.get("name", "") if isinstance(realm, dict) else realm}


def normalize_run(raw, guild_members, observed_at):
    """Require complete, distinct five-character rosters; never guess missing slots."""
    if raw.get("status") != "finished" or raw.get("deleted_at") or raw.get("isTournamentProfile"):
        raise ValueError("Not a finished live run")
    season = str(raw.get("season", ""))
    if not re.fullmatch(r"season-[a-z0-9-]+", season):
        raise ValueError("Invalid season")
    roster = [person(row["character"]) for row in raw.get("roster", [])]
    if len(roster) != 5 or len({p["key"] for p in roster}) != 5:
        raise ValueError("Incomplete or duplicate roster")
    members = sorted({p["key"] for p in roster} & set(guild_members))
    run_id = int(raw["keystone_run_id"])
    level, elapsed, timer = int(raw["mythic_level"]), int(raw["clear_time_ms"]), int(raw["keystone_time_ms"])
    if min(run_id, level, elapsed, timer) <= 0:
        raise ValueError("Invalid run timing or level")
    completed = stamp(raw["completed_at"])
    if completed > observed_at:
        raise ValueError("Run completes in the future")
    return {"id": f"{season}/{run_id}", "season": season, "level": level,
            "completed": completed.isoformat(), "elapsed_ms": elapsed, "timer_ms": timer,
            "timed": elapsed <= timer, "dungeon": str(raw["dungeon"]["name"]),
            "roster": roster, "guild_members": members, "observed": observed_at.isoformat(),
            "url": f"https://raider.io/mythic-plus-runs/{season}/{run_id}"}


def _rank(rows, metric):
    rows.sort(key=lambda r: (-r[metric], r.get("name", "").casefold(), r["key"]))
    prior = None
    for i, row in enumerate(rows, 1):
        if row[metric] != prior:
            rank = i
        row["rank"] = rank
        prior = row[metric]
    return rows


def summarize(runs, snapshots, start, end):
    """snapshots contains explicit same-season start/end scores, not inferred zeros.

    Total IO delta is ranked only for characters with a qualifying guild run.
    Its score covers the character's overall IO, including other groups; run-count
    categories never include solo-guild-member groups. This distinction is visible.
    """
    if start >= end:
        raise ValueError("Empty reporting interval")
    # Earliest observation wins, freezing membership at discovery rather than today.
    unique = {}
    for run in sorted(runs, key=lambda r: r["observed"]):
        unique.setdefault(run["id"], run)
    eligible = [r for r in unique.values() if start <= stamp(r["completed"]) < end
                and len(set(r["guild_members"])) >= 2]
    timed = [r for r in eligible if r["timed"]]
    people = {p["key"]: p for r in eligible for p in r["roster"] if p["key"] in r["guild_members"]}
    boards = {key: [] for key, _ in CATEGORIES}
    best = {}
    for r in timed:
        for key in r["guild_members"]:
            previous = best.get(key)
            if previous is None or (r["level"], -r["elapsed_ms"] / r["timer_ms"], r["id"]) > (
                    previous["level"], -previous["elapsed_ms"] / previous["timer_ms"], previous["id"]):
                best[key] = r
    boards["highest"] = _rank([{**people[k], "value": r["level"], "detail": r["dungeon"],
                                "run": r["id"], "url": r["url"]} for k, r in best.items()], "value")
    for category, selected in (("timed", timed), ("ten", [r for r in timed if r["level"] >= 10]),
                               ("guild_timed", [r for r in timed if len(r["guild_members"]) == 5])):
        counts = Counter(k for r in selected for k in r["guild_members"])
        boards[category] = _rank([{**people[k], "value": n, "detail": "timed runs"} for k, n in counts.items()], "value")
    all_guild = [r for r in timed if len(r["guild_members"]) == 5]
    boards["guild_highest"] = _rank([{"key":r["id"], "name":r["dungeon"], "value":r["level"],
        "detail": ", ".join(p["name"] for p in r["roster"]), "url":r["url"], "roster":r["roster"]}
        for r in all_guild], "value")
    unavailable = 0
    for key, p in people.items():
        pair = snapshots.get(key, {})
        if (pair.get("start") is None or pair.get("end") is None
                or not pair.get("season_start") or pair.get("season_start") != pair.get("season_end")):
            unavailable += 1
            continue
        first, last = float(pair["start"]), float(pair["end"])
        if not all(math.isfinite(n) and n >= 0 for n in (first, last)):
            unavailable += 1
            continue
        gain = round(last - first, 1)
        if gain > 0:
            boards["score"].append({**p, "value": gain, "detail": f"{first:,.1f} → {last:,.1f}"})
    _rank(boards["score"], "value")
    return {"start": start.isoformat(), "end": end.isoformat(), "boards": boards,
            "runs": sorted(eligible, key=lambda r:r["completed"], reverse=True),
            "timed_count":len(timed), "guild_count":len(all_guild), "members":len(people),
            "score_unavailable":unavailable,
            "coverage": "Observed runs; source APIs can omit runs or update late. Guild membership is captured when a run is first collected.",
            "score_note": "Overall character IO change between weekly snapshots, among characters with a 2+ guild-member run; includes score earned in other groups. Missing or cross-season baselines are not ranked.",
            "ranking_note": "Character-based standings; alts are separate. Equal values share a rank, with names ordering ties; the card shows three entries per category and the full page shows everyone."}


def display_value(category, row):
    if category in ("highest", "guild_highest"):
        return f'+{row["value"]}'
    if category == "score":
        return f'+{row["value"]:,.1f}'
    return str(row["value"])
