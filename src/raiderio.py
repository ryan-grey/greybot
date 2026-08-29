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
        self.by_boss = {}
        for raid in raids:
            slug = raid.get("slug")
            if not slug:
                continue
            encounters = [e.get("name", "") for e in (raid.get("encounters") or [])]
            meta = {"slug": slug, "name": raid.get("name") or slug,
                    "shortName": raid.get("short_name") or "",
                    "encounters": encounters,
                    "starts": raid.get("starts") or {}, "ends": raid.get("ends") or {}}
            self.raids[slug] = meta
            for enc in encounters:
                # First raid to claim a boss name wins. Names are unique across raids in
                # practice; if that ever stops being true the collision is logged by the
                # caller as a rung-1 result that disagrees with rung 3.
                self.by_boss.setdefault(normalize(enc), meta)

    def __bool__(self):
        return bool(self.raids)

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
    """Load the static raid list, widening the expansion search until it covers the raids
    the guild's profile actually reports.

    Anchoring on the profile rather than on a hardcoded expansion number is what carries
    this across an expansion launch: the day raid_progression starts naming raids the
    hinted expansion has never heard of, the search walks forward and finds them.
    """
    keys = set((profile.get("raid_progression") or {}).keys())
    tried, best = [], RaidIndex([])
    for exp in [expansion_hint, expansion_hint + 1, expansion_hint + 2, expansion_hint - 1]:
        if exp < 1 or exp in tried:
            continue
        tried.append(exp)
        idx = RaidIndex(static_raids(exp))
        if not idx:
            continue
        if not best:
            best = idx
        if keys & set(idx.raids):
            return idx, exp
    return best, (tried[0] if tried else expansion_hint)


def resolve_raid(profile, boss_name, zone_name, killed_at, region, index):
    """Which raid slug does this kill belong to? Returns (slug, meta, how)."""
    progression = profile.get("raid_progression") or {}

    if index:
        meta = index.by_boss.get(normalize(boss_name))
        if meta and meta["slug"] in progression:
            return meta["slug"], meta, "encounter-name"

    zone_slug = slugify(zone_name)
    if zone_slug and zone_slug in progression:
        return zone_slug, (index.raids.get(zone_slug) if index else None), "zone-slug"

    if index and killed_at:
        live = [m for m in index.live_at(killed_at, region) if m["slug"] in progression]
        if len(live) == 1:
            return live[0]["slug"], live[0], "live-window"

    if progression:
        slug = list(progression)[-1]
        return slug, (index.raids.get(slug) if index else None), "last-key-fallback"

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
    if not encounters or killed is None:
        return names, "history-only"

    if total and killed >= total:
        before = len(names)
        names |= {normalize(e) for e in encounters}
        return names, ("history-only" if len(names) == before else "cleared-tier")

    if killed > len(names):
        names |= {normalize(e) for e in encounters[:killed]}
        return names, "assumed-kill-order"

    return names, "history-only"
