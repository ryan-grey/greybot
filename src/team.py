"""Which of Scrambled's two raid teams filed a report.

The guild runs an A team and a B team into the same Warcraft Logs guild, and Ryan only
wants the A team recapped. Nothing in the API says which is which, and Ryan will not
maintain a roster by hand -- a list of names goes stale the first time somebody
transfers, and a stale list is worse than none because it fails silently.

So the roster is DERIVED from something the bot already knows for certain: who was
standing there for each of the tier's first Heroic kills. Under the guild's own premise
-- B team does not kill a Heroic boss before A team -- every first kill is A team's, so
the participants of those kills are A team by construction. Frequency, not membership:
a player has to show up in at least `min_pct` of the tier's first kills, so a one-off
fill-in never enters the roster no matter how well they performed that night.

Two signals classify a report, and both are needed:

  A  roster overlap.   What fraction of this report's raiders are on the derived roster?
     Requires a MARGIN rather than a majority. Several people raid on both teams, so
     a B-team report legitimately contains A-team players and a 51% rule would call it
     PROG about half the time. Above `high` is PROG, at or below `low` is OTHER, and the
     gap between them is an admission that the number does not know.

  B  progression evidence. Does the report contain a Heroic encounter that had not been
     killed before the report started? B team farms what is already dead; pushing an
     undead boss is A team essentially by definition. This is what carries the cold
     start, where the roster is empty or merely seeded and signal A has nothing to say.

One conclusive signal decides. Two conclusive signals that disagree do not. Nothing
here ever falls back to "probably prog" -- the same rule that took the raid-resolution
fallback out of the announcer applies with more force here, because a recap names
individual people and posts their worst parse. Silence beats a confident wrong answer.

Everything in this module is a pure function over already-extracted names and fights.
That is deliberate: the shapes of Warcraft Logs' untyped `table` and `rankings` blobs
are undocumented and have changed before, so the classification logic must not be
entangled with whatever they happen to look like this month.
"""

import unicodedata

import raiderio

PROG = "PROG"
OTHER = "OTHER"
UNKNOWN = "UNKNOWN"

# Below this many raiders, signal A abstains. A five-person report is an alt run, a
# split, or a log that stopped early; a percentage over a handful of names swings wildly
# on one person and reads as confident while being nearly random.
MIN_RAIDERS_FOR_OVERLAP = 5

# How many first kills the derived roster needs before it outranks the carried-forward
# seed. The arithmetic is the reason for the number: at a 50% threshold, a fill-in who
# raided once is at 100% of a one-kill sample and 50% of a two-kill sample -- both of
# which clear the bar. Three kills is the first sample where a single appearance (33%)
# fails it, which is precisely the contamination this roster exists to avoid.
MIN_FIRST_KILLS_FOR_ROSTER = 3


def normalize_player(name):
    """Fold a character name to a comparison key.

    Accents are stripped to the base letter FIRST, which raiderio.normalize does not do:
    it turns every non-ASCII character into a space, so a real raider called Fûrry folds
    to "f rry" and Yòshi to "y shi". Those match consistently as long as both sides go
    through the same function, so nothing was broken -- but "f rry" is one stray space
    away from colliding with a different player, and the names in Scrambled's own logs
    are full of them.

    Deliberately NOT applied to raiderio.normalize itself. That function produces the boss
    keys already stored in DynamoDB, and changing it would silently re-key the announced
    set -- which is to say, re-announce bosses the guild killed weeks ago.
    """
    folded = unicodedata.normalize("NFKD", str(name or ""))
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return raiderio.normalize(folded)


def player_key(name, server=None):
    """One raider, as a comparison key.

    Server is part of the identity when it is known. Cross-realm raiding means two
    different people genuinely can both be called Shadowstep, and folding them together
    puts a pug on the prog roster.
    """
    base = normalize_player(name)
    if not base:
        return ""
    srv = normalize_player(server or "")
    return f"{base}@{srv}" if srv else base


def player_keys(players):
    """Accepts dicts ({"name":..., "server":...}), bare name strings, or keys this module
    already produced, so callers are not forced to invent a shape before the response
    format is known.

    THE ALREADY-A-KEY CASE IS NOT DEFENSIVE PADDING. First-kill participants are written to
    DynamoDB in key form, and the derived roster is built by reading them back through here
    -- so a key goes through player_key twice. That is only harmless while the key has no
    server: `raiderio.normalize` turns punctuation into a space, so re-keying
    "thaydan@proudmoore" yields "thaydan proudmoore", which matches no live raider at all.

    That is exactly what happened. Every recorded first kill carried a server, the roster
    derived from them shared not one name with the report it was classifying, signal A read
    0% overlap and said OTHER while signal B said PROG, and the disagreement made the night
    UNKNOWN -- so the 2026-09-02 recap posted nothing about a raid that had happened.
    """
    out = set()
    for p in players or ():
        if isinstance(p, dict):
            key = player_key(p.get("name"), p.get("server"))
        elif "@" in str(p):
            name, _, server = str(p).partition("@")
            key = player_key(name, server)
        else:
            key = player_key(p)
        if key:
            out.add(key)
    return out


def derive_roster(first_kills, min_pct=50):
    """The prog roster, from the participants of this tier's first Heroic kills.

    `first_kills` is a list of participant lists -- one per recorded first kill. Returns
    the roster alongside the evidence for it, because "why is this person not on the
    roster" is a question that gets asked at 1am and a bare set cannot answer it.

    `provisional` marks a roster derived from too few kills to discriminate. It is not
    empty and it is not wrong, it is simply not yet able to reject anybody, so the caller
    should keep preferring a seed while it is set. See MIN_FIRST_KILLS_FOR_ROSTER.
    """
    samples = [player_keys(p) for p in (first_kills or ())]
    samples = [s for s in samples if s]
    total = len(samples)
    if not total:
        return {"roster": set(), "sample": 0, "counts": {}, "provisional": True,
                "minPct": float(min_pct)}

    counts = {}
    for s in samples:
        for key in s:
            counts[key] = counts.get(key, 0) + 1

    threshold = float(min_pct) / 100.0
    roster = {k for k, n in counts.items() if (n / total) >= threshold}
    return {"roster": roster, "sample": total, "counts": counts,
            "provisional": total < MIN_FIRST_KILLS_FOR_ROSTER, "minPct": float(min_pct)}


def roster_overlap(raiders, roster):
    """Fraction of THIS REPORT's raiders who are on the prog roster, or None.

    None means the question could not be asked -- an empty roster, or too few raiders to
    make a percentage mean anything. It must stay distinct from 0.0, which is a real
    answer meaning "not one of these people is on the prog roster". Collapsing the two
    turns "we have no roster yet" into a confident OTHER.
    """
    raiders = set(raiders or ())
    if not roster or len(raiders) < MIN_RAIDERS_FOR_OVERLAP:
        return None
    return len(raiders & set(roster)) / len(raiders)


def signal_roster(raiders, roster, high, low):
    """Signal A. Returns (verdict-or-None, detail)."""
    frac = roster_overlap(raiders, roster)
    detail = {"overlap": None if frac is None else round(frac, 4),
              "raiders": len(set(raiders or ())), "rosterSize": len(roster or ()),
              "high": float(high), "low": float(low)}
    if frac is None:
        detail["why"] = ("no derived roster to compare against" if not roster
                         else f"only {len(set(raiders or ()))} raiders in the report")
        return None, detail
    if frac >= float(high) / 100.0:
        return PROG, detail
    if frac <= float(low) / 100.0:
        return OTHER, detail
    detail["why"] = "overlap fell between the thresholds"
    return None, detail


def signal_progression(fights, killed_before, difficulty=None):
    """Signal B. Progression on an encounter that was not dead when the report started.

    `killed_before` is the set of boss keys whose FIRST kill predates this report -- not
    simply everything the bot has ever announced. The distinction matters on the night it
    matters most: the announcer runs every fifteen minutes, so a boss A team killed at
    9pm is already in the announced set by the time the recap runs the next morning, and
    comparing against that set would erase the very evidence that this was A team.

    Both wipes and kills count. The brief names wipes, and wipes are the usual shape of
    this signal, but a report whose only progression was the kill itself -- a one-pull
    clear of the last undead boss -- is not less A team for having been efficient, and
    under the guild's premise a first Heroic kill is A team by definition. Excluding it
    would abstain on exactly the report the guild most wants recapped.

    Returns (PROG-or-None, detail). It never returns OTHER: a clean farm night is silent
    evidence, not evidence of B team. A team farms too.
    """
    dead = set(killed_before or ())
    wiped_on, killed_new = set(), set()
    for f in fights or ():
        if difficulty is not None and int(f.get("difficulty") or 0) != int(difficulty):
            continue
        if not f.get("encounterID"):            # trash; carries encounterID 0
            continue
        key = raiderio.normalize(f.get("name"))
        if not key or key in dead:
            continue
        (killed_new if f.get("kill") else wiped_on).add(key)

    detail = {"wipesOnUndeadBosses": sorted(wiped_on),
              "firstKillsInReport": sorted(killed_new),
              "alreadyDead": len(dead)}
    if wiped_on or killed_new:
        return PROG, detail
    detail["why"] = ("every encounter in this report was already dead before it started"
                     if dead else "no encounters in this report at all")
    return None, detail


def resolve_team(raiders, fights, roster, killed_before, high, low, difficulty=None,
                 roster_seeded=False):
    """PROG | OTHER | UNKNOWN, with the reasoning attached for the log line.

    One conclusive signal decides. Two that disagree -- low roster overlap on a report
    that is nonetheless pushing an undead boss -- means something is happening that
    neither signal models, which is the case for saying nothing rather than the case for
    picking the more confident of two guesses.
    """
    a, a_detail = signal_roster(raiders, roster, high, low)
    b, b_detail = signal_progression(fights, killed_before, difficulty=difficulty)

    conclusive = {v for v in (a, b) if v}
    if len(conclusive) == 1:
        verdict = conclusive.pop()
        why = "both signals agree" if a and b else f"decided by signal {'A' if a else 'B'}"
    elif not conclusive:
        verdict, why = UNKNOWN, "neither signal was conclusive"
    else:
        verdict, why = UNKNOWN, "signals disagree"

    return verdict, {"verdict": verdict, "why": why,
                     "signalA": a or "inconclusive", "signalB": b or "inconclusive",
                     "rosterSeeded": bool(roster_seeded),
                     "roster": a_detail, "progression": b_detail}
