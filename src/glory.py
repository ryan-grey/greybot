"""The raid meta achievement: "Glory of the <tier> Raider" and the achievements under it,
tracked FOR THE GUILD.

One card when the guild earns each listed achievement, then a gold one when it has earned
them all. Nobody is named: this is the guild's progress, not a raider's.

THERE IS NO GUILD RECORD TO READ. Blizzard's guild achievements are the Guild Runs -- boss
kills in a guild group -- and the Glory achievements are not among them. They exist only on
characters. So "the guild earned it" is derived: GROUP_MIN or more watched raiders earning
the same achievement within GROUP_WINDOW_MS of each other is a guild group doing it
together. One raider picking it up in a pug is one timestamp on its own and counts for
nothing, now or later -- that raider simply is not part of the group when the guild does it.

WHERE IT COMES FROM. Blizzard's character achievements profile. Two things about it shape
everything below:

  It is a SNAPSHOT TAKEN AT LOGOUT. An achievement earned at 9:40 appears when that
  character next logs out, and an alt that has not logged in since does not show it at all
  even though it is account-wide. So every mapped character is read, and what each person
  holds is KEPT between runs: the group fills in over the evening as people log out, and
  the card goes out on the run that sees the tenth.

  It is ONE TO TWO MEGABYTES per character. The character summary is 4 KB and carries the
  achievement points and last login, which change whenever the big document could have.
  That pair is the signature: an unchanged one skips the download, so a quiet half hour
  costs a few small calls rather than forty megabytes.

THE META IS THE GUILD'S TOO. It posts when every listed achievement has been earned by the
guild, whether or not any one raider holds the meta and its mount yet.

WHO IS WATCHED. The install's roll call mapping (ROLLCALL#SETUP) with the vault's character
locks applied, narrowed to one role's holders when the setup names a role.

Pure functions above the fold, network below, so the tests can drive all of it offline.
"""

import json
import urllib.parse

import raiderio

BRAND_ACCENT = 0x5CA8F0
GOLD = 0xE8B44A
GROUP_MIN = 10                 # raiders earning it together before it is the guild's
GROUP_WINDOW_MS = 60_000       # one kill stamps everyone within the same few seconds


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


def merge(held, person, got):
    """Record one character's achievements against its player. One entry per person per
    achievement and the earliest wins, so a main and an alt are one raider."""
    for aid, at in got.items():
        mine = held.setdefault(aid, {})
        if person not in mine or at < mine[person]:
            mine[person] = at


def combine(stored, scanned):
    """What everyone is known to hold: what earlier runs saw plus what this one read."""
    out = {aid: dict(entry) for aid, entry in stored.items()}
    for aid, entry in scanned.items():
        for person, at in entry.items():
            merge(out, person, {aid: at})
    return out


def group_at(entry, need=GROUP_MIN, window=GROUP_WINDOW_MS):
    """When a guild group earned this: the first moment `need` people earned it within
    `window` of each other, or None while no such group exists."""
    times = sorted((entry or {}).values())
    for i in range(len(times) - need + 1):
        if times[i + need - 1] - times[i] <= window:
            return times[i]
    return None


def guild_earned(held, ids, need=GROUP_MIN):
    """{achievement id: when} for the listed achievements a guild group has earned."""
    return {aid: at for aid in ids if (at := group_at(held.get(aid), need)) is not None}


def fresh(earned_at, announced):
    """Guild-earned achievements not yet announced, in the order they were earned."""
    return sorted((aid for aid in earned_at if aid not in announced),
                  key=lambda aid: (earned_at[aid], aid))


def sub_copy(who, name, done, total, meta_name):
    return {"headline": f"{who} earned", "name": name,
            "lines": [f"{done} of {total} toward {meta_name}"],
            "title": f"{who} earned {name}", "color": BRAND_ACCENT}


def meta_copy(who, meta):
    lines = [f"All {len(meta['subs'])} raid achievements earned"]
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

    {"held": {achievement id: {person: earned ms}}, "sigs": {character: signature},
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
    out = {"held": {}, "sigs": {}, "read": 0, "unchanged": 0, "missing": []}
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
            merge(out["held"], person, got)
    return out
