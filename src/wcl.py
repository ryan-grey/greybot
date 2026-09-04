"""Warcraft Logs v2 client — the EVENT source: which boss died, and when.

Two things about this API shape the code more than anything else.

First, the rate limit is points per hour, not requests per hour, and a query's cost
depends on what you ask for rather than how often you ask. Guessing a "safe" poll
interval is therefore guessing at the wrong variable. Every query here asks for
`rateLimitData` alongside the real payload -- it rides along for free -- and the caller
backs off on the number it reports rather than on a hardcoded interval.

Second, a fight's startTime is a millisecond OFFSET from its report's startTime, not an
absolute timestamp. Treating it as absolute puts every kill in January 1970, which is
only obvious once an AOTC announcement is dated 56 years ago.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

# WCL difficulty ids for retail raids. Heroic is 4; the others are here so the constant
# is self-documenting rather than a bare 4 three files away from its meaning.
LFR, NORMAL, HEROIC, MYTHIC = 1, 3, 4, 5

_token = {"value": None, "expires_at": 0.0}


class WCLError(RuntimeError):
    pass


def _post(url, data, headers, timeout=20):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise WCLError(f"HTTP {exc.code} from {urllib.parse.urlparse(url).path}: {body}") from exc
    except urllib.error.URLError as exc:
        raise WCLError(f"network error calling {urllib.parse.urlparse(url).path}: {exc.reason}") from exc


def get_token(client_id, client_secret, now=None):
    """Client-credentials token, cached in module scope across warm invocations.

    WCL issues these with a very long life, so re-minting one on every poll is pure waste.
    The 60s safety margin is there because the token is checked here but spent a moment
    later; expiring in between would surface as a confusing 401 on the real query.
    """
    now = now if now is not None else time.time()
    if _token["value"] and now < _token["expires_at"] - 60:
        return _token["value"]

    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    import base64
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = _post(TOKEN_URL, body, {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    tok = payload.get("access_token")
    if not tok:
        raise WCLError("token response carried no access_token")
    _token["value"] = tok
    _token["expires_at"] = now + float(payload.get("expires_in", 3600))
    return tok


def query(token, document, variables=None):
    body = json.dumps({"query": document, "variables": variables or {}}).encode()
    payload = _post(API_URL, body, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    if payload.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in payload["errors"])
        raise WCLError(f"GraphQL errors: {msgs}")
    return payload.get("data") or {}


RATE = "rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }"

RATE_ONLY_Q = """
query { %s }
""" % RATE

GUILD_Q = """
query($name: String!, $server: String!, $region: String!) {
  %s
  guildData {
    guild(name: $name, serverSlug: $server, serverRegion: $region) {
      id
      name
      server { slug region { slug } }
    }
  }
}
""" % RATE

REPORTS_Q = """
query($guildID: Int!, $start: Float!, $limit: Int!, $difficulty: Int!, $page: Int!) {
  %s
  reportData {
    reports(guildID: $guildID, startTime: $start, limit: $limit, page: $page) {
      data {
        code
        startTime
        endTime
        zone { id name }
        fights(killType: Kills, difficulty: $difficulty) {
          id
          encounterID
          name
          kill
          difficulty
          startTime
          endTime
        }
      }
    }
  }
}
""" % RATE


# The same two queries for a USER's reports rather than a guild's. A raid team that logs
# under a personal account -- Meer's Raid uploads through one raider's Warcraft Logs
# login, unattached to the guild -- is invisible to reports(guildID:) and reachable
# only this way. Separate documents rather than nullable arguments on the guild ones, so
# the guild query the announcer has run since day one is byte-for-byte unchanged.
USER_REPORTS_Q = REPORTS_Q.replace("$guildID: Int!", "$userID: Int!").replace(
    "guildID: $guildID", "userID: $userID")


def rate_limit(data):
    """Pull rateLimitData out of any response that carried it. Never raises -- a missing
    block means 'unknown', and unknown must not take down an announcement."""
    r = (data or {}).get("rateLimitData") or {}
    try:
        limit = int(r.get("limitPerHour") or 0)
        spent = float(r.get("pointsSpentThisHour") or 0.0)
        resets = int(r.get("pointsResetIn") or 0)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    return {"limit": limit, "spent": spent, "resetsIn": resets,
            "fraction": round(spent / limit, 4)}


def find_guild(token, name, realm_slug, region):
    data = query(token, GUILD_Q, {"name": name, "server": realm_slug,
                                  "region": region.upper()})
    guild = ((data.get("guildData") or {}).get("guild")) or None
    return guild, rate_limit(data)


def heroic_kills_since(token, guild_id, since_ms, limit=12, difficulty=HEROIC,
                       max_pages=1, user_id=None):
    """Every boss KILL at `difficulty` in the guild's reports since `since_ms`.

    Named for the Heroic default it has always had; `difficulty` picks Normal for an
    install that announces both. `user_id` swaps the source from the guild's reports to
    one Warcraft Logs user's -- `guild_id` is then ignored, and the report's start time
    rides along as `reportStartMs` so the caller can keep only the team's raid nights.

    Returns a flat, chronologically ascending list of kills so the caller can announce a
    raid night in the order it actually happened rather than in whatever order the reports
    came back.

    Paged, because the limit is not the only thing this API charges for. Query complexity
    is computed from the SHAPE of the request, and asking for `fights` inside each report
    multiplies by the report count: 100 reports priced out at 70,705 against a ceiling of
    50,000 and was rejected outright, before a single byte came back. Roughly 707 per
    report, so pages stay small and the deep seed pass walks several of them instead.

    Paging stops on a short page rather than on a `has_more_pages` flag -- a page holding
    fewer reports than asked for is the last page under any pagination scheme, and that
    needs no extra field in the query to be true.
    """
    kills, rate, page = [], None, 1
    reports = []
    doc = USER_REPORTS_Q if user_id else REPORTS_Q
    who = ({"userID": int(user_id)} if user_id else {"guildID": int(guild_id)})
    while page <= max(1, int(max_pages)):
        data = query(token, doc, {**who, "start": float(since_ms),
                                  "limit": int(limit), "difficulty": int(difficulty),
                                  "page": page})
        rate = rate_limit(data) or rate
        batch = (((data.get("reportData") or {}).get("reports") or {}).get("data")) or []
        reports.extend(batch)
        if len(batch) < int(limit):
            break
        page += 1

    for rep in reports:
        base = rep.get("startTime") or 0
        zone = rep.get("zone") or {}
        for f in rep.get("fights") or []:
            if not f.get("kill"):
                continue
            if int(f.get("difficulty") or 0) != int(difficulty):
                continue
            enc = f.get("encounterID")
            if not enc:                      # trash and non-encounter kills carry 0
                continue
            kills.append({
                "encounterID": int(enc),
                "name": f.get("name") or f"Encounter {enc}",
                "zoneID": zone.get("id"),
                "zoneName": zone.get("name") or "",
                "reportCode": rep.get("code"),
                "reportStartMs": int(base),
                # fight times are offsets from the report start; see the module docstring
                "killedAtMs": int(base + (f.get("endTime") or f.get("startTime") or 0)),
            })
    kills.sort(key=lambda k: k["killedAtMs"])
    return kills, rate


# ------------------------------------------------------------------ recap queries
#
# Every argument below was read off a real introspection of reportData/Report rather than
# guessed (scripts/introspect-wcl.py). Two of them carry the findings that a guessed query
# would have got silently wrong:
#
#   fightIDs on the tables. A report is not one activity -- the captured Scrambled report
#   held sixteen Heroic raid fights, a Normal kill and three Mythic+ dungeons. Unscoped,
#   the DamageDone table returned twenty-seven rows for an eighteen-person raid and a
#   different top three, led by a player whose total was mostly dungeon damage.
#
#   startTime on the Deaths table. That table stops at 200 rows without saying so. The
#   captured night had 245 deaths, and the top of the "most deaths" list read differently
#   depending on whether you had noticed.
#
# GuildTag rides along on the reports query. Scrambled tags nothing today, so it is always
# null and the roster classifier does the work -- but it costs nothing to ask, and the day
# somebody tags the B team's reports the bot starts using the authoritative answer.

NIGHT_REPORTS_Q = """
query($guildID: Int!, $start: Float!, $end: Float!, $limit: Int!) {
  %s
  reportData {
    reports(guildID: $guildID, startTime: $start, endTime: $end, limit: $limit) {
      data { code title startTime endTime guildTag { id name } zone { id name }
             owner { id name } }
    }
  }
}
""" % RATE

# masterData is asked for once per report, not once per fight. It comes back large -- 2,295
# players across 130 realms in the captured response, because it holds every actor the log
# ever saw rather than the raid -- but it is the only way an id in friendlyPlayers becomes
# a name, and one 225KB read a week is not worth a cleverer scheme.
REPORT_DETAIL_Q = """
query($code: String!) {
  %s
  reportData {
    report(code: $code) {
      code title startTime endTime
      guild { id name } guildTag { id name } zone { id name }
      masterData { actors(type: "Player") { id name server type subType } }
      fights(killType: Encounters) {
        id name kill difficulty encounterID startTime endTime fightPercentage size
        friendlyPlayers
      }
    }
  }
}
""" % RATE

REPORT_TABLES_Q = """
query($code: String!, $fightIDs: [Int]!) {
  %s
  reportData {
    report(code: $code) {
      damage: table(dataType: DamageDone, killType: Encounters, viewBy: Source,
                    fightIDs: $fightIDs)
      # Healing and DamageTaken ride along in the SAME query rather than in two more of
      # their own. Measured against the live report: both together cost 1.00 point, where
      # a separate round trip would pay the base cost twice for the same fight list.
      healing: table(dataType: Healing, killType: Encounters, viewBy: Source,
                     fightIDs: $fightIDs)
      damageTaken: table(dataType: DamageTaken, killType: Encounters, viewBy: Source,
                         fightIDs: $fightIDs)
      # Roles for EVERY raider, which the rankings blob cannot give: rankings only covers
      # people who were in a ranked kill, and a card that shows a role icon for two thirds
      # of the raid reads as broken rather than as incomplete. Measured at 1.00 point.
      playerDetails(killType: Encounters, fightIDs: $fightIDs)
      rankings
    }
  }
}
""" % RATE

DEATHS_Q = """
query($code: String!, $fightIDs: [Int]!, $start: Float!, $end: Float!) {
  %s
  reportData {
    report(code: $code) {
      deaths: table(dataType: Deaths, killType: Encounters, fightIDs: $fightIDs,
                    startTime: $start, endTime: $end)
    }
  }
}
""" % RATE

# One kill's roster, for the derived prog roster. Filtered to the encounter rather than
# fetching the whole report, and only ever run just after a first kill was announced --
# a handful of times a tier, not once a poll.
KILL_ROSTER_Q = """
query($code: String!, $encounterID: Int!, $difficulty: Int!) {
  %s
  reportData {
    report(code: $code) {
      masterData { actors(type: "Player") { id name server type } }
      fights(encounterID: $encounterID, difficulty: $difficulty, killType: Kills) {
        id startTime endTime friendlyPlayers
      }
    }
  }
}
""" % RATE


def _report(data):
    return ((data.get("reportData") or {}).get("report")) or {}


USER_NIGHT_REPORTS_Q = NIGHT_REPORTS_Q.replace("$guildID: Int!", "$userID: Int!").replace(
    "guildID: $guildID", "userID: $userID")


def reports_in_window(token, guild_id, start_ms, end_ms, limit=10, user_id=None):
    """Reports the guild -- or, with `user_id`, one user -- filed in one raid night's
    window. Deliberately cheap: no fights, so this costs a fraction of what the
    announcer's paged query does."""
    doc = USER_NIGHT_REPORTS_Q if user_id else NIGHT_REPORTS_Q
    who = ({"userID": int(user_id)} if user_id else {"guildID": int(guild_id)})
    data = query(token, doc, {**who, "start": float(start_ms),
                              "end": float(end_ms), "limit": int(limit)})
    reports = (((data.get("reportData") or {}).get("reports") or {}).get("data")) or []
    return reports, rate_limit(data)


def report_detail(token, code):
    data = query(token, REPORT_DETAIL_Q, {"code": code})
    return _report(data), rate_limit(data)


def report_tables(token, code, fight_ids):
    data = query(token, REPORT_TABLES_Q, {"code": code,
                                          "fightIDs": [int(i) for i in fight_ids]})
    return _report(data), rate_limit(data)


def deaths_pages(token, code, fight_ids, span_ms, max_pages=6, is_truncated=None,
                 cursor_of=None):
    """Every death of the night, walked with a timestamp cursor.

    The Deaths table returns at most 200 rows and gives no indication that it stopped
    early, so a full page is treated as "there is more" and the next call resumes one
    millisecond after the last death seen. Resuming by timestamp rather than halving the
    range means no page is fetched twice, and the merge in recap.death_counts de-duplicates
    anyway in case a tie on the cursor makes one overlap.

    The truncation test and the cursor are injected so this module stays free of any
    opinion about the blob's internal shape -- that lives in recap.py, which is where the
    captured response is documented.
    """
    pages, rate, start, guard = [], None, 0.0, 0
    while guard < max(1, int(max_pages)):
        guard += 1
        data = query(token, DEATHS_Q, {"code": code,
                                       "fightIDs": [int(i) for i in fight_ids],
                                       "start": float(start), "end": float(span_ms)})
        rate = rate_limit(data) or rate
        blob = (_report(data) or {}).get("deaths")
        pages.append(blob)
        if not is_truncated or not is_truncated(blob):
            break
        nxt = cursor_of(blob) if cursor_of else None
        if nxt is None or float(nxt) + 1 <= start:
            break                       # no forward progress; stop rather than loop
        start = float(nxt) + 1
    return pages, rate, guard


DIFFICULTY_IDS = {"normal": NORMAL, "heroic": HEROIC, "mythic": MYTHIC}


def kill_participants(token, code, encounter_id, difficulty=HEROIC):
    """Who was standing there for one first kill: [{"name":..., "server":...}]."""
    data = query(token, KILL_ROSTER_Q, {"code": code, "encounterID": int(encounter_id),
                                        "difficulty": int(difficulty)})
    rep = _report(data)
    actors = {}
    for a in ((rep.get("masterData") or {}).get("actors")) or []:
        if a.get("id") is not None and a.get("name"):
            actors[int(a["id"])] = {"name": a["name"], "server": a.get("server") or ""}
    people, seen = [], set()
    for f in rep.get("fights") or []:
        for pid in f.get("friendlyPlayers") or []:
            pid = int(pid)
            if pid in actors and pid not in seen:
                seen.add(pid)
                people.append(actors[pid])
    return people, rate_limit(data)
