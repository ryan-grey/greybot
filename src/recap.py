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
        # subType is the class ("Priest", "DeathKnight"). Taken from masterData rather
        # than from the rankings blob because masterData covers EVERY raider, and rankings
        # only covers the ones who were in a ranked kill -- colouring half a roster and
        # leaving the rest grey would read as a bug rather than as missing data.
        out[int(aid)] = {"name": a["name"], "server": a.get("server") or "",
                         "class": a.get("subType") or ""}
    return out


# Warcraft Logs buckets both playerDetails and rankings as "tanks"/"healers"/"dps".
# Folded to the singular here because the card and the page talk about ONE person's role.
ROLES = {"tanks": "tank", "healers": "healer", "dps": "dps"}


def role_index(player_details):
    """Actor id -> "tank" | "healer" | "dps", for every raider in the report.

    Read from playerDetails rather than from the rankings blob on purpose. Rankings only
    contain people who were in a ranked KILL, so a wipe night ranks nobody and a raider who
    sat the only kill is missing -- and a role icon that appears for two thirds of the raid
    reads as a bug rather than as missing data. playerDetails covers everyone who swung.
    """
    node = (player_details or {}).get("data") or {}
    node = node.get("playerDetails") if isinstance(node, dict) else None
    out = {}
    for bucket, role in ROLES.items():
        for p in (node or {}).get(bucket) or ():
            if isinstance(p, dict) and p.get("id") is not None:
                out[int(p["id"])] = role
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


def boss_order(encounters):
    """(normalised boss name -> its 1-based place in the tier, the tier's boss count)."""
    seq = [e for e in (encounters or ()) if e]
    order = {}
    for i, name in enumerate(seq, 1):
        # First occurrence wins. A name appearing twice in one raid's list would be a
        # Raider.IO oddity rather than two bosses, and picking the later index would push
        # every subsequent boss's number out by one.
        order.setdefault(raiderio.normalize(name), i)
    return order, len(seq)


def boss_label(name, order, total):
    """One boss as "3/8 The Lost Explorers" -- its place in the tier's PUBLISHED order.

    Not the order it died tonight. Raider.IO's encounter list is the raid's own running
    order, so Entombed Sentinels is 2/8 whether it fell first on a farm night or last on a
    prog one, and the number means the same thing every week. Deriving it from tonight's
    kill order instead would produce a "1/8" for whatever the raid happened to open with.

    A boss the encounter list does not contain, or a tier whose list could not be read,
    yields the bare name. That is the same rule as every other section of the card: a
    number that cannot be established is omitted, never guessed. A wrong position is worse
    than no position here, because it reads as progression the guild does not have.

    EVERY place the card names a boss goes through this -- the kill list, the pull count,
    and the parse fields. A card that numbers one of the three reads as though the other
    two are bosses of some different kind.
    """
    i = (order or {}).get(raiderio.normalize(name))
    return f"{i}/{total} {name}" if name and i and total else (name or "")


def boss_labels(bosses, encounters):
    """The night's kills, each numbered, ordered by position in the tier."""
    order, total = boss_order(encounters)
    numbered, plain = [], []
    for b in bosses or ():
        i = order.get(raiderio.normalize(b))
        (numbered if i and total else plain).append((i, b))

    # Sorted by POSITION, which reorders the list away from the order they died in. That
    # is the point of numbering them: "1/8, 3/8, 2/8, 4/8" reads as a mistake on the card
    # even though every number is right. Kill order is still what `bosses` carries, so the
    # log line remains the record of how the night actually went.
    numbered.sort(key=lambda p: p[0])
    return [f"{i}/{total} {b}" for i, b in numbered] + [b for _, b in plain]


def unkilled(scope):
    """Bosses that were pulled tonight and did NOT die, worst-first by effort spent.

    Not the same as `prog_encounter`, which returns the single boss the night was mostly
    about. A night can end with two bosses still standing, and a card that mentions one of
    them is describing part of the evening.

    A boss that was wiped on and then killed is absent from this: it is a kill, and it is
    already named as one. `best` is the closest attempt, or None where no percentage came
    back -- reported separately from the pull count because "12 pulls" is true even when
    the health percentages could not be read.
    """
    tally = {}
    for f in scope["fights"]:
        key = raiderio.normalize(f.get("name"))
        if not key:
            continue
        rec = tally.setdefault(key, {"name": f.get("name"), "pulls": 0, "killed": False,
                                     "best": None})
        rec["pulls"] += 1
        if f.get("kill"):
            rec["killed"] = True
            continue
        pct = f.get("fightPercentage")
        # fightPercentage is the boss's REMAINING health, so lower is closer.
        if isinstance(pct, (int, float)) and (rec["best"] is None or pct < rec["best"]):
            rec["best"] = float(pct)
    rows = [r for r in tally.values() if not r["killed"]]
    return sorted(rows, key=lambda r: (-r["pulls"], r["name"]))


def first_kills(bosses, dead):
    """Of tonight's kills, the ones that had not died before -- the actual progression.

    `dead` is the set of normalised boss keys already killed when the night opened. A
    farm night returns [], which is correct and is why the card has to be able to say
    "killed four bosses" with no "including" clause after it.
    """
    already = {raiderio.normalize(d) for d in (dead or ())}
    return [b for b in bosses or () if raiderio.normalize(b) not in already]


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


def _roles_by_key(sources):
    """Player key -> role, merged across the night's reports."""
    out = {}
    for src in sources or ():
        roles = role_index(src.get("playerDetails"))
        actors = src.get("actors") or {}
        for aid, role in roles.items():
            found = _person(actors, aid)
            if found:
                out.setdefault(found[0], role)
    return out


def totals(sources, blob_key, limit=None):
    """Highest total of one numeric table, summed per PLAYER across the night's reports.

    DamageDone, Healing and DamageTaken are the same table shape with a different
    dataType, so they are read by the same function rather than by three that would drift.
    Every filter that matters is shared as a result: pets excluded, eligibility enforced,
    and the per-report actor ids resolved to a stable player key before anything is summed.
    """
    out = {}
    for src in sources or ():
        actors, elig = src.get("actors") or {}, src.get("eligible") or set()
        for e in _entries(src.get(blob_key), "data", "entries"):
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
            rec = out.setdefault(key, {"key": key, "name": a["name"],
                                       "server": a.get("server") or "",
                                       "class": a.get("class") or "", "total": 0})
            rec["total"] += int(total)
    roles = _roles_by_key(sources)
    for key, rec in out.items():
        rec["role"] = roles.get(key)
    rows = sorted(out.values(), key=lambda r: (-r["total"], r["name"]))
    return rows[:limit]


def top_healing(sources, limit=3):
    """Most effective healing done. `total` is effective; overheal is a separate field and
    is deliberately not counted -- healing that landed on a full health bar did nothing."""
    return totals(sources, "healing", limit)


def top_damage_taken(sources, limit=3):
    """Most damage taken. Presented without judgement: on most fights the tank is supposed
    to be at the top of this list, which is why it is a category and not a leaderboard."""
    return totals(sources, "damageTaken", limit)


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
    return totals(sources, "damage", limit)


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
                rec = counts.setdefault(key, {"key": key, "name": a["name"],
                                              "server": a.get("server") or "",
                                              "class": a.get("class") or "", "deaths": 0})
                rec["deaths"] += 1
    roles = _roles_by_key(sources)
    for key, rec in counts.items():
        rec["role"] = roles.get(key)
    return sorted(counts.values(), key=lambda r: (-r["deaths"], r["name"]))


def page_is_truncated(blob):
    """Did this Deaths page stop at the cap rather than at the end of the night?"""
    return len(_entries(blob, "data", "entries")) >= DEATHS_PAGE_CAP


def last_timestamp(blob):
    stamps = [e.get("timestamp") for e in _entries(blob, "data", "entries")
              if isinstance(e.get("timestamp"), (int, float))]
    return max(stamps) if stamps else None


def _rank_rows(r, eligible_names, difficulty):
    """One rankings entry flattened to per-character rows, or nothing.

    The role bucket is kept on every row, and it is the whole reason the parse column can
    be trusted. Warcraft Logs ranks each bucket on its OWN metric -- tanks and damage
    dealers on damage, healers on healing -- so a healer's 82 is a healing rank and mixing
    it into a damage average would be averaging two different measurements. Verified
    against the live rankings blob: a Resto Druid's `amount` is HPS, a Blood DK's is DPS.
    """
    if int(r.get("difficulty") or 0) != int(difficulty) or not r.get("kill"):
        return []
    boss = (r.get("encounter") or {}).get("name") or ""
    roles = r.get("roles") if isinstance(r.get("roles"), dict) else {}
    out = []
    for bucket, role in roles.items():
        if not isinstance(role, dict):
            continue
        role_name = ROLES.get(bucket)
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
            out.append({"key": team.player_key(name, server), "name": name,
                        "server": server or "", "percent": float(pct), "boss": boss,
                        "role": role_name,
                        "spec": c.get("spec") or "", "class": c.get("class") or ""})
    return out


def parse_rows(sources, eligible_names, difficulty=HEROIC):
    """Every ranked parse of the night, one row per character per kill.

    Split out of `parses` because the card needs the two extremes and the page needs
    all of it. One traversal, one set of filters, so the page and the card can never
    disagree about whose parses counted.
    """
    rows = []
    for src in sources or ():
        allowed = set(src.get("fightIDs") or ()) or None
        for r in _entries(src.get("rankings"), "data"):
            fid = r.get("fightID")
            if allowed is not None and fid is not None and int(fid) not in allowed:
                continue
            rows.extend(_rank_rows(r, eligible_names, difficulty))
    return rows


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
    rows = parse_rows(sources, eligible_names, difficulty)
    if not rows:
        return None
    return {"best": max(rows, key=lambda r: r["percent"]),
            "worst": min(rows, key=lambda r: r["percent"]),
            "sample": len(rows)}


def summarise(scope, sources, show_worst_parse=False, encounters=None,
              dead=None):
    """Everything the card needs, with each section independently omittable.

    A section that could not be read is absent from the result rather than present and
    empty, so the card renders what is known and says nothing about what is not. The log
    line the caller writes from `missing` is what makes a thin card explicable.

    `bosses` and `bossLabels` are both returned and both are load-bearing. The card renders
    the labels; the LOGS record the bare names, because "3/8" is meaningless six months
    later when the tier has rolled over and `recap_posted` is the only record of what died.
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
    out["bossLabels"] = boss_labels(out["bosses"], encounters)
    # The card leads on the count and then on the progression, because those answer two
    # different questions: how much did the night clear, and did any of it matter. Naming
    # every kill was answering neither -- a farm clear and a breakthrough read identically.
    out["killed"] = len(out["bosses"])
    out["firstKills"] = first_kills(out["bosses"], dead)
    out["firstKillLabels"] = boss_labels(out["firstKills"], encounters)
    order, tier_total = boss_order(encounters)
    out["unkilled"] = [dict(r, label=boss_label(r["name"], order, tier_total))
                       for r in unkilled(scope)]
    out["damage"] = top_damage(sources)
    out["healing"] = top_healing(sources)
    out["damageTaken"] = top_damage_taken(sources)
    out["deaths"] = death_counts(sources)
    out["parses"] = parses(sources, eligible_names)
    if not show_worst_parse and out["parses"]:
        # Dropped here rather than at render time, so a card that is not supposed to carry
        # a worst parse never has one in the object at all.
        out["parses"] = {k: v for k, v in out["parses"].items() if k != "worst"}

    # Numbering is attached to every boss the card names, not just the kill list. `name`
    # and `boss` are left untouched beside the labels, because those are what the logs
    # carry and a position is only meaningful while the tier is current.
    total = tier_total
    if out["prog"]:
        out["prog"]["label"] = boss_label(out["prog"].get("name"), order, total)
    for value in (out["parses"] or {}).values():
        # `parses` also carries `sample`, which is an int rather than a parse row.
        if isinstance(value, dict):
            value["bossLabel"] = boss_label(value.get("boss"), order, total)

    out["missing"] = [k for k in ("damage", "healing", "damageTaken", "deaths",
                                  "parses") if not out.get(k)]
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


def raider_rows(sources, eligible_names, difficulty=HEROIC):
    """Every raider of the night, across every category the card samples.

    The card shows three names for damage, whoever tied for most deaths, and two parses.
    That is the right amount for a Discord embed and it is not a full account: eighteen people
    raided and fifteen of them appear nowhere. This is the same numbers with nothing
    dropped -- one row per person, joined on the player key rather than the display name,
    for the same reason every other aggregate here does. Actor ids are per-report and two
    raiders genuinely can share a name across realms.

    A person who raided but whose damage row was missing still gets a row, with the
    sections that could not be read left as None. Absent is not zero: a raider with no
    parse did not parse 0, they killed nothing that ranks, and rendering that as a number
    would be inventing data. The renderer prints an em dash for None and never a 0.

    SCOPED TO THE TIER, AND THE WORLD BOSS IS NOT IN IT. Every column here is built from
    sources whose fightIDs are the night's tier fights, so a Heroic world boss killed the
    same evening is named on the card and the page but contributes no damage, no deaths and
    no parse. That is a decision rather than an oversight: a world boss is a ten-minute tag
    with a full raid on it, and averaging its parse against progression pulls flatters
    everybody and means nothing.

    The parse column is the raider's MEAN rankPercent across the kills they were actually
    in. Kills they missed are not counted as anything -- not zero, not absent-and-averaged
    -- because a raider who sat one boss did not parse badly on it. That makes the column
    "how did you do on the bosses you were there for", which is the only version of an
    overall parse that does not silently punish attendance.
    """
    rows = {}

    def row(key, name, server="", klass=""):
        rec = rows.setdefault(key, {"key": key, "name": name, "server": server or "",
                                    "class": klass or "",
                                    "damage": None, "healing": None,
                                    "damageTaken": None, "deaths": None,
                                    "parse": None, "parseBoss": None,
                                    "parseAvg": None, "parseCount": 0,
                                    "parseRole": None, "role": None, "_parses": []})
        # A later source may know the class when the first one did not.
        if klass and not rec.get("class"):
            rec["class"] = klass
        return rec

    # Seeded from who RAIDED, not from who appears in a table. Anything else makes the
    # roster a function of which blobs happened to parse.
    for src in sources or ():
        for aid in src.get("eligible") or ():
            found = _person(src.get("actors"), aid)
            if found:
                row(found[0], found[1]["name"], found[1].get("server"),
                    found[1].get("class"))

    for blob, field in (("damage", "damage"), ("healing", "healing"),
                        ("damageTaken", "damageTaken")):
        for r in totals(sources, blob):
            row(r["key"], r["name"], r.get("server"), r.get("class"))[field] = r["total"]
    for r in death_counts(sources):
        row(r["key"], r["name"], r.get("server"))["deaths"] = r["deaths"]

    for p in parse_rows(sources, eligible_names, difficulty):
        # Rankings carry a real server and the damage tables do not, so a rankings row can
        # key to somebody already seeded under a bare name. Fall back to that rather than
        # opening a second row for one person.
        key = p["key"] if p["key"] in rows else team.normalize_player(p["name"])
        rec = row(key, p["name"], p.get("server"), p.get("class"))
        rec["_parses"].append((p.get("role"), p["percent"]))
        if rec["parse"] is None or p["percent"] > rec["parse"]:
            rec["parse"], rec["parseBoss"] = p["percent"], p["boss"]

    roles = _roles_by_key(sources)
    for key, rec in rows.items():
        rec["role"] = roles.get(key) or rec.get("role")
        got = rec.pop("_parses")
        if not got:
            continue
        # ONE role's parses, never a blend. Somebody who tanked three bosses and then went
        # damage for one has a tank average and a damage average, and the mean of the two
        # is a number about nobody. The role they killed the most bosses in wins; a tie
        # falls back to the role playerDetails lists them under, which is what the rest of
        # the page already calls them.
        by_role = {}
        for role, pct in got:
            by_role.setdefault(role, []).append(pct)
        best = max(by_role, key=lambda r: (len(by_role[r]), r == rec.get("role")))
        picked = by_role[best]
        rec["parseRole"] = best or rec.get("role")
        rec["parseCount"] = len(picked)
        rec["parseAvg"] = sum(picked) / len(picked)
        rec["parseRoles"] = {r: len(v) for r, v in by_role.items()}

    # Damage descending, because that is the column people look at first. Nobody with a
    # missing damage figure is promoted above somebody with a real one.
    return sorted(rows.values(),
                  key=lambda r: (r["damage"] is None, -(r["damage"] or 0), r["name"]))
