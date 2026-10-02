"""The raid meta achievement: "Glory of the <tier> Raider" and the achievements under it.

One card the first time anyone on the team holds each listed achievement, then a gold one
the first time anyone holds the meta itself. Never one per raider: a ten-boss raid earning
an achievement together is one piece of news, not twenty.

WHERE IT COMES FROM. Blizzard's character achievements profile, the only source that has
them -- Warcraft Logs and Raider.IO carry kills, not achievements. Two things about it
shape everything below:

  It is a SNAPSHOT TAKEN AT LOGOUT. An achievement earned at 9:40 appears when that
  character next logs out, not at 9:40, and an alt that has not logged in since does not
  show it at all even though it is account-wide. So every mapped character is read, not
  one per member, and a card names whoever was visible when it was first seen.

  It is ONE TO TWO MEGABYTES per character. The character summary is 4 KB and carries the
  achievement points and last login, which change whenever the big document could have.
  That pair is the signature: an unchanged one skips the download, so a quiet half hour
  costs a few small calls rather than forty megabytes.

THE META IS THE NINTH ACHIEVEMENT, not a count of the other eight. Eight first entries can
belong to eight different people, none of whom has the mount; "completed" is only true when
somebody holds the meta. Whoever does holds all of its parts, so those are announced first,
in the order they were earned, and the meta last.

WHO IS WATCHED. The install's roll call mapping (ROLLCALL#SETUP) with the vault's character
locks applied, narrowed to one role's holders when the setup names a role.

Pure functions above the fold, network below, so the tests can drive all of it offline.
"""

import json
import urllib.parse

import raiderio

BRAND_ACCENT = 0x5CA8F0
GOLD = 0xE8B44A
NAMES_ON_CARD = 4


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, default=str))


def fold(name):
    return str(name or "").strip().casefold()


def meta_definition(detail):
    """{"id", "name", "reward", "subs": [{"id", "name"}]} from Blizzard's achievement.

    A child criterion with no achievement behind it (Glory of the Midnight Delver has one)
    is not something a character profile can ever show as earned, so it is left out rather
    than tracked forever.
    """
    subs = [{"id": int(c["achievement"]["id"]), "name": str(c["achievement"]["name"])}
            for c in ((detail.get("criteria") or {}).get("child_criteria") or [])
            if (c.get("achievement") or {}).get("id")]
    if not subs:
        raise ValueError(f"achievement {detail.get('id')} lists no achievements under it")
    return {"id": int(detail["id"]), "name": str(detail["name"]),
            "reward": str(detail.get("reward_description") or ""), "subs": subs}


def earned(payload, ids):
    """{achievement id: completed ms} for the watched ids this character has finished. An
    entry without a completed_timestamp is progress, not an achievement."""
    wanted = set(ids)
    return {int(a["id"]): int(a["completed_timestamp"])
            for a in (payload.get("achievements") or [])
            if a.get("id") in wanted and a.get("completed_timestamp")}


def signature(summary):
    return f"{summary.get('achievement_points')}:{summary.get('last_login_timestamp')}"


def people(member_ids, mapping, roster, default_realm):
    """[{"id", "characters": [(name, realm)]}] for the members who have characters. The
    mapping carries names alone, so the realm is the guild roster's, else the guild's own."""
    out = []
    for uid in member_ids:
        chars = [(name, (roster.get(fold(name)) or {}).get("realm") or default_realm)
                 for name in mapping.get(uid) or []]
        if chars:
            out.append({"id": uid, "characters": chars})
    return out


def merge(holders, person, name, got):
    """Record one character's achievements against its player: one entry per person per
    achievement, the earliest wins, so a main and an alt are not two names on the card."""
    for aid, at in got.items():
        mine = holders.setdefault(aid, {})
        if person not in mine or at < mine[person]["at"]:
            mine[person] = {"at": at, "name": name}


def fresh(holders, announced, order):
    """Achievement ids with a holder and no announcement, oldest first, the meta (last in
    `order`) always last."""
    new = [aid for aid in order if aid in holders and aid not in announced]
    meta = order[-1]
    return sorted(new, key=lambda aid: (aid == meta, first_at(holders[aid]), order.index(aid)))


def first_at(entry):
    return min(h["at"] for h in entry.values())


def names_text(entry, limit=NAMES_ON_CARD):
    names = [h["name"] for h in sorted(entry.values(), key=lambda h: (h["at"], fold(h["name"])))]
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" +{len(names) - limit} more"
    if len(names) > 1:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return names[0] if names else ""


def sub_copy(who, name, entry, done, total, meta_name):
    return {"headline": "Raid achievement earned", "name": name,
            "lines": [f"First in {who}: {names_text(entry)}",
                      f"{done} of {total} toward {meta_name}"],
            "title": f"Raid achievement earned: {name}", "color": BRAND_ACCENT}


def meta_copy(who, meta, entry):
    lines = [f"All {len(meta['subs'])} raid achievements, first: {names_text(entry)}"]
    if meta["reward"]:
        lines.append(meta["reward"])
    return {"headline": f"{who} completed", "name": meta["name"], "lines": lines,
            "title": f"{who} completed {meta['name']}", "color": GOLD}


def payload(copy, card_url=None, iso_ts=None):
    """The message. The drawn card carries the text when there is one, as a kill card's
    does; without it the embed says the same lines itself. Pings nobody."""
    embed = {"title": copy["title"], "description": "\n".join(copy["lines"]),
             "color": copy["color"]}
    if iso_ts:
        embed["timestamp"] = iso_ts
    if card_url:
        embed["image"] = {"url": card_url}
        embed.pop("description")
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def boss_in(description, bosses):
    """Which of the raid's bosses an achievement's description is about, or None.

    The whole name first. Blizzard then shortens some ("Defeat Vashnik" for Vashnik the
    Malignant), so a boss's leading name is tried second, and only when it is long enough
    not to be an ordinary word.
    """
    text = f" {raiderio.normalize(description)} "
    for boss in sorted(bosses, key=len, reverse=True):
        if f" {raiderio.normalize(boss)} " in text:
            return boss
    for boss in bosses:
        lead = raiderio.normalize(boss).split(" ")[0]
        if len(lead) >= 5 and f" {lead} " in text:
            return boss
    return None


# ------------------------------------------------------------------ network


def _path(realm, name):
    # Blizzard drops apostrophes from realm slugs where slugify would hyphenate them.
    slug = raiderio.slugify(str(realm or "").replace("'", ""))
    return f"/profile/wow/character/{slug}/{urllib.parse.quote(str(name).lower())}"


def scan(watched, token, get, sigs, ids):
    """Read every watched character whose signature moved.

    {"holders": {achievement id: {person: {"at", "name"}}}, "sigs": {character: signature},
     "read": n, "unchanged": n, "missing": [character]}. A character Blizzard will not
    answer for (another realm, private, renamed) is `missing` and costs nothing else.
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(job):
        person, name, realm = job
        key = f"{raiderio.slugify(str(realm).replace(chr(39), ''))}/{fold(name)}"
        try:
            sig = signature(get(token, _path(realm, name), namespace="profile-us"))
            if sigs.get(key) == sig:
                return person, name, key, sig, None
            got = earned(get(token, _path(realm, name) + "/achievements",
                             namespace="profile-us"), ids)
            return person, name, key, sig, got
        except Exception as exc:                               # noqa: BLE001
            log("glory_character_missing", character=name, error=str(exc)[:120])
            return person, name, key, None, None

    jobs = [(p["id"], name, realm) for p in watched for name, realm in p["characters"]]
    out = {"holders": {}, "sigs": {}, "read": 0, "unchanged": 0, "missing": []}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for person, name, key, sig, got in pool.map(one, jobs):
            if sig is None:
                out["missing"].append(name)
                continue
            out["sigs"][key] = sig
            if got is None:
                out["unchanged"] += 1
                continue
            out["read"] += 1
            merge(out["holders"], person, name, got)
    return out
