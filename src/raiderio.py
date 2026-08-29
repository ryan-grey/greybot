"""Raider.IO client — the ENRICHMENT source: how many bosses down, and the realm rank.

No API key, so this is a plain GET. The interesting work here is not fetching, it is
deciding WHICH raid a kill belongs to.

The obvious approach -- slugify the Warcraft Logs zone name and look it up in
raid_progression -- looks fine until you check the live data. The keys currently present
are `tier-mn-1`, `sporefall`, `the-tidebound-grotto` and `the-venomous-abyss`, and the
first of those is named "MN Tier 1 (VS / DR / MQD)". No slugification of any zone name
Warcraft Logs reports will ever produce `tier-mn-1`, so that approach silently attributes
those kills to the wrong raid, which corrupts the "n of total" count rather than failing
loudly.

Raider.IO publishes the mapping directly at /raiding/static-data: every raid, with its
slug and its ORDERED list of encounters by name. So the raid is resolved from the boss
that actually died, which is the one fact the event source is certain about. Four rungs,
tried in order, and the rung that fired is logged on every announcement:

    1. encounter name matches a boss in a known raid   <- the reliable one
    2. the Warcraft Logs zone name slugifies to a key present in raid_progression
    3. exactly one raid's live window contains the kill timestamp
    4. the last key in raid_progression (Raider.IO appends the newest tier last)

Rung 1 also yields the ordered encounter list, which is how the final boss is identified
for AOTC without hardcoding a boss name per tier.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROFILE_URL = "https://raider.io/api/v1/guilds/profile"
STATIC_URL = "https://raider.io/api/v1/raiding/static-data"

_static_cache = {}


class RaiderIOError(RuntimeError):
    pass


def _get(url, params, timeout=15):
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"Accept": "application/json",
                                                "User-Agent": "scrambled-raid-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise RaiderIOError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RaiderIOError(f"network error: {exc.reason}") from exc


def guild_profile(region, realm, name):
    return _get(PROFILE_URL, {"region": region, "realm": realm, "name": name,
                              "fields": "raid_progression,raid_rankings"})


def static_raids(expansion_id):
    if expansion_id not in _static_cache:
        try:
            data = _get(STATIC_URL, {"expansion_id": int(expansion_id)})
            _static_cache[expansion_id] = data.get("raids") or []
        except RaiderIOError:
            _static_cache[expansion_id] = []
    return _static_cache[expansion_id]


def normalize(text):
    """Fold a boss name to a comparison key.

    Warcraft Logs and Raider.IO agree on boss names but not always on their punctuation --
    "Vaelgor & Ezzorak" against "Vaelgor and Ezzorak", "Belo'ren, Child of Al'ar" against
    the same name without the apostrophes. Comparing raw strings loses those matches and
    drops the lookup to a weaker rung for no reason.
    """
    t = (text or "").lower().replace("&", " and ")
    # Apostrophes are DELETED, not turned into separators. Warcraft Logs and Raider.IO do
    # not agree on straight versus typographic apostrophes, and WoW boss names are full of
    # them -- folding "Belo'ren" to "belo ren" makes it stop matching "Beloren".
    t = re.sub(r"[\u2018\u2019'`\u00b4]", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def slugify(text):
    t = (text or "").lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


class RaidIndex:
    """Raids from one or more expansions, indexed by slug and by boss name."""

    def __init__(self, raids):
        self.raids = {}
        self.by_boss = {}          # normalised boss name -> [raid meta, ...]
        for raid in raids:
            slug = raid.get("slug")
            if not slug:
                continue
            encounters = [e.get("name", "") for e in (raid.get("encounters") or [])]
            meta = {"slug": slug, "name": raid.get("name") or slug,
                    "shortName": raid.get("short_name") or "",
                    "icon": raid.get("icon") or "",
                    "encounters": encounters,
                    "starts": raid.get("starts") or {}, "ends": raid.get("ends") or {}}
            self.raids[slug] = meta
            for enc in encounters:
                self.by_boss.setdefault(normalize(enc), []).append(meta)

    def __bool__(self):
        return bool(self.raids)

    def find_boss(self, boss_name, progression):
        """Every raid containing this boss, preferring one the guild's profile tracks.

        The index spans more than one expansion, so a name can legitimately appear twice.
        Preferring the tracked raid means an ambiguous name resolves to the tier actually
        being raided rather than to whichever expansion happened to load first.
        """
        metas = self.by_boss.get(normalize(boss_name)) or []
        tracked = [m for m in metas if m["slug"] in (progression or {})]
        return (tracked or metas)

    def live_at(self, when, region):
        """Raids whose published window contains `when`, for this region."""
        out = []
        for meta in self.raids.values():
            start, end = meta["starts"].get(region), meta["ends"].get(region)
            if not start:
                continue
            try:
                s = datetime.fromisoformat(start.replace("Z", "+00:00"))
                e = (datetime.fromisoformat(end.replace("Z", "+00:00"))
                     if end else datetime.max.replace(tzinfo=timezone.utc))
            except ValueError:
                continue
            if s <= when <= e:
                out.append(meta)
        return out


def build_index(profile, expansion_hint=11):
    """Static raid data across the current expansion AND its neighbours.

    Loading only the expansion matching raid_progression is not enough. Guilds keep
    killing last expansion's bosses -- mounts, transmog, achievements -- and a boss the
    index has never heard of cannot be recognised as out of scope. It has to be
    identifiable in order to be *rejected*, which is why the previous expansion is loaded
    even though none of its raids appear in raid_progression.

    Anchoring on the profile is what carries this across an expansion launch: if the
    hinted range does not cover the raids the guild's own profile reports, the search
    walks forward until it does.
    """
    keys = set((profile.get("raid_progression") or {}).keys())
    raids, tried, covered = [], [], set()

    def add(exp):
        if exp < 1 or exp in tried:
            return
        tried.append(exp)
        found = static_raids(exp)
        raids.extend(found)
        covered.update(r.get("slug") for r in found)

    add(expansion_hint)
    add(expansion_hint - 1)          # last tier's bosses, so they can be RECOGNISED
    for step in (1, 2, -2):          # widen only if the profile names something unseen
        if keys and keys.issubset(covered):
            break
        add(expansion_hint + step if step > 0 else expansion_hint + step)

    return RaidIndex(raids), tried


def resolve_raid(profile, boss_name, zone_name, killed_at, region, index):
    """Which raid slug does this kill belong to? Returns (slug, meta, how).

    A slug of None means "do not attribute this kill to anything", which is a valid and
    important answer. The version of this function that always returned *something* --
    falling back to the newest key in raid_progression when nothing else matched -- turned
    every unrecognised boss into a phantom kill in the current tier. Scrambled's logs still
    contain Heroic Manaforge Omega kills from the previous expansion; those eight bosses
    were seeded into the-venomous-abyss, which has eight bosses of its own, and the next
    real kill there would have read "8 of 8" and fired AOTC. Guessing was worse than
    declining to answer, by a distance.
    """
    progression = profile.get("raid_progression") or {}

    if index:
        metas = index.find_boss(boss_name, progression)
        if metas:
            meta = metas[0]
            if meta["slug"] in progression:
                return meta["slug"], meta, "encounter-name"
            # Recognised, and it belongs to a raid this profile does not track -- an old
            # expansion's tier being farmed. That is the STRONGEST signal available that
            # the kill is out of scope, so it stops here rather than falling through to
            # weaker rungs that would eventually guess.
            return None, meta, "known-raid-not-tracked"

    zone_slug = slugify(zone_name)
    if zone_slug and zone_slug in progression:
        return zone_slug, (index.raids.get(zone_slug) if index else None), "zone-slug"

    if index and killed_at:
        live = [m for m in index.live_at(killed_at, region) if m["slug"] in progression]
        if len(live) == 1:
            return live[0]["slug"], live[0], "live-window"

    # Deliberately no last-resort guess. An unattributable kill is skipped and logged.
    return None, None, "unresolved"


def progress_for(profile, slug):
    """(heroic_bosses_killed, total_bosses) for a raid, or (None, None) if absent."""
    entry = (profile.get("raid_progression") or {}).get(slug) or {}
    killed, total = entry.get("heroic_bosses_killed"), entry.get("total_bosses")
    if killed is None or total is None:
        return None, None
    return int(killed), int(total)


def realm_rank(profile, slug, difficulty="heroic"):
    """Realm rank, or None. Raider.IO writes 0 for 'not ranked yet', which must not be
    rendered as "Ranked server #0" -- an unranked guild is not the zeroth best guild."""
    ranks = ((profile.get("raid_rankings") or {}).get(slug) or {}).get(difficulty) or {}
    rank = ranks.get("realm")
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None


def seed_names(meta, killed, total, history_names):
    """Which bosses should a tier be seeded as already-killed?

    Warcraft Logs history is the exact answer when it is available. It often is not: a
    tier cleared longer ago than the lookback reaches, or a guild whose logs are private,
    both return nothing. That is the dangerous case rather than a harmless one -- seeding
    a tier as empty when the guild has actually cleared it means the next transmog run
    through it announces nine "first kills" that happened months ago.

    So Raider.IO's count fills the gap, against its ordered encounter list:

      * fully cleared  -> seed every boss. No guessing involved; killed == total says
                          all of them are dead, whatever order they died in.
      * partly cleared -> seed the first `killed` bosses in the published order, unioned
                          with whatever history did show. Heroic is cleared in order in
                          all but the rarest cases, and the union means history always
                          wins where it exists.

    Returns (names, basis) so the caller can log which of those actually happened rather
    than leaving an assumption unrecorded.
    """
    names = {normalize(n) for n in (history_names or []) if n}
    encounters = [e for e in ((meta or {}).get("encounters") or []) if e]
    if not encounters:
        return names, "history-only"

    # Belt and braces against a resolution mistake: a tier can only ever be seeded with
    # bosses it actually contains. Anything else is dropped rather than inflating the
    # count, because an inflated seed is what manufactures a false AOTC.
    known = {normalize(e) for e in encounters}
    foreign = names - known
    names &= known
    if killed is None:
        return names, ("history-only" if not foreign else "history-filtered")

    if total and killed >= total:
        before = len(names)
        names |= {normalize(e) for e in encounters}
        return names, ("history-only" if len(names) == before else "cleared-tier")

    if killed > len(names):
        names |= {normalize(e) for e in encounters[:killed]}
        return names, "assumed-kill-order"

    return names, "history-only"


# Blizzard's own icon CDN rather than Wowhead's. It is first-party, and it happens to
# accept BOTH forms Raider.IO hands out -- some raids give an icon name
# ("inv_achievement_raid_darkwell") and others a bare FileDataID ("8039569"), with no
# apparent rule. Both resolve here; only one of them resolves on Wowhead's.
ICON_CDN = "https://render.worldofwarcraft.com/us/icons/56/{}.jpg"


def icon_url(meta):
    """Raid icon URL, or None. 56px is the largest size this CDN publishes for icons --
    real per-boss portraits need the Blizzard Game Data API and its credentials."""
    icon = (meta or {}).get("icon") or ""
    icon = str(icon).strip()
    if not icon or "/" in icon or ".." in icon:
        return None
    return ICON_CDN.format(icon)


def profile_url(profile, region, realm, name):
    """The guild's Raider.IO page.

    Raider.IO's API terms require that a public-facing application using their data links
    back to raider.io, and this bot's "n of X" and realm rank both come from them. The
    response already carries the canonical URL, so use that and only construct one if it
    is ever absent.
    """
    url = (profile or {}).get("profile_url")
    if url:
        return url
    return (f"https://raider.io/guilds/{region}/{realm}/"
            + urllib.parse.quote(name))


def guild_display(profile, name, realm_slug):
    """'Scrambled · Proudmoore' -- the realm as Raider.IO spells it, not the slug."""
    realm = (profile or {}).get("realm") or realm_slug.replace("-", " ").title()
    return f"{name} \u00b7 {realm}"
