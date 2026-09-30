"""Independent Mythic+ evidence for the vault. Never sum counts across providers.

Public providers are incomplete: a successful empty response means zero observed runs,
whereas a failed request means unavailable. All comparisons use one character and reset
window. Warcraft Logs discovery includes character reports, not just guild uploads.
"""
from concurrent.futures import ThreadPoolExecutor
import re
import urllib.parse

import vault
import wcl

SOURCES = ("Raider.IO", "Blizzard", "Warcraft Logs")


def best_source(candidates):
    """Most +10 slots, then qualifying runs, then stable provider order. No mixed slots."""
    available = [(name, sorted(levels, reverse=True)) for name in SOURCES
                 if (levels := candidates.get(name)) is not None]
    if not available:
        return None, []
    return max(available, key=lambda pair: (
        vault.mplus_slots(pair[1])[0],
        sum(level >= vault.MPLUS_LEVEL for level in pair[1]),
        -SOURCES.index(pair[0])))


def _realm(value):
    return re.sub(r"[^\w]", "", vault.fold(value))


def identity(name, realm):
    return vault.fold(name), _realm(realm)


def _parallel(items, fn):
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(fn, items))


def blizzard_levels(documents, start, end):
    lo, hi = start.timestamp() * 1000, end.timestamp() * 1000
    runs = {}
    for document in documents:
        for run in document.get("best_runs") or []:
            try:
                at, level = int(run["completed_timestamp"]), int(run["keystone_level"])
                dungeon = int(run["dungeon"]["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if lo <= at < hi and level > 0:
                runs[(dungeon, at)] = max(level, runs.get((dungeon, at), 0))
    return sorted(runs.values(), reverse=True)


def fetch_blizzard(profiles, token, get, start, end):
    """Read current-period and season best runs, discovering seasons from their dates."""
    seasons = []
    try:
        index = get(token, "/data/wow/mythic-keystone/season/index", namespace="dynamic-us")
        for sid in sorted({s["id"] for s in index.get("seasons") or []}, reverse=True):
            season = get(token, f"/data/wow/mythic-keystone/season/{sid}", namespace="dynamic-us")
            began = season["start_timestamp"]
            ended = season.get("end_timestamp", float("inf"))
            if began < end.timestamp() * 1000 and ended > start.timestamp() * 1000:
                seasons.append(sid)
            if began <= start.timestamp() * 1000:
                break
    except Exception as exc:
        vault.log("vault_blizzard_seasons_unavailable", error=type(exc).__name__)

    def fetch(item):
        key, profile = item
        realm = vault.raiderio.slugify(str(profile["realm"]).replace("'", ""))
        name = urllib.parse.quote(str(profile["name"]).lower())
        path = f"/profile/wow/character/{realm}/{name}/mythic-keystone-profile"
        docs = []
        for suffix in [""] + [f"/season/{sid}" for sid in seasons]:
            try:
                data = get(token, path + suffix, namespace="profile-us")
                # Never accept a transfer/identity mismatch as another character's evidence.
                char = data.get("character") or {}
                if identity(char.get("name"), (char.get("realm") or {}).get("slug")) != identity(
                        profile["name"], profile["realm"]):
                    raise ValueError("character mismatch")
                docs.append(data if suffix else data.get("current_period") or {})
            except Exception as exc:
                vault.log("vault_blizzard_keys_unavailable", character=profile["name"],
                          error=type(exc).__name__)
        return key, blizzard_levels(docs, start, end) if docs else None
    return _parallel(profiles.items(), fetch)


CHARACTER_REPORTS = """query($name: String!, $realm: String!, $region: String!, $page: Int!) {
  characterData { character(name: $name, serverSlug: $realm, serverRegion: $region) {
    recentReports(limit: 100, page: $page) {
      has_more_pages data { code startTime endTime }
    }
  }}
}"""
KEY_REPORT = """query($code: String!) { reportData { report(code: $code) {
  code startTime
  masterData { actors(type: "Player") { id name server } }
  fights { id encounterID startTime endTime kill inProgress
           keystoneLevel keystoneTime friendlyPlayers }
}}}"""


def wcl_levels(reports, profile, start, end):
    """Completed full keys only; deduplicate overlapping uploads of the same party/run."""
    lo, hi = start.timestamp() * 1000, end.timestamp() * 1000
    wanted = identity(profile["name"], profile["realm"])
    runs = []
    for report in reports:
        actors = {a["id"]: identity(a.get("name"), a.get("server"))
                  for a in (report.get("masterData") or {}).get("actors") or []}
        for fight in report.get("fights") or []:
            try:
                level = int(fight.get("keystoneLevel") or 0)
                at = int(report["startTime"] + fight["endTime"])
                party = frozenset(actors[p] for p in fight.get("friendlyPlayers") or [] if p in actors)
                encounter = int(fight["encounterID"])
            except (KeyError, TypeError, ValueError):
                continue
            if (level <= 0 or not encounter or fight.get("kill") is not True
                    or fight.get("inProgress") is not False or not fight.get("keystoneTime")
                    or wanted not in party or not lo <= at < hi):
                continue
            # Different loggers' clocks can differ slightly; repeated keys are not duplicates.
            if not any(encounter == e and level == lv and abs(at - t) <= 60_000
                       for e, lv, p, t in runs):
                runs.append((encounter, level, party, at))
    return sorted((level for _, level, _, _ in runs), reverse=True)


def fetch_wcl(profiles, token, start, end, region="us", query=wcl.query, extra_codes=()):
    lo, hi = start.timestamp() * 1000, end.timestamp() * 1000

    def discover(item):
        key, profile = item
        codes, successful = set(extra_codes), False
        try:
            # Bounded paging keeps unusually active accounts within the recap runtime.
            for page in range(1, 6):
                data = query(token, CHARACTER_REPORTS, {
                    "name": profile["name"], "realm": vault.raiderio.slugify(profile["realm"]),
                    "region": region, "page": page})
                char = (data.get("characterData") or {}).get("character")
                if char is None:
                    break
                successful = True
                batch = char.get("recentReports") or {}
                reports = batch.get("data") or []
                codes.update(r["code"] for r in reports
                             if r.get("startTime", hi) < hi and r.get("endTime", 0) >= lo)
                if (not batch.get("has_more_pages") or not reports
                        or max(r.get("endTime", 0) for r in reports) < lo):
                    break
            else:
                vault.log("vault_wcl_page_limit", character=profile["name"])
        except Exception as exc:
            vault.log("vault_wcl_keys_unavailable", character=profile["name"], error=type(exc).__name__)
        return key, (codes, successful)

    found = _parallel(profiles.items(), discover)

    def detail(code):
        try:
            data = query(token, KEY_REPORT, {"code": code})
            return code, (data.get("reportData") or {}).get("report")
        except Exception as exc:
            vault.log("vault_wcl_key_report_unavailable", error=type(exc).__name__)
            return code, None

    reports = _parallel(sorted(set().union(*(codes for codes, _ in found.values()))), detail)
    return {key: (wcl_levels([reports[c] for c in codes if reports.get(c)], profiles[key], start, end)
                  if successful or any(reports.get(c) for c in codes) else None)
            for key, (codes, successful) in found.items()}
