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
    ("overall", "Highest overall IO"),
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


def season_week(seasons, region, start, end):
    """Label the reporting week from the region's main-season launch date.

    The report cutoff is 10am Eastern, before the usual US season opening time.
    Date arithmetic therefore labels the launch Tuesday's report interval Week 1;
    elapsed UTC hours would produce an off-by-one (and can drift at DST).
    """
    candidates=[s for s in seasons if s.get('is_main_season') and s.get('starts',{}).get(region)
                and stamp(s['starts'][region]) < end
                and (not s.get('ends',{}).get(region) or stamp(s['ends'][region]) > start)]
    if not candidates:
        raise ValueError('No main Mythic+ season covers this reporting week')
    season=max(candidates,key=lambda s:stamp(s['starts'][region]))
    launch=stamp(season['starts'][region])
    week=max(1,(start.astimezone(EASTERN).date()-launch.astimezone(EASTERN).date()).days//7+1)
    name=season['name'].replace('MN Season','Midnight Season').split(' • ')[0]
    return {'slug':season['slug'],'name':name,'week':week,'starts':launch.isoformat(),'region':region}


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
    result = {"key": key, "name": str(character["name"]),
            "class": cls.get("name", "") if isinstance(cls, dict) else cls,
            "server": realm.get("name", "") if isinstance(realm, dict) else realm}
    result['role']=class_role(result['class'])
    return result


def class_role(klass):
    """Only classes whose specializations all deal damage have a safe fallback."""
    return 'dps' if klass in ('Mage','Rogue','Hunter','Warlock') else None


def score_role(scores, klass):
    """Highest positive role IO; ties prefer damage, tank, then healer."""
    valid = {}
    for role in ('dps', 'tank', 'healer'):
        try:
            value = float((scores or {}).get(role, 0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            valid[role] = value
    return max(valid, key=valid.get) if valid else class_role(klass)


def run_person(character):
    result=person(character)
    spec=character.get('spec') or {}
    role=spec.get('role') if isinstance(spec,dict) else None
    if role in ('tank','healer','dps'):
        result.update(role=role,role_source='run_spec',spec=spec.get('name'))
    return result


def record_person(run, key):
    return next(p for p in run['roster'] if p['key']==key)


def normalize_run(raw, guild_members, observed_at):
    """Require complete, distinct five-character rosters; never guess missing slots."""
    if raw.get("status") != "finished" or raw.get("deleted_at") or raw.get("isTournamentProfile"):
        raise ValueError("Not a finished live run")
    season = str(raw.get("season", ""))
    if not re.fullmatch(r"season-[a-z0-9-]+", season):
        raise ValueError("Invalid season")
    roster = [run_person(row["character"]) for row in raw.get("roster", [])]
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
    unique={}
    for row in rows:unique.setdefault(row['key'],row)
    rows[:]=unique.values()
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


def summarize(runs, snapshots, start, end, overall_scores=None, role_scores=None):
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
    def aggregate_role(p):
        return score_role((role_scores or {}).get(p['key']), p['class'])
    best = {}
    for r in timed:
        for key in r["guild_members"]:
            previous = best.get(key)
            if previous is None or (r["level"], -r["elapsed_ms"] / r["timer_ms"], r["id"]) > (
                    previous["level"], -previous["elapsed_ms"] / previous["timer_ms"], previous["id"]):
                best[key] = r
    boards["highest"] = _rank([{**record_person(r,k), "value": r["level"], "detail": r["dungeon"],
                                "run": r["id"], "url": r["url"]} for k, r in best.items()], "value")
    for category, selected in (("ten", [r for r in timed if r["level"] >= 10]),
                               ("guild_timed", [r for r in timed if len(r["guild_members"]) == 5])):
        counts = Counter(k for r in selected for k in r["guild_members"])
        boards[category] = _rank([{**people[k], "role":aggregate_role(people[k]), "value": n, "detail": "timed runs"} for k, n in counts.items()], "value")
    all_guild = [r for r in timed if len(r["guild_members"]) == 5]
    guild_best={}
    for r in all_guild:
        for key in r['guild_members']:
            previous=guild_best.get(key)
            if previous is None or (r['level'],-r['elapsed_ms']/r['timer_ms'],r['id']) > (previous['level'],-previous['elapsed_ms']/previous['timer_ms'],previous['id']):
                guild_best[key]=r
    boards["guild_highest"] = _rank([{**record_person(r,key), "value":r["level"],
        "detail":r['dungeon'], "url":r["url"], "roster":r["roster"]}
        for key,r in guild_best.items()], "value")
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
            boards["score"].append({**p, "role":aggregate_role(p), "value": gain, "detail": f"{first:,.1f} → {last:,.1f}"})
    _rank(boards["score"], "value")
    for person in overall_scores or []:
        value=float(person['score'])
        if math.isfinite(value) and value >= 0:
            boards['overall'].append({**person,'role':aggregate_role(person),'value':value,'detail':'Overall season IO'})
    _rank(boards['overall'],'value')
    return {"start": start.isoformat(), "end": end.isoformat(), "boards": boards,
            "runs": sorted(eligible, key=lambda r:r["completed"], reverse=True),
            "timed_count":len(timed), "guild_count":len(all_guild), "members":len(people),
            "score_unavailable":unavailable,
            "coverage": "Observed runs; source APIs can omit runs or update late. Guild membership is captured when a run is first collected.",
            "score_note": "Weekly IO gain requires a 2+ guild-member run and comparable weekly snapshots; includes score earned in other groups. Highest overall IO includes all guild characters with a fresh end-of-week score, regardless of group participation. Missing or cross-season scores are not ranked.",
            "ranking_note": "One entry per character per category; alts are separate. Positions are consecutive and unique, with names ordering equal values. The card shows three entries per category and the full page shows at most 20 per category."}


def display_value(category, row):
    if category in ("highest", "guild_highest"):
        return f'+{row["value"]}'
    if category == "score":
        return f'+{row["value"]:,.1f}'
    if category == "overall":
        return f'{row["value"]:,.1f}'
    return str(row["value"])
