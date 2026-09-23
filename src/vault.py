"""The weekly vault and gear check: every Prog Raider's raid and Mythic+ vault slots for the
week that just reset, and their gems and enchants as equipped, posted to an officers-only
channel on Tuesday morning. One row per raider: Discord name -> character, then vault, gems,
enchants.

GEAR. Blizzard's equipment profile, read when the report runs. An enchant is missing from a
slot that takes one (ENCHANT_SLOTS) or low when its crafted rank is below the top; a gem is
missing from an empty socket or low when it is below the season's top gem rank. See
gear_check.

WHAT COUNTS. The vault's own rules, restricted to the two rows the raid team cares about:

  Raid     unique current-season bosses killed on Heroic or Mythic; 2 / 4 / 6 fill 1 / 2 / 3.
  Mythic+  completed keystone runs; 1 / 4 / 8 fill 1 / 2 / 3, and a slot is worth the level
           of the Nth-best run -- so a +10 slot is filled when that run was +10 or higher.

A raider is flagged when fewer than two Mythic+ slots reached +10, i.e. fewer than four runs
at +10 or above. Only flagged raiders get the Discord user -> character line on the card;
everyone else is shown by character alone.

WHERE THE NUMBERS COME FROM, and what each misses:

  Mythic+  Raider.IO's weekly-highest-level runs (up to ten per character, which covers the
           eighth-best run the third slot needs), filtered to the reset window by completion
           time so a week Raider.IO has not rolled over yet cannot leak in. Raider.IO learns
           runs from Blizzard and from its own crawls; a run it never saw is not counted.
  Raid     Blizzard's per-character encounter profile (last kill per boss per difficulty)
           unioned with the guild's own Warcraft Logs kills in the window. Blizzard catches
           pug kills the guild never logged; the logs catch kills Blizzard has since
           overwritten -- which it does the moment a boss dies again after reset, so a
           re-run after Tuesday's raid still counts the week correctly for logged kills.

WHO IS WHO. Prog Raiders are the members holding the prog role. Their characters come from the
prog-raid roll call mapping (ROLLCALL#SETUP), the same one the attendance card uses -- one map,
maintained once. A member with several characters is reported on the one that raided with the
guild that week, falling back to the highest item level.

Pure functions above the fold, network below, so the tests can drive all of it offline.
"""

import json
import re
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone

import raiderio

RESET_WEEKDAY = 1                  # Tuesday
RESET_HOUR_UTC = 15                # US weekly reset
RAID_THRESHOLDS = (2, 4, 6)
MPLUS_THRESHOLDS = (1, 4, 8)
MPLUS_LEVEL = 10
MIN_MPLUS_SLOTS = 2
RAID_MODES = ("HEROIC", "MYTHIC")  # Blizzard's difficulty types
WCL_DIFFICULTIES = (4, 5)          # Warcraft Logs: Heroic, Mythic
RAID_MIN_SIZE = 10                 # a five-player dungeon is never a raid kill


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, default=str))


# ------------------------------------------------------------------ the week


def week_window(now):
    """(start, end) of the most recently COMPLETED vault week: the last two US resets."""
    now = now.astimezone(timezone.utc)
    end = now.replace(hour=RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    end -= timedelta(days=(now.weekday() - RESET_WEEKDAY) % 7)
    if end > now:
        end -= timedelta(days=7)
    return end - timedelta(days=7), end


def week_label(start, end):
    """"Sep 15 – 22", or "Sep 29 – Oct 6" across a month: reset to reset."""
    if start.month == end.month:
        return f"{start:%b} {start.day} – {end.day}"
    return f"{start:%b} {start.day} – {end:%b} {end.day}"


# ------------------------------------------------------------------ slots


def filled(count, thresholds):
    return sum(1 for t in thresholds if count >= t)


def fold(name):
    """A character-name key that survives Warcraft Logs, Raider.IO and Blizzard disagreeing
    on case and Unicode normal form ("Fûrry")."""
    return unicodedata.normalize("NFC", str(name or "")).casefold().strip()


def _stamp(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def mplus_levels(profile, start, end):
    """Key levels of every completed run in the window, highest first, one per run."""
    runs = {}
    for field in ("mythic_plus_previous_weekly_highest_level_runs",
                  "mythic_plus_weekly_highest_level_runs"):
        for run in (profile or {}).get(field) or []:
            try:
                at = _stamp(run["completed_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= at < end:
                runs[run.get("url") or f"{run.get('dungeon')}|{run['completed_at']}"] = int(
                    run.get("mythic_level") or 0)
    return sorted(runs.values(), reverse=True)


def mplus_slots(levels, level=MPLUS_LEVEL):
    """(slots at `level`+, slot levels): the second is what each of the three would pay out
    at, None where the slot is empty."""
    at_level = sum(1 for lv in levels if lv >= level)
    return (filled(at_level, MPLUS_THRESHOLDS),
            [levels[t - 1] if len(levels) >= t else None for t in MPLUS_THRESHOLDS])


def blizzard_bosses(encounters, start, end):
    """Normalised boss names this character killed on Heroic or Mythic inside the window,
    from Blizzard's encounters/raids profile. Only the "Current Season" group counts -- it
    is Blizzard's own list of the raids the vault is paying out for."""
    lo, hi = start.timestamp() * 1000, end.timestamp() * 1000
    bosses = set()
    for exp in (encounters or {}).get("expansions") or []:
        if ((exp.get("expansion") or {}).get("name") or "") != "Current Season":
            continue
        for inst in exp.get("instances") or []:
            for mode in inst.get("modes") or []:
                if ((mode.get("difficulty") or {}).get("type") or "") not in RAID_MODES:
                    continue
                for enc in ((mode.get("progress") or {}).get("encounters")) or []:
                    at = enc.get("last_kill_timestamp") or 0
                    name = (enc.get("encounter") or {}).get("name")
                    if name and lo <= at < hi:
                        bosses.add(raiderio.normalize(name))
    return bosses


def wcl_bosses(reports, start, end):
    """{folded character name: set of normalised boss names} from Warcraft Logs report
    details (wcl.report_detail) -- Heroic and Mythic raid kills inside the window."""
    lo, hi = start.timestamp() * 1000, end.timestamp() * 1000
    out = {}
    for rep in reports:
        actors = {int(a["id"]): a["name"] for a in
                  ((rep.get("masterData") or {}).get("actors")) or []
                  if a.get("id") is not None and a.get("name")}
        base = rep.get("startTime") or 0
        for f in rep.get("fights") or []:
            if not f.get("kill") or not f.get("encounterID"):
                continue
            if int(f.get("difficulty") or 0) not in WCL_DIFFICULTIES:
                continue
            if int(f.get("size") or 0) and int(f["size"]) < RAID_MIN_SIZE:
                continue
            if not lo <= base + (f.get("endTime") or 0) < hi:
                continue
            boss = raiderio.normalize(f.get("name"))
            for pid in f.get("friendlyPlayers") or []:
                name = actors.get(int(pid))
                if name:
                    out.setdefault(fold(name), set()).add(boss)
    return out


def wcl_realms(reports):
    """{folded character name: realm} as the logs recorded it -- the only realm on record
    for a raider whose character is not in the guild."""
    return {fold(a["name"]): a["server"]
            for rep in reports
            for a in ((rep.get("masterData") or {}).get("actors")) or []
            if a.get("name") and a.get("server")}


def union_bosses(*sets):
    """One set of bosses from several sources, without counting "Dimensius" and
    "Dimensius, the All-Devouring" as two kills."""
    known = set()
    for group in sets:
        for boss in sorted(group or (), key=len):
            if not raiderio.alias_match(known, boss):
                known.add(boss)
    return known


# ------------------------------------------------------------------ who is who


def display_name(member):
    user = member.get("user") or {}
    return (member.get("nick") or user.get("global_name") or user.get("username") or "").strip()


def avatar_url(member, guild_id):
    user = member.get("user") or {}
    if member.get("avatar"):
        return (f"https://cdn.discordapp.com/guilds/{guild_id}/users/{user['id']}/avatars/"
                f"{member['avatar']}.png?size=64")
    if user.get("avatar"):
        return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=64"
    return ""


def choose(characters, raided, profiles):
    """The one character to report for a member: whichever raided with the guild most that
    week, then the highest item level, then the order the mapping lists them in."""
    found = [c for c in characters if profiles.get(fold(c))]
    if not found:
        return None
    return max(found, key=lambda c: (len(raided.get(fold(c)) or ()),
                                     ((profiles[fold(c)].get("gear") or {})
                                      .get("item_level_equipped") or 0),
                                     -characters.index(c)))


# ------------------------------------------------------------------ gems and enchants
#
# Midnight's enchantable slots: head, shoulders, chest, legs, feet, both rings, the weapon,
# and the off hand only when it is a weapon -- not a shield or a held-in-off-hand item.
# Checked against every Prog Raider's equipment on 2026-09-23: every slot here was enchanted
# on nearly everyone, and no other slot on anyone. This list follows the expansion.
ENCHANT_SLOTS = ("HEAD", "SHOULDER", "CHEST", "LEGS", "FEET", "FINGER_1", "FINGER_2",
                 "MAIN_HAND")
SLOT_NAMES = {"HEAD": "Head", "NECK": "Neck", "SHOULDER": "Shoulder", "CHEST": "Chest",
              "WAIST": "Belt", "LEGS": "Legs", "FEET": "Feet", "WRIST": "Wrist",
              "HANDS": "Hands", "FINGER_1": "Ring 1", "FINGER_2": "Ring 2", "BACK": "Back",
              "MAIN_HAND": "Weapon", "OFF_HAND": "Off hand"}
# A crafted enchant's rank rides in its display string as a chat-icon atlas:
# "Professions-ChatIcon-Quality-12-Tier2" is rank 2 of Midnight's two ("12"); the Dragonflight
# and War Within form "Quality-Tier3" had three. An enchant with no marker (a death knight's
# runeforge) has no ranks and is never low.
ENCHANT_RANK = re.compile(r"Quality-(?:(\d+)-)?Tier(\d)")
GEM_QUALITIES = ("RARE", "EPIC")   # uncommon gems are a whole tier below


def enchant_rank(display):
    """(rank, top rank) or None when the enchant has no ranks."""
    m = ENCHANT_RANK.search(display or "")
    if not m:
        return None
    return int(m.group(2)), int(m.group(1)[-1]) if m.group(1) else 3


def best_gem_level(gems):
    """The item level of the top gem rank this season, read from the gems themselves:
    rank 2 is item level 295 and rank 1 is 278 in Midnight Season 2. Taken from what the
    raid team has socketed rather than hard-coded, so next season needs no edit."""
    return max((level for quality, level in gems.values() if quality in GEM_QUALITIES),
               default=0)


def gear_check(equipment, gems, best):
    """What is missing or below max rank on one character. `gems` maps gem item id to
    (quality, item level) from Blizzard's item data; `best` is best_gem_level's answer."""
    out = {"enchant_missing": [], "enchant_low": [], "gem_empty": [], "gem_low": []}
    for item in (equipment or {}).get("equipped_items") or []:
        slot = (item.get("slot") or {}).get("type") or ""
        name = SLOT_NAMES.get(slot, slot.title())
        permanent = [e for e in item.get("enchantments") or []
                     if ((e.get("enchantment_slot") or {}).get("type")) == "PERMANENT"]
        wants = slot in ENCHANT_SLOTS or (
            slot == "OFF_HAND" and ((item.get("item_class") or {}).get("name")) == "Weapon")
        if wants and not permanent:
            out["enchant_missing"].append(name)
        for e in permanent:
            rank = enchant_rank(e.get("display_string"))
            if rank and rank[0] < rank[1]:
                out["enchant_low"].append(name)
        for socket in item.get("sockets") or []:
            gem = (socket.get("item") or {}).get("id")
            if not gem:
                out["gem_empty"].append(name)
                continue
            quality, level = gems.get(int(gem), (None, 0))
            if quality is not None and (quality not in GEM_QUALITIES or level < best):
                out["gem_low"].append(name)
    return out


ROLE = {"TANK": "tank", "HEALING": "healer", "DPS": "dps"}
STALE = timedelta(days=1)


def stale_since(profile, end):
    """The date Raider.IO last refreshed this character, when that was more than a day
    before the week ended -- runs after it may be missing, and a short count that might be
    Raider.IO's rather than the raider's should say so. None when fresh or unknown."""
    try:
        seen = _stamp(profile["last_crawled_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return seen.date().isoformat() if seen < end - STALE else None


def build(members, mapping, profiles, encounters, raided, start, end, equipment=None,
          gems=None):
    """One row per prog raider, those with something to fix first.

    `members` are Discord guild-member objects already filtered to the role; `mapping` is
    {discord id: [character, ...]}; `profiles`, `encounters` and `equipment` are keyed by
    folded character name; `raided` is wcl_bosses' output; `gems` maps gem item id to
    (quality, item level). A character Blizzard would not show equipment for gets
    "gear": None, which the card prints as unknown rather than as clean.
    """
    equipment, gems = equipment or {}, gems or {}
    best = best_gem_level(gems)
    rows = []
    for m in members:
        uid = str((m.get("user") or {}).get("id") or "")
        chars = list(mapping.get(uid) or [])
        name = choose(chars, raided, profiles)
        row = {"id": uid, "member": display_name(m), "character": name, "mapped": bool(chars)}
        if name:
            profile = profiles[fold(name)]
            levels = mplus_levels(profile, start, end)
            slots, at = mplus_slots(levels)
            bosses = union_bosses(blizzard_bosses(encounters.get(fold(name)), start, end),
                                  raided.get(fold(name)))
            row.update({
                "character": profile.get("name") or name,
                "realm": profile.get("realm") or "",
                "class": profile.get("class") or "",
                "role": ROLE.get(str(profile.get("active_spec_role") or "").upper(), ""),
                "runs": len(levels), "runs_at_level": sum(1 for lv in levels if lv >= MPLUS_LEVEL),
                "mplus_slots": slots, "mplus_levels": at,
                "bosses": len(bosses), "raid_slots": filled(len(bosses), RAID_THRESHOLDS),
                "seen": stale_since(profile, end),
                "gear": (gear_check(equipment[fold(name)], gems, best)
                         if fold(name) in equipment else None),
            })
        else:
            row.update({"runs": 0, "runs_at_level": 0, "mplus_slots": 0,
                        "mplus_levels": [None] * 3, "bosses": 0, "raid_slots": 0,
                        "gear": None})
        row["flagged"] = row["mplus_slots"] < MIN_MPLUS_SLOTS
        row["issues"] = int(row["flagged"]) + sum(len(v) for v in (row["gear"] or {}).values())
        rows.append(row)
    rows.sort(key=lambda r: (not r["issues"], not r["flagged"], r["mplus_slots"],
                             -r["issues"], fold(r.get("character") or r["member"])))
    return rows


def gear_text(gear):
    """"missing Shoulder; low Feet" -- the slots behind a card's count, for the post's text."""
    if gear is None:
        return None
    parts = []
    for label, keys in (("enchants", ("enchant_missing", "enchant_low")),
                        ("gems", ("gem_empty", "gem_low"))):
        bits = [f"{'missing' if k.endswith(('missing', 'empty')) else 'low rank'} "
                f"{', '.join(gear[k])}" for k in keys if gear[k]]
        if bits:
            parts.append(f"{label}: {'; '.join(bits)}")
    return " · ".join(parts)


# ------------------------------------------------------------------ the post


def payload(rows, start, end, team_name, has_card):
    """The Discord message. Every raider with something to fix is also listed as text with
    the slots behind the card's counts, mention-shaped so the names resolve in the client,
    with every mention suppressed -- the card is a report for officers, not a ping to the
    people on it."""
    short = [r for r in rows if r["flagged"]]
    geared = [r for r in rows if (r["gear"] or {}) and any(r["gear"].values())]
    label = week_label(start, end)
    lines = [f"**{len(short)} of {len(rows)}** short of {MIN_MPLUS_SLOTS} Mythic+ vault slots at "
             f"+{MPLUS_LEVEL} · **{len(geared)}** with gems or enchants to fix."]
    for r in rows:
        if not r["issues"] and r.get("realm"):
            continue
        who = f"<@{r['id']}>" if r["id"] else r["member"]
        if not r.get("realm"):
            lines.append(f"{who} → no character on file")
            continue
        bits = []
        if r["flagged"]:
            vault_bit = f"M+ {r['runs_at_level']} of 4 at +{MPLUS_LEVEL}"
            if r.get("seen"):
                seen = datetime.fromisoformat(r["seen"])
                vault_bit += f" (Raider.IO last updated {seen:%b} {seen.day})"
            bits.append(vault_bit)
        if gear_text(r["gear"]):
            bits.append(gear_text(r["gear"]))
        lines.append(f"{who} → {r['character']} · " + " · ".join(bits))
    embed = {"title": f"Vault & gear check · {label}", "description": "\n".join(lines)[:4000],
             "color": 0x4493F8,
             "footer": {"text": f"{team_name} · vault: Heroic+ bosses and completed keys, "
                                f"reset to reset · gear: as equipped at posting"}}
    if has_card:
        embed["image"] = {"url": "attachment://vault.png"}
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


# ------------------------------------------------------------------ network


def fetch_members(get, guild_id, role_id):
    """Every member holding `role_id`, via the paged members list (needs the members intent,
    which the gateway worker already has)."""
    out, after = [], "0"
    while True:
        page = get(f"/guilds/{guild_id}/members?limit=1000&after={after}")
        out.extend(m for m in page if str(role_id) in (m.get("roles") or [])
                   and not (m.get("user") or {}).get("bot"))
        if len(page) < 1000:
            return out
        after = page[-1]["user"]["id"]


def fetch_profiles(characters, roster, logged, region, default_realm, get=raiderio._get):
    """{folded name: Raider.IO profile}. The mapping carries names alone and several raiders
    live on other realms, so the realm comes from the guild roster, then from the logs
    (`logged`, wcl_realms' output), then the guild's own realm."""
    out = {}
    for name in characters:
        realm = ((roster.get(fold(name)) or {}).get("realm") or logged.get(fold(name))
                 or default_realm)
        try:
            out[fold(name)] = get("https://raider.io/api/v1/characters/profile", {
                "region": region, "realm": realm, "name": name,
                "fields": "mythic_plus_previous_weekly_highest_level_runs,"
                          "mythic_plus_weekly_highest_level_runs,gear"})
        except raiderio.RaiderIOError as exc:
            log("vault_profile_missing", character=name, error=str(exc)[:120])
    return out


def fetch_equipment(profiles, token, get):
    """{folded name: Blizzard equipment profile}, as the character stands right now."""
    out = {}
    for key, profile in profiles.items():
        realm = raiderio.slugify(str(profile.get("realm") or "").replace("'", ""))
        name = urllib.parse.quote(str(profile.get("name") or "").lower())
        try:
            out[key] = get(token, f"/profile/wow/character/{realm}/{name}/equipment",
                           namespace="profile-us")
        except Exception as exc:                               # noqa: BLE001
            log("vault_equipment_missing", character=profile.get("name"), error=str(exc)[:120])
    return out


def fetch_gems(equipment, token, get):
    """{gem item id: (quality, item level)} for every gem anyone has socketed. One static
    lookup per distinct gem -- a raid team shares a dozen or so."""
    ids = {int(s["item"]["id"]) for eq in equipment.values()
           for item in eq.get("equipped_items") or []
           for s in item.get("sockets") or [] if (s.get("item") or {}).get("id")}
    out = {}
    for gem in sorted(ids):
        try:
            data = get(token, f"/data/wow/item/{gem}")
            out[gem] = (((data.get("quality") or {}).get("type")), int(data.get("level") or 0))
        except Exception as exc:                               # noqa: BLE001
            log("vault_gem_unknown", gem=gem, error=str(exc)[:120])
    return out


def fetch_encounters(profiles, token, get):
    """{folded name: Blizzard encounters/raids profile}; a character Blizzard will not
    answer for (private, transferred) simply contributes no Blizzard kills."""
    out = {}
    for key, profile in profiles.items():
        # Blizzard drops apostrophes from realm slugs ("Vek'nilash" -> veknilash) where
        # slugify would hyphenate them.
        realm = raiderio.slugify(str(profile.get("realm") or "").replace("'", ""))
        name = urllib.parse.quote(str(profile.get("name") or "").lower())
        try:
            out[key] = get(token, f"/profile/wow/character/{realm}/{name}/encounters/raids",
                           namespace="profile-us")
        except Exception as exc:                               # noqa: BLE001
            log("vault_encounters_missing", character=profile.get("name"), error=str(exc)[:120])
    return out
