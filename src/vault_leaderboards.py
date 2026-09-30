"""Discover Blizzard periods/dungeons and collect only the tracked character identities.

Approach adapted from wowaudit/core/retrievers/keystones (MIT); see bundled license.
The API's leading groups are partial evidence, never a complete vault ledger.
"""
import re

import vault
import vault_sources as sources


def periods(token, get, start, end):
    index = get(token, "/data/wow/mythic-keystone/period/index", namespace="dynamic-us")
    found = []
    # Index includes years of history. Only fetch newest period documents until the
    # requested window is covered; IDs are discovered, not calculated from a reset epoch.
    for pid in sorted({int(p["id"]) for p in index["periods"]}, reverse=True):
        period = get(token, f"/data/wow/mythic-keystone/period/{pid}", namespace="dynamic-us")
        lo, hi = period["start_timestamp"], period["end_timestamp"]
        if lo < end.timestamp()*1000 and hi >= start.timestamp()*1000:
            found.append(pid)
        if lo <= start.timestamp()*1000:
            break
    return found


def extract(document, profiles, start, end):
    wanted = {sources.identity(p["name"], p["realm"]): key for key, p in profiles.items()}
    out = {key: [] for key in profiles}
    dungeon = document["map_challenge_mode_id"]
    for group in document["leading_groups"]:
        matching = set()
        for member in group.get("members") or []:
            char = member.get("profile") or {}
            ident = sources.identity(char.get("name"), (char.get("realm") or {}).get("slug"))
            if ident in wanted:
                matching.add(wanted[ident])
        # Blizzard documents only completed groups; untimed runs qualify too.
        runs = sources.blizzard_runs([{"best_runs": [{**group, "dungeon": {"id": dungeon}}]}], start, end)
        for key in matching:
            out[key].extend(runs)
    return out


def fetch(profiles, token, get, start, end):
    """Collect every active dungeon on each tracked connected realm, current + prior week."""
    output = {key: None for key in profiles}
    try:
        pids = periods(token, get, start, end)
    except Exception as exc:
        vault.log("vault_leaderboard_periods_unavailable", error=type(exc).__name__)
        return output
    connected, tasks = {}, set()
    for realm in sorted({sources._realm(p["realm"]) for p in profiles.values()}):
        try:
            # Use Blizzard-compatible slug, not the punctuation-free comparison identity.
            profile = next(p for p in profiles.values() if sources._realm(p["realm"]) == realm)
            slug = vault.raiderio.slugify(profile["realm"].replace("'", ""))
            data = get(token, f"/data/wow/realm/{slug}", namespace="dynamic-us")
            cid = int(re.search(r"/connected-realm/(\d+)", data["connected_realm"]["href"])[1])
            connected[realm] = cid
            index = get(token, f"/data/wow/connected-realm/{cid}/mythic-leaderboard/index",
                        namespace="dynamic-us")
            tasks.update((cid, int(d["id"]), pid)
                         for d in index["current_leaderboards"] for pid in pids)
        except Exception as exc:
            vault.log("vault_leaderboard_realm_unavailable", error=type(exc).__name__)

    def read(task):
        cid, dungeon, pid = task
        try:
            doc = get(token, f"/data/wow/connected-realm/{cid}/mythic-leaderboard/{dungeon}/period/{pid}",
                      namespace="dynamic-us")
            if int(doc["period"]) != pid or int(doc["map_challenge_mode_id"]) != dungeon:
                raise ValueError("leaderboard identity mismatch")
            return task, extract(doc, profiles, start, end)
        except Exception as exc:
            vault.log("vault_leaderboard_unavailable", dungeon=dungeon, period=pid,
                      error=type(exc).__name__)
            return task, None
    for (cid, _, _), rows in sources._parallel(sorted(tasks), read).items():
        if rows is None:
            continue
        for key, runs in rows.items():
            # Cross-realm parties can supply evidence on another tracked realm's board.
            if runs or connected.get(sources._realm(profiles[key]["realm"])) == cid:
                output[key] = (output[key] or []) + runs
    return output
