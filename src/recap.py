"""Reading the untyped blobs: what a raid night actually looked like.

`table` and `rankings` return the JSON scalar. Their contents are not in the GraphQL
schema, are not documented, and have changed before, so every rule in this module comes
from a real captured response (scripts/fixtures/report-recap.json) rather than from
reasoning about what they ought to contain. Four things in that capture would each have
broken a parser written from assumption:

  ONE REPORT IS NOT ONE ACTIVITY. The captured report holds sixteen Heroic raid fights, a
  Normal kill, and three Mythic+ dungeon runs. A DamageDone table over the report's full
  time range returned twenty-seven entries for an eighteen-person raid; eight of those
  people never entered a Heroic fight. Scoping the table to explicit fightIDs is not an
  optimisation, it is the difference between a raid leaderboard and a list of whoever
  logged the most damage in the building that evening.

  THE DEATHS TABLE IS CAPPED. It returned exactly 200 entries and stopped 37% of the way
  through the night, silently. "Most deaths" from one call is simply a wrong answer, so
  deaths are read with a timestamp cursor until a short page comes back.

  masterData.actors IS NOT A ROSTER. It carried 2,295 players across 130 realms. The
  raiders are the eighteen ids in each fight's friendlyPlayers; actors is only how an id
  becomes a name and a server.

  A PET IS IN THE DAMAGE TABLE. Entries carry no server, and one of them was a warlock's
  pet rather than a person.

Nothing here raises on a missing field. A blob that has lost a key it used to have costs
the card that one section and nothing else -- the alternative is a parser that takes the
whole recap down the week Warcraft Logs renames something.
"""

import raiderio
import team

HEROIC = 4

# The Deaths table stops at this many entries with no flag, no error and no indication in
# the payload that anything was left out. Reading a page of exactly this size as "all the
# deaths" is the bug; see death_counts.
DEATHS_PAGE_CAP = 200


def _entries(blob, *path):
    """Dig into a blob defensively. Returns [] for anything that is not a list of dicts.

    The tables nest their real payload under {"data": {"entries": [...]}} and the rankings
    under {"data": [...]}, which is two shapes for one idea and exactly the sort of thing
    that is not worth a KeyError."""
    node = blob
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return [e for e in node if isinstance(e, dict)] if isinstance(node, list) else []


def actor_index(master_data):
    """Actor id -> name and server, for Players only.

    Pets share the id space with people and turn up in damage tables under their owner's
    damage, so the type check here is what keeps 'Lightspawn Lasher' off the leaderboard.
    """
    out = {}
    for a in _entries(master_data or {}, "actors"):
        if a.get("type") and a.get("type") != "Player":
            continue
        aid = a.get("id")
        if aid is None or not a.get("name"):
            continue
        out[int(aid)] = {"name": a["name"], "server": a.get("server") or ""}
    return out


def raid_scope(fights, difficulty=HEROIC):
    """The Heroic raid inside a report that may also contain other content.

    Everything downstream is scoped by what this returns: the fight ids bound the tables,
    and the participant ids bound the leaderboards.
    """
    fights = [f for f in (fights or []) if isinstance(f, dict)]
    mine = [f for f in fights
            if int(f.get("difficulty") or 0) == int(difficulty) and f.get("encounterID")]
    raiders, fight_ids = set(), []
    kills, wipes = [], []
    for f in mine:
        if f.get("id") is not None:
            fight_ids.append(int(f["id"]))
        for pid in f.get("friendlyPlayers") or []:
            raiders.add(int(pid))
        (kills if f.get("kill") else wipes).append(f)
    return {"fightIDs": fight_ids, "raiderIDs": raiders, "kills": kills, "wipes": wipes,
            "fights": mine, "excluded": len(fights) - len(mine)}


def bosses_killed(scope):
    """Distinct bosses that died tonight, in the order they died."""
    seen, out = set(), []
    for f in sorted(scope["kills"], key=lambda f: f.get("startTime") or 0):
        key = raiderio.normalize(f.get("name"))
        if key and key not in seen:
            seen.add(key)
            out.append(f.get("name"))
    return out


def prog_encounter(scope):
    """The boss the night was actually spent on: most wipes, ties broken by total pulls.

    Pull count includes the kill. A night that wiped nine times and then killed it was ten
    pulls on that boss, and reporting nine would be describing the failures only.
    """
    tally = {}
    for f in scope["fights"]:
        key = raiderio.normalize(f.get("name"))
        if not key:
            continue
        rec = tally.setdefault(key, {"name": f.get("name"), "wipes": 0, "pulls": 0,
                                     "best": None})
        rec["pulls"] += 1
        if not f.get("kill"):
            rec["wipes"] += 1
            pct = f.get("fightPercentage")
            # fightPercentage is the boss's remaining health, so LOWER is closer.
            if isinstance(pct, (int, float)) and (rec["best"] is None or pct < rec["best"]):
                rec["best"] = float(pct)
        else:
            rec["best"] = 0.0
    ranked = [r for r in tally.values() if r["wipes"] > 0]
    if not ranked:
        return None
    return max(ranked, key=lambda r: (r["wipes"], r["pulls"]))


def _person(actors, aid):
    """An actor id resolved to a stable cross-report identity, or None.

    THE reason this function exists: actor ids are scoped to ONE REPORT. Warcraft Logs
    numbers actors per report, so id 2 in Thursday's first log and id 2 in the log that
    replaced it after a restart are different people. Aggregating a split raid night by id
    silently merges two strangers and splits one raider in half -- which is exactly what a
    live dry run produced: Thaydan at 27 deaths and again at 14, and a "most deaths" winner
    who was not actually the answer.

    So every aggregate in this module keys on the player, name and server folded together,
    and the actor id is only ever used to look that player up inside the report it came
    from.
    """
    a = (actors or {}).get(aid)
    if not a or not a.get("name"):
        return None
    return team.player_key(a["name"], a.get("server")), a


def raider_keys(sources):
    """Distinct people who raided, across however many reports the night took."""
    out = set()
    for src in sources or ():
        for aid in src.get("eligible") or ():
            found = _person(src.get("actors"), aid)
            if found:
                out.add(found[0])
    return out


def top_damage(sources, limit=3):
    """Highest total damage, summed per PLAYER across every report of the night.

    `sources` is a list because a raid night is not always one report -- a log restarted at
    the break produces two, and recapping only the larger half would drop an hour of the
    night without saying so.

    Eligibility is decided by the caller and is the explicit answer to "who counts". The
    tables are already scoped to the raid's fight ids, which removes the Mythic+ dungeons,
    but a pug standing in the raid itself is still in it and would otherwise top the
    guild's own leaderboard.
    """
    totals = {}
    for src in sources or ():
        actors, elig = src.get("actors") or {}, src.get("eligible") or set()
        for e in _entries(src.get("damage"), "data", "entries"):
            aid, total = e.get("id"), e.get("total")
            if aid is None or not isinstance(total, (int, float)):
                continue
            aid = int(aid)
            if aid not in elig:
                continue
            found = _person(actors, aid)
            if not found:
                continue
            key, a = found
            rec = totals.setdefault(key, {"name": a["name"], "server": a.get("server") or "",
                                          "total": 0})
            rec["total"] += int(total)
    rows = sorted(totals.values(), key=lambda r: (-r["total"], r["name"]))
    return rows[:limit]


def death_counts(sources):
    """Deaths per raider, merged across pages AND across reports.

    Two independent merges, and they need different keys. Pages within one report are
    de-duplicated on (id, timestamp, fight) so an overlapping cursor refetch cannot inflate
    a count. Reports are combined on the PLAYER, because neither actor ids nor fight ids
    mean anything outside the report that issued them -- so the dedupe marker is namespaced
    by report code.
    """
    counts, seen = {}, set()
    for src in sources or ():
        actors, elig = src.get("actors") or {}, src.get("eligible") or set()
        allowed = set(src.get("fightIDs") or ()) or None
        code = src.get("report") or id(src)
        for blob in src.get("deaths") or ():
            for e in _entries(blob, "data", "entries"):
                aid, ts, fid = e.get("id"), e.get("timestamp"), e.get("fight")
                if aid is None:
                    continue
                aid = int(aid)
                if aid not in elig:
                    continue
                if allowed is not None and fid is not None and int(fid) not in allowed:
                    continue
                marker = (code, aid, ts, fid)
                if marker in seen:
                    continue
                seen.add(marker)
                found = _person(actors, aid)
                if not found:
                    continue
                key, a = found
                rec = counts.setdefault(key, {"name": a["name"], "deaths": 0})
                rec["deaths"] += 1
    return sorted(counts.values(), key=lambda r: (-r["deaths"], r["name"]))


def page_is_truncated(blob):
    """Did this Deaths page stop at the cap rather than at the end of the night?"""
    return len(_entries(blob, "data", "entries")) >= DEATHS_PAGE_CAP


def last_timestamp(blob):
    stamps = [e.get("timestamp") for e in _entries(blob, "data", "entries")
              if isinstance(e.get("timestamp"), (int, float))]
    return max(stamps) if stamps else None


def _rank_rows(r, eligible_names, difficulty):
    """One rankings entry flattened to per-character rows, or nothing."""
    if int(r.get("difficulty") or 0) != int(difficulty) or not r.get("kill"):
        return []
    boss = (r.get("encounter") or {}).get("name") or ""
    roles = r.get("roles") if isinstance(r.get("roles"), dict) else {}
    out = []
    for role in roles.values():
        if not isinstance(role, dict):
            continue
        for c in role.get("characters") or []:
            if not isinstance(c, dict):
                continue
            pct, name = c.get("rankPercent"), c.get("name")
            if not name or not isinstance(pct, (int, float)):
                continue
            server = c.get("server")
            server = server.get("name") if isinstance(server, dict) else server
            if eligible_names is not None:
                # Rankings carry a real server, so match on the full identity when both
                # sides have one and fall back to the bare name when they do not.
                if (team.player_key(name, server) not in eligible_names
                        and team.normalize_player(name) not in eligible_names):
                    continue
            out.append({"name": name, "percent": float(pct), "boss": boss,
                        "spec": c.get("spec") or "", "class": c.get("class") or ""})
    return out


def parses(sources, eligible_names, difficulty=HEROIC):
    """Best and worst parse of the night.

    Two filters that are not optional. Rankings covers every ranked KILL in the report,
    which in the captured response meant a Normal kill and three Mythic+ dungeons alongside
    the Heroic raid -- so difficulty is checked per entry. And `rankPercent` is used rather
    than `bracketPercent`: both are present, but the first is the number raiders mean when
    they say "parse", and the second is bracketed by item level.

    It is also scoped to the SAME fights as everything else on the card. Difficulty alone
    is not enough: the captured report's Heroic kills span two raids, so a card correctly
    labelled "The Venomous Abyss" was crediting its best parse to Nymrissa Wavecaller --
    a boss in The Tidebound Grotto, from the one warm-up kill the card had already
    excluded from its own list of what died.

    Returns None when there is nothing to report, which is the ordinary state of a wipe
    night: rankings only exist for kills.
    """
    rows = []
    for src in sources or ():
        allowed = set(src.get("fightIDs") or ()) or None
        for r in _entries(src.get("rankings"), "data"):
            fid = r.get("fightID")
            if allowed is not None and fid is not None and int(fid) not in allowed:
                continue
            rows.extend(_rank_rows(r, eligible_names, difficulty))
    if not rows:
        return None
    return {"best": max(rows, key=lambda r: r["percent"]),
            "worst": min(rows, key=lambda r: r["percent"]),
            "sample": len(rows)}


def summarise(scope, sources, show_worst_parse=False):
    """Everything the card needs, with each section independently omittable.

    A section that could not be read is absent from the result rather than present and
    empty, so the card renders what is known and says nothing about what is not. The log
    line the caller writes from `missing` is what makes a thin card explicable.
    """
    people = raider_keys(sources)
    eligible_names = set(people)
    for src in sources or ():
        for aid in src.get("eligible") or ():
            found = _person(src.get("actors"), aid)
            if found:
                eligible_names.add(team.normalize_player(found[1]["name"]))

    out = {"bosses": bosses_killed(scope), "prog": prog_encounter(scope),
           "raiders": len(people)}
    out["damage"] = top_damage(sources)
    out["deaths"] = death_counts(sources)
    out["parses"] = parses(sources, eligible_names)
    if not show_worst_parse and out["parses"]:
        # Dropped here rather than at render time, so a card that is not supposed to carry
        # a worst parse never has one in the object at all.
        out["parses"] = {k: v for k, v in out["parses"].items() if k != "worst"}

    out["missing"] = [k for k in ("damage", "deaths", "parses") if not out.get(k)]
    return out


def restrict_to_roster(sources, roster):
    """Narrow each report's eligible set to people on the prog roster.

    Applied per source, because eligibility is expressed in actor ids and those mean
    nothing outside the report that issued them. Returns how many were dropped, for the
    log line.

    A source whose eligible set would become EMPTY is left alone. That is the guard against
    a roster that is subtly wrong -- from a tier rollover, or a night the whole team was
    replaced -- silently producing a card with no leaderboard on it at all.
    """
    if not roster:
        return 0
    excluded = 0
    for src in sources or ():
        keep = set()
        for aid in src.get("eligible") or ():
            found = _person(src.get("actors"), aid)
            if found and found[0] in roster:
                keep.add(aid)
        if keep:
            excluded += len(src["eligible"]) - len(keep)
            src["eligible"] = keep
    return excluded


def drop_duplicate_logs(reports, overlap_threshold=0.5):
    """Discard reports that are another report of the SAME raid night.

    Two people in Scrambled both log, so a single Thursday produces two reports covering
    the same pulls: 'Prog Raid' by one raider and 'Starting Heroic - 8/27' by another,
    starting four minutes apart, 98% overlapping, with all three Heroic kills present in
    both at identical wall-clock times.

    That is not the same thing as a night logged in two parts, which also happens when a
    log is restarted at the break -- and the two cases need opposite handling. Parts must
    be summed or an hour of the night vanishes. Duplicates must NOT be summed or every
    number on the card doubles: the first live dry run of this code reported a top damage
    of 848M for a night whose real figure was 565M.

    Time overlap separates them, because it is the one thing that cannot be true of two
    halves of the same evening. Where two reports overlap by more than half of the shorter
    one, the more complete is kept -- most Heroic fights first, then longest -- and the
    other is dropped with a reason.

    Returns (kept, dropped). Order of `kept` follows the original list, so the caller's
    "earliest report" logic is unaffected.
    """
    def span(r):
        return max(0, int(r.get("end") or 0) - int(r.get("start") or 0))

    ranked = sorted(reports or (),
                    key=lambda r: (-int(r.get("heroicFights") or 0), -span(r)))
    kept, dropped = [], []
    for r in ranked:
        clash = None
        for k in kept:
            shorter = min(span(r), span(k))
            if shorter <= 0:
                continue
            overlap = min(int(r.get("end") or 0), int(k.get("end") or 0)) \
                - max(int(r.get("start") or 0), int(k.get("start") or 0))
            if overlap > 0 and (overlap / shorter) > overlap_threshold:
                clash = k
                break
        if clash:
            dropped.append({"report": r.get("code"), "duplicateOf": clash.get("code"),
                            "why": "another log of the same raid night"})
        else:
            kept.append(r)
    order = {r.get("code"): i for i, r in enumerate(reports or ())}
    kept.sort(key=lambda r: order.get(r.get("code"), 0))
    return kept, dropped
